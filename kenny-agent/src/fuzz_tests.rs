//! Randomized fuzz-style tests for the wire-facing `dispatch::handle` entry point.
//!
//! `request.args` arrives off the wire as an untyped `serde_json::Value` (see
//! `protocol::Request`) — a malicious or buggy server can put anything there. This
//! feeds `dispatch::handle` adversarial `(tool, args)` pairs and asserts the call
//! never panics, only ever returning an `ok`/`err` response.
//!
//! Mutating tools (see `control::is_mutating`) are exercised with the local kill
//! switch explicitly OFF, so the fuzzer-generated args never reach a handler that
//! would actually run a shell command, touch an account, or change network config —
//! they only exercise the `Disabled` short-circuit in `dispatch::run`. Switching it
//! off takes a control file that says so: remote control ships **on** and a missing
//! file reads as enabled (ADR-0011), so an unset `KENNY_CONTROL_FILE` is the *most*
//! permissive state, not the safest one. [`fuzz_dispatch_never_panics`] asserts the
//! switch really is off before it fuzzes anything, because nothing else in the test
//! would notice if it were not.
//!
//! Tools are split by whether their handler actually reads `args`:
//! - [`RANDOM_LOOP_TOOLS`] either deserialize `args` into something handler-specific
//!   (`fs_*`) or are mutating and gated off before a handler ever sees `args` —
//!   cheap either way, so these run thousands of times with fresh random args each
//!   iteration. `fs_cleanup_scan` is read-only, so the kill switch does not stop it;
//!   its `target` is rewritten whenever it would name a real catalog entry, so the
//!   loop only ever reaches its `bad_args` path and never walks a real root.
//! - [`SMOKE_ONCE_TOOLS`] are non-mutating handlers whose top-level signature is
//!   `_args: Value` (Windows-only diagnostics/network/remotehelp/webfilter status
//!   reads): they ignore `args` completely, so randomizing it thousands of times adds
//!   no coverage, while several of them do a real OS/WMI/subprocess call on Windows
//!   that is too slow to repeat thousands of times in CI. Each is called exactly once,
//!   which is all the args-blind routing path needs. Every one of them reaches the OS
//!   through a probe that kills its child on timeout, so a wedged host costs this test
//!   a bounded wait and never a stuck one. `screen_capture` is deliberately not
//!   dispatched for real here at all (not even once) — like the existing
//!   `dispatch::tests::screen_capture_paused_while_protected_game_runs`, which only
//!   exercises it behind the coexist gate, its real Windows capture path depends on
//!   an interactive session/IPC pipe that a CI runner may not have, so it belongs in
//!   the dedicated integration job, not a unit-test fuzz loop.
//! - `telemetry_collect` belongs in neither list, and is never dispatched here with
//!   random args. Its handler reads `args` only to build a section filter, and
//!   anything that is not an array of strings under `sections` selects *every*
//!   section — a full snapshot, which on Windows is ~35 PowerShell/CIM probes and on
//!   Linux is the same number of `n/a` stubs. Random args land on that case the vast
//!   majority of the time, so a random loop over this tool costs a real snapshot per
//!   iteration for no coverage the two split-out paths do not already give: the
//!   args-reading half is fuzzed as the pure [`dispatch::wanted_sections`] in
//!   [`fuzz_telemetry_section_filter_never_panics`], and the routing half is
//!   dispatched for real in [`fuzz_dispatch_never_panics`] behind an explicit
//!   section filter, so at most one cheap collector ever runs.

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
    "fs_cleanup_scan",
    "fs_cleanup",
    "service_restart",
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
    "agent_update",
    "",
    "not_a_real_tool",
    "🔥unicode_tool🔥",
];

/// `winget_list` is deliberately absent, for the same reason `screen_capture` is: its
/// handler is args-blind, so dispatching it proves only that the tool name routes,
/// and `winget list` is not a call a unit test can make. It refreshes its sources over
/// the network before answering, and on a host where that cannot complete — a CI
/// runner among them — it does not return at all. The handler bounds and kills it, so
/// the tool is safe to call; spending that bound here buys nothing. `winget`'s real
/// behaviour belongs to the integration job, which runs against a real desktop.
const SMOKE_ONCE_TOOLS: &[&str] = &[
    "diag_processes",
    "diag_services",
    "diag_eventlog",
    "diag_autostart",
    "net_config",
    "remotehelp_status",
    "webfilter_status",
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
        return defang_cleanup_target(random_value(rng, 0));
    }
    let known_keys: &[&str] = match tool {
        "fs_list" | "fs_read" => &["path"],
        "fs_search" => &["root", "pattern"],
        "powershell_exec" => &["script"],
        "shell_exec" => &["command"],
        "winget_install" | "winget_uninstall" | "winget_update" => &["id", "package_id"],
        "net_adapter_reset" | "service_restart" => &["name"],
        "fs_cleanup_scan" | "fs_cleanup" => &["target"],
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
    defang_cleanup_target(Value::Object(map))
}

/// Every name in either cleanup catalog.
fn cleanup_target_names() -> Vec<&'static str> {
    use crate::handlers::cleanup::{LINUX_CATALOG, WINDOWS_CATALOG};
    WINDOWS_CATALOG
        .iter()
        .chain(LINUX_CATALOG)
        .map(|e| e.target)
        .collect()
}

