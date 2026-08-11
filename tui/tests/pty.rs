use nix::pty::{Winsize, openpty};
use nix::sys::termios::{LocalFlags, tcgetattr};
use std::fs;
use std::io::{Read, Write};
use std::os::fd::OwnedFd;
use std::os::unix::fs::PermissionsExt;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[test]
fn restores_terminal_after_normal_failure_panic_and_ctrl_c() {
    let directory = tempfile::tempdir().unwrap();
    let ok_dump = executable(directory.path().join("pw-dump-ok"), "printf '[]\\n'");
    let failed_dump = executable(
        directory.path().join("pw-dump-fail"),
        "printf 'PipeWire unavailable\\n' >&2; exit 7",
    );

    let normal = run_in_pty(&ok_dump, Some(b"q"), false);
    assert!(
        normal.status.success(),
        "normal status: {}\n{}",
        normal.status,
        String::from_utf8_lossy(&normal.output)
    );
    assert_restored(&normal);

    let failure = run_in_pty(&failed_dump, Some(b"q"), false);
    assert!(
        failure.status.success(),
        "failure modal status: {}",
        failure.status
    );
    assert_restored(&failure);

    let panic = run_in_pty(&ok_dump, None, true);
    assert!(!panic.status.success());
    assert_restored(&panic);

    let ctrl_c = run_in_pty(&ok_dump, Some(&[3]), false);
    assert_eq!(ctrl_c.status.code(), Some(130));
    assert_restored(&ctrl_c);
}

struct Outcome {
    status: ExitStatus,
    before: LocalFlags,
    after: LocalFlags,
    output: Vec<u8>,
}

fn run_in_pty(pw_dump: &std::path::Path, input: Option<&[u8]>, panic: bool) -> Outcome {
    let size = Winsize {
        ws_row: 24,
        ws_col: 80,
        ws_xpixel: 0,
        ws_ypixel: 0,
    };
    let pty = openpty(Some(&size), None).unwrap();
    let before = tcgetattr(&pty.master).unwrap().local_flags;
    let stdin = clone_fd(&pty.slave);
    let stdout = clone_fd(&pty.slave);
    let stderr = pty.slave;
    let mut command = Command::new(env!("CARGO_BIN_EXE_native-asr-tui"));
    command
        .env("PW_DUMP_BIN", pw_dump)
        .env("TERM", "xterm-256color")
        .stdin(Stdio::from(stdin))
        .stdout(Stdio::from(stdout))
        .stderr(Stdio::from(stderr));
    if panic {
        command.env("NATIVE_ASR_TUI_TEST_PANIC", "1");
    }
    let mut child = command.spawn().unwrap();
    drop(command);
    let mut master = std::fs::File::from(pty.master);
    if let Some(input) = input {
        thread::sleep(Duration::from_millis(250));
        master.write_all(input).unwrap();
        master.flush().unwrap();
    }
    let status = wait_bounded(&mut child);
    let after = tcgetattr(&master).unwrap().local_flags;
    let mut output = Vec::new();
    let _ = master.read_to_end(&mut output);
    Outcome {
        status,
        before,
        after,
        output,
    }
}

fn wait_bounded(child: &mut Child) -> ExitStatus {
    let deadline = Instant::now() + Duration::from_secs(5);
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        assert!(
            Instant::now() < deadline,
            "TUI did not exit within five seconds"
        );
        thread::sleep(Duration::from_millis(20));
    }
}

fn assert_restored(outcome: &Outcome) {
    let relevant = LocalFlags::ECHO | LocalFlags::ICANON | LocalFlags::ISIG;
    assert_eq!(outcome.before & relevant, outcome.after & relevant);
    assert!(
        contains(&outcome.output, b"\x1b[?1049h"),
        "alternate screen was not entered"
    );
    assert!(
        contains(&outcome.output, b"\x1b[?1049l"),
        "alternate screen was not left"
    );
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    haystack
        .windows(needle.len())
        .any(|window| window == needle)
}

fn clone_fd(fd: &OwnedFd) -> OwnedFd {
    fd.try_clone().unwrap()
}

fn executable(path: std::path::PathBuf, body: &str) -> std::path::PathBuf {
    fs::write(
        &path,
        format!("#!/usr/bin/env bash\nset -euo pipefail\n{body}\n"),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path
}
