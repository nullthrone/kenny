//! Diagnostic tools: `diag_processes`, `diag_services`, `diag_eventlog`,
//! `diag_autostart`.
//!
//! `diag_processes` is portable via `sysinfo`. Services/eventlog/autostart are
//! Windows-only; off Windows they return `unsupported`.

use std::time::Duration;

use serde::Deserialize;
use serde_json::{json, Value};
use sysinfo::{ProcessesToUpdate, System};

use crate::protocol::ErrorCode;
use crate::telemetry::collectors::ProbeFailure;

/// Wall-clock budget for one Windows diagnostic query (services, event log,
/// autostart). Longer than a telemetry probe's `winps::PROBE_BUDGET`: an operator
/// is waiting on this one call, and on a busy machine — a telemetry snapshot's
/// own CIM probes running at the same moment — `Win32_Service` alone can take
/// longer than a snapshot probe may. It stays below the server's forwarding
/// timeout for these tools (`kenny_server/tools.py`, `_TOOL_MIN_TIMEOUT_S`), so a
/// slow query ends in the agent's own `timeout` error rather than the server
/// giving up first; `tests/test_tool_timeouts.py` holds the two in order.
#[cfg_attr(not(windows), allow(dead_code))]
pub const DIAG_BUDGET: Duration = Duration::from_secs(50);

/// The error an interactive diagnostic reports when its query produced nothing.
#[cfg_attr(not(windows), allow(dead_code))]
fn probe_error(what: &str, failure: ProbeFailure) -> (ErrorCode, String) {
    match failure {
        ProbeFailure::Timeout(budget) => (
            ErrorCode::Timeout,
            format!("{what} query timed out after {}s", budget.as_secs()),
        ),
        ProbeFailure::Spawn => (
            ErrorCode::ExecFailed,
            format!("{what} query could not start PowerShell"),
        ),
        ProbeFailure::Exit(Some(code)) => (
            ErrorCode::ExecFailed,
            format!("{what} query exited with code {code}"),
        ),
        ProbeFailure::Exit(None) => (
            ErrorCode::ExecFailed,
            format!("{what} query was terminated"),
        ),
        ProbeFailure::Empty => (
            ErrorCode::ExecFailed,
            format!("{what} query produced no output"),
        ),
        ProbeFailure::Invalid => (
            ErrorCode::ExecFailed,
            format!("{what} query produced output that is not valid JSON"),
        ),
    }
}

/// `diag_processes` — running processes with cpu and memory.
pub fn processes(_args: Value) -> Result<Value, (ErrorCode, String)> {
    let mut sys = System::new();
    sys.refresh_processes(ProcessesToUpdate::All, true);
    let processes: Vec<Value> = sys
        .processes()
        .values()
        .map(|p| {
            json!({
                "pid": p.pid().as_u32(),
                "name": p.name().to_string_lossy(),
                "cpu": p.cpu_usage(),
                "mem_bytes": p.memory(),
            })
        })
        .collect();
    Ok(json!({ "processes": processes }))
}

#[derive(Debug, Deserialize)]
struct ServicesArgs {
    #[serde(default)]
    #[allow(dead_code)]
    filter: Option<String>,
}

/// `diag_services` — Windows service inventory.
pub fn services(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let _a: ServicesArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::services(_a.filter.as_deref())
    }
    #[cfg(not(windows))]
    {
        let _ = ServicesArgs { filter: None };
        Err(unsupported("diag_services"))
    }
}

#[derive(Debug, Deserialize)]
struct EventLogArgs {
    #[allow(dead_code)]
    log: String,
    #[allow(dead_code)]
    count: u32,
}

/// `diag_eventlog` — recent Windows Event Log entries.
pub fn eventlog(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        let a: EventLogArgs =
            serde_json::from_value(_args).map_err(|e| (ErrorCode::BadArgs, e.to_string()))?;
        windows_impl::eventlog(&a.log, a.count)
    }
    #[cfg(not(windows))]
    {
        let _ = EventLogArgs {
            log: String::new(),
            count: 0,
        };
        Err(unsupported("diag_eventlog"))
    }
}

/// `diag_autostart` — startup programs.
pub fn autostart(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::autostart()
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("diag_autostart"))
    }
}

