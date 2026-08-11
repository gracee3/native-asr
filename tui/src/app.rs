use crate::pipewire::{self, Source};
use crate::protocol::{Transcript, Validator};
use crate::session::{self, Config, Handle, Message};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::collections::VecDeque;
use std::sync::mpsc::{Receiver, TryRecvError};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum State {
    Idle,
    Loading,
    Listening,
    Stopping,
    Completed,
    Failed,
    Cancelling,
}

impl State {
    pub fn label(self) -> &'static str {
        match self {
            Self::Idle => "Idle",
            Self::Loading => "Loading",
            Self::Listening => "Listening",
            Self::Stopping => "Stopping",
            Self::Completed => "Completed",
            Self::Failed => "Failed",
            Self::Cancelling => "Cancelling",
        }
    }

    pub fn is_active(self) -> bool {
        matches!(
            self,
            Self::Loading | Self::Listening | Self::Stopping | Self::Cancelling
        )
    }
}

#[derive(Debug)]
pub struct App {
    pub state: State,
    pub sources: Vec<Source>,
    pub selected: Option<usize>,
    pub audit: Option<String>,
    pub audit_edit: Option<String>,
    pub confirm_cancel: bool,
    pub transcript: Transcript,
    pub error: Option<String>,
    pub diagnostics: VecDeque<String>,
    pub no_color: bool,
    validator: Validator,
    session: Option<Handle>,
    messages: Option<Receiver<Message>>,
    exit_when_done: bool,
    ctrl_c_exit: bool,
    cancel_to_idle: bool,
    protocol_failure: Option<String>,
}

impl App {
    pub fn new(no_color: bool) -> Self {
        Self {
            state: State::Idle,
            sources: Vec::new(),
            selected: None,
            audit: None,
            audit_edit: None,
            confirm_cancel: false,
            transcript: Transcript::default(),
            error: None,
            diagnostics: VecDeque::new(),
            no_color,
            validator: Validator::default(),
            session: None,
            messages: None,
            exit_when_done: false,
            ctrl_c_exit: false,
            cancel_to_idle: false,
            protocol_failure: None,
        }
    }

    pub fn refresh_sources(&mut self) {
        self.selected = None;
        match pipewire::enumerate() {
            Ok(sources) => {
                self.sources = sources;
                self.error = None;
                if self.state == State::Failed {
                    self.state = State::Idle;
                }
            }
            Err(error) => {
                self.sources.clear();
                self.error = Some(error);
                self.state = State::Failed;
            }
        }
    }

    fn start(&mut self) {
        let Some(selected) = self
            .selected
            .and_then(|index| self.sources.get(index))
            .cloned()
        else {
            self.error = Some("Select a PipeWire source before starting.".to_owned());
            return;
        };
        let refreshed = match pipewire::enumerate() {
            Ok(sources) => sources,
            Err(error) => {
                self.fail(format!("Could not revalidate the selected source: {error}"));
                return;
            }
        };
        let Some(index) = pipewire::revalidate(&refreshed, &selected) else {
            self.sources = refreshed;
            self.selected = None;
            self.error =
                Some("The selected source disappeared or changed. Select it again.".to_owned());
            return;
        };
        self.sources = refreshed;
        self.selected = Some(index);
        let config = Config {
            source: selected.node_name,
            audit: self.audit.clone(),
        };
        match session::spawn(&config) {
            Ok((handle, messages)) => {
                self.session = Some(handle);
                self.messages = Some(messages);
                self.validator = Validator::default();
                self.transcript = Transcript::default();
                self.error = None;
                self.diagnostics.clear();
                self.state = State::Loading;
                self.exit_when_done = false;
                self.ctrl_c_exit = false;
                self.cancel_to_idle = false;
                self.protocol_failure = None;
            }
            Err(error) => self.fail(error),
        }
    }

    pub fn drain_messages(&mut self) -> Option<i32> {
        loop {
            let message = match self.messages.as_ref().map(Receiver::try_recv) {
                Some(Ok(message)) => message,
                Some(Err(TryRecvError::Empty)) | None => return None,
                Some(Err(TryRecvError::Disconnected)) => {
                    self.messages = None;
                    return None;
                }
            };
            if let Some(exit) = self.handle_message(message) {
                return Some(exit);
            }
        }
    }

    fn handle_message(&mut self, message: Message) -> Option<i32> {
        match message {
            Message::JsonLine(line) => match self.validator.parse_and_validate(&line) {
                Ok(event) => self.transcript.apply(&event),
                Err(error) => self.protocol_error(error),
            },
            Message::ProtocolError(error) => self.protocol_error(error),
            Message::Diagnostic(line) => {
                if line == "cascade: ready" && self.state == State::Loading {
                    self.state = State::Listening;
                }
                if self.diagnostics.len() == 20 {
                    self.diagnostics.pop_front();
                }
                self.diagnostics.push_back(line);
            }
            Message::Exited { code, signal } => {
                self.session = None;
                self.messages = None;
                if self.ctrl_c_exit {
                    return Some(130);
                }
                if self.cancel_to_idle {
                    self.reset_to_idle();
                    return None;
                }
                if let Some(error) = self.protocol_failure.take() {
                    self.fail(error);
                } else if code == Some(0) {
                    self.state = State::Completed;
                } else {
                    let status = code.map_or_else(
                        || format!("signal {}", signal.unwrap_or_default()),
                        |value| format!("status {value}"),
                    );
                    let detail = self.diagnostics.back().cloned().unwrap_or_default();
                    self.fail(if detail.is_empty() {
                        format!("Cascade exited with {status}.")
                    } else {
                        format!("Cascade exited with {status}: {detail}")
                    });
                }
                if self.exit_when_done {
                    return Some(if code == Some(0) { 0 } else { 1 });
                }
            }
        }
        None
    }

