//! `disk_smart` section — physical disk identity, health and reliability counters.
//!
//! Windows: `Get-PhysicalDisk` + `Get-StorageReliabilityCounter` (one PowerShell script),
//! the SMART WMI classes under `root\wmi`, and the NVMe health log read from
//! `\\.\PhysicalDriveN` (the only read anti-cheat coexistence pauses). Linux: sysfs
//! identity, the NVMe admin ioctl (paused alike) and an opportunistic `smartctl --json`. Row shape and grading: `docs/protocol.md` (the `disk_smart`
//! section). The NVMe log decoder is shared and lives in [`super::nvme`].

use crate::telemetry::Section;

/// Collect the `disk_smart` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(target_os = "linux")]
    {
        linux_impl::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        Section::with_fields(
            crate::protocol::Status::Ok,
            "n/a on this platform",
            serde_json::json!({ "disks": [], "truncated": false }),
        )
    }
}

/// Portable row-shape, parsing and grading core — compiled and tested on every platform.
/// Platform modules supply the raw reads; everything that decides what a row says lives
/// here, so it is tested without hardware.
#[allow(dead_code)] // each platform uses a different subset
pub mod core {
    use std::collections::BTreeMap;

    use base64::engine::general_purpose::STANDARD;
    use base64::Engine as _;
    use serde_json::{json, Map, Value};

    use crate::protocol::Status;
    use crate::telemetry::collectors::bus_type::bus_type_name;
    use crate::telemetry::collectors::nvme::NvmeHealth;
    use crate::telemetry::Section;

    /// The most disks one snapshot reports (the frame budget).
    pub const MAX_DISKS: usize = 16;

    /// The highest `wear` value: percent of rated endurance, clamped. The raw NVMe
    /// `percentage_used` may exceed it and stays raw in `nvme`.
    pub const MAX_WEAR: u64 = 100;

    /// The ATA attribute ids kept in `smart_attributes`.
    pub const SMART_IDS: [u8; 6] = [5, 187, 188, 197, 198, 199];

    /// The script-only key carrying a disk's `VendorSpecific` table (base64). It is
    /// parsed into `smart_attributes` and never reaches the row.
    pub const SMART_DATA_FIELD: &str = "smart_data";

    /// Row fields read from `Get-StorageReliabilityCounter`, paired with the
    /// property each one is read from. Every one is a lifetime value and `null`
    /// when the drive does not report it.
    pub const COUNTERS: &[(&str, &str)] = &[
        ("wear", "Wear"),
        ("temperature_c", "Temperature"),
        ("temperature_max_c", "TemperatureMax"),
        ("power_on_hours", "PowerOnHours"),
        ("read_errors_total", "ReadErrorsTotal"),
        ("read_errors_uncorrected", "ReadErrorsUncorrected"),
        ("write_errors_uncorrected", "WriteErrorsUncorrected"),
    ];

    /// Row fields read from `Get-PhysicalDisk` itself, paired with the PowerShell
    /// expression each one is read from. `$num` (the `DeviceId` as an integer or
    /// `$null`), `$bus` (the raw `BusType`, mapped in Rust by [`bus_type_name`]) and
    /// `$removable` (disk numbers of removable media) are set by the script before the
    /// row is built.
    pub const DISK_FIELDS: &[(&str, &str)] = &[
        ("model", "[string]$_.FriendlyName"),
        ("health_status", "[string]$_.HealthStatus"),
        ("device_number", "$num"),
        (
            "serial",
            "$(if ($_.SerialNumber -and ([string]$_.SerialNumber).Trim()) { ([string]$_.SerialNumber).Trim() } else { $null })",
        ),
        ("bus_type", "$bus"),
        (
            "media_type",
            "$(switch ([string]$_.MediaType) { 'SSD' { 'SSD' } 'HDD' { 'HDD' } default { 'Unspecified' } })",
        ),
        (
            "size_bytes",
            "$(if ($null -ne $_.Size) { [int64]$_.Size } else { $null })",
        ),
        (
            "removable",
            "[bool]($null -ne $num -and $removable.ContainsKey($num))",
        ),
    ];

    /// Script fields filled by the best-effort SMART WMI join (`$null` when the join is
    /// unmatched or ambiguous).
    pub const SMART_FIELDS: &[(&str, &str)] = &[
        ("predictive_failure", "$predict"),
        (SMART_DATA_FIELD, "$vendor"),
    ];

    /// Row fields the agent fills after the script ran.
    pub const DERIVED_FIELDS: &[&str] = &["smart_attributes", "nvme", "nvme_error", "paused"];

    /// Every key of an emitted row, in the order of the contract's example.
    pub const ROW_FIELDS: &[&str] = &[
        "model",
        "health_status",
        "predictive_failure",
        "wear",
        "temperature_c",
        "power_on_hours",
        "read_errors_total",
        "read_errors_uncorrected",
        "write_errors_uncorrected",
        "device_number",
        "serial",
        "bus_type",
        "media_type",
        "size_bytes",
        "removable",
        "temperature_max_c",
        "smart_attributes",
        "nvme",
        "nvme_error",
        "paused",
    ];

    const NORM_FUNCTION: &str = r#"
function Norm([string]$s, [bool]$instance) {
  if (-not $s) { return '' }
  $s = $s.ToLowerInvariant() -replace '^\\\\\?\\', ''
  $s = ($s -split '#\{')[0]
  $s = $s -replace '#', '\'
  if ($instance) { $s = $s -replace '_\d+$', '' }
  return $s
}
"#;

    /// Reads the SMART WMI classes once and joins them to the physical disks by the PnP
    /// device id: `Get-Disk`'s `Path` and the WMI `InstanceName` name the same device
    /// instance. A disk matching no instance, or more than one, gets `$null`.
    const SMART_PRELUDE: &str = r#"
$pfMap = @{}
$fdMap = @{}
function Add-Smart($map, [string]$instance, $value) {
  $k = Norm $instance $true
  if (-not $map.ContainsKey($k)) { $map[$k] = New-Object System.Collections.ArrayList }
  [void]$map[$k].Add($value)
}
try {
  Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictStatus -ErrorAction Stop |
    ForEach-Object { Add-Smart $pfMap $_.InstanceName ([bool]$_.PredictFailure) }
} catch {}
try {
  Get-CimInstance -Namespace root\wmi -ClassName MSStorageDriver_FailurePredictData -ErrorAction Stop |
    ForEach-Object { if ($_.VendorSpecific) { Add-Smart $fdMap $_.InstanceName ([Convert]::ToBase64String([byte[]]$_.VendorSpecific)) } }
} catch {}
$keyOf = @{}
$keyCount = @{}
foreach ($pd in $disks) {
  $k = ''
  try { $k = Norm ((Get-Disk -Number ([int]$pd.DeviceId) -ErrorAction Stop).Path) $false } catch {}
  $keyOf[[string]$pd.DeviceId] = $k
  if ($k) { $keyCount[$k] = 1 + [int]$keyCount[$k] }
}
"#;

    const SMART_ROW: &str = r#"
  $predict = $null
  $vendor = $null
  $key = $keyOf[[string]$_.DeviceId]
  if ($key -and $keyCount[$key] -eq 1) {
    if ($pfMap.ContainsKey($key) -and $pfMap[$key].Count -eq 1) { $predict = $pfMap[$key][0] }
    if ($fdMap.ContainsKey($key) -and $fdMap[$key].Count -eq 1) { $vendor = $fdMap[$key][0] }
  }
"#;

    const SCRIPT: &str = r#"
@@NORM@@
$removable = @{}
try {
  Get-CimInstance Win32_DiskDrive -ErrorAction Stop | ForEach-Object {
    if ($_.MediaType -eq 'Removable Media') { $removable[[int]$_.Index] = $true }
  }
} catch {}
$disks = @(Get-PhysicalDisk)
@@PRELUDE@@
$disks | ForEach-Object {
  $num = $null
  try { $num = [int]$_.DeviceId } catch {}
  $bus = [string]$_.BusType
  $rc = $null
  try { $rc = $_ | Get-StorageReliabilityCounter -ErrorAction Stop } catch {}
@@SMART_ROW@@
  [pscustomobject]@{
@@FIELDS@@  }
} | ConvertTo-Json -Compress
"#;

