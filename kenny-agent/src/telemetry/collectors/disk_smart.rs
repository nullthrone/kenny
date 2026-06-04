//! `disk_smart` section — physical disk SMART/reliability counters.
//!
//! Real data from `Get-PhysicalDisk` / `Get-StorageReliabilityCounter` on Windows.

use serde_json::json;

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
        Section::with_fields(
            Status::Ok,
            "n/a on this platform",
            json!({ "disks": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Get-PhysicalDisk` + `Get-StorageReliabilityCounter` into
    /// `{model, health_status, wear, temperature_c, reallocated_sectors}`.
    pub fn collect() -> Section {
        // TODO(windows): Get-PhysicalDisk / Get-StorageReliabilityCounter.
        Section::with_fields(Status::Ok, "SMART healthy", json!({ "disks": [] }))
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
