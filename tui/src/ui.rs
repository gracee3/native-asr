use crate::app::{App, State};
use ratatui::Frame;
use ratatui::layout::{Alignment, Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap};

const MIN_WIDTH: u16 = 60;
const MIN_HEIGHT: u16 = 15;

pub fn render(frame: &mut Frame, app: &App) {
    let area = frame.area();
    if area.width < MIN_WIDTH || area.height < MIN_HEIGHT {
        frame.render_widget(
            Paragraph::new("Terminal too small\nResize to at least 60×15")
                .alignment(Alignment::Center)
                .block(Block::bordered().title(" native-asr-tui ")),
            area,
        );
        return;
    }
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(6),
            Constraint::Length(4),
            Constraint::Length(2),
        ])
        .split(area);
    let title = format!(" native-asr-tui — {} ", app.state.label());
    frame.render_widget(
        Paragraph::new(state_summary(app))
            .block(Block::default().borders(Borders::ALL).title(title)),
        chunks[0],
    );
    if app.state == State::Idle {
        render_sources(frame, app, chunks[1]);
    } else {
        render_transcript(frame, app, chunks[1]);
    }
    frame.render_widget(
        Paragraph::new(metrics(app))
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL).title(" Status ")),
        chunks[2],
    );
    frame.render_widget(
        Paragraph::new(controls(app)).alignment(Alignment::Center),
        chunks[3],
    );

    if let Some(value) = &app.audit_edit {
        modal(
            frame,
            " Audit destination ",
            &format!(
                "Explicit new directory (blank disables):\n{value}_\n\nEnter save · Esc cancel"
            ),
        );
    } else if app.confirm_cancel {
        modal(
            frame,
            " Hard cancellation ",
            "Stop immediately? A successful audit will not be published.\n\nx confirm · Esc keep listening",
        );
    } else if app.state == State::Failed {
        modal(
            frame,
            " Session failed ",
            &format!(
                "{}\n\nEsc return · q exit",
                app.error.as_deref().unwrap_or("Unknown failure")
            ),
        );
    }
}

fn render_sources(frame: &mut Frame, app: &App, area: Rect) {
    let mut items = Vec::new();
    if app.sources.is_empty() {
        items.push(ListItem::new("No PipeWire audio sources found."));
    }
    for (index, source) in app.sources.iter().enumerate() {
        let marker = if app.selected == Some(index) {
            ">"
        } else {
            " "
        };
        let style = if app.selected == Some(index) {
            styled(app, Color::Cyan).add_modifier(Modifier::BOLD)
        } else {
            Style::default()
        };
        items.push(ListItem::new(vec![
            Line::styled(format!("{marker} {}", source.description), style),
            Line::from(format!(
                "    node={} · {} · serial={}",
                source.node_name,
                source.kind(),
                source.serial
            )),
        ]));
    }
    frame.render_widget(
        List::new(items).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" PipeWire sources — selection is required "),
        ),
        area,
    );
}

fn render_transcript(frame: &mut Frame, app: &App, area: Rect) {
    let mut lines = Vec::new();
    for segment in &app.transcript.committed {
        if segment.degraded {
            lines.push(Line::from(vec![
                Span::styled(
                    "! degraded ",
                    styled(app, Color::Red).add_modifier(Modifier::BOLD),
                ),
                Span::raw(&segment.text),
            ]));
        } else {
            lines.push(Line::raw(&segment.text));
        }
    }
    for segment in app.transcript.pending.values() {
        lines.push(Line::from(vec![
            Span::styled("… correcting ", styled(app, Color::Yellow)),
            Span::raw(&segment.text),
        ]));
    }
    if let Some(segment) = &app.transcript.provisional {
        lines.push(Line::from(vec![
            Span::styled("~ provisional ", styled(app, Color::Cyan)),
            Span::raw(&segment.text),
        ]));
    }
    if lines.is_empty() {
        lines.push(Line::raw("Waiting for speech…"));
    }
    let scroll = u16::try_from(
        lines
            .len()
            .saturating_sub(usize::from(area.height.saturating_sub(2))),
    )
    .unwrap_or(u16::MAX);
    frame.render_widget(
        Paragraph::new(Text::from(lines))
            .scroll((scroll, 0))
            .wrap(Wrap { trim: false })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Live transcript "),
            ),
        area,
    );
}

fn state_summary(app: &App) -> String {
    let source = app
        .selected
        .and_then(|index| app.sources.get(index))
        .map_or("none", |source| source.node_name.as_str());
    let audit = app.audit.as_deref().unwrap_or("disabled");
    format!("source: {source} · audit: {audit}")
}

fn metrics(app: &App) -> String {
    let partial = app
        .transcript
        .latest_partial_latency_ms
        .map_or_else(|| "—".to_owned(), |value| format!("{value} ms"));
    let commit = app
        .transcript
        .latest_commit_latency_ms
        .map_or_else(|| "—".to_owned(), |value| format!("{value} ms"));
    let reason = app
        .transcript
        .last_degradation_reason
        .as_deref()
        .unwrap_or("—");
    let error = app
        .error
        .as_deref()
        .map_or(String::new(), |value| format!("\n{value}"));
    format!(
        "partial {partial} · commit {commit} · pending {} · degraded {} · last reason {reason}{error}",
        app.transcript.pending.len(),
        app.transcript.degraded_count,
    )
}

