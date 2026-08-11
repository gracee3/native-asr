mod app;
mod pipewire;
mod protocol;
mod session;
mod ui;

use app::App;
use crossterm::cursor;
use crossterm::event::{self, Event, KeyEventKind};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use std::env;
use std::io::{self, IsTerminal};
use std::process::ExitCode;
use std::time::Duration;

fn main() -> ExitCode {
    let arguments: Vec<_> = env::args_os().skip(1).collect();
    if arguments.len() == 1 && (arguments[0] == "--help" || arguments[0] == "-h") {
        println!(
            "native-asr-tui\n\nLive PipeWire client for scripts/cascade.\nRun without arguments."
        );
        return ExitCode::SUCCESS;
    }
    if !arguments.is_empty() {
        eprintln!("error: native-asr-tui takes no arguments");
        return ExitCode::from(2);
    }
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        eprintln!("error: native-asr-tui requires an interactive terminal");
        return ExitCode::from(2);
    }
    match run() {
        Ok(status) => ExitCode::from(u8::try_from(status).unwrap_or(1)),
        Err(error) => {
            eprintln!("error: {error}");
            ExitCode::FAILURE
        }
    }
}

fn run() -> Result<i32, String> {
    enable_raw_mode().map_err(|error| format!("could not enable terminal raw mode: {error}"))?;
    let mut guard = TerminalGuard { active: true };
    let backend = CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend)
        .map_err(|error| format!("could not initialize terminal: {error}"))?;
    execute!(terminal.backend_mut(), EnterAlternateScreen, cursor::Hide)
        .map_err(|error| format!("could not enter alternate screen: {error}"))?;
    if cfg!(debug_assertions) && env::var_os("NATIVE_ASR_TUI_TEST_PANIC").is_some() {
        panic!("requested terminal-restoration test panic");
    }
    let mut app = App::new(env::var_os("NO_COLOR").is_some());
    app.refresh_sources();
    let status = loop {
        if let Some(status) = app.drain_messages() {
            break status;
        }
        terminal
            .draw(|frame| ui::render(frame, &app))
            .map_err(|error| format!("could not draw terminal: {error}"))?;
        if event::poll(Duration::from_millis(50))
            .map_err(|error| format!("could not poll terminal input: {error}"))?
        {
            match event::read()
                .map_err(|error| format!("could not read terminal input: {error}"))?
            {
                Event::Key(key)
                    if matches!(key.kind, KeyEventKind::Press | KeyEventKind::Repeat) =>
                {
                    if let Some(status) = app.handle_key(key) {
                        break status;
                    }
                }
                _ => {}
            }
        }
    };
    terminal.show_cursor().ok();
    guard.restore();
    Ok(status)
}

struct TerminalGuard {
    active: bool,
}

impl TerminalGuard {
    fn restore(&mut self) {
        if !self.active {
            return;
        }
        let _ = disable_raw_mode();
        let _ = execute!(io::stdout(), LeaveAlternateScreen, cursor::Show);
        self.active = false;
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        self.restore();
    }
}
