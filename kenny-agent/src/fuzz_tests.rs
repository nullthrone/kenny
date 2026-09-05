//! Randomized fuzz-style tests for the wire-facing `dispatch::handle` entry point.
//!
//! `request.args` arrives off the wire as an untyped `serde_json::Value` (see
//! `protocol::Request`) — a malicious or buggy server can put anything there. This
//! feeds `dispatch::handle` adversarial `(tool, args)` pairs and asserts the call
//! never panics, only ever returning an `ok`/`err` response.
//!
//! Mutating tools (see `control::is_mutating`) are exercised with remote control at
//! its default OFF state, so the fuzzer-generated args never reach a handler that
//! would actually run a shell command, touch an account, or change network config —
//! they only exercise the `Disabled` short-circuit in `dispatch::run`.
//!
//! Tools are split by whether their handler actually reads `args`:
//! - [`RANDOM_LOOP_TOOLS`] either deserialize `args` into something handler-specific
//!   (`fs_*`, `telemetry_collect`) or are mutating and gated off before a handler
//!   ever sees `args` — cheap either way, so these run thousands of times with fresh
//!   random args each iteration.
//! - [`SMOKE_ONCE_TOOLS`] are non-mutating handlers whose top-level signature is
//!   `_args: Value` (Windows-only diagnostics/network/remotehelp/webfilter status
//!   reads, `winget_list`): they ignore `args` completely, so randomizing it
//!   thousands of times adds no coverage, while several of them do a real OS/WMI/
//!   subprocess call on Windows (e.g. `winget_list` shells out to `winget`) that is
//!   too slow to repeat thousands of times in CI. Each is called exactly once, which
//!   is all the args-blind routing path needs. `screen_capture` is deliberately not
//!   dispatched for real here at all (not even once) — like the existing
//!   `dispatch::tests::screen_capture_paused_while_protected_game_runs`, which only
//!   exercises it behind the coexist gate, its real Windows capture path depends on
//!   an interactive session/IPC pipe that a CI runner may not have, so it belongs in
//!   the dedicated integration job, not a unit-test fuzz loop.

use serde_json::{json, Map, Value};

use crate::dispatch::handle;
use crate::protocol::Request;

const RANDOM_LOOP_TOOLS: &[&str] = &[
    "powershell_exec",
    "shell_exec",
    "fs_list",
    "fs_search",
    "fs_read",
    "fs_disk_usage",
    "winget_install",
    "winget_uninstall",
    "winget_update",
    "net_dns_flush",
    "net_adapter_reset",
    "remotehelp_start",
    "remotehelp_stop",
    "webfilter_apply",
    "webfilter_clear",
    "account_set_enabled",
    "account_set_admin",
    "account_set_logon_rights",
    "account_create",
    "account_delete",
    "account_session_action",
    "password_policy_set",
    "telemetry_collect",
    "agent_update",
    "",
    "not_a_real_tool",
    "🔥unicode_tool🔥",
];

const SMOKE_ONCE_TOOLS: &[&str] = &[
    "diag_processes",
    "diag_services",
    "diag_eventlog",
    "diag_autostart",
    "net_config",
    "remotehelp_status",
    "webfilter_status",
    "winget_list",
];

/// Tiny dependency-free xorshift64* PRNG. Fixed seed so a failure is reproducible.
struct Rng(u64);

impl Rng {
    fn next_u64(&mut self) -> u64 {
        let mut x = self.0;
        x ^= x << 13;
        x ^= x >> 7;
        x ^= x << 17;
        self.0 = x;
        x.wrapping_mul(0x2545_F491_4F6C_DD1D)
    }

    fn range(&mut self, n: usize) -> usize {
        if n == 0 {
            0
        } else {
            (self.next_u64() as usize) % n
        }
    }

    fn bool(&mut self) -> bool {
        self.next_u64() & 1 == 0
    }
}

const NASTY_STRINGS: &[&str] = &[
    "",
    "\0",
    "../../../../etc/passwd",
    "C:\\Windows\\System32",
    "%s%s%s%n",
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "🔥💀🚀 unicode party 你好 مرحبا",
    "\u{0}\u{1}\u{2}",
    "-1",
    "99999999999999999999999999",
    "NaN",
    "Infinity",
    "true",
    "null",
    "[]",
    "{}",
    "\"",
    "\\",
    "\n\r\t",
    "a\u{301}\u{301}\u{301}",
    "🇦🇧",
];

fn random_string(rng: &mut Rng) -> String {
    if rng.bool() {
        NASTY_STRINGS[rng.range(NASTY_STRINGS.len())].to_string()
    } else {
        let len = rng.range(40);
        (0..len)
            .map(|_| (32u8 + (rng.next_u64() % 95) as u8) as char)
            .collect()
    }
}

