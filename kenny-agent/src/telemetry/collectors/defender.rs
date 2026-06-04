//! `defender` section — Microsoft Defender status.
//!
//! Real data comes from `Get-MpComputerStatus` on Windows. Off Windows we stub.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `defender` section.
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
            json!({
                "enabled": null,
                "realtime_protection": null,
                "last_scan": null,
                "last_scan_type": null,
                "last_signature_update": null,
                "threats_found": 0,
                "action_needed": false
            }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Parse `Get-MpComputerStatus` into the defender fields. Scan time is the
    /// newer of the quick/full scan end times; `last_scan_type` records which.
    pub fn collect() -> Section {
        // Emit a stable JSON object with ISO-8601 timestamps for the times we care
        // about. `Get-Date -Format o` produces a round-trippable timestamp.
        let script = r#"
$s = Get-MpComputerStatus
function Iso($d) { if ($d) { (Get-Date $d).ToUniversalTime().ToString("o") } else { $null } }
[pscustomobject]@{
  enabled               = [bool]($s.AntivirusEnabled -or $s.AMServiceEnabled)
  realtime_protection   = [bool]$s.RealTimeProtectionEnabled
  quick_scan            = Iso $s.QuickScanEndTime
  full_scan             = Iso $s.FullScanEndTime
  last_signature_update = Iso $s.AntivirusSignatureLastUpdated
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Warn,
                "Defender status unavailable",
                json!({
                    "enabled": null,
                    "realtime_protection": null,
                    "last_scan": null,
                    "last_scan_type": null,
                    "last_signature_update": null,
                    "threats_found": 0,
                    "action_needed": true
                }),
            );
        };

        let enabled = v.get("enabled").and_then(Value::as_bool);
        let realtime = v.get("realtime_protection").and_then(Value::as_bool);
        let quick = v.get("quick_scan").and_then(Value::as_str);
        let full = v.get("full_scan").and_then(Value::as_str);
        // Pick the most recent of the two scans for `last_scan`.
        let (last_scan, last_scan_type) = match (quick, full) {
            (Some(q), Some(f)) => {
                if f >= q {
                    (Some(f.to_string()), "full")
                } else {
                    (Some(q.to_string()), "quick")
                }
            }
            (Some(q), None) => (Some(q.to_string()), "quick"),
            (None, Some(f)) => (Some(f.to_string()), "full"),
            (None, None) => (None, "unknown"),
        };
        let last_sig = v
            .get("last_signature_update")
            .and_then(Value::as_str)
            .map(str::to_string);

        // Count quarantined/active threats separately so we can flag action.
        let threats = winps::run_json("@(Get-MpThreat).Count | ConvertTo-Json -Compress")
            .and_then(|t| t.as_u64())
            .unwrap_or(0);

        let realtime_off = realtime == Some(false) || enabled == Some(false);
        let action_needed = realtime_off || threats > 0;
        let status = if realtime_off {
            Status::Crit
        } else if threats > 0 {
            Status::Warn
        } else {
            Status::Ok
        };
        let summary = if realtime_off {
            "Real-time protection OFF".to_string()
        } else if threats > 0 {
            format!("{threats} active threat(s)")
        } else {
            "Defender healthy".to_string()
        };

        Section::with_fields(
            status,
            summary,
            json!({
                "enabled": enabled,
                "realtime_protection": realtime,
                "last_scan": last_scan,
                "last_scan_type": last_scan_type,
                "last_signature_update": last_sig,
                "threats_found": threats,
                "action_needed": action_needed
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defender_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v.get("threats_found").is_some());
    }
}