fn controls(app: &App) -> &'static str {
    match app.state {
        State::Idle => "↑/↓ or j/k select · r refresh · a audit · Enter/s start · q quit",
        State::Listening => {
            "s graceful stop · x hard cancel · q stop and quit · Ctrl-C cancel and quit"
        }
        State::Loading | State::Stopping | State::Cancelling => {
            "x hard cancel · q stop and quit · Ctrl-C cancel and quit"
        }
        State::Completed | State::Failed => "Esc return · q quit",
    }
}

fn styled(app: &App, color: Color) -> Style {
    if app.no_color {
        Style::default()
    } else {
        Style::default().fg(color)
    }
}

fn modal(frame: &mut Frame, title: &str, text: &str) {
    let area = centered(70, 45, frame.area());
    frame.render_widget(Clear, area);
    frame.render_widget(
        Paragraph::new(text)
            .alignment(Alignment::Center)
            .wrap(Wrap { trim: true })
            .block(Block::default().borders(Borders::ALL).title(title)),
        area,
    );
}

fn centered(horizontal: u16, vertical: u16, area: Rect) -> Rect {
    let vertical_chunks = Layout::vertical([
        Constraint::Percentage((100 - vertical) / 2),
        Constraint::Percentage(vertical),
        Constraint::Percentage((100 - vertical) / 2),
    ])
    .split(area);
    Layout::horizontal([
        Constraint::Percentage((100 - horizontal) / 2),
        Constraint::Percentage(horizontal),
        Constraint::Percentage((100 - horizontal) / 2),
    ])
    .split(vertical_chunks[1])[1]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::pipewire::Source;
    use crate::protocol::TextSegment;
    use ratatui::Terminal;
    use ratatui::backend::TestBackend;

    fn rendered(app: &App, width: u16, height: u16) -> (String, Vec<Color>) {
        let backend = TestBackend::new(width, height);
        let mut terminal = Terminal::new(backend).unwrap();
        terminal.draw(|frame| render(frame, app)).unwrap();
        let buffer = terminal.backend().buffer();
        let mut text = String::new();
        let mut colors = Vec::new();
        for y in 0..height {
            for x in 0..width {
                let cell = &buffer[(x, y)];
                text.push_str(cell.symbol());
                colors.push(cell.fg);
            }
            text.push('\n');
        }
        (text, colors)
    }

    #[test]
    fn snapshots_source_selector_and_audit_editor() {
        let mut app = App::new(false);
        app.sources.push(Source {
            node_name: "alsa_input.test".to_owned(),
            description: "Built-in Microphone".to_owned(),
            serial: "42".to_owned(),
            is_virtual: false,
        });
        app.selected = Some(0);
        let (text, _) = rendered(&app, 80, 24);
        assert!(text.contains("Built-in Microphone"));
        assert!(text.contains("node=alsa_input.test · physical · serial=42"));
        app.audit_edit = Some("/tmp/new.audit".to_owned());
        let (text, _) = rendered(&app, 80, 24);
        assert!(text.contains("Explicit new directory"));
        assert!(text.contains("/tmp/new.audit_"));
    }

    #[test]
    fn snapshots_loading_transcript_degradation_and_no_color() {
        let mut app = App::new(false);
        app.state = State::Loading;
        app.transcript.committed.push(TextSegment {
            segment_id: 0,
            text: "committed words".to_owned(),
            degraded: false,
            degradation_reason: None,
        });
        app.transcript.committed.push(TextSegment {
            segment_id: 1,
            text: "fallback words".to_owned(),
            degraded: true,
            degradation_reason: Some("timeout".to_owned()),
        });
        app.transcript.pending.insert(
            2,
            TextSegment {
                segment_id: 2,
                text: "pending words".to_owned(),
                degraded: false,
                degradation_reason: None,
            },
        );
        app.transcript.provisional = Some(TextSegment {
            segment_id: 3,
            text: "active words".to_owned(),
            degraded: false,
            degradation_reason: None,
        });
        let (text, colors) = rendered(&app, 90, 24);
        assert!(text.contains("! degraded fallback words"));
        assert!(text.contains("… correcting pending words"));
        assert!(text.contains("~ provisional active words"));
        assert!(colors.contains(&Color::Red));
        app.no_color = true;
        let (_, colors) = rendered(&app, 90, 24);
        assert!(!colors.contains(&Color::Red));
        assert!(!colors.contains(&Color::Cyan));
    }

    #[test]
    fn snapshots_failure_confirmation_and_narrow_terminal() {
        let mut app = App::new(false);
        app.state = State::Failed;
        app.error = Some("wrapper failed".to_owned());
        let (text, _) = rendered(&app, 80, 24);
        assert!(text.contains("Session failed"));
        assert!(text.contains("wrapper failed"));
        app.state = State::Listening;
        app.confirm_cancel = true;
        let (text, _) = rendered(&app, 80, 24);
        assert!(text.contains("Hard cancellation"));
        let (text, _) = rendered(&app, 59, 14);
        assert!(text.contains("Terminal too small"));
        assert!(text.contains("60×15"));
    }
}