    /// The PowerShell script: `Get-PhysicalDisk` joined with
    /// `Get-StorageReliabilityCounter` and the SMART WMI classes. One row per disk, built
    /// from [`DISK_FIELDS`], [`SMART_FIELDS`] and [`COUNTERS`] so the emitted keys cannot
    /// drift from the ones the fixture test checks. `bus_type` is the raw `BusType`; the
    /// agent maps it with [`bus_type_name`].
    pub fn script() -> String {
        let fields: String = DISK_FIELDS
            .iter()
            .chain(SMART_FIELDS)
            .map(|(field, expr)| format!("    {field} = {expr}\n"))
            .chain(COUNTERS.iter().map(|(field, prop)| {
                format!("    {field} = if ($rc) {{ $rc.{prop} }} else {{ $null }}\n")
            }))
            .collect();
        SCRIPT
            .replace("@@NORM@@", NORM_FUNCTION)
            .replace("@@PRELUDE@@", SMART_PRELUDE)
            .replace("@@SMART_ROW@@", SMART_ROW)
            .replace("@@FIELDS@@", &fields)
    }

    /// Parse an ATA `VendorSpecific` SMART data block: a 2-byte revision, then 30
    /// entries of 12 bytes (id, flags(2), current, worst, raw(6, little-endian),
    /// reserved). Only [`SMART_IDS`] are kept, keyed by the decimal id; the first entry
    /// of an id wins and an id of 0 marks an unused slot.
    pub fn parse_ata_smart_attributes(bytes: &[u8]) -> BTreeMap<String, u64> {
        let mut out = BTreeMap::new();
        for entry in bytes
            .get(2..)
            .unwrap_or_default()
            .as_chunks::<12>()
            .0
            .iter()
            .take(30)
        {
            let id = entry[0];
            if id == 0 || !SMART_IDS.contains(&id) {
                continue;
            }
            let mut raw = [0u8; 8];
            raw[..6].copy_from_slice(&entry[5..11]);
            out.entry(id.to_string())
                .or_insert_with(|| u64::from_le_bytes(raw));
        }
        out
    }

    /// The smallest block holding a full attribute table.
    const SMART_TABLE_LEN: usize = 2 + 30 * 12;

    /// `smart_attributes` from the script's base64 `VendorSpecific`; `None` when it is
    /// not base64 or too short to hold the table.
    pub fn smart_attributes_from_vendor(vendor: &str) -> Option<BTreeMap<String, u64>> {
        let bytes = STANDARD.decode(vendor.trim()).ok()?;
        (bytes.len() >= SMART_TABLE_LEN).then(|| parse_ata_smart_attributes(&bytes))
    }

