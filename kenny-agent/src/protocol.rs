//! Wire-protocol types mirroring `../docs/protocol.md` (v0.17).
//!
//! These serde models are the Rust side of the contract between `kenny-server`
//! (Python) and `kenny-agent`. They are round-tripped against `../docs/fixtures/`
//! in the `fixtures` test. Do not change a frame/tool shape here without first
//! changing the contract in `docs/protocol.md`.

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Wire-protocol version implemented by this binary (see protocol.md § Versioning).
///
/// From v0.8 this is placed on the wire in `register.protocol` to select the
/// mutual-auth handshake.
pub const PROTOCOL_VERSION: &str = "0.17";

/// One WebSocket text message. Tagged by the `type` field.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Frame {
    /// agent → server: identifies the agent right after connect.
    Register(Register),
    /// server → agent: the server's signed nonce (mutual-auth step 2). See ADR-0022.
    Challenge(Challenge),
    /// agent → server: the agent's signature over the transcript (mutual-auth step 3).
    Auth(Auth),
    /// server → agent: invoke one capability tool.
    Request(Request),
    /// agent → server: result/error for a `request` (by `id`).
    Response(Response),
    /// agent → server: periodic pushed snapshot (no request).
    Telemetry(Telemetry),
    /// agent → server: a forwarded `tracing` log record.
    Log(Log),
    /// server → agent: operator's append-only extra deny rules (ADR-0020). Additive to
    /// the compiled-in built-ins; can never weaken or remove them.
    Policy(Policy),
    /// heartbeat (either direction).
    Ping,
    /// heartbeat reply (either direction).
    Pong,
}

/// `register` frame body.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Register {
    pub agent_id: String,
    /// The agent's `PROTOCOL_VERSION`; present from v0.8 to select the mutual-auth path.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub protocol: Option<String>,
    /// 32 fresh random bytes (base64) the server must sign in the `challenge`.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub client_nonce: Option<String>,
    /// Per-agent bearer secret. Optional and legacy: honoured only during the migration
    /// window when the signature path is not in use. See ADR-0022.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub token: Option<String>,
    pub meta: RegisterMeta,
}

/// `challenge` frame body (server → agent): the server's signed nonce.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Challenge {
    /// 32 random bytes (base64) the agent must bind into its own signature.
    pub server_nonce: String,
    /// Ed25519 signature (base64) over the transcript, made with the server's private key.
    pub server_sig: String,
}

/// `auth` frame body (agent → server): the agent's signature over the transcript.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Auth {
    /// Ed25519 signature (base64) over the transcript, made with the agent's private key.
    pub agent_sig: String,
}

/// Metadata describing the registering agent.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct RegisterMeta {
    pub hostname: String,
    /// One of `windows`, `linux`, `macos`.
    pub os: String,
    pub version: String,
    /// Normalized CPU architecture: `x86_64` or `aarch64`.
    pub arch: String,
    /// Release channel this binary was built from: `stable` or `dev` (ADR-0048).
    pub channel: String,
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
    /// endpoint (via the tray menu); mutating tools are refused. See ADR-0011.
    Disabled,
    /// Refused by the agent's deterministic, always-on safety guard: a compiled-in
    /// policy that blocks individually dangerous calls regardless of operator approval
    /// or kill-switch state. Cannot be turned off remotely. See ADR-0019.
    Blocked,
    /// The agent is online but voluntarily stepped back because a protected game is
    /// running on the endpoint: it suspends its most anti-cheat-visible tools (today
    /// `screen_capture`) while the game runs, to avoid being mistaken for cheating
    /// software. Automatic and game-scoped; clears when the game exits. See ADR-0035.
    Paused,
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

/// `log` frame body: a single forwarded `tracing` record (agent → server).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Log {
    pub agent_id: String,
    /// RFC 3339 / ISO 8601 timestamp of when the record was emitted.
    pub at: String,
    pub level: LogLevel,
    /// `tracing` event target (module path or explicit `target:`).
    pub target: String,
    /// The formatted log message.
    pub message: String,
    /// Structured fields carried alongside the message, if any.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub fields: Option<Value>,
}

