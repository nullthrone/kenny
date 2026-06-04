//! Shared Windows PowerShell helper for telemetry collectors.
//!
//! Telemetry collection is synchronous (`fn collect() -> Section`), so this runs
//! PowerShell via the blocking [`std::process::Command`] and parses the captured
//! stdout as JSON. Keeping it here keeps the 18 Windows collectors DRY.
//!
//! Windows-only: gated `#[cfg(windows)]` so it never compiles into the portable
//! (Linux/macOS) build.

use std::process::Command;
use std::time::Duration;

use serde_json::Value;

/// Run a PowerShell snippet and parse its stdout as JSON.
///
/// The script is responsible for emitting JSON (typically by piping to
/// `ConvertTo-Json -Compress`). A scalar/array/object are all accepted. Returns
/// `None` on spawn failure, a non-zero exit, empty output, or invalid JSON — the
/// caller then falls back to a safe default so a single flaky probe never breaks
/// the whole telemetry push.
pub fn run_json(script: &str) -> Option<Value> {
    let out = run_raw(script)?;
    let trimmed = out.trim();
    if trimmed.is_empty() {
        return None;
    }
    serde_json::from_str(trimmed).ok()
}

/// Run a PowerShell snippet and return its raw stdout (for non-JSON tools such as
/// `netsh` or `w32tm`). Returns `None` on spawn failure or non-zero exit.
pub fn run_text(script: &str) -> Option<String> {
    run_raw(script)
}

/// Run a program with raw arguments (e.g. `netsh`, `w32tm`) and return stdout.
pub fn run_command(program: &str, args: &[&str]) -> Option<String> {
    let output = Command::new(program).args(args).output().ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn run_raw(script: &str) -> Option<String> {
    // `-NonInteractive`/`-NoProfile` keep this fast and deterministic; the
    // ExecutionPolicy bypass avoids inheriting a locked-down machine policy.
    let output = Command::new("powershell.exe")
        .args([
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&output.stdout).into_owned())
}

/// Normalize a value that PowerShell's `ConvertTo-Json` may emit either as a
/// single object (one row) or an array (many rows) into a `Vec<Value>`.
///
/// `ConvertTo-Json` collapses a one-element collection to a bare object, so
/// collectors that expect a list must accept both shapes.
pub fn as_array(value: Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items,
        Value::Null => Vec::new(),
        other => vec![other],
    }
}

/// Best-effort: a tiny sleep budget marker kept for future timeout plumbing.
///
/// Not currently used to bound the child (PowerShell startup dominates), but
/// documents the intent that probes should stay short.
#[allow(dead_code)]
pub const PROBE_BUDGET: Duration = Duration::from_secs(20);
