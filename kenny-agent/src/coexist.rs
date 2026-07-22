//! Anti-cheat coexistence (ADR-0039).
//!
//! kenny does none of the "hard" cheat behaviours — it never reads another process's
//! memory, injects, hooks, or simulates input. But a kernel anti-cheat (e.g. Easy
//! Anti-Cheat) flags software *heuristically*, and a few of kenny's legitimate actions
//! look cheat-shaped: a full-screen `screen_capture` of a protected game, and the
//! periodic whole-machine process/port enumeration.
//!
//! So while a protected game is running, kenny **voluntarily steps back**: it suspends
//! `screen_capture` (reporting the `paused` error code) and relaxes the process/port
//! telemetry (those sections report a "paused" summary and the push cadence stretches).
//!
//! This is legitimacy, never evasion. The back-off is **symmetric and transparent** —
//! kenny genuinely stops the visible action and reports it; it never hides, renames, or
//! disguises itself. Trying to evade a kernel anti-cheat would risk banning the player's
//! game account, which is exactly what this module exists to avoid.
//!
//! Detection reads only the process *name list* (the cheapest `sysinfo` refresh), so it
//! opens no rights-bearing handle against the protected process.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{OnceLock, RwLock};
use std::time::Duration;

use crate::protocol::ErrorCode;

/// Default watchlist: the anti-cheat **processes** themselves, present exactly while a
/// protected title is being protected. Matched case-insensitively and `.exe`-insensitively
/// against the running process image names. Operators extend this with game exes via
/// `KENNY_COEXIST_PROCESSES`.
const COEXIST_DEFAULT: &[&str] = &[
    "EasyAntiCheat.exe",
    "EasyAntiCheat_EOS.exe",
    "BEService.exe",
    "BEService_x64.exe",
    "BEServer.exe",
];

/// Enable/disable the whole feature (default on).
const ENV_ENABLED: &str = "KENNY_COEXIST_ENABLED";
/// Comma-separated extra process names to watch (extends [`COEXIST_DEFAULT`]).
const ENV_PROCESSES: &str = "KENNY_COEXIST_PROCESSES";
/// How often to poll the process list, in seconds (default [`DEFAULT_POLL_SECS`]).
const ENV_POLL_SECS: &str = "KENNY_COEXIST_POLL_SECS";
/// Stretched telemetry push interval while a game is active, in seconds
/// (default [`DEFAULT_TELEMETRY_SECS`]).
const ENV_TELEMETRY_SECS: &str = "KENNY_COEXIST_TELEMETRY_INTERVAL_SECS";

const DEFAULT_POLL_SECS: u64 = 5;
const DEFAULT_TELEMETRY_SECS: u64 = 3600;

/// Is a watched process currently running? Read by the dispatch gate, the telemetry
/// scheduler, and the process/port collectors — all in the service process. Updated by
/// the poll task.
static GAME_ACTIVE: AtomicBool = AtomicBool::new(false);

/// Image name of the process that most recently matched, for human-readable messages
/// and the "paused" telemetry summary.
static ACTIVE_REASON: RwLock<Option<String>> = RwLock::new(None);

/// Resolved, process-global configuration (read once from the environment).
struct Settings {
    enabled: bool,
    /// Normalized (lowercased, `.exe`-stripped) watchlist entries.
    processes: Vec<String>,
    poll: Duration,
    telemetry: Duration,
}

impl Settings {
    fn from_env() -> Settings {
        let processes = build_watchlist(std::env::var(ENV_PROCESSES).ok().as_deref());
        Settings {
            enabled: env_bool(ENV_ENABLED, true),
            processes,
            poll: Duration::from_secs(env_u64(ENV_POLL_SECS, DEFAULT_POLL_SECS).max(1)),
            telemetry: Duration::from_secs(
                env_u64(ENV_TELEMETRY_SECS, DEFAULT_TELEMETRY_SECS).max(1),
            ),
        }
    }
}

fn settings() -> &'static Settings {
    static S: OnceLock<Settings> = OnceLock::new();
    S.get_or_init(Settings::from_env)
}

