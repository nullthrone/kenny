//! Wire-protocol types mirroring `../docs/protocol.md` (v0.1).
//!
//! These serde models are the Rust side of the contract between `kenny-server`
//! (Python) and `kenny-agent`. They are round-tripped against `../docs/fixtures/`
//! in the `fixtures` test. Do not change a frame/tool shape here without first
//! changing the contract in `docs/protocol.md`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Wire-protocol version implemented by this binary (see protocol.md § Versioning).
///
/// Not currently placed on the wire (reserved for `register.meta.protocol`).
pub const PROTOCOL_VERSION: &str = "0.3";

/// One WebSocket text message. Tagged by the `type` field.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    /// agent → server: identifies the agent right after connect.
    Register(Register),
    /// server → agent: invoke one capability tool.
    Request(Request),
    /// agent → server: result/error for a `request` (by `id`).
    Response(Response),
    /// agent → server: periodic pushed snapshot (no request).
    Telemetry(Telemetry),
    /// heartbeat (either direction).
    Ping,
    /// heartbeat reply (either direction).
    Pong,
}

/// `register` frame body.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Register {
    pub agent_id: String,
    pub token: String,
    pub meta: RegisterMeta,
}

/// Metadata describing the registering agent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RegisterMeta {
    pub hostname: String,
    /// One of `windows`, `linux`, `macos`.
    pub os: String,
    pub version: String,
}

/// `request` frame body.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Request {
    /// Server-generated UUID.
    pub id: String,
    /// Tool name from the catalog (e.g. `powershell_exec`).
    pub tool: String,
    /// Per-tool argument object. Absent in the fixture is treated as `{}`.
    #[serde(default)]
    pub args: Value,
}

/// `response` frame body. Models both success (`ok:true`, `result`) and error
/// (`ok:false`, `error`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Response {
    pub id: String,
    pub ok: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub result: Option<Value>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<ResponseError>,
}

impl Response {
    /// Build a success response carrying `result`.
    pub fn ok(id: impl Into<String>, result: Value) -> Self {
        Self {
            id: id.into(),
            ok: true,
            result: Some(result),
            error: None,
        }
    }

    /// Build an error response.
    pub fn err(id: impl Into<String>, code: ErrorCode, message: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            ok: false,
            result: None,
            error: Some(ResponseError {
                code,
                message: message.into(),
            }),
        }
    }
}

/// `response.error` payload.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ResponseError {
    pub code: ErrorCode,
    pub message: String,
}

/// Closed set of error codes (`response.error.code`).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ErrorCode {
    Timeout,
    NotFound,
    ExecFailed,
    Unsupported,
    BadArgs,
    Internal,
    /// The agent is online but remote control was switched off locally at the
    /// endpoint (via the tray menu); mutating tools are refused. See ADR-0010.
    Disabled,
}

/// `telemetry` frame body (also the shape returned by `telemetry_collect`).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Telemetry {
    pub agent_id: String,
    /// RFC 3339 / ISO 8601 collection timestamp.
    pub collected_at: String,
    /// Map of section name → section payload (`{status, summary, ...}`).
    pub snapshot: Map<String, Value>,
}

/// Health status carried by every telemetry section.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Ok,
    Warn,
    Crit,
}

impl Status {
    /// Lowercase wire string.
    pub fn as_str(self) -> &'static str {
        match self {
            Status::Ok => "ok",
            Status::Warn => "warn",
            Status::Crit => "crit",
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;

    /// Round-trip every golden fixture: parse into `Frame`, re-serialize, and assert
    /// the JSON `Value` is structurally identical (key order independent).
    #[test]
    fn fixtures_round_trip() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../docs/fixtures");
        let mut checked = 0;
        for entry in fs::read_dir(&dir).expect("read fixtures dir") {
            let path = entry.unwrap().path();
            if path.extension().and_then(|e| e.to_str()) != Some("json") {
                continue;
            }
            let raw = fs::read_to_string(&path).expect("read fixture");
            let original: Value = serde_json::from_str(&raw)
                .unwrap_or_else(|e| panic!("invalid JSON in {}: {e}", path.display()));

            let frame: Frame = serde_json::from_value(original.clone())
                .unwrap_or_else(|e| panic!("Frame deserialize failed for {}: {e}", path.display()));
            let reser = serde_json::to_value(&frame)
                .unwrap_or_else(|e| panic!("Frame serialize failed for {}: {e}", path.display()));

            assert_eq!(
                reser,
                original,
                "round-trip mismatch for {}",
                path.display()
            );
            checked += 1;
        }
        assert!(
            checked >= 7,
            "expected to check the golden fixtures, got {checked}"
        );
    }

    #[test]
    fn error_code_wire_names() {
        assert_eq!(
            serde_json::to_string(&ErrorCode::ExecFailed).unwrap(),
            "\"exec_failed\""
        );
        assert_eq!(
            serde_json::to_string(&ErrorCode::BadArgs).unwrap(),
            "\"bad_args\""
        );
    }

    #[test]
    fn response_helpers() {
        let ok = Response::ok("abc", serde_json::json!({"x": 1}));
        let v = serde_json::to_value(Frame::Response(ok)).unwrap();
        assert_eq!(v["type"], "response");
        assert_eq!(v["ok"], true);
        assert!(v.get("error").is_none());

        let err = Response::err("abc", ErrorCode::Timeout, "boom");
        let v = serde_json::to_value(Frame::Response(err)).unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"]["code"], "timeout");
        assert!(v.get("result").is_none());
    }
}
