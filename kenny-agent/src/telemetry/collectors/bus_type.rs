//! The one mapping from a Windows disk `BusType` to the contract's `bus_type`
//! vocabulary (`docs/protocol.md`, the `disk_smart` section).
//!
//! Both collectors that name a disk's bus — `disk_smart` rows and the `disk_bus_type`
//! detail of `hardware_errors` storage events — read the raw `Get-PhysicalDisk` `BusType`
//! and map it here, so the two cannot disagree about the same drive.

/// Map a raw `Get-PhysicalDisk` `BusType` — the enum name PowerShell prints, or its number
/// when the name is unknown to the host — to the contract vocabulary.
///
/// The numbering is `MSFT_PhysicalDisk.BusType`, which follows `STORAGE_BUS_TYPE`
/// (`winioctl.h`): 0 Unknown, 1 SCSI, 2 ATAPI, 3 ATA, 4 1394, 5 SSA, 6 Fibre Channel,
/// 7 USB, 8 RAID, 9 iSCSI, 10 SAS, 11 SATA, 12 SD, 13 MMC, 14 Virtual,
/// 15 File Backed Virtual, 16 Storage Spaces, 17 NVMe, 18 SCM, 19 UFS.
///
/// ATA and ATAPI report as `SATA`; SCSI, iSCSI and Fibre Channel as `SCSI`; SD and MMC as
/// `SD`. Everything else (virtual, spaces, 1394, SCM, UFS, a number outside the enum) is
/// `Unknown`.
pub(crate) fn bus_type_name(raw: &str) -> &'static str {
    match raw.trim().to_ascii_lowercase().as_str() {
        "nvme" | "17" => "NVMe",
        "sata" | "ata" | "atapi" | "11" | "3" | "2" => "SATA",
        "sas" | "10" => "SAS",
        "usb" | "7" => "USB",
        "raid" | "8" => "RAID",
        "scsi" | "iscsi" | "fibre channel" | "fibrechannel" | "1" | "9" | "6" => "SCSI",
        "sd" | "mmc" | "12" | "13" => "SD",
        _ => "Unknown",
    }
}

#[cfg(test)]
mod tests {
    use std::collections::HashMap;

    use serde_json::{json, Map, Value};

    use super::bus_type_name;
    use crate::telemetry::collectors::disk_smart::core::finish_windows_row;
    use crate::telemetry::collectors::hardware_errors::winevent;

    /// Every raw `BusType` spelling both collectors can be handed, with the expected name.
    const CASES: &[(&str, &str)] = &[
        ("NVMe", "NVMe"),
        ("17", "NVMe"),
        ("SATA", "SATA"),
        ("ATA", "SATA"),
        ("ATAPI", "SATA"),
        ("11", "SATA"),
        ("3", "SATA"),
        ("2", "SATA"),
        ("SAS", "SAS"),
        ("10", "SAS"),
        ("USB", "USB"),
        ("7", "USB"),
        ("RAID", "RAID"),
        ("8", "RAID"),
        ("SCSI", "SCSI"),
        ("iSCSI", "SCSI"),
        ("Fibre Channel", "SCSI"),
        ("1", "SCSI"),
        ("9", "SCSI"),
        ("6", "SCSI"),
        ("SD", "SD"),
        ("MMC", "SD"),
        ("12", "SD"),
        ("13", "SD"),
        ("Virtual", "Unknown"),
        ("File Backed Virtual", "Unknown"),
        ("Storage Spaces", "Unknown"),
        ("1394", "Unknown"),
        ("SCM", "Unknown"),
        ("UFS", "Unknown"),
        ("0", "Unknown"),
        ("4", "Unknown"),
        ("14", "Unknown"),
        ("99", "Unknown"),
        ("", "Unknown"),
        ("  usb ", "USB"),
    ];

    #[test]
    fn the_mapping_follows_the_storage_bus_type_enum() {
        for (raw, want) in CASES {
            assert_eq!(bus_type_name(raw), *want, "{raw:?}");
        }
    }

    /// The seam: the `disk_smart` row and the `hardware_errors` storage event name the
    /// same bus for the same raw `BusType`.
    #[test]
    fn disk_smart_and_hardware_errors_agree_on_every_raw_bus_type() {
        for (raw, want) in CASES {
            let Value::Object(row) = json!({
                "model": "D", "health_status": "Healthy", "predictive_failure": false,
                "device_number": 1, "bus_type": raw, "removable": false,
            }) else {
                unreachable!()
            };
            let row: Map<String, Value> = row;
            let finished = finish_windows_row(row, false, &mut |_| Err("n/a".to_string()));
            assert_eq!(finished["bus_type"], *want, "disk_smart, {raw:?}");

            let disks: HashMap<String, String> =
                HashMap::from([("1".to_string(), (*raw).to_string())]);
            let details = winevent::event_details(
                "disk",
                11,
                "<EventData><Data>\\Device\\Harddisk1\\DR1</Data></EventData>",
                "",
                &disks,
            );
            let bus = details
                .iter()
                .find(|(k, _)| *k == "disk_bus_type")
                .map(|(_, v)| v.as_str());
            assert_eq!(bus, Some(*want), "hardware_errors, {raw:?}");
        }
    }
}