/// Lowercase and strip a trailing `.exe` so `EasyAntiCheat.exe`, `easyanticheat`, and
/// `EASYANTICHEAT.EXE` all compare equal (sysinfo may report the name with or without
/// the extension across platforms).
fn normalize(name: &str) -> String {
    let n = name.trim().to_ascii_lowercase();
    n.strip_suffix(".exe").unwrap_or(&n).to_string()
}

/// Build the normalized watchlist from the compiled default plus an optional
/// comma-separated operator extension. De-duplicated; empty entries dropped.
fn build_watchlist(extra: Option<&str>) -> Vec<String> {
    let mut processes: Vec<String> = COEXIST_DEFAULT.iter().map(|s| normalize(s)).collect();
    if let Some(extra) = extra {
        for part in extra.split(',') {
            let norm = normalize(part);
            if !norm.is_empty() && !processes.contains(&norm) {
                processes.push(norm);
            }
        }
    }
    processes
}

/// First process name (in its original form) that matches the normalized watchlist.
fn matched_name<I, S>(names: I, watchlist: &[String]) -> Option<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    for name in names {
        let name = name.as_ref();
        if watchlist.contains(&normalize(name)) {
            return Some(name.to_string());
        }
    }
    None
}

fn env_bool(key: &str, default: bool) -> bool {
    match std::env::var(key) {
        Ok(v) => {
            let t = v.trim().to_ascii_lowercase();
            if t.is_empty() {
                default
            } else {
                !matches!(t.as_str(), "0" | "false" | "no" | "off")
            }
        }
        Err(_) => default,
    }
}

fn env_u64(key: &str, default: u64) -> u64 {
    std::env::var(key)
        .ok()
        .and_then(|v| v.trim().parse::<u64>().ok())
        .unwrap_or(default)
}

/// Whether the coexistence feature is enabled at all.
pub fn enabled() -> bool {
    settings().enabled
}

/// Poll cadence for the background watcher.
pub fn poll_interval() -> Duration {
    settings().poll
}

/// True when the feature is enabled AND a watched (anti-cheat) process is running.
pub fn game_active() -> bool {
    settings().enabled && GAME_ACTIVE.load(Ordering::Relaxed)
}

/// The image name of the process that triggered the current pause, if any.
pub fn active_reason() -> Option<String> {
    ACTIVE_REASON.read().ok().and_then(|g| g.clone())
}

/// Human-readable summary for a telemetry section that is paused during a game.
pub fn paused_summary() -> String {
    match active_reason() {
        Some(r) => format!("paused while a protected game is running ({r})"),
        None => "paused while a protected game is running".to_string(),
    }
}

/// The delay before the next telemetry push. While a game is active the interval is
/// stretched (never shortened) so the periodic process/port enumeration backs off.
pub fn telemetry_delay(base: Duration) -> Duration {
    if game_active() {
        stretched_delay(base, settings().telemetry)
    } else {
        base
    }
}

fn stretched_delay(base: Duration, stretched: Duration) -> Duration {
    base.max(stretched)
}

/// Tools that are hard-suspended while a protected game runs. Only the single strongest
/// anti-cheat signal (`screen_capture`, a full-screen grab of the game surface) is
/// blocked outright; process enumeration is relaxed in the telemetry path instead, so
/// the rare on-demand `diag_processes` diagnostic still works.
fn is_suspended(tool: &str) -> bool {
    matches!(tool, "screen_capture")
}

/// Gate a tool call: refuse a suspended tool with `paused` while a game is active.
/// Returns `Ok(())` otherwise. Consulted by `dispatch::run` after the kill-switch and
/// safety-guard checks.
pub fn gate(tool: &str) -> Result<(), (ErrorCode, String)> {
    if is_suspended(tool) && game_active() {
        let reason = active_reason().unwrap_or_else(|| "a protected game".to_string());
        return Err((
            ErrorCode::Paused,
            format!(
                "{tool} is paused while a protected game is running ({reason}); \
                 kenny steps back so it is not mistaken for cheating software"
            ),
        ));
    }
    Ok(())
}