#[cfg(not(windows))]
fn unsupported(tool: &str) -> (ErrorCode, String) {
    (
        ErrorCode::Unsupported,
        format!("{tool} is only available on Windows"),
    )
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Largest number of events we'll pull in one `diag_eventlog` call.
    const MAX_EVENTS: u32 = 1000;

    /// Escape a value for embedding inside a single-quoted PowerShell string
    /// literal: a literal `'` is written as `''`. Prevents a crafted `log`/`filter`
    /// from breaking out of the quoted argument and injecting script.
    fn ps_single_quote(s: &str) -> String {
        s.replace('\'', "''")
    }

    /// Run `script` (which must emit a `{ok, ...}` JSON envelope) within
    /// [`DIAG_BUDGET`] and return the parsed value, mapping an `ok:false` envelope
    /// or a query that produced nothing to an error that says which it was.
    fn run_envelope(script: &str, what: &str) -> Result<Value, (ErrorCode, String)> {
        let v = winps::run_json_within(script, DIAG_BUDGET)
            .map_err(|failure| probe_error(what, failure))?;
        if v.get("ok").and_then(Value::as_bool) != Some(true) {
            let msg = v
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("query failed")
                .to_string();
            return Err((ErrorCode::ExecFailed, msg));
        }
        Ok(v)
    }

    /// Real impl: `Get-CimInstance Win32_Service` into `{name, display, status, start}`.
    pub fn services(filter: Option<&str>) -> Result<Value, (ErrorCode, String)> {
        // Optional case-insensitive name/display filter, applied server-side in PS.
        let where_clause = match filter {
            Some(f) if !f.is_empty() => format!(
                "| Where-Object {{ $_.Name -like '*{0}*' -or $_.DisplayName -like '*{0}*' }} ",
                ps_single_quote(f)
            ),
            _ => String::new(),
        };
        let script = format!(
            r#"try {{
  $services = @(Get-CimInstance -ClassName Win32_Service -ErrorAction Stop {where_clause}|
    ForEach-Object {{
      [pscustomobject]@{{
        name    = [string]$_.Name
        display = [string]$_.DisplayName
        status  = [string]$_.State
        start   = [string]$_.StartMode
      }}
    }})
  [pscustomobject]@{{ ok = $true; services = $services }} | ConvertTo-Json -Depth 4 -Compress
}} catch {{
  [pscustomobject]@{{ ok = $false; error = [string]$_.Exception.Message }} | ConvertTo-Json -Compress
}}"#
        );
        let v = run_envelope(&script, "services")?;
        let services = winps::as_array(v.get("services").cloned().unwrap_or(Value::Null));
        Ok(json!({ "services": services }))
    }

    /// Real impl: `Get-WinEvent -LogName <log> -MaxEvents <count>` into
    /// `{time, level, source, message}`.
    pub fn eventlog(log: &str, count: u32) -> Result<Value, (ErrorCode, String)> {
        if count == 0 {
            return Err((ErrorCode::BadArgs, "count must be >= 1".to_string()));
        }
        let count = count.min(MAX_EVENTS);
        let script = format!(
            r#"try {{
  $events = @(Get-WinEvent -LogName '{log}' -MaxEvents {count} -ErrorAction Stop |
    ForEach-Object {{
      [pscustomobject]@{{
        time    = $_.TimeCreated.ToString('o')
        level   = [string]$_.LevelDisplayName
        source  = [string]$_.ProviderName
        message = [string]$_.Message
      }}
    }})
  [pscustomobject]@{{ ok = $true; events = $events }} | ConvertTo-Json -Depth 4 -Compress
}} catch {{
  [pscustomobject]@{{ ok = $false; error = [string]$_.Exception.Message }} | ConvertTo-Json -Compress
}}"#,
            log = ps_single_quote(log)
        );
        let v = run_envelope(&script, "eventlog")?;
        let events = winps::as_array(v.get("events").cloned().unwrap_or(Value::Null));
        Ok(json!({ "events": events }))
    }

    /// Real impl: `Win32_StartupCommand` into `{name, command, location}`.
    /// Covers HKLM/HKCU Run keys and the Startup folders in a single CIM call.
    pub fn autostart() -> Result<Value, (ErrorCode, String)> {
        let script = r#"try {
  $entries = @(Get-CimInstance -ClassName Win32_StartupCommand -ErrorAction Stop |
    ForEach-Object {
      [pscustomobject]@{
        name     = [string]$_.Name
        command  = [string]$_.Command
        location = [string]$_.Location
      }
    })
  [pscustomobject]@{ ok = $true; entries = $entries } | ConvertTo-Json -Depth 4 -Compress
} catch {
  [pscustomobject]@{ ok = $false; error = [string]$_.Exception.Message } | ConvertTo-Json -Compress
}"#;
        let v = run_envelope(script, "autostart")?;
        let entries = winps::as_array(v.get("entries").cloned().unwrap_or(Value::Null));
        Ok(json!({ "entries": entries }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_query_that_ran_out_of_time_reports_a_timeout() {
        let (code, message) = probe_error("services", ProbeFailure::Timeout(DIAG_BUDGET));
        assert_eq!(code, ErrorCode::Timeout);
        assert_eq!(message, "services query timed out after 50s");
    }

    #[test]
    fn each_other_failure_says_what_happened() {
        let cases = [
            (
                ProbeFailure::Spawn,
                "services query could not start PowerShell",
            ),
            (
                ProbeFailure::Exit(Some(1)),
                "services query exited with code 1",
            ),
            (ProbeFailure::Exit(None), "services query was terminated"),
            (ProbeFailure::Empty, "services query produced no output"),
            (
                ProbeFailure::Invalid,
                "services query produced output that is not valid JSON",
            ),
        ];
        for (failure, expected) in cases {
            let (code, message) = probe_error("services", failure);
            assert_eq!(code, ErrorCode::ExecFailed, "{failure:?}");
            assert_eq!(message, expected);
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_probe_past_its_budget_is_killed_and_reported_as_a_timeout() {
        use crate::telemetry::collectors::winps;

        let budget = Duration::from_secs(1);
        let result = winps::run_json_within("Start-Sleep -Seconds 10; '{}'", budget);
        assert_eq!(result.unwrap_err(), ProbeFailure::Timeout(budget));
    }

    #[test]
    fn processes_lists_self() {
        let v = processes(json!({})).unwrap();
        assert!(v["processes"]
            .as_array()
            .map(|a| !a.is_empty())
            .unwrap_or(false));
    }

    #[cfg(not(windows))]
    #[test]
    fn services_unsupported_off_windows() {
        let err = services(json!({})).unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }

    #[cfg(not(windows))]
    #[test]
    fn eventlog_unsupported_off_windows() {
        let err = eventlog(json!({"log": "System", "count": 5})).unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }

    #[cfg(not(windows))]
    #[test]
    fn autostart_unsupported_off_windows() {
        let err = autostart(json!({})).unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }
}
