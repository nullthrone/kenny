//! `defender_quarantine` section — quarantined threats.
//!
//! Real data from `Get-MpThreatDetection` / `Get-MpThreat` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `defender_quarantine` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "items": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-MpThreatDetection` joined with `Get-MpThreat` for the threat name into
    /// `{name, severity, detected_at}` items.
    pub fn collect() -> Section {
        let script = r#"
$threats = @{}
foreach ($t in @(Get-MpThreat)) { $threats[[string]$t.ThreatID] = $t }
@(Get-MpThreatDetection) | ForEach-Object {
  $t = $threats[[string]$_.ThreatID]
  [pscustomobject]@{
    name        = if ($t) { [string]$t.ThreatName } else { [string]$_.ThreatID }
    severity    = if ($t) { [string]$t.SeverityID } else { $null }
    detected_at = if ($_.InitialDetectionTime) { (Get-Date $_.InitialDetectionTime).ToUniversalTime().ToString("o") } else { $null }
  }
} | ConvertTo-Json -Compress
"#;

        let items: Vec<Value> = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();

        let count = items.len();
        let (status, summary) = if count == 0 {
            (Status::Ok, "0 quarantined items".to_string())
        } else {
            (Status::Warn, format!("{count} quarantined item(s)"))
        };
        Section::with_fields(status, summary, json!({ "items": items }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defender_quarantine_section_is_valid() {
        assert!(collect().into_value()["items"].is_array());
    }
}