/// Refresh the process list (name only — opens no handle against the game) and update
/// the global active flag. Called on a timer by the background poll task.
pub fn poll_once(sys: &mut sysinfo::System) {
    use sysinfo::{ProcessRefreshKind, ProcessesToUpdate};

    let s = settings();
    if !s.enabled {
        set_state(false, None);
        return;
    }
    sys.refresh_processes_specifics(ProcessesToUpdate::All, true, ProcessRefreshKind::nothing());
    let matched = matched_name(
        sys.processes().values().map(|p| p.name().to_string_lossy()),
        &s.processes,
    );
    set_state(matched.is_some(), matched);
}

/// Store the active flag + reason, logging the transition so the operator can see kenny
/// stepping back and resuming (transparency, not stealth).
fn set_state(active: bool, reason: Option<String>) {
    let was = GAME_ACTIVE.swap(active, Ordering::Relaxed);
    if active != was {
        if active {
            tracing::info!(
                process = reason.as_deref().unwrap_or("unknown"),
                "protected game detected; pausing screen capture and process telemetry (anti-cheat coexistence)"
            );
        } else {
            tracing::info!("protected game exited; resuming normal behaviour");
        }
    }
    if let Ok(mut g) = ACTIVE_REASON.write() {
        *g = reason;
    }
}

/// Test-only: force the global active state so `dispatch`/collector tests can exercise
/// the paused paths without a real anti-cheat process. Guard callers with
/// `control::TEST_ENV_LOCK` to serialize access to the global.
#[cfg(test)]
pub(crate) fn force_active_for_test(active: bool, reason: Option<&str>) {
    set_state(active, reason.map(String::from));
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalize_lowercases_and_strips_exe() {
        assert_eq!(normalize("EasyAntiCheat.exe"), "easyanticheat");
        assert_eq!(normalize("  BEService_x64.EXE "), "beservice_x64");
        assert_eq!(normalize("EasyAntiCheat"), "easyanticheat");
    }

    #[test]
    fn build_watchlist_adds_extends_and_dedupes() {
        let wl = build_watchlist(Some("ARC-Raiders.exe, easyanticheat.exe ,, "));
        // Operator game exe is added (normalized).
        assert!(wl.contains(&"arc-raiders".to_string()));
        // Already-present default is not duplicated.
        assert_eq!(
            wl.iter().filter(|w| w.as_str() == "easyanticheat").count(),
            1
        );
        // Empty entries dropped.
        assert!(!wl.iter().any(|w| w.is_empty()));
        // Defaults always present.
        assert!(wl.contains(&"beservice".to_string()));
    }

    #[test]
    fn matched_name_is_case_and_exe_insensitive() {
        let wl = build_watchlist(None);
        assert_eq!(
            matched_name(["notepad.exe", "EASYANTICHEAT.EXE"], &wl).as_deref(),
            Some("EASYANTICHEAT.EXE")
        );
        // sysinfo sometimes reports the bare name without the extension.
        assert_eq!(
            matched_name(["BEService"], &wl).as_deref(),
            Some("BEService")
        );
        assert!(matched_name(["chrome.exe", "explorer.exe"], &wl).is_none());
    }

    #[test]
    fn stretched_delay_never_shortens() {
        assert_eq!(
            stretched_delay(Duration::from_secs(900), Duration::from_secs(3600)),
            Duration::from_secs(3600)
        );
        // Never poll more often than the configured base interval.
        assert_eq!(
            stretched_delay(Duration::from_secs(7200), Duration::from_secs(3600)),
            Duration::from_secs(7200)
        );
    }

    #[test]
    fn gate_pauses_only_screen_capture_and_only_when_active() {
        let _g = crate::control::TEST_ENV_LOCK.lock().unwrap();
        force_active_for_test(true, Some("EasyAntiCheat.exe"));
        // The one hard-suspended tool is refused with `paused`.
        let err = gate("screen_capture").unwrap_err();
        assert_eq!(err.0, ErrorCode::Paused);
        assert!(err.1.contains("EasyAntiCheat.exe"));
        // Other tools (including diag_processes) are unaffected.
        assert!(gate("diag_processes").is_ok());
        assert!(gate("powershell_exec").is_ok());
        assert!(gate("shell_exec").is_ok());
        // When no game is active, nothing is suspended.
        force_active_for_test(false, None);
        assert!(gate("screen_capture").is_ok());
    }
}