/// `fs_cleanup_scan` is read-only and so is not stopped by the kill switch: a random
/// `target` that happened to name a catalog entry would scan a real root of this
/// machine. Rewrite such a value so the fuzz loop only ever reaches `bad_args`.
fn defang_cleanup_target(mut args: Value) -> Value {
    if let Some(t) = args.get_mut("target") {
        if t.as_str()
            .is_some_and(|s| cleanup_target_names().contains(&s))
        {
            *t = Value::String(format!("fuzz-{}", t.as_str().unwrap_or_default()));
        }
    }
    args
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
    // Turn the local kill switch off for the duration, so every mutating tool below
    // stops at the `Disabled` short-circuit instead of reaching its handler. This
    // needs a control file that says so — leaving `KENNY_CONTROL_FILE` unset points
    // at the machine's real state path, and a missing file there reads as *enabled*.
    let control = std::env::temp_dir().join("kenny-fuzz-dispatch.control.json");
    std::env::set_var(crate::control::CONTROL_FILE_ENV, &control);
    crate::control::set_remote_control_enabled(false).expect("write the off state");

    // Prove it before fuzzing: dispatch one mutating tool and require `Disabled`.
    // Every "this never reaches the OS" claim in this module rests on this gate, and
    // a fuzz loop cannot tell a refused call from a call that quietly succeeded — so
    // if the gate is open, the right outcome is a failed test, not 8000 real ones.
    let gate = handle(Request {
        id: "fuzz-gate".to_string(),
        tool: "winget_install".to_string(),
        args: json!({"id": "kenny.fuzz.gate.probe"}),
    })
    .await;
    assert_eq!(
        gate.error.map(|e| e.code),
        Some(crate::protocol::ErrorCode::Disabled),
        "the kill switch is not off, so this fuzz loop would run mutating tools for real"
    );

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

    // `telemetry_collect` routes for real, but always behind an explicit section
    // filter (see module docs), so this covers the dispatch path without ever asking
    // for the unfiltered snapshot: one filter naming a single cheap portable
    // collector, one naming nothing that exists (which runs no collector at all).
    for sections in [json!(["memory"]), json!(["🔥not-a-section", "", "\u{0}"])] {
        let args = json!({ "sections": sections });
        if let Err(p) = dispatch_once("telemetry_collect", args).await {
            panics.push(p);
        }
    }

    std::env::remove_var(crate::control::CONTROL_FILE_ENV);
    let _ = std::fs::remove_file(&control);

    assert!(
        panics.is_empty(),
        "fuzzing found {} panicking (tool, args) pair(s): {:#?}",
        panics.len(),
        &panics[..panics.len().min(5)]
    );
}

/// `telemetry_collect`'s only args-reading code, driven with the same adversarial
/// args the dispatch loop uses.
///
/// The tool itself is kept out of that loop because random args select the full
/// snapshot (see module docs), so this pins what the loop would otherwise have
/// covered: the filter never panics on any shape of `args`, and it never invents a
/// section name — every name it returns was a string in the caller's own `sections`
/// array, so a hostile server cannot steer collection to anything it did not ask for
/// by name.
#[test]
fn fuzz_telemetry_section_filter_never_panics() {
    let mut rng = Rng(0x2545_F491_4F6C_DD1D);
    const ITERATIONS: usize = 8_000;
    let (mut named, mut unfiltered) = (0usize, 0usize);

    for _ in 0..ITERATIONS {
        let args = random_args(&mut rng, "telemetry_collect");
        let wanted = crate::dispatch::wanted_sections(&args);
        let offered: Vec<&str> = args
            .get("sections")
            .and_then(|s| s.as_array())
            .map(|arr| arr.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        for name in &wanted {
            assert!(
                offered.contains(&name.as_str()),
                "filter returned {name:?}, which was not in the args: {args:#?}"
            );
        }
        if wanted.is_empty() {
            unfiltered += 1;
        } else {
            named += 1;
        }
    }

    // Both branches must actually be reached, or this test passes without having
    // exercised the one that matters. `named` is the path that reads strings out of
    // the array; `unfiltered` is the path that asks for every section — the reason
    // the tool is not in the dispatch loop in the first place.
    assert!(
        named > 0 && unfiltered > 0,
        "{named} named, {unfiltered} unfiltered — the generator no longer reaches both branches"
    );
}

/// The fuzz generator never forms a valid cleanup target, so the dispatch loop can
/// never make `fs_cleanup_scan` walk (or, with the kill switch failing open,
/// `fs_cleanup` delete under) a real root of the machine running the tests.
#[test]
fn fuzz_args_never_name_a_cleanup_target() {
    let names = cleanup_target_names();
    for s in NASTY_STRINGS {
        assert!(!names.contains(s), "{s:?} is a real cleanup target");
    }
    let mut rng = Rng(0x9E37_79B9_7F4A_7C15);
    for _ in 0..20_000 {
        for tool in ["fs_cleanup_scan", "fs_cleanup"] {
            let args = random_args(&mut rng, tool);
            let res = crate::handlers::cleanup::lookup(
                crate::handlers::cleanup::CatalogOs::native(),
                &args,
            );
            assert!(res.is_err(), "fuzz args {args} name a real cleanup target");
        }
    }
    // The defang itself rewrites a real name.
    for name in names {
        let args = defang_cleanup_target(json!({ "target": name }));
        assert_ne!(args["target"], name);
    }
}
