//! Diagnostic tools: `diag_processes`, `diag_services`, `diag_eventlog`,
//! `diag_autostart`.
//!
//! `diag_processes` is portable via `sysinfo`. Services/eventlog/autostart are
//! Windows-only; off Windows they return `unsupported`.

use serde::Deserialize;
use serde_json::{json, Value};
use sysinfo::{ProcessesToUpdate, System};

use crate::protocol::ErrorCode;

/// `diag_processes` — running processes with cpu and memory.
pub fn processes(_args: Value) -> Result<Value, (ErrorCode, String)> {
    let mut sys = System::new();
    sys.refresh_processes(ProcessesToUpdate::All);
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

    /// Run `script` (which must emit a `{ok, ...}` JSON envelope) and return the
    /// parsed value, mapping an `ok:false` envelope or empty/invalid output to a
    /// proper `ExecFailed` error instead of a silently empty result.
    fn run_envelope(script: &str, what: &str) -> Result<Value, (ErrorCode, String)> {
        let Some(v) = winps::run_json(script) else {
            return Err((
                ErrorCode::ExecFailed,
                format!("{what} query produced no output"),
            ));
        };
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
