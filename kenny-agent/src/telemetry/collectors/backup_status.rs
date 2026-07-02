//! `backup_status` section — evidence that *any* backup mechanism is alive.
//!
//! Three best-effort, individually nullable probes on Windows:
//! - System Restore: `Get-ComputerRestorePoint` (count + latest `CreationTime`),
//!   `enabled` from the `SystemRestore` registry key (falling back to "points
//!   exist ⇒ enabled").
//! - File History: the `fhsvc` service state. The per-user File History
//!   *configuration* lives under each user's protected AppData `FileHistory`
//!   `Config` and is unreadable from the session-0 service, so `configured`
//!   stays `null`.
//! - OneDrive: installed (known `OneDrive.exe` paths under Program Files and
//!   per-user LocalAppData) + running (an `OneDrive` process exists).
//!
//! Status is always `ok` — judgment is server-side.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `backup_status` section.
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
                "restore_points": { "enabled": null, "count": null, "latest": null },
                "file_history": { "service_state": null, "configured": null },
                "onedrive": { "installed": null, "running": null },
            }),
        )
    }
}

/// Portable summarizing core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    /// Whole days between an RFC3339 `latest` timestamp and `now_unix`;
    /// `None` when unparseable. Negative (clock-skewed) ages clamp to 0.
    pub fn days_ago(latest_rfc3339: &str, now_unix: i64) -> Option<i64> {
        let t = chrono::DateTime::parse_from_rfc3339(latest_rfc3339).ok()?;
        Some((now_unix - t.timestamp()).max(0) / 86_400)
    }

    /// Fixture-style summary, e.g. `restore point 2d ago; OneDrive running`.
    pub fn summarize(
        restore_days_ago: Option<i64>,
        restore_count: Option<u64>,
        onedrive_installed: Option<bool>,
        onedrive_running: Option<bool>,
    ) -> String {
        let rp = match (restore_days_ago, restore_count) {
            (Some(d), _) => format!("restore point {d}d ago"),
            (None, Some(0)) => "no restore points".to_string(),
            (None, Some(n)) => format!("{n} restore points"),
            (None, None) => "restore points unknown".to_string(),
        };
        let od = match (onedrive_installed, onedrive_running) {
            (_, Some(true)) => "OneDrive running",
            (Some(true), _) => "OneDrive not running",
            (Some(false), _) => "OneDrive not installed",
            _ => "OneDrive unknown",
        };
        format!("{rp}; {od}")
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn days_ago_computes_whole_days() {
            let now = 1_750_000_000i64;
            let two_days = chrono::DateTime::from_timestamp(now - 2 * 86_400 - 3600, 0)
                .unwrap()
                .to_rfc3339();
            assert_eq!(days_ago(&two_days, now), Some(2));
            let future = chrono::DateTime::from_timestamp(now + 86_400, 0)
                .unwrap()
                .to_rfc3339();
            assert_eq!(days_ago(&future, now), Some(0), "clock skew clamps to 0");
            // PowerShell 'o' format (fractional seconds) parses too.
            assert_eq!(
                days_ago("2026-06-02T11:30:00.0000000Z", 1_780_000_000),
                days_ago("2026-06-02T11:30:00Z", 1_780_000_000)
            );
            assert_eq!(days_ago("garbage", now), None);
        }

        #[test]
        fn summarize_covers_the_states() {
            assert_eq!(
                summarize(Some(2), Some(5), Some(true), Some(true)),
                "restore point 2d ago; OneDrive running"
            );
            assert_eq!(
                summarize(None, Some(0), Some(true), Some(false)),
                "no restore points; OneDrive not running"
            );
            assert_eq!(
                summarize(None, Some(3), Some(false), None),
                "3 restore points; OneDrive not installed"
            );
            assert_eq!(
                summarize(None, None, None, None),
                "restore points unknown; OneDrive unknown"
            );
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    pub fn collect() -> Section {
        let script = r#"
$rpEnabled = $null; $rpCount = $null; $rpLatest = $null
try {
  $rps = @(Get-ComputerRestorePoint -ErrorAction Stop)
  $rpCount = $rps.Count
  if ($rps.Count -gt 0) {
    # CreationTime is a DMTF (WMI) datetime string.
    $latest = $rps | Sort-Object -Property CreationTime -Descending | Select-Object -First 1
    $rpLatest = ([System.Management.ManagementDateTimeConverter]::ToDateTime([string]$latest.CreationTime)).ToUniversalTime().ToString('o')
  }
} catch {}
try {
  $sr = Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\SystemRestore' -ErrorAction Stop
  if ($null -ne $sr.PSObject.Properties['RPSessionInterval']) {
    $rpEnabled = ([int]$sr.RPSessionInterval -ge 1)
  }
} catch {}
if ($null -eq $rpEnabled -and $rpCount -gt 0) { $rpEnabled = $true }

$fhState = $null
try { $fhState = ([string](Get-Service -Name fhsvc -ErrorAction Stop).Status).ToLower() } catch {}

$odInstalled = $false
$odPaths = @()
if ($env:ProgramFiles) { $odPaths += (Join-Path $env:ProgramFiles 'Microsoft OneDrive\OneDrive.exe') }
if (${env:ProgramFiles(x86)}) { $odPaths += (Join-Path ${env:ProgramFiles(x86)} 'Microsoft OneDrive\OneDrive.exe') }
Get-ChildItem -Path 'C:\Users' -Directory -ErrorAction SilentlyContinue | ForEach-Object {
  $odPaths += (Join-Path $_.FullName 'AppData\Local\Microsoft\OneDrive\OneDrive.exe')
}
foreach ($p in $odPaths) { if (Test-Path -LiteralPath $p) { $odInstalled = $true; break } }
$odRunning = (@(Get-Process -Name OneDrive -ErrorAction SilentlyContinue).Count -gt 0)

[pscustomobject]@{
  restore_points = [pscustomobject]@{ enabled = $rpEnabled; count = $rpCount; latest = $rpLatest }
  # Per-user File History configuration is unreadable from session 0 => null.
  file_history = [pscustomobject]@{ service_state = $fhState; configured = $null }
  onedrive = [pscustomobject]@{ installed = $odInstalled; running = $odRunning }
} | ConvertTo-Json -Compress -Depth 3
"#;

        let v = winps::run_json(script).unwrap_or(Value::Null);
        let restore_points = v.get("restore_points").cloned().unwrap_or(json!({
            "enabled": null, "count": null, "latest": null
        }));
        let file_history = v.get("file_history").cloned().unwrap_or(json!({
            "service_state": null, "configured": null
        }));
        let onedrive = v.get("onedrive").cloned().unwrap_or(json!({
            "installed": null, "running": null
        }));

        let now_unix = chrono::Utc::now().timestamp();
        let restore_days_ago = restore_points
            .get("latest")
            .and_then(Value::as_str)
            .and_then(|s| core::days_ago(s, now_unix));
        let summary = core::summarize(
            restore_days_ago,
            restore_points.get("count").and_then(Value::as_u64),
            onedrive.get("installed").and_then(Value::as_bool),
            onedrive.get("running").and_then(Value::as_bool),
        );

        Section::with_fields(
            Status::Ok,
            summary,
            json!({
                "restore_points": restore_points,
                "file_history": file_history,
                "onedrive": onedrive,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backup_status_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["restore_points"].is_object());
        assert!(v["file_history"].is_object());
        assert!(v["onedrive"].is_object());
        // Every contract sub-field is present (values may be null).
        for key in ["enabled", "count", "latest"] {
            assert!(v["restore_points"].get(key).is_some());
        }
        for key in ["service_state", "configured"] {
            assert!(v["file_history"].get(key).is_some());
        }
        for key in ["installed", "running"] {
            assert!(v["onedrive"].get(key).is_some());
        }
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert!(v["restore_points"]["enabled"].is_null());
        assert!(v["file_history"]["configured"].is_null());
        assert!(v["onedrive"]["installed"].is_null());
    }
}
