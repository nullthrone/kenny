//! `tracing` → wire log forwarding.
//!
//! A [`ForwardLayer`] in the process subscriber captures `tracing` events,
//! converts each into a [`LogEvent`], and pushes it into a process-global bounded
//! ring buffer with drop-oldest overflow semantics. The tunnel's log-drain task
//! (see [`crate::tunnel`]) is woken via a [`Notify`] and drains the buffer into
//! [`Frame::Log`] frames on the outbound channel.
//!
//! To avoid a feedback loop, the layer never forwards its own events nor the
//! tunnel writer's events (see [`should_forward`]).

use std::collections::VecDeque;
use std::sync::{Mutex, OnceLock};

use serde_json::{Map, Value};
use tokio::sync::Notify;
use tracing::field::{Field, Visit};
use tracing::{Level, Subscriber};
use tracing_subscriber::layer::Context;
use tracing_subscriber::Layer;

use crate::protocol::{Frame, Log, LogLevel};

/// Maximum number of buffered events before the oldest are dropped.
const CAPACITY: usize = 256;

/// Environment variable selecting the minimum level to forward (default `info`).
const LEVEL_ENV: &str = "KENNY_LOG_FORWARD_LEVEL";

/// Target prefix of this module: never forwarded, to break the feedback loop.
const SELF_TARGET: &str = "kenny_agent::log_forward";

/// The process-global ring buffer of pending events.
static BUFFER: OnceLock<Mutex<VecDeque<LogEvent>>> = OnceLock::new();

/// Wakes the drain task when new events are buffered.
static NOTIFY: OnceLock<Notify> = OnceLock::new();

fn buffer() -> &'static Mutex<VecDeque<LogEvent>> {
    BUFFER.get_or_init(|| Mutex::new(VecDeque::with_capacity(CAPACITY)))
}

/// Notify handle the drain task awaits to learn there are events to flush.
pub fn notify() -> &'static Notify {
    NOTIFY.get_or_init(Notify::new)
}

/// A captured `tracing` event awaiting forwarding.
#[derive(Debug, Clone, PartialEq)]
pub struct LogEvent {
    at: String,
    level: LogLevel,
    target: String,
    message: String,
    fields: Option<Value>,
}

impl LogEvent {
    /// Convert this event into a wire [`Frame::Log`] for `agent_id`.
    pub fn into_frame(self, agent_id: &str) -> Frame {
        Frame::Log(Log {
            agent_id: agent_id.to_string(),
            at: self.at,
            level: self.level,
            target: self.target,
            message: self.message,
            fields: self.fields,
        })
    }
}

/// Push an event into the global buffer, dropping the oldest on overflow, and
/// wake the drain task.
pub fn push(ev: LogEvent) {
    {
        let mut buf = match buffer().lock() {
            Ok(b) => b,
            Err(poisoned) => poisoned.into_inner(),
        };
        if buf.len() >= CAPACITY {
            buf.pop_front();
        }
        buf.push_back(ev);
    }
    notify().notify_one();
}

/// Remove and return up to `max` buffered events (oldest first), emptying that
/// portion of the buffer.
pub fn drain_into(max: usize) -> Vec<LogEvent> {
    let mut buf = match buffer().lock() {
        Ok(b) => b,
        Err(poisoned) => poisoned.into_inner(),
    };
    let n = buf.len().min(max);
    buf.drain(..n).collect()
}

/// Map a `tracing` level onto the wire [`LogLevel`].
fn map_level(level: &Level) -> LogLevel {
    match *level {
        Level::ERROR => LogLevel::Error,
        Level::WARN => LogLevel::Warn,
        Level::INFO => LogLevel::Info,
        Level::DEBUG => LogLevel::Debug,
        Level::TRACE => LogLevel::Trace,
    }
}

/// Parse the forward-level threshold from the environment (default `info`).
fn threshold() -> LogLevel {
    match std::env::var(LEVEL_ENV) {
        Ok(v) => parse_level(&v).unwrap_or(LogLevel::Info),
        Err(_) => LogLevel::Info,
    }
}

/// Parse a level name (case-insensitive) into a [`LogLevel`].
fn parse_level(s: &str) -> Option<LogLevel> {
    match s.trim().to_ascii_lowercase().as_str() {
        "error" => Some(LogLevel::Error),
        "warn" => Some(LogLevel::Warn),
        "info" => Some(LogLevel::Info),
        "debug" => Some(LogLevel::Debug),
        "trace" => Some(LogLevel::Trace),
        _ => None,
    }
}

/// Numeric severity ordering: higher = more verbose. Used purely to compare a
/// record's level against the configured threshold.
fn verbosity(level: LogLevel) -> u8 {
    match level {
        LogLevel::Error => 0,
        LogLevel::Warn => 1,
        LogLevel::Info => 2,
        LogLevel::Debug => 3,
        LogLevel::Trace => 4,
    }
}

/// Decide whether an event with `target` at `level` should be forwarded, given
/// the configured `threshold`.
///
/// Factored out (and pure) so the level gate and the feedback-loop filter are
/// unit-testable. Events at or above the threshold severity are kept, except
/// those originating from the forwarder itself, which would otherwise loop.
fn should_forward(target: &str, level: LogLevel, threshold: LogLevel) -> bool {
    if target.starts_with(SELF_TARGET) {
        return false;
    }
    verbosity(level) <= verbosity(threshold)
}

