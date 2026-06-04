//! `reliability` section — system stability index / recent crashes.
//!
//! Real data from `Win32_ReliabilityStabilityMetrics` / `Get-WinEvent` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `reliability` section.
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
            json!({ "stability_index": null, "recent_crashes": 0 }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Count Error/Critical (Level 1/2) events in the System + Application logs over
    /// the last 7 days, and read the latest reliability stability index if exposed.
    pub fn collect() -> Section {
        let script = r#"
$since = (Get-Date).AddDays(-7)
$errors = 0
foreach ($log in 'System','Application') {
  try {
    $errors += @(Get-WinEvent -FilterHashtable @{ LogName=$log; Level=1,2; StartTime=$since } -ErrorAction Stop).Count
  } catch {}
}
$index = $null
try {
  $m = Get-CimInstance -ClassName Win32_ReliabilityStabilityMetrics -ErrorAction Stop |
       Sort-Object TimeGenerated -Descending | Select-Object -First 1
  if ($m) { $index = [double]$m.SystemStabilityIndex }
} catch {}
[pscustomobject]@{ stability_index = $index; recent_crashes = $errors } | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "reliability unavailable",
                json!({ "stability_index": null, "recent_crashes": 0 }),
            );
        };

        let crashes = v.get("recent_crashes").and_then(Value::as_u64).unwrap_or(0);
        let index = v.get("stability_index").cloned().unwrap_or(Value::Null);

        let (status, summary) = if crashes >= 20 {
            (
                Status::Warn,
                format!("{crashes} error/critical events in 7d"),
            )
        } else {
            (Status::Ok, format!("{crashes} error event(s) in 7d"))
        };
        Section::with_fields(
            status,
            summary,
            json!({ "stability_index": index, "recent_crashes": crashes }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reliability_section_is_valid() {
        let v = collect().into_value();
        assert!(v.get("recent_crashes").is_some());
    }
}
