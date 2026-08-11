use crate::protocol::MAX_LINE_BYTES;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;
use std::env;
use std::ffi::{OsStr, OsString};
use std::io::{BufRead, BufReader, Read};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, Sender};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Clone, Debug)]
pub struct Config {
    pub source: String,
    pub audit: Option<String>,
}

#[derive(Debug)]
pub enum Message {
    JsonLine(Vec<u8>),
    ProtocolError(String),
    Diagnostic(String),
    Exited {
        code: Option<i32>,
        signal: Option<i32>,
    },
}

#[derive(Clone, Copy, Debug)]
enum Control {
    Graceful,
    Cancel,
}

#[derive(Debug)]
pub struct Handle {
    control: Sender<Control>,
}

impl Handle {
    pub fn graceful_stop(&self) {
        let _ = self.control.send(Control::Graceful);
    }

    pub fn cancel(&self) {
        let _ = self.control.send(Control::Cancel);
    }
}

impl Drop for Handle {
    fn drop(&mut self) {
        let _ = self.control.send(Control::Cancel);
    }
}

pub fn cascade_binary() -> OsString {
    env::var_os("NATIVE_ASR_CASCADE_BIN").unwrap_or_else(|| {
        let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        path.push("..");
        path.push("scripts");
        path.push("cascade");
        path.into_os_string()
    })
}

pub fn spawn(config: &Config) -> Result<(Handle, Receiver<Message>), String> {
    spawn_with_binary(config, cascade_binary())
}

fn spawn_with_binary(
    config: &Config,
    binary: impl AsRef<OsStr>,
) -> Result<(Handle, Receiver<Message>), String> {
    let mut command = Command::new(binary.as_ref());
    command
        .arg("live")
        .arg("--source")
        .arg(&config.source)
        .arg("--control-stdin")
        .arg("--jsonl")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0);
    if let Some(audit) = &config.audit {
        command.arg("--audit").arg(audit);
    }
    let mut child = command.spawn().map_err(|error| {
        format!(
            "could not start {}: {error}",
            binary.as_ref().to_string_lossy()
        )
    })?;
    let pid = Pid::from_raw(i32::try_from(child.id()).map_err(|_| "child PID exceeds i32")?);
    let stdin = child
        .stdin
        .take()
        .ok_or_else(|| "cascade stdin was not piped".to_owned())?;
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "cascade stdout was not piped".to_owned())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "cascade stderr was not piped".to_owned())?;
    let (message_tx, message_rx) = mpsc::channel();
    let stdout_tx = message_tx.clone();
    let stdout_thread = thread::spawn(move || read_stdout(stdout, &stdout_tx));
    let stderr_tx = message_tx.clone();
    let stderr_thread = thread::spawn(move || read_stderr(stderr, &stderr_tx));
    let (control_tx, control_rx) = mpsc::channel();
    thread::spawn(move || {
        manage_process(
            child,
            Some(stdin),
            pid,
            control_rx,
            message_tx,
            stdout_thread,
            stderr_thread,
        );
    });
    Ok((
        Handle {
            control: control_tx,
        },
        message_rx,
    ))
}

fn read_stdout(stdout: impl Read, messages: &Sender<Message>) {
    let mut reader = BufReader::new(stdout);
    loop {
        match read_bounded_line(&mut reader) {
            Ok(Some(line)) => {
                if messages.send(Message::JsonLine(line)).is_err() {
                    return;
                }
            }
            Ok(None) => return,
            Err(error) => {
                let _ = messages.send(Message::ProtocolError(error));
                return;
            }
        }
    }
}

