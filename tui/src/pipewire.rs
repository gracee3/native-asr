use serde_json::{Map, Value};
use std::collections::BTreeMap;
use std::env;
use std::ffi::OsString;
use std::process::Command;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Source {
    pub node_name: String,
    pub description: String,
    pub serial: String,
    pub is_virtual: bool,
}

impl Source {
    pub fn kind(&self) -> &'static str {
        if self.is_virtual {
            "virtual"
        } else {
            "physical"
        }
    }
}

pub fn pw_dump_binary() -> OsString {
    env::var_os("PW_DUMP_BIN").unwrap_or_else(|| OsString::from("pw-dump"))
}

pub fn enumerate() -> Result<Vec<Source>, String> {
    let binary = pw_dump_binary();
    let output = Command::new(&binary)
        .output()
        .map_err(|error| format!("could not run {}: {error}", binary.to_string_lossy()))?;
    if !output.status.success() {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if detail.is_empty() {
            format!("{} exited with {}", binary.to_string_lossy(), output.status)
        } else {
            format!("{}: {detail}", binary.to_string_lossy())
        });
    }
    parse(&output.stdout)
}

pub fn parse(input: &[u8]) -> Result<Vec<Source>, String> {
    let value: Value =
        serde_json::from_slice(input).map_err(|error| format!("invalid pw-dump JSON: {error}"))?;
    let objects = value
        .as_array()
        .ok_or_else(|| "invalid pw-dump JSON: top level is not an array".to_owned())?;
    let mut by_name = BTreeMap::<String, Source>::new();
    for object in objects {
        let Some(root) = object.as_object() else {
            continue;
        };
        if root.get("type").and_then(Value::as_str) != Some("PipeWire:Interface:Node") {
            continue;
        }
        let Some(props) = root
            .get("info")
            .and_then(Value::as_object)
            .and_then(|info| info.get("props"))
            .and_then(Value::as_object)
        else {
            continue;
        };
        let Some(class) = string_property(props, "media.class") else {
            continue;
        };
        if class != "Audio/Source" && !class.starts_with("Audio/Source/") {
            continue;
        }
        let Some(node_name) = string_property(props, "node.name") else {
            continue;
        };
        if node_name.is_empty() {
            continue;
        }
        let description = string_property(props, "node.description")
            .or_else(|| string_property(props, "node.nick"))
            .unwrap_or_else(|| node_name.clone());
        let serial = scalar_property(props, "object.serial")
            .or_else(|| root.get("id").and_then(scalar_value))
            .unwrap_or_else(|| "unknown".to_owned());
        let device_api = string_property(props, "device.api");
        let is_virtual = class != "Audio/Source"
            || bool_property(props, "node.virtual")
            || !props.contains_key("device.id")
            || device_api.is_some_and(|api| api == "virtual");
        let source = Source {
            node_name: node_name.clone(),
            description,
            serial,
            is_virtual,
        };
        by_name
            .entry(node_name)
            .and_modify(|existing| {
                if source_key(&source) < source_key(existing) {
                    existing.clone_from(&source);
                }
            })
            .or_insert(source);
    }
    let mut sources: Vec<_> = by_name.into_values().collect();
    sources.sort_by(|left, right| source_key(left).cmp(&source_key(right)));
    Ok(sources)
}

pub fn revalidate(sources: &[Source], selected: &Source) -> Option<usize> {
    sources.iter().position(|source| source == selected)
}

fn source_key(source: &Source) -> (&str, &str, &str, bool) {
    (
        &source.node_name,
        &source.description,
        &source.serial,
        source.is_virtual,
    )
}

fn string_property(props: &Map<String, Value>, name: &str) -> Option<String> {
    props.get(name).and_then(Value::as_str).map(str::to_owned)
}

fn scalar_property(props: &Map<String, Value>, name: &str) -> Option<String> {
    props.get(name).and_then(scalar_value)
}

fn scalar_value(value: &Value) -> Option<String> {
    match value {
        Value::String(value) => Some(value.clone()),
        Value::Number(value) => Some(value.to_string()),
        _ => None,
    }
}

fn bool_property(props: &Map<String, Value>, name: &str) -> bool {
    match props.get(name) {
        Some(Value::Bool(value)) => *value,
        Some(Value::String(value)) => value == "true",
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_deduplicates_and_sorts_sources() {
        let input = br#"[
          {"id":8,"type":"PipeWire:Interface:Node","info":{"props":{
            "media.class":"Audio/Source/Virtual","node.name":"z.virtual",
            "node.description":"Virtual Mic","object.serial":88}}},
          {"id":7,"type":"PipeWire:Interface:Node","info":{"props":{
            "media.class":"Audio/Sink","node.name":"ignored"}}},
          {"id":3,"type":"PipeWire:Interface:Node","info":{"props":{
            "media.class":"Audio/Source","node.name":"a.physical",
            "node.description":"Built-in Mic","object.serial":"33","device.id":2}}},
          {"id":4,"type":"PipeWire:Interface:Node","info":{"props":{
            "media.class":"Audio/Source","node.name":"a.physical",
            "node.description":"Duplicate","object.serial":"44","device.id":2}}}
        ]"#;
        let sources = parse(input).unwrap();
        assert_eq!(sources.len(), 2);
        assert_eq!(sources[0].node_name, "a.physical");
        assert_eq!(sources[0].serial, "33");
        assert!(!sources[0].is_virtual);
        assert_eq!(sources[1].node_name, "z.virtual");
        assert!(sources[1].is_virtual);
    }

    #[test]
    fn rejects_non_array_dump() {
        assert!(parse(br#"{}"#).unwrap_err().contains("top level"));
    }

    #[test]
    fn rejects_absent_or_replaced_selection() {
        let selected = Source {
            node_name: "source.one".to_owned(),
            description: "Microphone".to_owned(),
            serial: "10".to_owned(),
            is_virtual: false,
        };
        assert_eq!(
            revalidate(std::slice::from_ref(&selected), &selected),
            Some(0)
        );
        let mut replaced = selected.clone();
        replaced.serial = "11".to_owned();
        assert_eq!(revalidate(&[replaced], &selected), None);
        assert_eq!(revalidate(&[], &selected), None);
    }
}