    fn protocol_error(&mut self, error: String) {
        if self.protocol_failure.is_none() {
            self.protocol_failure = Some(error);
            if let Some(handle) = &self.session {
                handle.cancel();
            }
            self.state = State::Cancelling;
        }
    }

    pub fn handle_key(&mut self, key: KeyEvent) -> Option<i32> {
        if key.modifiers.contains(KeyModifiers::CONTROL) && key.code == KeyCode::Char('c') {
            if self.state.is_active() {
                self.ctrl_c_exit = true;
                self.state = State::Cancelling;
                if let Some(handle) = &self.session {
                    handle.cancel();
                }
                return None;
            }
            return Some(130);
        }
        if self.audit_edit.is_some() {
            return self.handle_audit_key(key);
        }
        if self.confirm_cancel {
            match key.code {
                KeyCode::Char('x') => {
                    self.confirm_cancel = false;
                    self.cancel_to_idle = true;
                    self.state = State::Cancelling;
                    if let Some(handle) = &self.session {
                        handle.cancel();
                    }
                }
                KeyCode::Esc => self.confirm_cancel = false,
                _ => {}
            }
            return None;
        }
        match key.code {
            KeyCode::Esc if self.state == State::Failed || self.state == State::Completed => {
                self.reset_to_idle();
            }
            KeyCode::Up | KeyCode::Char('k') if self.state == State::Idle => self.select_previous(),
            KeyCode::Down | KeyCode::Char('j') if self.state == State::Idle => self.select_next(),
            KeyCode::Char('r') if self.state == State::Idle => self.refresh_sources(),
            KeyCode::Char('a') if self.state == State::Idle => {
                self.audit_edit = Some(self.audit.clone().unwrap_or_default());
            }
            KeyCode::Enter if self.state == State::Idle => self.start(),
            KeyCode::Char('s') if self.state == State::Idle => self.start(),
            KeyCode::Char('s') if self.state == State::Listening => self.graceful_stop(false),
            KeyCode::Char('x') if self.state.is_active() && self.state != State::Cancelling => {
                self.confirm_cancel = true;
            }
            KeyCode::Char('q') if self.state.is_active() => self.graceful_stop(true),
            KeyCode::Char('q') => return Some(0),
            _ => {}
        }
        None
    }

    fn handle_audit_key(&mut self, key: KeyEvent) -> Option<i32> {
        match key.code {
            KeyCode::Esc => self.audit_edit = None,
            KeyCode::Enter => {
                let value = self.audit_edit.take().unwrap_or_default();
                self.audit = if value.trim().is_empty() {
                    None
                } else {
                    Some(value)
                };
            }
            KeyCode::Backspace => {
                self.audit_edit.as_mut().expect("editing").pop();
            }
            KeyCode::Char(character) if !key.modifiers.contains(KeyModifiers::CONTROL) => {
                self.audit_edit.as_mut().expect("editing").push(character);
            }
            _ => {}
        }
        None
    }

    fn graceful_stop(&mut self, exit: bool) {
        self.exit_when_done |= exit;
        if self.state != State::Cancelling {
            self.state = State::Stopping;
            if let Some(handle) = &self.session {
                handle.graceful_stop();
            }
        }
    }

    fn select_previous(&mut self) {
        if self.sources.is_empty() {
            return;
        }
        self.selected = Some(self.selected.map_or(self.sources.len() - 1, |index| {
            index.checked_sub(1).unwrap_or(self.sources.len() - 1)
        }));
        self.error = None;
    }

    fn select_next(&mut self) {
        if self.sources.is_empty() {
            return;
        }
        self.selected = Some(
            self.selected
                .map_or(0, |index| (index + 1) % self.sources.len()),
        );
        self.error = None;
    }

    fn fail(&mut self, error: String) {
        self.error = Some(error);
        self.state = State::Failed;
    }

    fn reset_to_idle(&mut self) {
        self.state = State::Idle;
        self.error = None;
        self.confirm_cancel = false;
        self.exit_when_done = false;
        self.ctrl_c_exit = false;
        self.cancel_to_idle = false;
        self.protocol_failure = None;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyEvent;

    #[test]
    fn readiness_requires_the_exact_diagnostic() {
        let mut app = App::new(false);
        app.state = State::Loading;
        app.handle_message(Message::Diagnostic("loading models".to_owned()));
        assert_eq!(app.state, State::Loading);
        app.handle_message(Message::Diagnostic("cascade: ready".to_owned()));
        assert_eq!(app.state, State::Listening);
    }

    #[test]
    fn start_requires_deliberate_source_selection() {
        let mut app = App::new(false);
        app.handle_key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::NONE));
        assert_eq!(app.state, State::Idle);
        assert!(app.error.as_deref().unwrap().contains("Select"));
    }

    #[test]
    fn audit_editor_saves_only_explicit_nonblank_text() {
        let mut app = App::new(false);
        app.handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('/'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::NONE));
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.audit.as_deref(), Some("/x"));
        app.handle_key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE));
        for _ in 0..2 {
            app.handle_key(KeyEvent::new(KeyCode::Backspace, KeyModifiers::NONE));
        }
        app.handle_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert_eq!(app.audit, None);
    }
}