fn read_bounded_line(reader: &mut impl BufRead) -> Result<Option<Vec<u8>>, String> {
    let mut output = Vec::new();
    loop {
        let available = reader
            .fill_buf()
            .map_err(|error| format!("could not read JSONL: {error}"))?;
        if available.is_empty() {
            if output.is_empty() {
                return Ok(None);
            }
            return Err("unterminated protocol line".to_owned());
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let take = newline.map_or(available.len(), |index| index + 1);
        if output.len() + take > MAX_LINE_BYTES + 1 {
            return Err(format!("protocol line exceeds {MAX_LINE_BYTES} bytes"));
        }
        output.extend_from_slice(&available[..take]);
        reader.consume(take);
        if newline.is_some() {
            output.pop();
            if output.last() == Some(&b'\r') {
                output.pop();
            }
            return Ok(Some(output));
        }
    }
}

fn read_stderr(stderr: impl Read, messages: &Sender<Message>) {
    for line in BufReader::new(stderr).lines() {
        match line {
            Ok(line) => {
                if messages.send(Message::Diagnostic(line)).is_err() {
                    return;
                }
            }
            Err(error) => {
                let _ = messages.send(Message::Diagnostic(format!(
                    "could not read cascade diagnostics: {error}"
                )));
                return;
            }
        }
    }
}

fn manage_process(
    mut child: std::process::Child,
    mut stdin: Option<ChildStdin>,
    pid: Pid,
    controls: Receiver<Control>,
    messages: Sender<Message>,
    stdout_thread: thread::JoinHandle<()>,
    stderr_thread: thread::JoinHandle<()>,
) {
    let mut cancellation_deadline = None;
    loop {
        match controls.recv_timeout(Duration::from_millis(25)) {
            Ok(Control::Graceful) => {
                stdin.take();
            }
            Ok(Control::Cancel) => {
                let _ = kill(pid, Signal::SIGTERM);
                cancellation_deadline
                    .get_or_insert_with(|| Instant::now() + Duration::from_secs(3));
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let _ = kill(pid, Signal::SIGTERM);
                cancellation_deadline
                    .get_or_insert_with(|| Instant::now() + Duration::from_secs(3));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {}
        }
        if cancellation_deadline.is_some_and(|deadline| Instant::now() >= deadline) {
            let _ = killpg(pid, Signal::SIGKILL);
            cancellation_deadline = None;
        }
        match child.try_wait() {
            Ok(Some(status)) => {
                drop(stdin);
                let _ = stdout_thread.join();
                let _ = stderr_thread.join();
                let _ = messages.send(Message::Exited {
                    code: status.code(),
                    signal: status.signal(),
                });
                return;
            }
            Ok(None) => {}
            Err(error) => {
                let _ = killpg(pid, Signal::SIGKILL);
                let _ = child.wait();
                let _ = messages.send(Message::ProtocolError(format!(
                    "could not wait for cascade: {error}"
                )));
                return;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::PermissionsExt;
    use std::sync::mpsc::RecvTimeoutError;

    fn fake_script(body: &str) -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("cascade");
        fs::write(
            &path,
            format!("#!/usr/bin/env bash\nset -euo pipefail\n{body}\n"),
        )
        .unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
        (directory, path)
    }

    #[test]
    fn passes_exact_source_and_closes_stdin_gracefully() {
        let body = r#"
printf '%s\n' "$@" > "$(dirname "$0")/args"
printf '%s\n' 'cascade: ready' >&2
cat >/dev/null
printf '%s\n' '{"sequence":0}'
"#;
        let (directory, binary) = fake_script(body);
        let log = directory.path().join("args");
        let config = Config {
            source: "exact source.name".to_owned(),
            audit: None,
        };
        let (handle, messages) = spawn_with_binary(&config, binary).unwrap();
        assert!(
            matches!(messages.recv_timeout(Duration::from_secs(2)), Ok(Message::Diagnostic(line)) if line == "cascade: ready")
        );
        handle.graceful_stop();
        let mut exited = false;
        for _ in 0..5 {
            match messages.recv_timeout(Duration::from_secs(2)) {
                Ok(Message::Exited { code, .. }) => {
                    assert_eq!(code, Some(0));
                    exited = true;
                    break;
                }
                Ok(_) => {}
                Err(error) => panic!("missing process exit: {error}"),
            }
        }
        assert!(exited);
        let args = fs::read_to_string(log).unwrap();
        let lines: Vec<_> = args.lines().collect();
        assert!(
            lines
                .windows(2)
                .any(|pair| pair == ["--source", "exact source.name"])
        );
        assert!(!lines.contains(&"--default"));
    }

    #[test]
    fn reports_failure_and_hard_cancellation() {
        let (_directory, binary) = fake_script("printf 'boom\\n' >&2\nexit 17");
        let config = Config {
            source: "source".to_owned(),
            audit: None,
        };
        let (_handle, messages) = spawn_with_binary(&config, binary).unwrap();
        let mut saw_failure = false;
        loop {
            match messages.recv_timeout(Duration::from_secs(2)) {
                Ok(Message::Exited { code, .. }) => {
                    assert_eq!(code, Some(17));
                    saw_failure = true;
                    break;
                }
                Ok(_) => {}
                Err(RecvTimeoutError::Timeout | RecvTimeoutError::Disconnected) => break,
            }
        }
        assert!(saw_failure);

        let body =
            "printf 'cascade: ready\\n' >&2\ntrap 'exit 130' TERM\nwhile :; do sleep 1; done";
        let (_directory, binary) = fake_script(body);
        let (handle, messages) = spawn_with_binary(&config, binary).unwrap();
        assert!(matches!(
            messages.recv_timeout(Duration::from_secs(2)),
            Ok(Message::Diagnostic(_))
        ));
        handle.cancel();
        loop {
            if let Message::Exited { code, signal } =
                messages.recv_timeout(Duration::from_secs(4)).unwrap()
            {
                assert!(code == Some(130) || signal == Some(Signal::SIGTERM as i32));
                break;
            }
        }
    }

    #[test]
    fn escalates_stubborn_process_group_without_orphans() {
        let body = r#"
trap '' TERM
sleep 30 &
child=$!
printf '%s %s\n' "$$" "$child" > "$(dirname "$0")/pids"
printf 'cascade: ready\n' >&2
wait "$child"
"#;
        let (directory, binary) = fake_script(body);
        let config = Config {
            source: "source".to_owned(),
            audit: None,
        };
        let (handle, messages) = spawn_with_binary(&config, binary).unwrap();
        assert!(matches!(
            messages.recv_timeout(Duration::from_secs(2)),
            Ok(Message::Diagnostic(_))
        ));
        handle.cancel();
        let exit = loop {
            if let Message::Exited { code, signal } =
                messages.recv_timeout(Duration::from_secs(5)).unwrap()
            {
                break (code, signal);
            }
        };
        assert_eq!(exit, (None, Some(Signal::SIGKILL as i32)));
        let pids = fs::read_to_string(directory.path().join("pids")).unwrap();
        for pid in pids.split_whitespace() {
            let path = PathBuf::from("/proc").join(pid);
            for _ in 0..50 {
                if !path.exists() {
                    break;
                }
                thread::sleep(Duration::from_millis(20));
            }
            assert!(!path.exists(), "orphaned process {pid}");
        }
    }

    #[test]
    fn bounded_reader_rejects_oversized_and_unterminated_lines() {
        let mut oversized = std::io::Cursor::new(vec![b'x'; MAX_LINE_BYTES + 2]);
        assert!(
            read_bounded_line(&mut oversized)
                .unwrap_err()
                .contains("exceeds")
        );
        let mut unterminated = std::io::Cursor::new(b"{}".to_vec());
        assert!(
            read_bounded_line(&mut unterminated)
                .unwrap_err()
                .contains("unterminated")
        );
    }
}
