//! `disk_smart` section — physical disk SMART/reliability counters.
//!
//! Real data from `Get-PhysicalDisk` / `Get-StorageReliabilityCounter` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `disk_smart` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "disks": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Get-PhysicalDisk` joined with `Get-StorageReliabilityCounter` into
    /// `{model, health_status, wear, temperature_c, reallocated_sectors}`; `crit`
    /// on predictive failure / non-Healthy health status.
    pub fn collect() -> Section {
        let script = r#"
Get-PhysicalDisk | ForEach-Object {
  $rc = $null
  try { $rc = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
  [pscustomobject]@{
    model               = [string]$_.FriendlyName
    health_status       = [string]$_.HealthStatus
    predictive_failure  = ($_.HealthStatus -ne 'Healthy')
    wear                = if ($rc) { $rc.Wear } else { $null }
    temperature_c       = if ($rc) { $rc.Temperature } else { $null }
    reallocated_sectors = if ($rc) { $rc.ReadErrorsTotal } else { $null }
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(Status::Ok, "SMART unavailable", json!({ "disks": [] }));
        };
        let disks = winps::as_array(v);

        let failing = disks.iter().any(|d| {
            d.get("predictive_failure").and_then(Value::as_bool) == Some(true)
                || d.get("health_status")
                    .and_then(Value::as_str)
                    .map(|s| !s.eq_ignore_ascii_case("Healthy"))
                    .unwrap_or(false)
        });

        let (status, summary) = if failing {
            (Status::Crit, "predictive disk failure".to_string())
        } else {
            (Status::Ok, "SMART healthy".to_string())
        };
        Section::with_fields(status, summary, json!({ "disks": disks }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_smart_section_is_valid() {
        assert!(collect().into_value()["disks"].is_array());
    }
}