fn random_number(rng: &mut Rng) -> Value {
    match rng.range(6) {
        0 => json!(0),
        1 => json!(-1),
        2 => json!(i64::MIN),
        3 => json!(i64::MAX),
        4 => json!(u64::MAX),
        _ => json!(rng.next_u64() as f64 / 3.0),
    }
}

fn random_value(rng: &mut Rng, depth: u32) -> Value {
    if depth >= 4 {
        return match rng.range(3) {
            0 => Value::Null,
            1 => json!(rng.bool()),
            _ => Value::String(random_string(rng)),
        };
    }
    match rng.range(7) {
        0 => Value::Null,
        1 => json!(rng.bool()),
        2 => random_number(rng),
        3 => Value::String(random_string(rng)),
        4 => {
            let n = rng.range(5);
            Value::Array((0..n).map(|_| random_value(rng, depth + 1)).collect())
        }
        _ => {
            let n = rng.range(5);
            let mut map = Map::new();
            for _ in 0..n {
                map.insert(random_string(rng), random_value(rng, depth + 1));
            }
            Value::Object(map)
        }
    }
}

/// Args biased toward the field names real tools expect, but with random/garbage
/// values — more likely to reach past `serde_json::from_value` into a handler's own
/// logic than pure noise, while pure noise (the `rng.range(3) == 0` branch) still
/// covers total type confusion (e.g. args that is a bare string or array).
fn random_args(rng: &mut Rng, tool: &str) -> Value {
    if rng.range(3) == 0 {
        return random_value(rng, 0);
    }
    let known_keys: &[&str] = match tool {
        "fs_list" | "fs_read" => &["path"],
        "fs_search" => &["root", "pattern"],
        "powershell_exec" => &["script"],
        "shell_exec" => &["command"],
        "winget_install" | "winget_uninstall" | "winget_update" => &["id", "package_id"],
        "net_adapter_reset" => &["name"],
        "webfilter_apply" => &["domains", "doh_policy", "list_hash"],
        "account_set_enabled" | "account_set_admin" | "account_delete" => {
            &["username", "enabled", "admin"]
        }
        "account_set_logon_rights" => &["username", "deny_rights"],
        "account_create" => &["username", "password", "admin"],
        "account_session_action" => &["username", "action"],
        "password_policy_set" => &["min_length", "max_age_days"],
        "telemetry_collect" => &["sections"],
        _ => &[],
    };
    let mut map = Map::new();
    for key in known_keys {
        if rng.bool() {
            map.insert((*key).to_string(), random_value(rng, 1));
        }
    }
    if rng.bool() {
        map.insert(random_string(rng), random_value(rng, 1));
    }
    Value::Object(map)
}

async fn dispatch_once(tool: &str, args: Value) -> Result<(), (String, Value)> {
    let req = Request {
        id: "fuzz".to_string(),
        tool: tool.to_string(),
        args: args.clone(),
    };
    let task = tokio::spawn(async move { handle(req).await });
    match task.await {
        Ok(_resp) => Ok(()),
        Err(e) if e.is_panic() => Err((tool.to_string(), args)),
        Err(_) => Ok(()), // cancelled; not a panic
    }
}

#[allow(clippy::await_holding_lock)] // single-threaded test runtime; lock guards env
#[tokio::test]
async fn fuzz_dispatch_never_panics() {
    let _guard = crate::control::TEST_ENV_LOCK.lock().unwrap();
    // Default (unset) state is remote-control-disabled, so mutating tools
    // short-circuit before a handler ever sees the fuzzer-generated args.
    std::env::remove_var(crate::control::CONTROL_FILE_ENV);

    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    let mut panics: Vec<(String, Value)> = Vec::new();
    const ITERATIONS: usize = 8_000;

    for _ in 0..ITERATIONS {
        let tool = RANDOM_LOOP_TOOLS[rng.range(RANDOM_LOOP_TOOLS.len())];
        let args = random_args(&mut rng, tool);
        if let Err(p) = dispatch_once(tool, args).await {
            panics.push(p);
        }
    }

    // Args-blind handlers (see module docs): args are never read, so one call each
    // is all the routing path needs — no point repeating it thousands of times.
    for tool in SMOKE_ONCE_TOOLS {
        if let Err(p) = dispatch_once(tool, random_args(&mut rng, tool)).await {
            panics.push(p);
        }
    }

    assert!(
        panics.is_empty(),
        "fuzzing found {} panicking (tool, args) pair(s): {:#?}",
        panics.len(),
        &panics[..panics.len().min(5)]
    );
}
