//! `services` section — Windows service inventory.
//!
//! Real data comes from `Get-Service`/CIM on Windows. Off Windows we stub to
//! `n/a`; the section shape (`services: [{name, display, status, start}]`) matches
//! the `diag.services` tool result.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `services` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "services": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Enumerate services via `Win32_Service` into `{name, display, status, start}`;
    /// `warn` when an auto-start service is Stopped.
    pub fn collect() -> Section {
        let script = r#"
Get-CimInstance -ClassName Win32_Service | ForEach-Object {
  [pscustomobject]@{
    name    = [string]$_.Name
    display = [string]$_.DisplayName
    status  = [string]$_.State
    start   = [string]$_.StartMode
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "services unavailable",
                json!({ "services": [] }),
            );
        };
        let services = winps::as_array(v);

        // Auto-start services that are not Running. Ignore "Auto (Delayed)" naming
        // by matching the StartMode "Auto" prefix.
        let stalled: Vec<&str> = services
            .iter()
            .filter(|s| {
                let start = s.get("start").and_then(Value::as_str).unwrap_or("");
                let state = s.get("status").and_then(Value::as_str).unwrap_or("");
                start.eq_ignore_ascii_case("Auto") && !state.eq_ignore_ascii_case("Running")
            })
            .filter_map(|s| s.get("name").and_then(Value::as_str))
            .collect();

        let total = services.len();
        let (status, summary) = if stalled.is_empty() {
            (Status::Ok, format!("{total} services, all auto running"))
        } else {
            (
                Status::Warn,
                format!("{} auto service(s) stopped", stalled.len()),
            )
        };
        Section::with_fields(status, summary, json!({ "services": services }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn services_section_is_valid() {
        let v = collect().into_value();
        assert!(v["services"].is_array());
    }
}