/// `policy` frame body: the operator's current set of append-only deny rules (ADR-0020).
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct Policy {
    pub rules: Vec<PolicyRule>,
}

/// A single deny rule: `applies_to` selects which call surface the `pattern` is matched
/// against, and `reason` is reported on a hit. Shared shape between the embedded built-in
/// catalog (`docs/policy/deny_rules.json`) and operator-supplied rules.
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct PolicyRule {
    pub id: String,
    pub applies_to: PolicyTarget,
    pub pattern: String,
    pub reason: String,
}

/// The call surface a [`PolicyRule`] applies to (`applies_to`).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PolicyTarget {
    Powershell,
    Posix,
    SelfProtection,
    Path,
}

/// Severity of a forwarded log record (`log.level`).
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LogLevel {
    Error,
    Warn,
    Info,
    Debug,
    Trace,
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
            checked >= 9,
            "expected to check the golden fixtures, got {checked}"
        );
    }

    /// Interop guard against the Python side: rebuild the transcript from the golden
    /// vectors, sign it with the agent seed, and assert it matches `agent_sig_b64`; then
    /// verify `server_sig_b64` against `server_public_key_b64`. If the transcript byte
    /// layout drifts between Rust and Python, this fails. See ADR-0022.
    #[test]
    fn mutual_auth_vectors_interop() {
        use base64::engine::general_purpose::STANDARD;
        use base64::Engine as _;
        use ed25519_dalek::{Signer, SigningKey, Verifier, VerifyingKey};

        let path =
            Path::new(env!("CARGO_MANIFEST_DIR")).join("../docs/fixtures/vectors/mutual_auth.json");
        let raw = fs::read_to_string(&path).expect("read mutual_auth vectors");
        let v: Value = serde_json::from_str(&raw).expect("parse mutual_auth vectors");

        let agent_id = v["agent_id"].as_str().unwrap();
        let client_nonce = STANDARD
            .decode(v["client_nonce_b64"].as_str().unwrap())
            .unwrap();
        let server_nonce = STANDARD
            .decode(v["server_nonce_b64"].as_str().unwrap())
            .unwrap();

        // Rebuild the transcript via the single-source-of-truth helper used by the tunnel.
        let transcript = crate::keys::build_transcript(agent_id, &client_nonce, &server_nonce);

        // Cross-check the documented hex transcript.
        let want_hex = v["transcript_hex"].as_str().unwrap();
        let got_hex: String = transcript.iter().map(|b| format!("{b:02x}")).collect();
        assert_eq!(got_hex, want_hex, "transcript bytes differ from the vector");

        // Sign with the agent seed; must reproduce agent_sig_b64.
        let agent_seed: [u8; 32] = STANDARD
            .decode(v["agent_seed_b64"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let agent_key = SigningKey::from_bytes(&agent_seed);
        let agent_sig = agent_key.sign(&transcript);
        assert_eq!(
            STANDARD.encode(agent_sig.to_bytes()),
            v["agent_sig_b64"].as_str().unwrap(),
            "agent signature differs from the vector"
        );

        // Verify server_sig_b64 against server_public_key_b64 (the agent's pin path).
        let server_pub: [u8; 32] = STANDARD
            .decode(v["server_public_key_b64"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let server_vk = VerifyingKey::from_bytes(&server_pub).unwrap();
        let server_sig_bytes: [u8; 64] = STANDARD
            .decode(v["server_sig_b64"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap();
        let server_sig = ed25519_dalek::Signature::from_bytes(&server_sig_bytes);
        server_vk
            .verify(&transcript, &server_sig)
            .expect("server signature must verify against the pinned public key");
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
        assert_eq!(
            serde_json::to_string(&ErrorCode::Blocked).unwrap(),
            "\"blocked\""
        );
        assert_eq!(
            serde_json::to_string(&ErrorCode::Paused).unwrap(),
            "\"paused\""
        );
    }

    #[test]
    fn log_frame_round_trip() {
        // With fields.
        let with = Frame::Log(Log {
            agent_id: "example-pc".to_string(),
            at: "2026-06-04T18:00:01Z".to_string(),
            level: LogLevel::Warn,
            target: "kenny_agent::tunnel".to_string(),
            message: "tunnel error; backing off".to_string(),
            fields: Some(serde_json::json!({"error": "connection reset", "backoff_secs": 4})),
        });
        let v = serde_json::to_value(&with).unwrap();
        assert_eq!(v["type"], "log");
        assert_eq!(v["level"], "warn");
        assert_eq!(v["fields"]["backoff_secs"], 4);
        let back: Frame = serde_json::from_value(v).unwrap();
        assert_eq!(back, with);

        // Without fields: the key is omitted entirely.
        let without = Frame::Log(Log {
            agent_id: "example-pc".to_string(),
            at: "2026-06-04T18:00:01Z".to_string(),
            level: LogLevel::Info,
            target: "kenny_agent::dispatch".to_string(),
            message: "hello".to_string(),
            fields: None,
        });
        let v = serde_json::to_value(&without).unwrap();
        assert_eq!(v["type"], "log");
        assert!(v.get("fields").is_none());
        let back: Frame = serde_json::from_value(v).unwrap();
        assert_eq!(back, without);
    }

    /// Randomized-mutation smoke test: mutated JSON derived from real frame shapes must
    /// never panic `Frame` deserialization, only return `Err`. A fixed seed keeps this
    /// deterministic; kept small (5k iterations, hand-rolled PRNG, no new dependency) as a
    /// permanent guard against a panicking `unwrap`/index creeping into `serde` derives or
    /// custom (de)serialization here.
    #[test]
    fn frame_from_random_bytes_never_panics() {
        // splitmix64
        struct Rng(u64);
        impl Rng {
            fn next(&mut self) -> u64 {
                self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
                let mut z = self.0;
                z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
                z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
                z ^ (z >> 31)
            }
        }
        let mut rng = Rng(0xC0FFEE);
        let seeds = [
            r#"{"type":"register"}"#,
            r#"{"type":"request","id":"1","tool":"x","args":{}}"#,
            r#"{"type":"response","id":"1","ok":true,"result":{}}"#,
            r#"{"type":"telemetry","agent_id":"a","collected_at":"x","snapshot":{}}"#,
            r#"{"type":"log","agent_id":"a","at":"x","level":"info","target":"t","message":"m"}"#,
            r#"{"type":"policy","rules":[{"id":"1","applies_to":"path","pattern":"p","reason":"r"}]}"#,
            r#"{"type":"challenge","server_nonce":"","server_sig":""}"#,
            r#"{"type":"auth","agent_sig":""}"#,
            "null",
            "{}",
            "[]",
            "1e999999",
            "-99999999999999999999999999999999",
            "\"\\ud800\"",
        ];
        for iter in 0..5000u32 {
            let base = seeds[(rng.next() as usize) % seeds.len()];
            let mut bytes: Vec<u8> = base.as_bytes().to_vec();
            // Mutate: flip/insert/delete a handful of random bytes.
            let mutations = 1 + (rng.next() % 6) as usize;
            for _ in 0..mutations {
                if bytes.is_empty() {
                    break;
                }
                let op = rng.next() % 3;
                let idx = (rng.next() as usize) % bytes.len();
                match op {
                    0 => bytes[idx] = (rng.next() % 256) as u8,
                    1 => bytes.insert(idx, (rng.next() % 256) as u8),
                    _ => {
                        bytes.remove(idx);
                    }
                }
            }
            let result = std::panic::catch_unwind(|| {
                let _: Result<Frame, _> = serde_json::from_slice(&bytes);
            });
            if result.is_err() {
                panic!(
                    "iter {iter}: panic on input {:?}",
                    String::from_utf8_lossy(&bytes)
                );
            }
        }
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
