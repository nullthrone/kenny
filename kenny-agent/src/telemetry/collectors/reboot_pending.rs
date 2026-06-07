//! `reboot_pending` section — whether a reboot is required.
//!
//! On Windows this checks the CBS / WindowsUpdate / PendingFileRename registry
//! markers. Off Windows we stub to `pending: false`.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Reason labels that are dropped before a reboot is reported as pending.
///
/// `PendingFileRenameOperations` is set by routine servicing, antivirus and
/// temp-file cleanup and then lingers indefinitely, so on its own it would keep
/// this section permanently in `warn` without indicating an actionable reboot.
/// Excluding it here is purely a reporting decision: the wire shape of `reasons`
/// is unchanged, the label simply never appears.
#[cfg_attr(not(windows), allow(dead_code))] // used by the Windows collector + tests
const EXCLUDED_REASONS: &[&str] = &["PendingFileRenameOperations"];

/// Drop excluded labels (see [`EXCLUDED_REASONS`]) from a list of reboot reasons.
#[cfg_attr(not(windows), allow(dead_code))] // used by the Windows collector + tests
fn apply_exclusions(reasons: Vec<String>) -> Vec<String> {
    reasons
        .into_iter()
        .filter(|r| !EXCLUDED_REASONS.contains(&r.as_str()))
        .collect()
}

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
        // Drop labels that are effectively always present (see EXCLUDED_REASONS)
        // so they don't pin the section to `warn`.
        let reasons = super::apply_exclusions(reasons);

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

    #[test]
    fn pending_file_rename_operations_is_excluded() {
        let kept = apply_exclusions(vec![
            "WindowsUpdate".to_string(),
            "PendingFileRenameOperations".to_string(),
        ]);
        assert_eq!(kept, vec!["WindowsUpdate".to_string()]);
    }

    #[test]
    fn excluded_only_reasons_clear_the_pending_flag() {
        // A reason list that is nothing but excluded labels collapses to empty,
        // so the section reports no pending reboot.
        let kept = apply_exclusions(vec!["PendingFileRenameOperations".to_string()]);
        assert!(kept.is_empty());
    }

    #[test]
    fn unlisted_reasons_pass_through() {
        let reasons = vec![
            "WindowsUpdate".to_string(),
            "ComponentBasedServicing".to_string(),
        ];
        assert_eq!(apply_exclusions(reasons.clone()), reasons);
    }
}
