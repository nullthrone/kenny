//! `printers` section — installed printers and queue state.
//!
//! Real data from `Get-Printer` / `Get-PrintJob` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `printers` section.
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
            json!({ "printers": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-Printer` + `Get-PrintJob` into `{name, status, is_default, jobs}`;
    /// `warn` on error/offline printers.
    pub fn collect() -> Section {
        let script = r#"
$default = (Get-CimInstance -ClassName Win32_Printer -ErrorAction SilentlyContinue | Where-Object { $_.Default } | Select-Object -First 1).Name
Get-Printer -ErrorAction SilentlyContinue | ForEach-Object {
  $jobs = 0
  try { $jobs = @(Get-PrintJob -PrinterName $_.Name -ErrorAction Stop).Count } catch {}
  [pscustomobject]@{
    name       = [string]$_.Name
    status     = [string]$_.PrinterStatus
    is_default = ($_.Name -eq $default)
    jobs       = [int]$jobs
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "printers unavailable",
                json!({ "printers": [] }),
            );
        };
        let printers = winps::as_array(v);

        // PrinterStatus values: Normal/Idle are fine; Error/Offline are not.
        let bad = printers
            .iter()
            .filter(|p| {
                p.get("status")
                    .and_then(Value::as_str)
                    .map(|s| {
                        let s = s.to_lowercase();
                        s.contains("error") || s.contains("offline")
                    })
                    .unwrap_or(false)
            })
            .count();

        let total = printers.len();
        let (status, summary) = if bad > 0 {
            (
                Status::Warn,
                format!("{bad} of {total} printers in error/offline"),
            )
        } else {
            (Status::Ok, format!("{total} printer(s) OK"))
        };
        Section::with_fields(status, summary, json!({ "printers": printers }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn printers_section_is_valid() {
        assert!(collect().into_value()["printers"].is_array());
    }
}