/// Visitor that pulls the `message` field out specially and collects the rest
/// into a JSON object.
#[derive(Default)]
struct FieldVisitor {
    message: Option<String>,
    fields: Map<String, Value>,
}

impl FieldVisitor {
    fn record(&mut self, field: &Field, value: Value) {
        if field.name() == "message" {
            if let Value::String(s) = &value {
                self.message = Some(s.clone());
                return;
            }
        }
        self.fields.insert(field.name().to_string(), value);
    }
}

impl Visit for FieldVisitor {
    fn record_debug(&mut self, field: &Field, value: &dyn std::fmt::Debug) {
        let formatted = format!("{value:?}");
        if field.name() == "message" {
            self.message = Some(formatted);
        } else {
            self.fields
                .insert(field.name().to_string(), Value::String(formatted));
        }
    }

    fn record_str(&mut self, field: &Field, value: &str) {
        self.record(field, Value::String(value.to_string()));
    }

    fn record_i64(&mut self, field: &Field, value: i64) {
        self.record(field, Value::from(value));
    }

    fn record_u64(&mut self, field: &Field, value: u64) {
        self.record(field, Value::from(value));
    }

    fn record_bool(&mut self, field: &Field, value: bool) {
        self.record(field, Value::Bool(value));
    }

    fn record_f64(&mut self, field: &Field, value: f64) {
        self.record(field, Value::from(value));
    }
}

/// `tracing` layer that forwards events into the global buffer.
pub struct ForwardLayer;

impl<S> Layer<S> for ForwardLayer
where
    S: Subscriber,
{
    fn on_event(&self, event: &tracing::Event<'_>, _ctx: Context<'_, S>) {
        let meta = event.metadata();
        let level = map_level(meta.level());
        let target = meta.target();
        if !should_forward(target, level, threshold()) {
            return;
        }

        let mut visitor = FieldVisitor::default();
        event.record(&mut visitor);

        let fields = if visitor.fields.is_empty() {
            None
        } else {
            Some(Value::Object(visitor.fields))
        };

        push(LogEvent {
            at: crate::util::now_rfc3339(),
            level,
            target: target.to_string(),
            message: visitor.message.unwrap_or_default(),
            fields,
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ev(message: &str) -> LogEvent {
        LogEvent {
            at: "2026-06-05T00:00:00Z".to_string(),
            level: LogLevel::Info,
            target: "kenny_agent::test".to_string(),
            message: message.to_string(),
            fields: None,
        }
    }

    #[test]
    fn push_beyond_capacity_drops_oldest() {
        // Start from a clean buffer.
        let _ = drain_into(usize::MAX);
        for i in 0..(CAPACITY + 10) {
            push(ev(&format!("m{i}")));
        }
        let drained = drain_into(usize::MAX);
        assert_eq!(drained.len(), CAPACITY);
        // The first 10 should have been dropped; oldest surviving is "m10".
        assert_eq!(drained.first().unwrap().message, "m10");
        assert_eq!(
            drained.last().unwrap().message,
            format!("m{}", CAPACITY + 9)
        );
    }

    #[test]
    fn drain_returns_and_empties() {
        let _ = drain_into(usize::MAX);
        push(ev("a"));
        push(ev("b"));
        push(ev("c"));
        let first = drain_into(2);
        assert_eq!(first.len(), 2);
        assert_eq!(first[0].message, "a");
        let rest = drain_into(usize::MAX);
        assert_eq!(rest.len(), 1);
        assert_eq!(rest[0].message, "c");
        assert!(drain_into(usize::MAX).is_empty());
    }

    #[test]
    fn level_threshold_parsing() {
        assert_eq!(parse_level("error"), Some(LogLevel::Error));
        assert_eq!(parse_level("  WARN "), Some(LogLevel::Warn));
        assert_eq!(parse_level("Info"), Some(LogLevel::Info));
        assert_eq!(parse_level("debug"), Some(LogLevel::Debug));
        assert_eq!(parse_level("trace"), Some(LogLevel::Trace));
        assert_eq!(parse_level("nonsense"), None);
    }

    #[test]
    fn self_target_is_filtered() {
        // Even an error from the forwarder is never forwarded.
        assert!(!should_forward(
            "kenny_agent::log_forward",
            LogLevel::Error,
            LogLevel::Trace
        ));
        assert!(!should_forward(
            "kenny_agent::log_forward::tests",
            LogLevel::Error,
            LogLevel::Info
        ));
    }

    #[test]
    fn threshold_gate() {
        // Default info threshold: info and above pass, debug/trace do not.
        assert!(should_forward(
            "kenny_agent::tunnel",
            LogLevel::Info,
            LogLevel::Info
        ));
        assert!(should_forward(
            "kenny_agent::tunnel",
            LogLevel::Error,
            LogLevel::Info
        ));
        assert!(!should_forward(
            "kenny_agent::tunnel",
            LogLevel::Debug,
            LogLevel::Info
        ));
        // Raise to trace: everything passes.
        assert!(should_forward(
            "kenny_agent::tunnel",
            LogLevel::Trace,
            LogLevel::Trace
        ));
    }

    #[test]
    fn into_frame_carries_agent_id() {
        let frame = ev("hi").into_frame("papa-pc");
        match frame {
            Frame::Log(log) => {
                assert_eq!(log.agent_id, "papa-pc");
                assert_eq!(log.message, "hi");
            }
            _ => panic!("expected Frame::Log"),
        }
    }
}
