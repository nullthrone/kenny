//! Portable bounded process runner for telemetry collectors.
//!
//! Telemetry collection is synchronous (`fn collect() -> Section`), so a probe runs a
//! child process through the blocking [`std::process::Command`] and reads its stdout.
//! A single hung probe (a wedged CIM/WMI query, a `journalctl` stuck on a corrupt
//! journal, an `nvidia-smi` waiting on a sick driver) must not stall the whole snapshot,
//! so every run is held to a wall-clock budget: past it the child is killed and the
//! caller gets [`ProbeFailure::Timeout`].
//!
//! OS-independent on purpose: `winps` builds its PowerShell wrappers on top of it, and
//! the Linux probes (`journalctl`, `smartctl`) and `nvidia-smi` use it directly.

use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use super::ProbeFailure;

/// Per-probe wall-clock budget. A telemetry probe that has not finished within this
/// window is killed and treated as "no data" (the collector then falls back to its
/// default), so one wedged probe can never stall the whole snapshot.
pub const PROBE_BUDGET: Duration = Duration::from_secs(20);

/// How often a running child is polled for completion.
const POLL_INTERVAL: Duration = Duration::from_millis(10);

/// What a child that ran to completion left behind.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcOutput {
    /// The child exited with code 0.
    pub success: bool,
    /// The exit code (`None`: killed by a signal).
    pub code: Option<i32>,
    /// Everything the child wrote to stdout, decoded losslessly (see [`run_command`]).
    pub stdout: String,
}

/// Run `program args…` within `budget` and capture its stdout.
///
/// `Ok` whenever the child ran to completion, whatever its exit code — the caller
/// decides whether a non-zero exit is fatal (some tools print a usable status and
/// still exit non-zero). `Err` means there is no output to reason about: the process
/// could not be started ([`ProbeFailure::Spawn`], including a missing binary), or it
/// was still running when the budget ran out and was killed ([`ProbeFailure::Timeout`]).
#[cfg_attr(windows, allow(dead_code))]
pub fn run(program: &str, args: &[&str], budget: Duration) -> Result<ProcOutput, ProbeFailure> {
    let mut cmd = Command::new(program);
    cmd.args(args);
    run_command(cmd, budget)
}

/// [`run`], returning stdout only when the child exits 0. A spawn failure, a timeout or
/// a non-zero exit all read as `None` (stdout discarded).
pub fn run_ok(program: &str, args: &[&str], budget: Duration) -> Option<String> {
    match run(program, args, budget) {
        Ok(out) if out.success => Some(out.stdout),
        _ => None,
    }
}

/// [`run`] for a prepared [`Command`] (environment, working directory).
///
/// stdin is null and stderr is discarded — only stdout is consumed, which also rules
/// out a stderr-pipe-buffer deadlock. Stdout is drained on its own thread while the
/// child runs, so output larger than the pipe buffer (a journal dump) cannot block the
/// child until the budget kills it. Stdout is decoded with `from_utf8_lossy` rather
/// than `read_to_string`: a probe may emit bytes that are not valid UTF-8 (PowerShell
/// defaults to the console code page, and tools like `netsh`/`w32tm` use it too), and a
/// single stray byte from a vendor-supplied name once dropped the entire probe.
pub fn run_command(mut cmd: Command, budget: Duration) -> Result<ProcOutput, ProbeFailure> {
    let mut child = cmd
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| ProbeFailure::Spawn)?;

    let mut stdout = child.stdout.take().ok_or(ProbeFailure::Spawn)?;
    let (tx, rx) = mpsc::channel::<Vec<u8>>();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        // A read error keeps whatever arrived before it.
        let _ = stdout.read_to_end(&mut buf);
        let _ = tx.send(buf);
    });

    let deadline = Instant::now() + budget;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                // The pipe closes when the child exits — unless it left a grandchild
                // holding it open, in which case the budget bounds the wait.
                let remaining = deadline.saturating_duration_since(Instant::now());
                let buf = rx
                    .recv_timeout(remaining.max(POLL_INTERVAL))
                    .map_err(|_| ProbeFailure::Timeout(budget))?;
                return Ok(ProcOutput {
                    success: status.success(),
                    code: status.code(),
                    stdout: String::from_utf8_lossy(&buf).into_owned(),
                });
            }
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    // The reader thread ends on its own once the pipe closes.
                    return Err(ProbeFailure::Timeout(budget));
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(_) => return Err(ProbeFailure::Spawn),
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    const BUDGET: Duration = Duration::from_secs(10);

    #[test]
    fn run_captures_stdout_and_exit_status() {
        let out = run("sh", &["-c", "echo hi"], BUDGET).unwrap();
        assert!(out.success);
        assert_eq!(out.code, Some(0));
        assert_eq!(out.stdout, "hi\n");
    }

    #[test]
    fn run_reports_a_non_zero_exit_with_its_stdout() {
        let out = run("sh", &["-c", "echo partial; exit 3"], BUDGET).unwrap();
        assert!(!out.success);
        assert_eq!(out.code, Some(3));
        assert_eq!(out.stdout, "partial\n");
    }

    #[test]
    fn run_ok_gates_stdout_on_a_zero_exit() {
        assert_eq!(
            run_ok("sh", &["-c", "echo hi"], BUDGET).as_deref(),
            Some("hi\n")
        );
        assert_eq!(run_ok("sh", &["-c", "echo hi; exit 1"], BUDGET), None);
    }

    #[test]
    fn a_child_past_its_budget_is_killed() {
        let budget = Duration::from_millis(200);
        let started = Instant::now();
        let result = run("sleep", &["5"], budget);
        assert_eq!(result, Err(ProbeFailure::Timeout(budget)));
        assert!(started.elapsed() < Duration::from_secs(4));
        assert_eq!(run_ok("sleep", &["5"], budget), None);
    }

    #[test]
    fn a_missing_binary_is_a_spawn_failure() {
        let result = run("kenny-no-such-binary", &[], BUDGET);
        assert_eq!(result, Err(ProbeFailure::Spawn));
        assert_eq!(run_ok("kenny-no-such-binary", &[], BUDGET), None);
    }

    #[test]
    fn output_larger_than_the_pipe_buffer_does_not_stall_the_child() {
        // 1 MiB is far past the 64 KiB pipe buffer; an undrained pipe would block the
        // writer until the budget killed it.
        let out = run_ok(
            "sh",
            &["-c", "head -c 1048576 /dev/zero | tr '\\0' x"],
            BUDGET,
        )
        .expect("large output is read in full");
        assert_eq!(out.len(), 1_048_576);
    }

    #[test]
    fn invalid_utf8_is_decoded_lossily() {
        let out = run_ok("sh", &["-c", "printf 'a\\377b'"], BUDGET).unwrap();
        assert_eq!(out, "a\u{fffd}b");
    }
}
