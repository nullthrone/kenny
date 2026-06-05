//! Telemetry: periodic snapshot collection and push.
//!
//! Each file in [`collectors`] produces one section (`{status, summary, ...}`).
//! [`scheduler`] collects every section on a timer and hands the resulting
//! snapshot to the tunnel to push as a `telemetry` frame. See `../docs/protocol.md`
//! § Telemetry sections and ADR-0007.

pub mod collectors;
pub mod scheduler;

use serde_json::{Map, Value};

use crate::protocol::{Status, Telemetry};

/// A single telemetry section payload: `status` + `summary` + raw fields.
///
/// Collectors build one of these; [`Section::into_value`] flattens it into the
/// `{status, summary, ...}` JSON object the wire contract requires.
#[derive(Debug, Clone)]
pub struct Section {
    /// Section health for this collection.
    pub status: Status,
    /// Short human-readable summary.
    pub summary: String,
    /// Section-specific raw fields (merged alongside `status`/`summary`).
    pub fields: Map<String, Value>,
}

impl Section {
    /// Build a section from an arbitrary JSON object of raw fields.
    pub fn with_fields(status: Status, summary: impl Into<String>, fields: Value) -> Self {
        let fields = match fields {
            Value::Object(m) => m,
            _ => Map::new(),
        };
        Self {
            status,
            summary: summary.into(),
            fields,
        }
    }

    /// Flatten into the `{status, summary, ...rawfields}` JSON object.
    pub fn into_value(self) -> Value {
        let mut obj = self.fields;
        obj.insert(
            "status".to_string(),
            Value::String(self.status.as_str().to_string()),
        );
        obj.insert("summary".to_string(), Value::String(self.summary));
        Value::Object(obj)
    }
}

/// Collect all sections and assemble a [`Telemetry`] frame body.
///
/// `sections` restricts collection to the named sections when non-empty (used by
/// the `telemetry_collect` request); an empty slice collects everything.
pub fn collect(agent_id: &str, sections: &[String]) -> Telemetry {
    let snapshot = collectors::collect_all(sections);
    Telemetry {
        agent_id: agent_id.to_string(),
        collected_at: crate::util::now_rfc3339(),
        snapshot,
    }
}
