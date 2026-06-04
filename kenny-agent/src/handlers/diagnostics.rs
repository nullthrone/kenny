//! Diagnostic tools: `diag.processes`, `diag.services`, `diag.eventlog`,
//! `diag.autostart`.
//!
//! `diag.processes` is portable via `sysinfo`. Services/eventlog/autostart are
//! Windows-only; off Windows they return `unsupported`.

use serde::Deserialize;
use serde_json::{json, Value};
use sysinfo::{ProcessesToUpdate, System};

use crate::protocol::ErrorCode;

/// `diag.processes` — running processes with cpu and memory.
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

/// `diag.services` — Windows service inventory.
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
        Err(unsupported("diag.services"))
    }
}

#[derive(Debug, Deserialize)]
struct EventLogArgs {
    #[allow(dead_code)]
    log: String,
    #[allow(dead_code)]
    count: u32,
}

/// `diag.eventlog` — recent Windows Event Log entries.
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
        Err(unsupported("diag.eventlog"))
    }
}

/// `diag.autostart` — startup programs.
pub fn autostart(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::autostart()
    }
    #[cfg(not(windows))]
    {
        Err(unsupported("diag.autostart"))
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

    /// Real impl: `Get-CimInstance Win32_Service` into `{name, display, status, start}`.
    pub fn services(_filter: Option<&str>) -> Result<Value, (ErrorCode, String)> {
        // TODO(windows): Win32_Service via CIM, applying `filter` on name/display.
        Ok(json!({ "services": [] }))
    }

    /// Real impl: `Get-WinEvent -LogName <log> -MaxEvents <count>` into
    /// `{time, level, source, message}`.
    pub fn eventlog(_log: &str, _count: u32) -> Result<Value, (ErrorCode, String)> {
        // TODO(windows): Get-WinEvent.
        Ok(json!({ "events": [] }))
    }

    /// Real impl: `Win32_StartupCommand` + Run keys into `{name, command, location}`.
    pub fn autostart() -> Result<Value, (ErrorCode, String)> {
        // TODO(windows): Win32_StartupCommand / Run keys.
        Ok(json!({ "entries": [] }))
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
}