    /// What `smartctl --json -H -A` said about one ATA disk.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct SmartReport {
        /// `smart_status.passed`.
        pub passed: Option<bool>,
        /// The [`SMART_IDS`] raw values; `None` without an attribute table.
        pub attributes: Option<BTreeMap<String, u64>>,
        pub temperature_c: Option<i64>,
        pub power_on_hours: Option<u64>,
    }

    /// Parse `smartctl --json` output. `None` unless it is JSON that says at least one
    /// thing about the disk. The exit status is deliberately not an input: smartctl
    /// reports findings as exit-code bits, so a non-zero exit still carries usable output.
    pub fn parse_smartctl(stdout: &str) -> Option<SmartReport> {
        let v: Value = serde_json::from_str(stdout.trim()).ok()?;
        let attributes = v["ata_smart_attributes"]["table"].as_array().map(|table| {
            let mut out = BTreeMap::new();
            for a in table {
                let (Some(id), Some(raw)) = (a["id"].as_u64(), a["raw"]["value"].as_u64()) else {
                    continue;
                };
                if u8::try_from(id).is_ok_and(|id| SMART_IDS.contains(&id)) {
                    out.entry(id.to_string()).or_insert(raw);
                }
            }
            out
        });
        let report = SmartReport {
            passed: v["smart_status"]["passed"].as_bool(),
            attributes,
            temperature_c: v["temperature"]["current"].as_i64(),
            power_on_hours: v["power_on_time"]["hours"].as_u64(),
        };
        (report != SmartReport::default()).then_some(report)
    }

    /// Whether a block device is a physical disk worth a row: not loop, RAM, zram,
    /// device-mapper, software RAID or optical.
    pub fn is_physical_block_name(name: &str) -> bool {
        ["loop", "ram", "zram", "dm-", "md", "sr"]
            .iter()
            .all(|prefix| !name.starts_with(prefix))
    }

    /// The `bus_type` of a Linux block device from its name and canonical sysfs path
    /// (`mmcblk*` are SD/MMC cards).
    pub fn linux_bus_type(name: &str, sysfs_path: &str) -> &'static str {
        if name.starts_with("nvme") {
            "NVMe"
        } else if sysfs_path.contains("/usb") {
            "USB"
        } else if name.starts_with("mmcblk") {
            "SD"
        } else if name.starts_with("sd") {
            "SATA"
        } else {
            "Unknown"
        }
    }

    /// The controller character device of an NVMe namespace block device
    /// (`nvme0n1` → `nvme0`).
    pub fn nvme_controller(name: &str) -> Option<&str> {
        let rest = name.strip_prefix("nvme")?;
        let digits = rest.bytes().take_while(u8::is_ascii_digit).count();
        let after = &rest[digits..];
        (digits > 0 && after.starts_with('n') && after.len() > 1)
            .then(|| &name[.."nvme".len() + digits])
    }

    /// The serial number in a SCSI VPD page 0x80 (`vpd_pg80`): a 4-byte header, then the
    /// ASCII serial padded with spaces or NULs.
    pub fn serial_from_vpd_pg80(bytes: &[u8]) -> Option<String> {
        if bytes.len() < 5 || bytes[1] != 0x80 {
            return None;
        }
        let len = usize::from(u16::from_be_bytes([bytes[2], bytes[3]]));
        let body = &bytes[4..4 + len.min(bytes.len() - 4)];
        let text = String::from_utf8_lossy(body)
            .trim_matches(|c: char| c.is_whitespace() || c == '\0')
            .to_string();
        (!text.is_empty()).then_some(text)
    }

    /// `media_type` from the kernel's `queue/rotational` flag.
    pub fn media_type_from_rotational(rotational: Option<&str>) -> &'static str {
        match rotational.map(str::trim) {
            Some("0") => "SSD",
            Some("1") => "HDD",
            _ => "Unspecified",
        }
    }

    /// The `nvme_error` for a disk whose driver does not pass the log through.
    pub const UNSUPPORTED: &str = "unsupported by driver";

    /// The `nvme_error` for a Win32 error from opening or querying a raw disk.
    pub fn nvme_error_from_win32(code: u32) -> String {
        match code {
            5 => "access denied".to_string(),
            // INVALID_FUNCTION, NOT_SUPPORTED, INVALID_PARAMETER, INVALID_USER_BUFFER.
            1 | 50 | 87 | 1784 => UNSUPPORTED.to_string(),
            other => format!("ioctl failed: {other}"),
        }
    }

    /// Complete a script row: map the raw `BusType` to the contract's `bus_type`, decode
    /// `smart_attributes`, read the NVMe log through `read_nvme` (the disk number in, the
    /// decoded log or an `nvme_error` out) and set `paused`. Every key of [`ROW_FIELDS`]
    /// is present afterwards. A paused read skips only `read_nvme` — the raw-device open
    /// anti-cheat coexistence guards — so `nvme` is `null` and `nvme_error` stays `null`;
    /// `predictive_failure`, `smart_attributes` and `health_status` come from the SMART
    /// WMI classes and `Get-PhysicalDisk` exactly as when not paused.
    pub fn finish_windows_row(
        mut row: Map<String, Value>,
        paused: bool,
        read_nvme: &mut dyn FnMut(u32) -> Result<NvmeHealth, String>,
    ) -> Value {
        let vendor = row.remove(SMART_DATA_FIELD);
        let bus = bus_type_name(row.get("bus_type").and_then(Value::as_str).unwrap_or(""));
        row.insert("bus_type".to_string(), json!(bus));
        // USB sticks and card readers are removable media whatever Win32_DiskDrive says.
        if matches!(bus, "USB" | "SD") {
            row.insert("removable".to_string(), json!(true));
        }
        let nvme_disk = bus == "NVMe";

        let smart_attributes = if nvme_disk {
            None
        } else {
            vendor
                .as_ref()
                .and_then(Value::as_str)
                .and_then(smart_attributes_from_vendor)
        };

        let (mut nvme, mut nvme_error) = (None, None);
        if nvme_disk && !paused {
            let number = row
                .get("device_number")
                .and_then(Value::as_u64)
                .and_then(|n| u32::try_from(n).ok());
            match number.map(read_nvme) {
                Some(Ok(health)) => nvme = Some(health),
                Some(Err(e)) => nvme_error = Some(e),
                None => nvme_error = Some("device number unavailable".to_string()),
            }
        }
        finish_row(row, smart_attributes, nvme, nvme_error, paused)
    }

    /// Set the derived keys and make every key of [`ROW_FIELDS`] present.
    pub fn finish_row(
        mut row: Map<String, Value>,
        smart_attributes: Option<BTreeMap<String, u64>>,
        nvme: Option<NvmeHealth>,
        nvme_error: Option<String>,
        paused: bool,
    ) -> Value {
        row.remove(SMART_DATA_FIELD);
        row.insert("smart_attributes".to_string(), json!(smart_attributes));
        row.insert(
            "nvme".to_string(),
            nvme.map_or(Value::Null, |h| h.to_json()),
        );
        row.insert("nvme_error".to_string(), json!(nvme_error));
        row.insert("paused".to_string(), json!(paused));
        for field in ROW_FIELDS {
            row.entry(*field).or_insert(match *field {
                "removable" => json!(false),
                _ => Value::Null,
            });
        }
        Value::Object(row)
    }

    /// What identifies a Linux disk before any raw read.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct LinuxIdentity {
        pub model: String,
        pub serial: Option<String>,
        pub bus_type: &'static str,
        pub media_type: &'static str,
        pub size_bytes: Option<u64>,
        pub removable: bool,
    }

    /// The raw reads of one Linux disk; all `None` when nothing could be read.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct LinuxReads {
        pub nvme: Option<NvmeHealth>,
        pub nvme_error: Option<String>,
        pub smart: Option<SmartReport>,
    }

    /// A Linux row. There is no OS-level health verdict, so `health_status` is `Unknown`
    /// unless `smartctl` gave one; only NVMe disks fill `read_errors_uncorrected` (from
    /// the NVMe `media_errors`) and the other error counters stay `null`.
    pub fn linux_row(id: &LinuxIdentity, reads: LinuxReads, paused: bool) -> Value {
        let smart = reads.smart.as_ref();
        let verdict = smart.and_then(|s| s.passed);
        let health = match verdict {
            Some(true) => "Healthy",
            Some(false) => "Unhealthy",
            None => "Unknown",
        };
        let nvme = reads.nvme;
        let mut row = Map::new();
        row.insert("model".to_string(), json!(id.model));
        row.insert("health_status".to_string(), json!(health));
        row.insert("predictive_failure".to_string(), json!(verdict.map(|p| !p)));
        row.insert(
            "wear".to_string(),
            json!(nvme.map(|h| u64::from(h.percentage_used).min(MAX_WEAR))),
        );
        row.insert(
            "temperature_c".to_string(),
            json!(nvme
                .and_then(|h| h.temperature_c)
                .or_else(|| smart.and_then(|s| s.temperature_c))),
        );
        row.insert(
            "power_on_hours".to_string(),
            json!(nvme
                .map(|h| h.power_on_hours)
                .or_else(|| smart.and_then(|s| s.power_on_hours))),
        );
        row.insert(
            "read_errors_uncorrected".to_string(),
            json!(nvme.map(|h| h.media_errors)),
        );
        row.insert("serial".to_string(), json!(id.serial));
        row.insert("bus_type".to_string(), json!(id.bus_type));
        row.insert("media_type".to_string(), json!(id.media_type));
        row.insert("size_bytes".to_string(), json!(id.size_bytes));
        row.insert("removable".to_string(), json!(id.removable));
        finish_row(
            row,
            smart.and_then(|s| s.attributes.clone()),
            nvme,
            reads.nvme_error,
            paused,
        )
    }

    fn model(d: &Value) -> &str {
        d.get("model").and_then(Value::as_str).unwrap_or("disk")
    }

    /// A counter as a number; `null` (not reported) counts as zero, because it
    /// is no evidence of an error.
    fn count(d: &Value, field: &str) -> u64 {
        d.get(field).and_then(Value::as_u64).unwrap_or(0)
    }

    /// Grade the rows: a failing SMART flag or a `health_status` that is neither
    /// `Healthy` nor `Unknown` (no verdict) is `crit`; otherwise any uncorrected read or
    /// write error is `warn`. `read_errors_total` never grades — it is mostly corrected
    /// errors, vendor-scaled, and large on healthy HDDs.
    pub fn grade(disks: &[Value]) -> (Status, String) {
        let failing: Vec<&str> = disks
            .iter()
            .filter(|d| {
                d.get("predictive_failure").and_then(Value::as_bool) == Some(true)
                    || d.get("health_status")
                        .and_then(Value::as_str)
                        .is_some_and(|s| {
                            !s.eq_ignore_ascii_case("Healthy") && !s.eq_ignore_ascii_case("Unknown")
                        })
            })
            .map(model)
            .collect();
        if !failing.is_empty() {
            return (
                Status::Crit,
                format!("predictive disk failure: {}", failing.join(", ")),
            );
        }

        let uncorrected: Vec<String> = disks
            .iter()
            .filter_map(|d| {
                let parts: Vec<String> = [
                    ("read", "read_errors_uncorrected"),
                    ("write", "write_errors_uncorrected"),
                ]
                .into_iter()
                .filter_map(|(label, field)| match count(d, field) {
                    0 => None,
                    n => Some(format!("{label} {n}")),
                })
                .collect();
                (!parts.is_empty()).then(|| format!("{} ({})", model(d), parts.join(", ")))
            })
            .collect();
        if !uncorrected.is_empty() {
            return (
                Status::Warn,
                format!("uncorrected errors: {}", uncorrected.join("; ")),
            );
        }

        (Status::Ok, "SMART healthy".to_string())
    }

    /// The section for already-collected rows (at most [`MAX_DISKS`]); `truncated` says
    /// the host has more disks than the rows cover.
    pub fn section_for(mut disks: Vec<Value>, truncated: bool) -> Section {
        let truncated = truncated || disks.len() > MAX_DISKS;
        disks.truncate(MAX_DISKS);
        if disks.is_empty() {
            return Section::with_fields(
                Status::Ok,
                "no physical disks listed",
                json!({ "disks": disks, "truncated": truncated }),
            );
        }
        let (status, summary) = grade(&disks);
        Section::with_fields(
            status,
            summary,
            json!({ "disks": disks, "truncated": truncated }),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use crate::telemetry::collectors::nvme::{decode_health_log, HEALTH_LOG_LEN};

        fn fixture(name: &str) -> Value {
            serde_json::from_str(
                &std::fs::read_to_string(
                    std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                        .join("../docs/fixtures")
                        .join(name),
                )
                .expect("read the fixture"),
            )
            .expect("parse the fixture")
        }

        fn sorted<'a>(keys: impl Iterator<Item = &'a str>) -> Vec<&'a str> {
            let mut keys: Vec<&str> = keys.collect();
            keys.sort_unstable();
            keys
        }

        /// A row as the (paused-free) script emits it, before the agent completes it.
        fn script_row(bus: &str, device_number: Value, vendor: Value) -> Map<String, Value> {
            let Value::Object(m) = json!({
                "model": "D1", "health_status": "Healthy", "predictive_failure": false,
                "wear": 2, "temperature_c": 41, "temperature_max_c": 74,
                "power_on_hours": 100, "read_errors_total": null,
                "read_errors_uncorrected": 0, "write_errors_uncorrected": 0,
                "device_number": device_number, "serial": "S1", "bus_type": bus,
                "media_type": "SSD", "size_bytes": 1000, "removable": false,
                "smart_data": vendor,
            }) else {
                unreachable!()
            };
            m
        }

        fn disk(health: &str, read_unc: Value, write_unc: Value, read_total: Value) -> Value {
            json!({
                "model": "D1", "health_status": health,
                "predictive_failure": health != "Healthy",
                "wear": null, "temperature_c": 30, "power_on_hours": 100,
                "read_errors_total": read_total,
                "read_errors_uncorrected": read_unc,
                "write_errors_uncorrected": write_unc,
                "device_number": 0, "serial": "S1", "bus_type": "SATA",
                "media_type": "HDD", "size_bytes": 1000, "removable": false,
                "temperature_max_c": 40, "smart_attributes": null, "nvme": null,
                "nvme_error": null, "paused": false,
            })
        }

        /// The ATA attribute block a drive returns: revision, then `entries` as
        /// `(id, raw)` in 12-byte slots, padded to 512 bytes.
        fn ata_block(entries: &[(u8, u64)]) -> Vec<u8> {
            let mut b = vec![0u8; 512];
            b[0] = 0x10;
            for (i, (id, raw)) in entries.iter().enumerate() {
                let at = 2 + i * 12;
                b[at] = *id;
                b[at + 1] = 0x33; // flags
                b[at + 3] = 100; // current
                b[at + 4] = 99; // worst
                b[at + 5..at + 11].copy_from_slice(&raw.to_le_bytes()[..6]);
                b[at + 11] = 0xEE; // reserved
            }
            b
        }

        #[test]
        fn huge_read_errors_total_alone_is_ok() {
            let (status, summary) =
                grade(&[disk("Healthy", json!(0), json!(0), json!(184_223_611u64))]);
            assert_eq!(status, Status::Ok);
            assert_eq!(summary, "SMART healthy");
        }

        #[test]
        fn uncorrected_errors_warn_and_name_the_disk() {
            let (status, summary) = grade(&[disk("Healthy", json!(2), json!(1), json!(5))]);
            assert_eq!(status, Status::Warn);
            assert_eq!(summary, "uncorrected errors: D1 (read 2, write 1)");
        }

        #[test]
        fn unreported_counters_are_not_errors() {
            let (status, _) = grade(&[disk("Healthy", Value::Null, Value::Null, Value::Null)]);
            assert_eq!(status, Status::Ok);
        }

        #[test]
        fn unhealthy_disk_is_crit_over_uncorrected_errors() {
            let (status, summary) = grade(&[disk("Warning", json!(3), json!(0), json!(3))]);
            assert_eq!(status, Status::Crit);
            assert_eq!(summary, "predictive disk failure: D1");
        }

        #[test]
        fn a_failing_smart_flag_is_crit_even_when_the_os_says_healthy() {
            let mut d = disk("Healthy", json!(0), json!(0), json!(0));
            d["predictive_failure"] = json!(true);
            assert_eq!(grade(&[d]).0, Status::Crit);
        }

        #[test]
        fn no_verdict_is_not_a_failure() {
            let mut d = disk("Unknown", json!(0), json!(0), json!(0));
            d["predictive_failure"] = Value::Null;
            assert_eq!(grade(&[d]).0, Status::Ok);
        }

        #[test]
        fn no_rows_say_so_and_the_cap_holds() {
            let empty = section_for(vec![], false).into_value();
            assert_eq!(empty["summary"], "no physical disks listed");
            assert_eq!(empty["disks"], json!([]));
            assert_eq!(empty["truncated"], false);
            let many = section_for(
                vec![disk("Healthy", json!(0), json!(0), json!(0)); 20],
                false,
            )
            .into_value();
            assert_eq!(many["disks"].as_array().map(Vec::len), Some(MAX_DISKS));
            assert_eq!(many["truncated"], true, "rows beyond the cap are dropped");
            let exact = section_for(
                vec![disk("Healthy", json!(0), json!(0), json!(0)); 16],
                false,
            )
            .into_value();
            assert_eq!(exact["truncated"], false);
            // A collector that stopped reading at the cap says so itself.
            let capped =
                section_for(vec![disk("Healthy", json!(0), json!(0), json!(0))], true).into_value();
            assert_eq!(capped["truncated"], true);
        }

        #[test]
        fn row_fields_are_the_script_fields_plus_the_derived_ones() {
            let script_keys = DISK_FIELDS
                .iter()
                .chain(COUNTERS)
                .map(|(field, _)| *field)
                .chain(["predictive_failure"])
                .chain(DERIVED_FIELDS.iter().copied());
            assert_eq!(
                sorted(script_keys),
                sorted(ROW_FIELDS.iter().copied()),
                "ROW_FIELDS and the script's field tables disagree"
            );
        }

        #[test]
        fn script_emits_every_row_field() {
            let script = script();
            for field in DISK_FIELDS
                .iter()
                .chain(COUNTERS)
                .chain(SMART_FIELDS)
                .map(|(field, _)| *field)
            {
                assert!(
                    script.contains(&format!("    {field} = ")),
                    "script lacks {field}"
                );
            }
            assert!(!script.contains("reallocated_sectors"));
            assert!(!script.contains("@@"), "unreplaced marker");
            assert!(
                !script.contains('"'),
                "double quotes break -Command quoting"
            );
        }

        #[test]
        fn the_script_always_reads_the_smart_wmi_classes() {
            let script = script();
            assert!(script.contains("MSStorageDriver_FailurePredictStatus"));
            assert!(script.contains("MSStorageDriver_FailurePredictData"));
            assert!(script.contains("PredictFailure"));
            assert!(script.contains("VendorSpecific"));
            assert!(script.contains("Get-Disk"));
            // Nothing in the script opens a raw device: that is the agent's NVMe read.
            assert!(!script.contains("PhysicalDrive"));
        }

        #[test]
        fn the_script_emits_the_raw_bus_type_for_rust_to_map() {
            let script = script();
            assert!(script.contains("$bus = [string]$_.BusType"));
            assert!(
                !script.contains("switch ([string]$_.BusType)"),
                "the mapping lives in bus_type_name, not in the script"
            );
        }

        #[test]
        fn usb_and_sd_rows_are_removable_and_the_bus_is_mapped_from_the_raw_value() {
            for (raw, bus, removable) in [
                ("7", "USB", true),
                ("USB", "USB", true),
                ("12", "SD", true),
                ("MMC", "SD", true),
                ("3", "SATA", false),
                ("1", "SCSI", false),
                ("17", "NVMe", false),
                ("Virtual", "Unknown", false),
            ] {
                let row = script_row(raw, json!(1), Value::Null);
                let v = finish_windows_row(row, false, &mut |_| Err("n/a".to_string()));
                assert_eq!(v["bus_type"], bus, "{raw}");
                assert_eq!(v["removable"], removable, "{raw}");
            }
            // Win32_DiskDrive's own removable flag still counts.
            let mut row = script_row("SATA", json!(1), Value::Null);
            row.insert("removable".to_string(), json!(true));
            let v = finish_windows_row(row, false, &mut |_| unreachable!());
            assert_eq!(v["removable"], true);
        }

        /// The contract's example rows are exactly the fields the collector
        /// emits, and grade to the fixture's own status and summary — a renamed,
        /// added or dropped field fails here, not on the dashboard.
        #[test]
        fn the_fixture_is_what_the_collector_produces() {
            let fixture = fixture("telemetry_snapshot.json");
            let expected = &fixture["snapshot"]["disk_smart"];
            let rows = expected["disks"].as_array().expect("disks").clone();
            assert!(!rows.is_empty());

            for row in &rows {
                let keys = sorted(row.as_object().expect("row").keys().map(String::as_str));
                assert_eq!(keys, sorted(ROW_FIELDS.iter().copied()));
            }

            assert_eq!(&section_for(rows, false).into_value(), expected);
        }

        /// The Windows fixture rebuilt from raw reads: the script's row, the decoded
        /// attribute block and the NVMe log produce exactly the contract's rows.
        #[test]
        fn windows_rows_are_rebuilt_from_raw_reads() {
            let fixture = fixture("telemetry_snapshot.json");
            let expected = fixture["snapshot"]["disk_smart"]["disks"]
                .as_array()
                .expect("disks")
                .clone();
            let vendor = STANDARD.encode(ata_block(&[
                (5, 0),
                (9, 8123),
                (187, 0),
                (188, 0),
                (194, 34),
                (197, 0),
                (198, 0),
                (199, 0),
            ]));

            let mut log = [0u8; HEALTH_LOG_LEN];
            log[1..3].copy_from_slice(&(41u16 + 273).to_le_bytes());
            log[3] = 100;
            log[4] = 10;
            log[5] = 2;
            log[48..56].copy_from_slice(&18_734_512u64.to_le_bytes());
            log[128..136].copy_from_slice(&1520u64.to_le_bytes());
            log[144..152].copy_from_slice(&14u64.to_le_bytes());

            let mut rows = Vec::new();
            for (i, want) in expected.iter().enumerate() {
                let Value::Object(mut base) = want.clone() else {
                    unreachable!()
                };
                for key in ["smart_attributes", "nvme", "nvme_error", "paused"] {
                    base.remove(key);
                }
                let nvme_disk = base["bus_type"] == "NVMe";
                base.insert(
                    SMART_DATA_FIELD.to_string(),
                    if nvme_disk {
                        Value::Null
                    } else {
                        json!(vendor)
                    },
                );
                let mut asked = Vec::new();
                rows.push(finish_windows_row(base, false, &mut |n| {
                    asked.push(n);
                    Ok(decode_health_log(&log))
                }));
                assert_eq!(asked.len(), usize::from(nvme_disk), "disk {i}");
            }
            assert_eq!(rows, expected);
        }

        #[test]
        fn a_paused_windows_row_skips_only_the_nvme_device_read() {
            let row = script_row("NVMe", json!(2), json!("AAAA"));
            let v = finish_windows_row(row, true, &mut |_| panic!("the device was read"));
            assert_eq!(v["paused"], true);
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(v["nvme_error"], Value::Null);
            // The WMI-sourced and reliability-counter facts are untouched.
            assert_eq!(v["predictive_failure"], false);
            assert_eq!(v["health_status"], "Healthy");
            assert_eq!(v["wear"], 2);
            assert_eq!(
                sorted(v.as_object().unwrap().keys().map(String::as_str)),
                sorted(ROW_FIELDS.iter().copied())
            );

            // A failing drive stays failing while paused.
            let mut failing = script_row(
                "SATA",
                json!(0),
                json!(STANDARD.encode(ata_block(&[(197, 4)]))),
            );
            failing.insert("predictive_failure".to_string(), json!(true));
            failing.insert("health_status".to_string(), json!("Warning"));
            let v = finish_windows_row(failing, true, &mut |_| panic!("the device was read"));
            assert_eq!(v["paused"], true);
            assert_eq!(v["predictive_failure"], true);
            assert_eq!(v["health_status"], "Warning");
            assert_eq!(v["smart_attributes"], json!({ "197": 4 }));
            assert_eq!(grade(&[v]).0, Status::Crit);
        }

        #[test]
        fn an_nvme_disk_that_cannot_be_read_says_why_and_is_never_healthy_by_default() {
            let row = script_row("NVMe", json!(3), Value::Null);
            let v = finish_windows_row(row, false, &mut |n| {
                assert_eq!(n, 3);
                Err("unsupported by driver".to_string())
            });
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(v["nvme_error"], "unsupported by driver");
            assert_eq!(v["paused"], false);

            let nameless = script_row("NVMe", Value::Null, Value::Null);
            let v = finish_windows_row(nameless, false, &mut |_| panic!("no disk number"));
            assert_eq!(v["nvme_error"], "device number unavailable");
        }

        #[test]
        fn a_sata_row_is_not_read_as_nvme_and_an_unmatched_join_stays_null() {
            let row = script_row("SATA", json!(1), Value::Null);
            let v = finish_windows_row(row, false, &mut |_| panic!("not an NVMe disk"));
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(v["nvme_error"], Value::Null);
            assert_eq!(v["smart_attributes"], Value::Null);

            let short = script_row("SATA", json!(1), json!(STANDARD.encode([0u8; 100])));
            let v = finish_windows_row(short, false, &mut |_| unreachable!());
            assert_eq!(v["smart_attributes"], Value::Null);
        }

        #[test]
        fn win32_errors_become_short_reasons() {
            assert_eq!(nvme_error_from_win32(5), "access denied");
            assert_eq!(nvme_error_from_win32(1), "unsupported by driver");
            assert_eq!(nvme_error_from_win32(50), "unsupported by driver");
            assert_eq!(nvme_error_from_win32(87), "unsupported by driver");
            assert_eq!(nvme_error_from_win32(21), "ioctl failed: 21");
        }

        #[test]
        fn ata_attributes_are_parsed_from_the_canned_buffer() {
            let block = ata_block(&[
                (1, 77),                 // not kept
                (5, 8),                  // reallocated sectors
                (9, 8123),               // not kept
                (187, 3),                // reported uncorrectable
                (188, 0x0001_0000_0002), // command timeout, bytes beyond 32 bits
                (197, 1),                // pending
                (198, 2),                // offline uncorrectable
                (199, 65_535),           // CRC
                (5, 999),                // a repeated id: the first wins
            ]);
            let attrs = parse_ata_smart_attributes(&block);
            let got: Vec<(&str, u64)> = attrs.iter().map(|(k, v)| (k.as_str(), *v)).collect();
            assert_eq!(
                got,
                vec![
                    ("187", 3),
                    ("188", 0x0001_0000_0002),
                    ("197", 1),
                    ("198", 2),
                    ("199", 65_535),
                    ("5", 8),
                ]
            );
        }

        #[test]
        fn ata_raw_uses_exactly_six_little_endian_bytes() {
            let mut block = ata_block(&[(5, 0)]);
            block[2 + 5..2 + 11].copy_from_slice(&[1, 2, 3, 4, 5, 6]);
            block[2 + 11] = 0xFF; // reserved byte must not leak into the value
            assert_eq!(
                parse_ata_smart_attributes(&block).get("5"),
                Some(&0x0000_0605_0403_0201)
            );
        }

        #[test]
        fn ata_parse_survives_garbage() {
            assert!(parse_ata_smart_attributes(&[]).is_empty());
            assert!(parse_ata_smart_attributes(&[0x10]).is_empty());
            assert!(parse_ata_smart_attributes(&[0x10, 0, 5, 0, 0]).is_empty());
            // A 30-entry limit: an id in the trailing bytes is not an entry.
            let mut block = ata_block(&[]);
            block.resize(2 + 30 * 12 + 12, 0);
            let at = 2 + 30 * 12;
            block[at] = 5;
            assert!(parse_ata_smart_attributes(&block).is_empty());
        }

        #[test]
        fn vendor_data_is_decoded_from_base64_and_validated() {
            let block = ata_block(&[(197, 4)]);
            let attrs = smart_attributes_from_vendor(&STANDARD.encode(&block)).unwrap();
            assert_eq!(attrs.get("197"), Some(&4));
            assert!(smart_attributes_from_vendor("not base64!!").is_none());
            assert!(smart_attributes_from_vendor("").is_none());
            assert!(smart_attributes_from_vendor(&STANDARD.encode(&block[..100])).is_none());
        }

        #[test]
        fn smartctl_json_maps_the_verdict_and_attributes() {
            let out = r#"{
              "smartctl": {"exit_status": 64},
              "smart_status": {"passed": false},
              "temperature": {"current": 38},
              "power_on_time": {"hours": 21457},
              "ata_smart_attributes": {"table": [
                {"id": 1, "name": "Raw_Read_Error_Rate", "raw": {"value": 184223611, "string": "x"}},
                {"id": 5, "name": "Reallocated_Sector_Ct", "raw": {"value": 8, "string": "8"}},
                {"id": 197, "name": "Current_Pending_Sector", "raw": {"value": 2, "string": "2"}},
                {"id": 199, "raw": {"value": 0}}
              ]}
            }"#;
            let r = parse_smartctl(out).expect("report");
            assert_eq!(r.passed, Some(false));
            assert_eq!(r.temperature_c, Some(38));
            assert_eq!(r.power_on_hours, Some(21457));
            let attrs = r.attributes.expect("attributes");
            assert_eq!(attrs.len(), 3);
            assert_eq!(attrs["5"], 8);
            assert_eq!(attrs["197"], 2);
            assert_eq!(attrs["199"], 0);

            let passed = parse_smartctl(r#"{"smart_status": {"passed": true}}"#).unwrap();
            assert_eq!(passed.passed, Some(true));
            assert_eq!(passed.attributes, None);
        }

        #[test]
        fn smartctl_output_without_a_finding_is_no_report() {
            assert_eq!(parse_smartctl(""), None);
            assert_eq!(parse_smartctl("smartctl: command not found"), None);
            // Device could not be opened: smartctl prints messages only.
            assert_eq!(
                parse_smartctl(r#"{"smartctl": {"exit_status": 2, "messages": []}}"#),
                None
            );
        }

        fn sata_identity() -> LinuxIdentity {
            LinuxIdentity {
                model: "ST2000DM008-2FR102".to_string(),
                serial: Some("WFL0ABCD".to_string()),
                bus_type: "SATA",
                media_type: "HDD",
                size_bytes: Some(2_000_398_934_016),
                removable: false,
            }
        }

        #[test]
        fn a_linux_sata_row_takes_the_smartctl_verdict() {
            let reads = LinuxReads {
                smart: parse_smartctl(
                    r#"{"smart_status":{"passed":false},"temperature":{"current":40},
                        "ata_smart_attributes":{"table":[{"id":5,"raw":{"value":3}}]}}"#,
                ),
                ..LinuxReads::default()
            };
            let v = linux_row(&sata_identity(), reads, false);
            assert_eq!(v["health_status"], "Unhealthy");
            assert_eq!(v["predictive_failure"], true);
            assert_eq!(v["smart_attributes"], json!({ "5": 3 }));
            assert_eq!(v["temperature_c"], 40);
            assert_eq!(v["read_errors_uncorrected"], Value::Null);
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(grade(&[v]).0, Status::Crit);

            let healthy = LinuxReads {
                smart: parse_smartctl(r#"{"smart_status":{"passed":true}}"#),
                ..LinuxReads::default()
            };
            let v = linux_row(&sata_identity(), healthy, false);
            assert_eq!(v["health_status"], "Healthy");
            assert_eq!(v["predictive_failure"], false);
            assert_eq!(v["smart_attributes"], Value::Null);
        }

        #[test]
        fn wear_is_clamped_to_one_hundred_while_the_nvme_log_stays_raw() {
            let id = LinuxIdentity {
                bus_type: "NVMe",
                media_type: "SSD",
                ..sata_identity()
            };
            for (used, wear) in [(0u8, 0u64), (99, 99), (100, 100), (101, 100), (255, 100)] {
                let mut log = [0u8; HEALTH_LOG_LEN];
                log[5] = used;
                let reads = LinuxReads {
                    nvme: Some(decode_health_log(&log)),
                    ..LinuxReads::default()
                };
                let v = linux_row(&id, reads, false);
                assert_eq!(v["wear"], wear, "used {used}");
                assert_eq!(v["nvme"]["percentage_used"], used, "used {used}");
            }
        }

        #[test]
        fn a_linux_row_without_smartctl_is_unknown_not_healthy() {
            let v = linux_row(&sata_identity(), LinuxReads::default(), false);
            assert_eq!(v["health_status"], "Unknown");
            assert_eq!(v["predictive_failure"], Value::Null);
            assert_eq!(v["smart_attributes"], Value::Null);
            assert_eq!(v["paused"], false);
            assert_eq!(grade(&[v]).0, Status::Ok);
        }

        #[test]
        fn the_linux_fixture_is_what_the_collector_produces() {
            let fixture = fixture("telemetry_snapshot_linux.json");
            let expected = &fixture["snapshot"]["disk_smart"];
            let want = &expected["disks"][0];

            let mut log = [0u8; HEALTH_LOG_LEN];
            log[1..3].copy_from_slice(&(38u16 + 273).to_le_bytes());
            log[3] = 100;
            log[4] = 10;
            log[5] = 4;
            log[48..56].copy_from_slice(&52_118_340u64.to_le_bytes());
            log[128..136].copy_from_slice(&6402u64.to_le_bytes());
            log[144..152].copy_from_slice(&31u64.to_le_bytes());
            let id = LinuxIdentity {
                model: "Samsung SSD 980 PRO 1TB".to_string(),
                serial: Some("S5GXNX0T123456A".to_string()),
                bus_type: "NVMe",
                media_type: "SSD",
                size_bytes: Some(1_000_204_886_016),
                removable: false,
            };
            let reads = LinuxReads {
                nvme: Some(decode_health_log(&log)),
                ..LinuxReads::default()
            };
            let row = linux_row(&id, reads, false);
            assert_eq!(&row, want);
            assert_eq!(&section_for(vec![row], false).into_value(), expected);
        }

        #[test]
        fn a_paused_or_unreadable_linux_nvme_row_has_no_log() {
            let id = LinuxIdentity {
                bus_type: "NVMe",
                media_type: "SSD",
                ..sata_identity()
            };
            let v = linux_row(&id, LinuxReads::default(), true);
            assert_eq!(v["paused"], true);
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(v["wear"], Value::Null);
            assert_eq!(v["read_errors_uncorrected"], Value::Null);

            let denied = LinuxReads {
                nvme_error: Some("access denied".to_string()),
                ..LinuxReads::default()
            };
            let v = linux_row(&id, denied, false);
            assert_eq!(v["nvme"], Value::Null);
            assert_eq!(v["nvme_error"], "access denied");
            assert_eq!(v["health_status"], "Unknown");
        }

        #[test]
        fn block_devices_are_filtered_and_classified() {
            for name in ["loop0", "ram0", "zram0", "dm-0", "md127", "sr0"] {
                assert!(!is_physical_block_name(name), "{name}");
            }
            for name in ["sda", "nvme0n1", "vda", "mmcblk0"] {
                assert!(is_physical_block_name(name), "{name}");
            }
            assert_eq!(
                linux_bus_type("nvme0n1", "/sys/devices/pci0000:00/x"),
                "NVMe"
            );
            assert_eq!(
                linux_bus_type("sdb", "/sys/devices/pci0000:00/0000:00:14.0/usb2/2-1/host6/target6:0:0/6:0:0:0/block/sdb"),
                "USB"
            );
            assert_eq!(
                linux_bus_type("sda", "/sys/devices/pci0000:00/ata1/host0/block/sda"),
                "SATA"
            );
            assert_eq!(
                linux_bus_type("vda", "/sys/devices/virtio-pci/virtio2/block/vda"),
                "Unknown"
            );
            assert_eq!(
                linux_bus_type(
                    "mmcblk0",
                    "/sys/devices/pci0000:00/0000:00:1a.0/mmc_host/mmc0/block/mmcblk0"
                ),
                "SD"
            );
        }

        #[test]
        fn the_nvme_controller_is_derived_from_the_namespace() {
            assert_eq!(nvme_controller("nvme0n1"), Some("nvme0"));
            assert_eq!(nvme_controller("nvme12n3"), Some("nvme12"));
            assert_eq!(nvme_controller("nvme0"), None);
            assert_eq!(nvme_controller("nvmen1"), None);
            assert_eq!(nvme_controller("sda"), None);
        }

        #[test]
        fn vpd_pg80_serials_are_trimmed() {
            let mut page = vec![0x00, 0x80, 0x00, 0x08];
            page.extend_from_slice(b"  WD1234 ");
            page.truncate(4 + 8);
            assert_eq!(serial_from_vpd_pg80(&page).as_deref(), Some("WD1234"));
            let mut nul = vec![0x00, 0x80, 0x00, 0x04];
            nul.extend_from_slice(b"AB\0\0");
            assert_eq!(serial_from_vpd_pg80(&nul).as_deref(), Some("AB"));
            // A length claiming more than the buffer holds is clamped, not a panic.
            let mut long = vec![0x00, 0x80, 0xFF, 0xFF];
            long.extend_from_slice(b"XYZ");
            assert_eq!(serial_from_vpd_pg80(&long).as_deref(), Some("XYZ"));
            assert_eq!(serial_from_vpd_pg80(&[0x00, 0x83, 0x00, 0x04, b'A']), None);
            assert_eq!(serial_from_vpd_pg80(&[]), None);
            assert_eq!(serial_from_vpd_pg80(&[0, 0x80, 0, 2, b' ', 0]), None);
        }

        #[test]
        fn rotational_decides_the_media_type() {
            assert_eq!(media_type_from_rotational(Some("0\n")), "SSD");
            assert_eq!(media_type_from_rotational(Some("1")), "HDD");
            assert_eq!(media_type_from_rotational(Some("x")), "Unspecified");
            assert_eq!(media_type_from_rotational(None), "Unspecified");
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use std::ffi::c_void;
    use std::mem::{offset_of, size_of};

    use serde_json::Value;
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, GENERIC_READ, HANDLE};
    use windows::Win32::Storage::FileSystem::{
        CreateFileW, FILE_FLAGS_AND_ATTRIBUTES, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    use windows::Win32::System::Ioctl::{
        NVMeDataTypeLogPage, PropertyStandardQuery, ProtocolTypeNvme,
        StorageDeviceProtocolSpecificProperty, IOCTL_STORAGE_QUERY_PROPERTY,
        STORAGE_PROPERTY_QUERY, STORAGE_PROTOCOL_DATA_DESCRIPTOR, STORAGE_PROTOCOL_SPECIFIC_DATA,
    };
    use windows::Win32::System::IO::DeviceIoControl;

    use super::core;
    use crate::telemetry::collectors::nvme::{self, NvmeHealth, HEALTH_LOG_ID, HEALTH_LOG_LEN};
    use crate::telemetry::collectors::winps;
    use crate::telemetry::Section;

    pub fn collect() -> Section {
        // Pausing skips only the raw `\\.\PhysicalDriveN` open of the NVMe health log; the
        // WMI classes and `Get-PhysicalDisk` are read as always.
        let paused = crate::coexist::game_active();
        let Some(v) = winps::run_json(&core::script()) else {
            return Section::with_fields(
                crate::protocol::Status::Ok,
                "SMART unavailable",
                serde_json::json!({ "disks": [], "truncated": false }),
            );
        };
        let objects: Vec<_> = winps::as_array(v)
            .into_iter()
            .filter_map(|row| match row {
                Value::Object(m) => Some(m),
                _ => None,
            })
            .collect();
        let truncated = objects.len() > core::MAX_DISKS;
        let rows = objects
            .into_iter()
            .take(core::MAX_DISKS)
            .map(|row| core::finish_windows_row(row, paused, &mut read_nvme_health))
            .collect();
        core::section_for(rows, truncated)
    }

    /// Closes the raw-disk handle.
    struct Device(HANDLE);

    impl Drop for Device {
        fn drop(&mut self) {
            // SAFETY: a handle `CreateFileW` returned, closed once.
            unsafe {
                let _ = CloseHandle(self.0);
            }
        }
    }

    fn open(path: &[u16], access: u32) -> windows::core::Result<Device> {
        // SAFETY: `path` is a valid NUL-terminated wide string. Never a write access.
        unsafe {
            CreateFileW(
                PCWSTR(path.as_ptr()),
                access,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAGS_AND_ATTRIBUTES(0),
                None,
            )
        }
        .map(Device)
    }

    /// The Win32 error behind a `windows` error (an `HRESULT_FROM_WIN32` value), or the
    /// raw HRESULT for anything else.
    fn win32_code(e: &windows::core::Error) -> u32 {
        let hr = e.code().0 as u32;
        if hr >> 16 == 0x8007 {
            hr & 0xFFFF
        } else {
            hr
        }
    }

    const QUERY_OFFSET: usize = offset_of!(STORAGE_PROPERTY_QUERY, AdditionalParameters);
    const BUFFER_LEN: usize =
        QUERY_OFFSET + size_of::<STORAGE_PROTOCOL_SPECIFIC_DATA>() + HEALTH_LOG_LEN;

    /// Read the NVMe SMART / health log of `\\.\PhysicalDrive{device_number}` with
    /// `IOCTL_STORAGE_QUERY_PROPERTY`. The disk is opened with access 0 (falling back to
    /// `GENERIC_READ`) and never for writing. Behind a RAID/VMD driver or a USB bridge the
    /// query fails or comes back malformed: that is an `Err`, never a zeroed log.
    pub fn read_nvme_health(device_number: u32) -> Result<NvmeHealth, String> {
        let path: Vec<u16> = format!(r"\\.\PhysicalDrive{device_number}")
            .encode_utf16()
            .chain(Some(0))
            .collect();
        let device = open(&path, 0)
            .or_else(|_| open(&path, GENERIC_READ.0))
            .map_err(|e| core::nvme_error_from_win32(win32_code(&e)))?;

        // 8-byte aligned, zeroed; the same buffer carries the query in and the answer out.
        let mut buffer = vec![0u64; BUFFER_LEN.div_ceil(8)];
        let base = buffer.as_mut_ptr().cast::<u8>();
        // SAFETY: `buffer` holds BUFFER_LEN zeroed bytes at 8-byte alignment, which covers
        // the query header and the protocol-specific data at QUERY_OFFSET.
        unsafe {
            let query = base.cast::<STORAGE_PROPERTY_QUERY>();
            (*query).PropertyId = StorageDeviceProtocolSpecificProperty;
            (*query).QueryType = PropertyStandardQuery;
            base.add(QUERY_OFFSET)
                .cast::<STORAGE_PROTOCOL_SPECIFIC_DATA>()
                .write(STORAGE_PROTOCOL_SPECIFIC_DATA {
                    ProtocolType: ProtocolTypeNvme,
                    DataType: NVMeDataTypeLogPage.0 as u32,
                    ProtocolDataRequestValue: u32::from(HEALTH_LOG_ID),
                    ProtocolDataRequestSubValue: 0,
                    ProtocolDataOffset: size_of::<STORAGE_PROTOCOL_SPECIFIC_DATA>() as u32,
                    ProtocolDataLength: HEALTH_LOG_LEN as u32,
                    FixedProtocolReturnData: 0,
                    ProtocolDataRequestSubValue2: 0,
                    ProtocolDataRequestSubValue3: 0,
                    ProtocolDataRequestSubValue4: 0,
                });
        }

        let mut returned = 0u32;
        // SAFETY: input and output are the same BUFFER_LEN-byte buffer; `device` is open.
        let ok = unsafe {
            DeviceIoControl(
                device.0,
                IOCTL_STORAGE_QUERY_PROPERTY,
                Some(base.cast::<c_void>().cast_const()),
                BUFFER_LEN as u32,
                Some(base.cast::<c_void>()),
                BUFFER_LEN as u32,
                Some(&mut returned),
                None,
            )
        };
        if let Err(e) = ok {
            return Err(core::nvme_error_from_win32(win32_code(&e)));
        }

        // SAFETY: the buffer is BUFFER_LEN initialised bytes at 8-byte alignment.
        let bytes = unsafe { std::slice::from_raw_parts(base.cast_const(), BUFFER_LEN) };
        let descriptor_len = size_of::<STORAGE_PROTOCOL_DATA_DESCRIPTOR>() as u32;
        // SAFETY: as above; the descriptor's fixed part fits in the buffer.
        let descriptor = unsafe { &*base.cast_const().cast::<STORAGE_PROTOCOL_DATA_DESCRIPTOR>() };
        let data = &descriptor.ProtocolSpecificData;
        let log_at = offset_of!(STORAGE_PROTOCOL_DATA_DESCRIPTOR, ProtocolSpecificData)
            + data.ProtocolDataOffset as usize;
        let well_formed = descriptor.Version == descriptor_len
            && descriptor.Size == descriptor_len
            && data.ProtocolDataOffset as usize >= size_of::<STORAGE_PROTOCOL_SPECIFIC_DATA>()
            && data.ProtocolDataLength as usize >= HEALTH_LOG_LEN
            && log_at + HEALTH_LOG_LEN <= BUFFER_LEN
            && returned as usize >= log_at + HEALTH_LOG_LEN;
        if !well_formed {
            return Err(core::UNSUPPORTED.to_string());
        }
        nvme::decode_health_slice(&bytes[log_at..]).ok_or_else(|| core::UNSUPPORTED.to_string())
    }
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use std::fs;
    use std::os::fd::AsRawFd;
    use std::path::Path;

    use super::core::{self, LinuxIdentity, LinuxReads};
    use crate::coexist;
    use crate::telemetry::collectors::nvme::{self, NvmeHealth, HEALTH_LOG_ID, HEALTH_LOG_LEN};
    use crate::telemetry::collectors::proc::{self, PROBE_BUDGET};
    use crate::telemetry::Section;

    const SYS_BLOCK: &str = "/sys/block";

    pub fn collect() -> Section {
        let paused = coexist::game_active();
        let root = Path::new(SYS_BLOCK);
        let mut smartctl_usable = true;
        let mut rows = Vec::new();
        let names = list_block_devices(root);
        let truncated = names.len() > core::MAX_DISKS;
        for name in names.into_iter().take(core::MAX_DISKS) {
            let identity = read_identity(root, &name);
            let mut reads = LinuxReads::default();
            // Pausing skips only the raw NVMe admin ioctl, as on Windows.
            if identity.bus_type == "NVMe" {
                if !paused {
                    let log = core::nvme_controller(&name)
                        .ok_or_else(|| core::UNSUPPORTED.to_string())
                        .and_then(read_nvme_health);
                    match log {
                        Ok(health) => reads.nvme = Some(health),
                        Err(e) => reads.nvme_error = Some(e),
                    }
                }
            } else if identity.bus_type == "SATA" && smartctl_usable {
                let dev = format!("/dev/{name}");
                match proc::run("smartctl", &["--json", "-H", "-A", &dev], PROBE_BUDGET) {
                    // smartctl signals findings through non-zero exit bits, so the
                    // exit code is not consulted: stdout is the answer.
                    Ok(out) => reads.smart = core::parse_smartctl(&out.stdout),
                    // Not installed, or wedged: stop asking for the remaining disks.
                    Err(_) => smartctl_usable = false,
                }
            }
            rows.push(core::linux_row(&identity, reads, paused));
        }
        core::section_for(rows, truncated)
    }

    /// Physical block devices under `root` (`/sys/block`), sorted by name.
    pub fn list_block_devices(root: &Path) -> Vec<String> {
        let mut names: Vec<String> = fs::read_dir(root)
            .into_iter()
            .flatten()
            .flatten()
            .filter_map(|e| e.file_name().into_string().ok())
            .filter(|n| core::is_physical_block_name(n))
            .collect();
        names.sort();
        names
    }

    fn read_trimmed(path: impl AsRef<Path>) -> Option<String> {
        let text = fs::read_to_string(path).ok()?;
        let text = text.trim();
        (!text.is_empty()).then(|| text.to_string())
    }

    /// Identity of `root/name` from sysfs.
    pub fn read_identity(root: &Path, name: &str) -> LinuxIdentity {
        let dev = root.join(name);
        let serial = read_trimmed(dev.join("device/serial")).or_else(|| {
            fs::read(dev.join("device/vpd_pg80"))
                .ok()
                .and_then(|b| core::serial_from_vpd_pg80(&b))
        });
        let canonical = fs::canonicalize(&dev)
            .map(|p| p.to_string_lossy().into_owned())
            .unwrap_or_default();
        LinuxIdentity {
            model: read_trimmed(dev.join("device/model")).unwrap_or_else(|| name.to_string()),
            serial,
            bus_type: core::linux_bus_type(name, &canonical),
            media_type: core::media_type_from_rotational(
                read_trimmed(dev.join("queue/rotational")).as_deref(),
            ),
            size_bytes: read_trimmed(dev.join("size"))
                .and_then(|s| s.parse::<u64>().ok())
                .and_then(|sectors| sectors.checked_mul(512)),
            removable: read_trimmed(dev.join("removable")).as_deref() == Some("1"),
        }
    }

    /// `struct nvme_admin_cmd` of `<linux/nvme_ioctl.h>`.
    #[repr(C)]
    #[derive(Default)]
    struct NvmeAdminCmd {
        opcode: u8,
        flags: u8,
        rsvd1: u16,
        nsid: u32,
        cdw2: u32,
        cdw3: u32,
        metadata: u64,
        addr: u64,
        metadata_len: u32,
        data_len: u32,
        cdw10: u32,
        cdw11: u32,
        cdw12: u32,
        cdw13: u32,
        cdw14: u32,
        cdw15: u32,
        timeout_ms: u32,
        result: u32,
    }

    const _: () = assert!(std::mem::size_of::<NvmeAdminCmd>() == 72);

    /// `_IOWR(ty, nr, size)` in the generic Linux encoding (x86, arm, riscv).
    const fn iowr(ty: u8, nr: u8, size: usize) -> u32 {
        (3 << 30) | ((size as u32) << 16) | ((ty as u32) << 8) | nr as u32
    }

    /// `NVME_IOCTL_ADMIN_CMD` = `_IOWR('N', 0x41, struct nvme_admin_cmd)`.
    const NVME_IOCTL_ADMIN_CMD: u32 = iowr(b'N', 0x41, std::mem::size_of::<NvmeAdminCmd>());

    /// The Get Log Page admin opcode.
    const NVME_ADMIN_GET_LOG_PAGE: u8 = 0x02;

    /// Retain Asynchronous Event: reading the log must not clear the controller's event.
    const GET_LOG_RAE: u32 = 1 << 15;

    fn errno_message(errno: i32) -> String {
        match errno {
            libc::EACCES | libc::EPERM => "access denied".to_string(),
            libc::ENOTTY | libc::EINVAL | libc::EOPNOTSUPP | libc::ENODEV => {
                core::UNSUPPORTED.to_string()
            }
            other => format!("ioctl failed: {other}"),
        }
    }

    /// Read the SMART / health log of the controller device `/dev/{controller}` with the
    /// admin passthrough ioctl (read-only open; the kernel wants privilege for it).
    pub fn read_nvme_health(controller: &str) -> Result<NvmeHealth, String> {
        let file = fs::File::open(format!("/dev/{controller}"))
            .map_err(|e| errno_message(e.raw_os_error().unwrap_or(0)))?;
        let mut log = [0u8; HEALTH_LOG_LEN];
        let mut cmd = NvmeAdminCmd {
            opcode: NVME_ADMIN_GET_LOG_PAGE,
            nsid: 0xFFFF_FFFF,
            addr: log.as_mut_ptr() as u64,
            data_len: HEALTH_LOG_LEN as u32,
            cdw10: ((HEALTH_LOG_LEN as u32 / 4 - 1) << 16) | GET_LOG_RAE | u32::from(HEALTH_LOG_ID),
            ..NvmeAdminCmd::default()
        };
        // SAFETY: `cmd` is a valid `nvme_admin_cmd` and `addr` points at `log`, which
        // outlives the call and is `data_len` bytes; the fd is open.
        let ret = unsafe {
            libc::ioctl(
                file.as_raw_fd(),
                NVME_IOCTL_ADMIN_CMD as _,
                &mut cmd as *mut NvmeAdminCmd,
            )
        };
        match ret {
            0 => Ok(nvme::decode_health_log(&log)),
            r if r < 0 => Err(errno_message(
                std::io::Error::last_os_error().raw_os_error().unwrap_or(0),
            )),
            status => Err(format!("ioctl failed: nvme status {status}")),
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn scratch(tag: &str) -> std::path::PathBuf {
            let dir = std::env::temp_dir().join(format!(
                "kenny-disk-smart-{tag}-{}-{}",
                std::process::id(),
                std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0, |d| d.as_nanos())
            ));
            fs::create_dir_all(&dir).unwrap();
            dir
        }

        #[test]
        fn the_admin_ioctl_matches_the_kernel_header() {
            assert_eq!(std::mem::size_of::<NvmeAdminCmd>(), 72);
            assert_eq!(NVME_IOCTL_ADMIN_CMD, 0xC048_4E41);
        }

        #[test]
        fn identity_is_read_from_sysfs() {
            let root = scratch("identity");
            let sda = root.join("sda");
            fs::create_dir_all(sda.join("device")).unwrap();
            fs::create_dir_all(sda.join("queue")).unwrap();
            fs::write(sda.join("device/model"), "ST2000DM008-2FR1\n").unwrap();
            fs::write(sda.join("size"), "3907029168\n").unwrap();
            fs::write(sda.join("removable"), "0\n").unwrap();
            fs::write(sda.join("queue/rotational"), "1\n").unwrap();
            let mut vpd = vec![0x00, 0x80, 0x00, 0x08];
            vpd.extend_from_slice(b"WFL0ABCD");
            fs::write(sda.join("device/vpd_pg80"), vpd).unwrap();

            let id = read_identity(&root, "sda");
            assert_eq!(id.model, "ST2000DM008-2FR1");
            assert_eq!(id.serial.as_deref(), Some("WFL0ABCD"));
            assert_eq!(id.bus_type, "SATA");
            assert_eq!(id.media_type, "HDD");
            assert_eq!(id.size_bytes, Some(3_907_029_168 * 512));
            assert!(!id.removable);

            // An NVMe controller exposes `serial` directly, which wins.
            let nvme0 = root.join("nvme0n1");
            fs::create_dir_all(nvme0.join("device")).unwrap();
            fs::write(nvme0.join("device/serial"), "S5GXNX0T123456A  \n").unwrap();
            fs::write(nvme0.join("removable"), "1\n").unwrap();
            let id = read_identity(&root, "nvme0n1");
            assert_eq!(id.serial.as_deref(), Some("S5GXNX0T123456A"));
            assert_eq!(id.bus_type, "NVMe");
            assert_eq!(id.model, "nvme0n1");
            assert_eq!(id.media_type, "Unspecified");
            assert_eq!(id.size_bytes, None);
            assert!(id.removable);

            let _ = fs::remove_dir_all(&root);
        }

        #[test]
        fn pseudo_devices_are_not_listed() {
            let root = scratch("list");
            for n in ["sda", "loop0", "zram0", "dm-0", "md0", "sr0", "nvme0n1"] {
                fs::create_dir_all(root.join(n)).unwrap();
            }
            assert_eq!(list_block_devices(&root), ["nvme0n1", "sda"]);
            let _ = fs::remove_dir_all(&root);
            assert!(list_block_devices(&root).is_empty());
        }

        #[test]
        fn errno_values_become_short_reasons() {
            assert_eq!(errno_message(libc::EACCES), "access denied");
            assert_eq!(errno_message(libc::EPERM), "access denied");
            assert_eq!(errno_message(libc::ENOTTY), "unsupported by driver");
            assert_eq!(
                errno_message(libc::EIO),
                format!("ioctl failed: {}", libc::EIO)
            );
        }

        #[test]
        fn a_missing_controller_device_is_an_error_not_a_panic() {
            assert!(read_nvme_health("kenny-no-such-controller").is_err());
        }
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
