use serde::Deserialize;
use std::collections::BTreeMap;

pub const MAX_LINE_BYTES: usize = 1024 * 1024;
const NEMOTRON: &str = "nemo:nemotron-streaming-en";
const PARAKEET: &str = "nemo:parakeet-tdt-v3";

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum EventState {
    Provisional,
    ModelFinal,
    Committed,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub struct Event {
    pub sequence: u64,
    pub monotonic_ms: u64,
    pub session_id: String,
    pub track_id: String,
    pub segment_id: u64,
    pub revision: u64,
    pub state: EventState,
    pub audio_start_ms: u64,
    pub audio_end_ms: u64,
    pub model: String,
    pub latency_ms: u64,
    pub text: String,
    pub degraded: bool,
    pub degradation_reason: Option<String>,
}

#[derive(Debug, Default)]
pub struct Validator {
    next_sequence: u64,
    last_monotonic_ms: u64,
    session_id: Option<String>,
    next_segment: u64,
    next_commit: u64,
    segments: BTreeMap<u64, SegmentState>,
}

#[derive(Debug)]
struct SegmentState {
    next_revision: u64,
    finalized: bool,
    committed: bool,
    selected_revision: u64,
    selected_model: String,
    selected_text: String,
    audio_start_ms: u64,
    audio_end_ms: u64,
}

impl Validator {
    pub fn parse_and_validate(&mut self, line: &[u8]) -> Result<Event, String> {
        if line.len() > MAX_LINE_BYTES {
            return Err(format!("protocol line exceeds {MAX_LINE_BYTES} bytes"));
        }
        let event: Event = serde_json::from_slice(line)
            .map_err(|error| format!("invalid protocol event: {error}"))?;
        self.validate(&event)?;
        Ok(event)
    }

    pub fn validate(&mut self, event: &Event) -> Result<(), String> {
        if event.sequence != self.next_sequence {
            return Err(format!(
                "noncontiguous sequence: expected {}, got {}",
                self.next_sequence, event.sequence
            ));
        }
        if event.sequence > 0 && event.monotonic_ms < self.last_monotonic_ms {
            return Err("event monotonic clock moved backwards".to_owned());
        }
        if event.session_id.is_empty() {
            return Err("empty session_id".to_owned());
        }
        if let Some(session_id) = &self.session_id
            && event.session_id != *session_id
        {
            return Err("session_id changed during the stream".to_owned());
        }
        if event.track_id != "interactive" {
            return Err(format!("unsupported track_id: {}", event.track_id));
        }
        if event.audio_end_ms < event.audio_start_ms {
            return Err("audio_end_ms precedes audio_start_ms".to_owned());
        }
        if event.model.is_empty() || event.text.trim().is_empty() {
            return Err("event model and text must be nonempty".to_owned());
        }
        if event.degraded {
            if event.state != EventState::Committed
                || event
                    .degradation_reason
                    .as_deref()
                    .is_none_or(str::is_empty)
            {
                return Err("degradation requires a committed event and reason".to_owned());
            }
        } else if event.degradation_reason.is_some() {
            return Err("non-degraded event has a degradation reason".to_owned());
        }

        if !self.segments.contains_key(&event.segment_id) {
            if event.segment_id != self.next_segment {
                return Err(format!(
                    "noncontiguous segment: expected {}, got {}",
                    self.next_segment, event.segment_id
                ));
            }
            self.segments.insert(
                event.segment_id,
                SegmentState {
                    next_revision: 0,
                    finalized: false,
                    committed: false,
                    selected_revision: 0,
                    selected_model: String::new(),
                    selected_text: String::new(),
                    audio_start_ms: 0,
                    audio_end_ms: 0,
                },
            );
            self.next_segment += 1;
        }
        let segment = self
            .segments
            .get_mut(&event.segment_id)
            .expect("inserted above");
        if segment.committed {
            return Err(format!(
                "event follows commit for segment {}",
                event.segment_id
            ));
        }
        match event.state {
            EventState::Provisional => {
                if segment.finalized || event.model != NEMOTRON {
                    return Err("invalid provisional transition".to_owned());
                }
                require_revision(segment.next_revision, event.revision)?;
                segment.next_revision += 1;
            }
            EventState::ModelFinal if !segment.finalized => {
                if event.model != NEMOTRON {
                    return Err("first model_final must come from Nemotron".to_owned());
                }
                require_revision(segment.next_revision, event.revision)?;
                segment.next_revision += 1;
                segment.finalized = true;
                segment.selected_revision = event.revision;
                segment.selected_model.clone_from(&event.model);
                segment.selected_text.clone_from(&event.text);
                segment.audio_start_ms = event.audio_start_ms;
                segment.audio_end_ms = event.audio_end_ms;
            }
            EventState::ModelFinal => {
                if event.model != PARAKEET || segment.selected_model != NEMOTRON {
                    return Err("invalid correction transition".to_owned());
                }
                require_bounds(segment, event)?;
                require_revision(segment.next_revision, event.revision)?;
                segment.next_revision += 1;
                segment.selected_revision = event.revision;
                segment.selected_model.clone_from(&event.model);
                segment.selected_text.clone_from(&event.text);
            }
            EventState::Committed => {
                if !segment.finalized
                    || event.segment_id != self.next_commit
                    || event.revision != segment.selected_revision
                    || event.model != segment.selected_model
                    || event.text != segment.selected_text
                {
                    return Err("unordered, duplicate, or noncanonical commit".to_owned());
                }
                require_bounds(segment, event)?;
                if (event.model == NEMOTRON) != event.degraded
                    || event.model == PARAKEET && event.degraded
                {
                    return Err("commit degradation does not match selected model".to_owned());
                }
                segment.committed = true;
                self.next_commit += 1;
            }
        }
        self.next_sequence += 1;
        self.last_monotonic_ms = event.monotonic_ms;
        if self.session_id.is_none() {
            self.session_id = Some(event.session_id.clone());
        }
        Ok(())
    }
}

fn require_revision(expected: u64, actual: u64) -> Result<(), String> {
    if actual == expected {
        Ok(())
    } else {
        Err(format!(
            "invalid revision: expected {expected}, got {actual}"
        ))
    }
}

fn require_bounds(segment: &SegmentState, event: &Event) -> Result<(), String> {
    if event.audio_start_ms == segment.audio_start_ms && event.audio_end_ms == segment.audio_end_ms
    {
        Ok(())
    } else {
        Err("finalized segment audio bounds changed".to_owned())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TextSegment {
    pub segment_id: u64,
    pub text: String,
    pub degraded: bool,
    pub degradation_reason: Option<String>,
}

#[derive(Debug, Default)]
pub struct Transcript {
    pub committed: Vec<TextSegment>,
    pub pending: BTreeMap<u64, TextSegment>,
    pub provisional: Option<TextSegment>,
    pub latest_partial_latency_ms: Option<u64>,
    pub latest_commit_latency_ms: Option<u64>,
    pub degraded_count: usize,
    pub last_degradation_reason: Option<String>,
}

impl Transcript {
    pub fn apply(&mut self, event: &Event) {
        let item = TextSegment {
            segment_id: event.segment_id,
            text: event.text.clone(),
            degraded: event.degraded,
            degradation_reason: event.degradation_reason.clone(),
        };
        match event.state {
            EventState::Provisional => {
                self.latest_partial_latency_ms = Some(event.latency_ms);
                self.provisional = Some(item);
            }
            EventState::ModelFinal => {
                if self
                    .provisional
                    .as_ref()
                    .is_some_and(|active| active.segment_id == event.segment_id)
                {
                    self.provisional = None;
                }
                self.pending.insert(event.segment_id, item);
            }
            EventState::Committed => {
                self.latest_commit_latency_ms = Some(event.latency_ms);
                self.pending.remove(&event.segment_id);
                if event.degraded {
                    self.degraded_count += 1;
                    self.last_degradation_reason
                        .clone_from(&event.degradation_reason);
                }
                self.committed.push(item);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn line(
        sequence: u64,
        segment: u64,
        revision: u64,
        state: &str,
        model: &str,
        text: &str,
    ) -> Vec<u8> {
        format!(r#"{{"sequence":{sequence},"monotonic_ms":{sequence},"session_id":"s","track_id":"interactive","segment_id":{segment},"revision":{revision},"state":"{state}","audio_start_ms":0,"audio_end_ms":100,"model":"{model}","latency_ms":4,"text":"{text}","degraded":false,"degradation_reason":null,"future":true}}"#).into_bytes()
    }

    #[test]
    fn accepts_replacement_then_ordered_commit_and_unknown_fields() {
        let mut validator = Validator::default();
        let mut transcript = Transcript::default();
        for bytes in [
            line(0, 0, 0, "provisional", NEMOTRON, "hel"),
            line(1, 0, 1, "model_final", NEMOTRON, "hello"),
            line(2, 0, 2, "model_final", PARAKEET, "hello world"),
            line(3, 0, 2, "committed", PARAKEET, "hello world"),
        ] {
            let event = validator.parse_and_validate(&bytes).unwrap();
            transcript.apply(&event);
        }
        assert_eq!(transcript.committed[0].text, "hello world");
        assert!(transcript.pending.is_empty());
        assert!(transcript.provisional.is_none());
    }

    #[test]
    fn tracks_degradation_state() {
        let mut validator = Validator::default();
        validator
            .parse_and_validate(&line(0, 0, 0, "model_final", NEMOTRON, "fallback"))
            .unwrap();
        let degraded = br#"{"sequence":1,"monotonic_ms":2,"session_id":"s","track_id":"interactive","segment_id":0,"revision":0,"state":"committed","audio_start_ms":0,"audio_end_ms":100,"model":"nemo:nemotron-streaming-en","latency_ms":9,"text":"fallback","degraded":true,"degradation_reason":"timeout"}"#;
        let event = validator.parse_and_validate(degraded).unwrap();
        let mut transcript = Transcript::default();
        transcript.apply(&event);
        assert_eq!(transcript.degraded_count, 1);
        assert_eq!(
            transcript.last_degradation_reason.as_deref(),
            Some("timeout")
        );
    }

    #[test]
    fn rejects_malformed_oversized_and_invalid_transitions() {
        let mut validator = Validator::default();
        assert!(validator.parse_and_validate(b"not-json").is_err());
        assert!(
            validator
                .parse_and_validate(&vec![b'x'; MAX_LINE_BYTES + 1])
                .is_err()
        );
        let mut validator = Validator::default();
        assert!(
            validator
                .parse_and_validate(&line(1, 0, 0, "provisional", NEMOTRON, "bad"))
                .unwrap_err()
                .contains("sequence")
        );
        let mut validator = Validator::default();
        assert!(
            validator
                .parse_and_validate(&line(0, 0, 2, "provisional", NEMOTRON, "bad"))
                .unwrap_err()
                .contains("revision")
        );
    }

    #[test]
    fn rejects_changed_session_noninteractive_and_unordered_commit() {
        let mut validator = Validator::default();
        validator
            .parse_and_validate(&line(0, 0, 0, "model_final", NEMOTRON, "one"))
            .unwrap();
        let changed = String::from_utf8(line(1, 1, 0, "provisional", NEMOTRON, "two"))
            .unwrap()
            .replace("\"session_id\":\"s\"", "\"session_id\":\"other\"");
        assert!(
            validator
                .parse_and_validate(changed.as_bytes())
                .unwrap_err()
                .contains("session_id")
        );
        let mut validator = Validator::default();
        let wrong_track = String::from_utf8(line(0, 0, 0, "provisional", NEMOTRON, "x"))
            .unwrap()
            .replace("interactive", "offline");
        assert!(
            validator
                .parse_and_validate(wrong_track.as_bytes())
                .unwrap_err()
                .contains("track_id")
        );

        let mut validator = Validator::default();
        validator
            .parse_and_validate(&line(0, 0, 0, "model_final", NEMOTRON, "one"))
            .unwrap();
        validator
            .parse_and_validate(&line(1, 1, 0, "model_final", NEMOTRON, "two"))
            .unwrap();
        let early_commit = String::from_utf8(line(2, 1, 0, "committed", NEMOTRON, "two"))
            .unwrap()
            .replace("\"degraded\":false", "\"degraded\":true")
            .replace(
                "\"degradation_reason\":null",
                "\"degradation_reason\":\"timeout\"",
            );
        assert!(
            validator
                .parse_and_validate(early_commit.as_bytes())
                .unwrap_err()
                .contains("unordered")
        );
    }
}
