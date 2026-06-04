//! `encryption` section — BitLocker volume encryption state.
//!
//! Real data from `Get-BitLockerVolume` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `encryption` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "volumes": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-BitLockerVolume` into `{mount, protection_status, encryption_percent}`;
    /// `warn` when the system drive (`$env:SystemDrive`) is unprotected.
    pub fn collect() -> Section {
        // ProtectionStatus: 0=Off, 1=On, 2=Unknown. We expose the raw int plus a
        // boolean-friendly summary. Get-BitLockerVolume requires elevation; on
        // failure we surface an unknown state rather than claiming "encrypted".
        let script = r#"
Get-BitLockerVolume | ForEach-Object {
  [pscustomobject]@{
    mount              = [string]$_.MountPoint
    protection_status  = [int]$_.ProtectionStatus
    encryption_percent = [int]$_.EncryptionPercentage
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Warn,
                "BitLocker state unavailable",
                json!({ "volumes": [] }),
            );
        };
        let volumes = winps::as_array(v);

        // System drive, e.g. "C:" (strip trailing backslash if present).
        let system_drive = std::env::var("SystemDrive").unwrap_or_else(|_| "C:".to_string());
        let sys_norm = system_drive.trim_end_matches('\\').to_uppercase();

        let system_unprotected = volumes.iter().any(|vol| {
            let mount = vol
                .get("mount")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim_end_matches('\\')
                .to_uppercase();
            mount == sys_norm && vol.get("protection_status").and_then(Value::as_i64) != Some(1)
        });

        let (status, summary) = if system_unprotected {
            (Status::Warn, format!("{sys_norm} not BitLocker-protected"))
        } else {
            (Status::Ok, "system drive encrypted".to_string())
        };
        Section::with_fields(status, summary, json!({ "volumes": volumes }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn encryption_section_is_valid() {
        assert!(collect().into_value()["volumes"].is_array());
    }
}
