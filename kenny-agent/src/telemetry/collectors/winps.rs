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
///
/// Success-gated: returns `None` on a non-zero exit, discarding any stdout.
pub fn run_command(program: &str, args: &[&str]) -> Option<String> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    run_with_budget(cmd)
}

/// Run a program and return its stdout **regardless of exit code**, as long as it
/// produced non-empty output.
///
/// Some tools (notably `w32tm /query /status`) print a perfectly usable status to
/// stdout while still exiting non-zero — e.g. when the Windows Time service is not
/// currently up. The success-gated [`run_command`] throws that stdout away, so the
/// caller can never see the real status. This variant mirrors the defensive
/// "inspect the output, don't trust the exit code" pattern used by
/// `handlers::diagnostics::run_envelope`: it hands back whatever was printed and lets
/// the caller decide whether it is usable. Returns `None` only on spawn failure,
/// timeout, or genuinely empty output.
pub fn run_command_output(program: &str, args: &[&str]) -> Option<String> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    let (_success, out) = run_capturing(cmd)?;
    let trimmed = out.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(out)
    }
}

fn run_raw(script: &str) -> Option<String> {
    // `-NonInteractive`/`-NoProfile` keep this fast and deterministic; the
    // ExecutionPolicy bypass avoids inheriting a locked-down machine policy.
    let mut cmd = Command::new("powershell.exe");
    cmd.args([
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]);
    run_with_budget(cmd)
}

/// Spawn `cmd` and return its stdout only when it exits successfully, but never block
/// longer than [`PROBE_BUDGET`]. Non-zero exit → `None` (stdout discarded).
///
/// This is the success-gated wrapper most collectors want. Callers that need the
/// output even on a non-zero exit use [`run_capturing`] directly (via
/// [`run_command_output`]).
fn run_with_budget(cmd: Command) -> Option<String> {
    match run_capturing(cmd)? {
        (true, out) => Some(out),
        (false, _) => None,
    }
}

/// Spawn `cmd` and capture its stdout with a `(success, stdout)` result, never
/// blocking longer than [`PROBE_BUDGET`].
///
/// A single hung probe (a wedged CIM/WMI query, a service stuck starting) must not
/// stall the whole telemetry snapshot — collectors run on a bounded pool, but a
/// child with no timeout would still pin one worker indefinitely. On timeout the
/// child is killed and `None` returned, so the collector falls back to its default.
/// `stderr` is discarded (we only consume stdout JSON), which also rules out a
/// stderr-pipe-buffer deadlock; telemetry stdout is small, so reading it after the
/// child exits cannot block.
///
/// `Some((success, stdout))` is returned whenever the child ran to completion, so the
/// exit-code decision is left to the caller. `None` means spawn failure, timeout, or
/// an unreadable pipe — cases where there is no output to reason about at all.
fn run_capturing(mut cmd: Command) -> Option<(bool, String)> {
    use std::io::Read;
    use std::process::Stdio;
    use std::time::Instant;

    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    let deadline = Instant::now() + PROBE_BUDGET;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let mut buf = String::new();
                child.stdout.take()?.read_to_string(&mut buf).ok()?;
                return Some((status.success(), buf));
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return None,
        }
    }
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

/// Per-probe wall-clock budget. A telemetry probe that has not finished within this
/// window is killed and treated as "no data" (the collector then falls back to its
/// default), so one wedged CIM/PowerShell call can never stall the whole snapshot.
pub const PROBE_BUDGET: Duration = Duration::from_secs(20);
