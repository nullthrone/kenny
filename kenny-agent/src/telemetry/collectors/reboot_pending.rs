//! `reboot_pending` section — whether a reboot is required.
//!
//! On Windows this checks the CBS / WindowsUpdate / PendingFileRename registry
//! markers. Off Windows we stub to `pending: false`.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `reboot_pending` section.
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
            json!({ "pending": false, "reasons": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Check the standard reboot-required registry markers and report which fired.
    pub fn collect() -> Section {
        // Each probe yields a reason label when present. PendingFileRenameOperations
        // is a value under the Session Manager key (presence = pending).
        let script = r#"
$reasons = @()
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $reasons += 'ComponentBasedServicing' }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $reasons += 'WindowsUpdate' }
$pfro = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue)
if ($pfro -and $pfro.PendingFileRenameOperations) { $reasons += 'PendingFileRenameOperations' }
$cn = (Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Services\Netlogon' -Name 'JoinDomain' -ErrorAction SilentlyContinue)
if ($cn) { $reasons += 'PendingComputerRename' }
ConvertTo-Json -Compress @($reasons)
"#;

        let reasons: Vec<String> = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default()
            .into_iter()
            .filter_map(|r| r.as_str().map(str::to_string))
            .collect();

        let pending = !reasons.is_empty();
        let (status, summary) = if pending {
            (
                Status::Warn,
                format!("Reboot required ({})", reasons.join(", ")),
            )
        } else {
            (Status::Ok, "no reboot pending".to_string())
        };
        let reasons_json: Vec<Value> = reasons.into_iter().map(Value::String).collect();
        Section::with_fields(
            status,
            summary,
            json!({ "pending": pending, "reasons": reasons_json }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reboot_pending_section_is_valid() {
        let v = collect().into_value();
        assert!(v["pending"].is_boolean());
    }
}
