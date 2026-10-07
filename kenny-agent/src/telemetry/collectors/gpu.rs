//! `gpu` section — graphics adapter inventory and raw health facts.
//!
//! Row shape: `docs/protocol.md` § Telemetry sections (`gpu`). The agent reports facts
//! and never grades: `status` is always `ok`, thresholds are the server's call.
//!
//! Facts come from up to three sources that are merged per adapter:
//!   * identity — `Win32_VideoController` on Windows (`wmi`), `/sys/class/drm` on Linux
//!     (`sysfs`);
//!   * `nvidia-smi` on both OSes (`nvidia-smi`), queried in separate field groups so an
//!     unknown field in one group (an older driver, a GeForce card) cannot take the
//!     others down with it;
//!   * amdgpu's sysfs/hwmon files on Linux (`sysfs`).
//!
//! Adapters that Windows lists but that are not graphics hardware are skipped: any
//! controller whose name starts with `Microsoft Basic Display` or `Microsoft Remote
//! Display` (the fallback and RDP display drivers). Hyper-V, VMware and other virtual
//! GPUs are real adapters from the guest's point of view and are reported.
//!
//! All parsing and merging is pure and portable; only the thin probes at the bottom of
//! each platform module touch the OS.

use std::collections::BTreeMap;

use serde_json::{json, Map, Value};

use super::nvidia::{self, Row};
use crate::protocol::Status;
use crate::telemetry::Section;

/// At most this many GPUs are reported; the rest set `truncated`.
const MAX_GPUS: usize = 8;

/// At most this many amdgpu RAS blocks are reported per GPU.
const MAX_RAS_BLOCKS: usize = 16;

/// Collect the `gpu` section.
pub fn collect() -> Section {
    let mut errors: Vec<String> = Vec::new();

    #[cfg(windows)]
    let identities = wmi::probe(&mut errors);
    #[cfg(not(windows))]
    let identities = sysfs::probe(std::path::Path::new(sysfs::DRM_ROOT));

    let wants_nvidia_smi = identities.iter().any(Gpu::expects_nvidia_smi);
    let nv = collect_nvidia(&|fields| nvidia::query(fields));
    if nv.is_none() && wants_nvidia_smi {
        errors.push("nvidia-smi: no usable output".to_string());
    }

    let (gpus, truncated) = assemble(identities, nv.unwrap_or_default());
    section_from(&gpus, truncated, errors)
}

/// The section for already merged GPUs.
fn section_from(gpus: &[Gpu], truncated: bool, errors: Vec<String>) -> Section {
    Section::with_fields(
        Status::Ok,
        format!("{} GPU(s)", gpus.len()),
        json!({
            "gpus": gpus.iter().map(Gpu::to_json).collect::<Vec<_>>(),
            "truncated": truncated,
            "errors": errors,
        }),
    )
}

// ---------------------------------------------------------------------------------
// Model
// ---------------------------------------------------------------------------------

/// PCIe link facts; any member may be unreported.
#[derive(Debug, Clone, Default, PartialEq)]
struct Pcie {
    gen_current: Option<u64>,
    gen_max: Option<u64>,
    width_current: Option<u64>,
    width_max: Option<u64>,
}

impl Pcie {
    fn is_empty(&self) -> bool {
        *self == Pcie::default()
    }

    fn to_json(&self) -> Value {
        json!({
            "gen_current": self.gen_current,
            "gen_max": self.gen_max,
            "width_current": self.width_current,
            "width_max": self.width_max,
        })
    }
}

/// The four clock-event reasons; any member may be unreported.
#[derive(Debug, Clone, Default, PartialEq)]
struct Throttle {
    hw_slowdown: Option<bool>,
    hw_thermal_slowdown: Option<bool>,
    hw_power_brake_slowdown: Option<bool>,
    sw_thermal_slowdown: Option<bool>,
}

impl Throttle {
    fn is_empty(&self) -> bool {
        *self == Throttle::default()
    }

    fn to_json(&self) -> Value {
        json!({
            "hw_slowdown": self.hw_slowdown,
            "hw_thermal_slowdown": self.hw_thermal_slowdown,
            "hw_power_brake_slowdown": self.hw_power_brake_slowdown,
            "sw_thermal_slowdown": self.sw_thermal_slowdown,
        })
    }
}

/// Row-remapping counters (Ampere and newer datacenter parts).
#[derive(Debug, Clone, Default, PartialEq)]
struct Remapped {
    correctable: Option<u64>,
    uncorrectable: Option<u64>,
    pending: Option<bool>,
    failure: Option<bool>,
}

impl Remapped {
    fn is_empty(&self) -> bool {
        *self == Remapped::default()
    }
}

/// ECC / retirement facts; `None` on the GPU when the card reports none of them.
#[derive(Debug, Clone, Default, PartialEq)]
struct Ecc {
    uncorrected_volatile: Option<u64>,
    retired_pages_pending: Option<bool>,
    remapped_rows: Option<Remapped>,
}

impl Ecc {
    fn is_empty(&self) -> bool {
        self.uncorrected_volatile.is_none()
            && self.retired_pages_pending.is_none()
            && self.remapped_rows.is_none()
    }

    fn to_json(&self) -> Value {
        let remapped = self.remapped_rows.as_ref().map(|r| {
            json!({
                "correctable": r.correctable,
                "uncorrectable": r.uncorrectable,
                "pending": r.pending,
                "failure": r.failure,
            })
        });
        json!({
            "uncorrected_volatile": self.uncorrected_volatile,
            "retired_pages_pending": self.retired_pages_pending,
            "remapped_rows": remapped,
        })
    }
}

/// amdgpu RAS error counts of one block.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct RasCounts {
    ue: u64,
    ce: u64,
}

/// One adapter, as the contract describes it.
#[derive(Debug, Clone, PartialEq)]
struct Gpu {
    name: String,
    vendor: &'static str,
    pci_id: Option<String>,
    bus_id: Option<String>,
    uuid: Option<String>,
    driver_version: Option<String>,
    /// Kernel driver bound to the device (Linux); never emitted.
    driver: Option<String>,
    sources: Vec<&'static str>,
    temperature_c: Option<f64>,
    utilization_percent: Option<f64>,
    power_draw_w: Option<f64>,
    power_limit_w: Option<f64>,
    fan_target_percent: Option<f64>,
    pcie: Option<Pcie>,
    throttle: Option<Throttle>,
    ecc: Option<Ecc>,
    ras: Option<BTreeMap<String, RasCounts>>,
}

impl Gpu {
    /// An adapter known by identity only.
    fn identity(source: &'static str, name: String, vendor: &'static str) -> Self {
        Gpu {
            name,
            vendor,
            pci_id: None,
            bus_id: None,
            uuid: None,
            driver_version: None,
            driver: None,
            sources: vec![source],
            temperature_c: None,
            utilization_percent: None,
            power_draw_w: None,
            power_limit_w: None,
            fan_target_percent: None,
            pcie: None,
            throttle: None,
            ecc: None,
            ras: None,
        }
    }

    /// Whether `nvidia-smi` should be able to see this adapter: an NVIDIA device driven
    /// by the proprietary driver (`nouveau` ships no `nvidia-smi`).
    fn expects_nvidia_smi(&self) -> bool {
        self.vendor == "nvidia" && self.driver.as_deref() != Some("nouveau")
    }

    /// Fold `nvidia-smi` facts in: they are authoritative for the fields they report.
    fn apply_nvidia(&mut self, nv: NvGpu) {
        self.vendor = "nvidia";
        if let Some(name) = nv.name {
            self.name = name;
        }
        if self.pci_id.is_none() {
            self.pci_id = nv.pci_id;
        }
        if nv.bus_id.is_some() {
            self.bus_id = nv.bus_id;
        }
        self.uuid = nv.uuid.or(self.uuid.take());
        self.driver_version = nv.driver_version.or(self.driver_version.take());
        self.temperature_c = nv.temperature_c;
        self.utilization_percent = nv.utilization_percent;
        self.power_draw_w = nv.power_draw_w;
        self.power_limit_w = nv.power_limit_w;
        self.fan_target_percent = nv.fan_target_percent;
        self.pcie = nv.pcie;
        self.throttle = nv.throttle;
        self.ecc = nv.ecc;
        self.sources.push("nvidia-smi");
    }

    /// An NVIDIA adapter that only `nvidia-smi` knows.
    fn from_nvidia(nv: NvGpu) -> Self {
        let mut gpu = Gpu::identity("nvidia-smi", "NVIDIA GPU".to_string(), "nvidia");
        gpu.sources.clear();
        gpu.apply_nvidia(nv);
        gpu
    }

    fn to_json(&self) -> Value {
        let ras = self.ras.as_ref().map(|blocks| {
            blocks
                .iter()
                .map(|(block, c)| (block.clone(), json!({ "ue": c.ue, "ce": c.ce })))
                .collect::<Map<String, Value>>()
        });
        json!({
            "name": self.name,
            "vendor": self.vendor,
            "pci_id": self.pci_id,
            "bus_id": self.bus_id,
            "uuid": self.uuid,
            "driver_version": self.driver_version,
            "sources": self.sources,
            "temperature_c": self.temperature_c.map(num),
            "utilization_percent": self.utilization_percent.map(num),
            "power_draw_w": self.power_draw_w.map(round1),
            "power_limit_w": self.power_limit_w.map(round1),
            "fan_target_percent": self.fan_target_percent.map(num),
            "pcie": self.pcie.as_ref().map(Pcie::to_json),
            "throttle": self.throttle.as_ref().map(Throttle::to_json),
            "ecc": self.ecc.as_ref().map(Ecc::to_json),
            "ras": ras,
        })
    }
}

/// A number as JSON: whole values as integers (`47`), the rest as floats.
fn num(v: f64) -> Value {
    if v.fract() == 0.0 && v.abs() < 1e15 {
        json!(v as i64)
    } else {
        json!(v)
    }
}

/// One decimal place, as a JSON float (`38.52` → `38.5`, `320` → `320.0`).
fn round1(v: f64) -> Value {
    json!((v * 10.0).round() / 10.0)
}

// ---------------------------------------------------------------------------------
// Identifiers
// ---------------------------------------------------------------------------------

/// The contract's vendor name for a PCI vendor id (lowercase hex, no prefix).
fn vendor_from_id(vendor_id: &str) -> &'static str {
    match vendor_id {
        "10de" => "nvidia",
        "1002" => "amd",
        "8086" => "intel",
        _ => "unknown",
    }
}

/// Lowercase 4-digit hex from `0x10DE`, `10de` or `0x1002`; `None` when it is not a
/// 16-bit hex number.
fn hex4(text: &str) -> Option<String> {
    let t = text.trim();
    let t = t
        .strip_prefix("0x")
        .or_else(|| t.strip_prefix("0X"))
        .unwrap_or(t);
    if t.is_empty() || t.len() > 4 || !t.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    Some(format!("{:0>4}", t.to_ascii_lowercase()))
}

/// Normalize a PCI address to `dddd:bb:dd.f` lowercase. nvidia-smi reports an 8-digit
/// domain (`00000000:01:00.0`), sysfs a 4-digit one (`0000:01:00.0`); an address without
/// a domain gets domain `0000`.
fn normalize_bus_id(raw: &str) -> Option<String> {
    let lower = raw.trim().to_ascii_lowercase();
    let parts: Vec<&str> = lower.split(':').collect();
    let (domain, bus, devfn) = match parts.as_slice() {
        [domain, bus, devfn] => (*domain, *bus, *devfn),
        [bus, devfn] => ("0000", *bus, *devfn),
        _ => return None,
    };
    let (dev, func) = devfn.split_once('.')?;
    let hex = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_hexdigit());
    if !hex(domain) || domain.len() > 8 || bus.len() != 2 || dev.len() != 2 {
        return None;
    }
    if !hex(bus) || !hex(dev) || func.len() != 1 || !matches!(func.as_bytes()[0], b'0'..=b'7') {
        return None;
    }
    // Domains above 16 bits are not used by any GPU host; keep the low 16 bits.
    let domain = &domain[domain.len().saturating_sub(4)..];
    Some(format!("{domain:0>4}:{bus}:{dev}.{func}"))
}

/// PCI generation of a link speed such as `16.0 GT/s PCIe`.
fn gen_from_link_speed(text: &str) -> Option<u64> {
    let gts: f64 = text.split_whitespace().next()?.parse().ok()?;
    [
        (2.5, 1),
        (5.0, 2),
        (8.0, 3),
        (16.0, 4),
        (32.0, 5),
        (64.0, 6),
    ]
    .iter()
    .find(|(speed, _)| (gts - speed).abs() < 0.05)
    .map(|&(_, gen)| gen)
}

// ---------------------------------------------------------------------------------
// Merging
// ---------------------------------------------------------------------------------

/// Merge identities and `nvidia-smi` GPUs, then cap the list. Returns the GPUs and
/// whether the cap cut any off.
///
/// An `nvidia-smi` GPU merges into the identity with the same bus id (sysfs), else with
/// an unclaimed NVIDIA identity that has the same PCI id (Windows has no bus id), else —
/// when `nvidia-smi` reported no PCI id — with the next unclaimed NVIDIA identity.
/// Otherwise it becomes an entry of its own.
fn assemble(identities: Vec<Gpu>, nvidia_gpus: Vec<NvGpu>) -> (Vec<Gpu>, bool) {
    let mut gpus = identities;
    let mut claimed = vec![false; gpus.len()];
    for nv in nvidia_gpus {
        let free_nvidia =
            |g: &Gpu, taken: bool| !taken && g.vendor == "nvidia" && g.bus_id.is_none();
        let found = (0..gpus.len())
            .find(|&i| !claimed[i] && nv.bus_id.is_some() && gpus[i].bus_id == nv.bus_id)
            .or_else(|| {
                (0..gpus.len()).find(|&i| {
                    free_nvidia(&gpus[i], claimed[i])
                        && nv.pci_id.is_some()
                        && gpus[i].pci_id == nv.pci_id
                })
            })
            .or_else(|| {
                (0..gpus.len()).find(|&i| free_nvidia(&gpus[i], claimed[i]) && nv.pci_id.is_none())
            });
        match found {
            Some(i) => {
                claimed[i] = true;
                gpus[i].apply_nvidia(nv);
            }
            None => {
                gpus.push(Gpu::from_nvidia(nv));
                claimed.push(true);
            }
        }
    }
    let truncated = gpus.len() > MAX_GPUS;
    gpus.truncate(MAX_GPUS);
    (gpus, truncated)
}

// ---------------------------------------------------------------------------------
// nvidia-smi
// ---------------------------------------------------------------------------------

/// What `nvidia-smi` reported for one GPU.
#[derive(Debug, Clone, Default, PartialEq)]
struct NvGpu {
    name: Option<String>,
    uuid: Option<String>,
    bus_id: Option<String>,
    pci_id: Option<String>,
    driver_version: Option<String>,
    temperature_c: Option<f64>,
    utilization_percent: Option<f64>,
    power_draw_w: Option<f64>,
    power_limit_w: Option<f64>,
    fan_target_percent: Option<f64>,
    pcie: Option<Pcie>,
    throttle: Option<Throttle>,
    ecc: Option<Ecc>,
}

/// Core group. `pci.bus_id` (column 2) joins the other groups to it.
const CORE_FIELDS: &[&str] = &[
    "name",
    "uuid",
    "pci.bus_id",
    "driver_version",
    "temperature.gpu",
    "utilization.gpu",
    "power.draw",
    "power.limit",
    "fan.speed",
    "pcie.link.gen.current",
    "pcie.link.gen.max",
    "pcie.link.width.current",
    "pcie.link.width.max",
];
/// What is asked when the full core group fails.
const CORE_MIN_FIELDS: &[&str] = &[
    "name",
    "uuid",
    "pci.bus_id",
    "driver_version",
    "temperature.gpu",
];
const CORE_BUS_COL: usize = 2;
/// Optional groups lead with `pci.bus_id`, the join key.
const IDENT_FIELDS: &[&str] = &["pci.bus_id", "pci.device_id"];
const THROTTLE_SUFFIXES: &[&str] = &[
    "hw_slowdown",
    "hw_thermal_slowdown",
    "hw_power_brake_slowdown",
    "sw_thermal_slowdown",
];
/// Current spelling first; drivers older than r525 only know the `throttle` one.
const THROTTLE_PREFIXES: &[&str] = &["clocks_event_reasons.", "clocks_throttle_reasons."];
const ECC_FIELDS: &[&str] = &[
    "pci.bus_id",
    "ecc.errors.uncorrected.volatile.total",
    "retired_pages.pending",
];
const REMAP_FIELDS: &[&str] = &[
    "pci.bus_id",
    "remapped_rows.correctable",
    "remapped_rows.uncorrectable",
    "remapped_rows.pending",
    "remapped_rows.failure",
];

/// Runs one `--query-gpu` field list; `nvidia::query` in production.
type Runner<'a> = dyn Fn(&[&str]) -> Option<Vec<Row>> + 'a;

/// Query every field group and join them per GPU. `None` when not even the minimal core
/// group answers (no `nvidia-smi`, driver not loaded); a failing optional group only
/// leaves its fields `null`.
fn collect_nvidia(run: &Runner<'_>) -> Option<Vec<NvGpu>> {
    let core = run(CORE_FIELDS).or_else(|| run(CORE_MIN_FIELDS))?;
    let ident = run(IDENT_FIELDS);
    let throttle = THROTTLE_PREFIXES.iter().find_map(|prefix| {
        let fields: Vec<String> = std::iter::once("pci.bus_id".to_string())
            .chain(THROTTLE_SUFFIXES.iter().map(|s| format!("{prefix}{s}")))
            .collect();
        let refs: Vec<&str> = fields.iter().map(String::as_str).collect();
        run(&refs)
    });
    let ecc = run(ECC_FIELDS);
    let remap = run(REMAP_FIELDS);
    Some(join_groups(&core, ident, throttle, ecc, remap))
}

/// The row of `rows` for the GPU on `bus_id` (column `bus_col`); falls back to the row at
/// the GPU's own index when the group carries no usable bus ids.
fn group_row<'a>(
    rows: &'a Option<Vec<Row>>,
    bus_col: usize,
    bus_id: Option<&str>,
    index: usize,
) -> Option<&'a Row> {
    let rows = rows.as_ref()?;
    let bus_of = |row: &Row| nvidia::cell_str(row, bus_col).and_then(normalize_bus_id);
    if let Some(bus_id) = bus_id {
        if let Some(row) = rows
            .iter()
            .find(|row| bus_of(row).as_deref() == Some(bus_id))
        {
            return Some(row);
        }
    }
    rows.get(index).filter(|row| bus_of(row).is_none())
}

/// Join the groups by bus id (row index as the fallback) and decode them.
fn join_groups(
    core: &[Row],
    ident: Option<Vec<Row>>,
    throttle: Option<Vec<Row>>,
    ecc: Option<Vec<Row>>,
    remap: Option<Vec<Row>>,
) -> Vec<NvGpu> {
    let throttle_rows = throttle;
    core.iter()
        .enumerate()
        .map(|(index, row)| {
            let bus_id = nvidia::cell_str(row, CORE_BUS_COL).and_then(normalize_bus_id);
            let bus = bus_id.as_deref();
            let pcie = Pcie {
                gen_current: nvidia::cell_u64(row, 9),
                gen_max: nvidia::cell_u64(row, 10),
                width_current: nvidia::cell_u64(row, 11),
                width_max: nvidia::cell_u64(row, 12),
            };
            let pci_id = group_row(&ident, 0, bus, index).and_then(|r| {
                let id = u32::from_str_radix(
                    nvidia::cell_str(r, 1)?
                        .trim_start_matches("0x")
                        .trim_start_matches("0X"),
                    16,
                )
                .ok()?;
                // The cell packs `device << 16 | vendor`.
                Some(format!("{:04x}:{:04x}", id & 0xffff, id >> 16))
            });
            let throttle = group_row(&throttle_rows, 0, bus, index).map(|r| Throttle {
                hw_slowdown: nvidia::cell_bool(r, 1),
                hw_thermal_slowdown: nvidia::cell_bool(r, 2),
                hw_power_brake_slowdown: nvidia::cell_bool(r, 3),
                sw_thermal_slowdown: nvidia::cell_bool(r, 4),
            });
            let ecc_row = group_row(&ecc, 0, bus, index);
            let remap_row = group_row(&remap, 0, bus, index);
            let remapped = remap_row.map(|r| Remapped {
                correctable: nvidia::cell_u64(r, 1),
                uncorrectable: nvidia::cell_u64(r, 2),
                pending: flag(r, 3),
                failure: flag(r, 4),
            });
            let ecc = Ecc {
                uncorrected_volatile: ecc_row.and_then(|r| nvidia::cell_u64(r, 1)),
                retired_pages_pending: ecc_row.and_then(|r| flag(r, 2)),
                remapped_rows: remapped.filter(|r| !r.is_empty()),
            };
            NvGpu {
                name: nvidia::cell_str(row, 0).map(str::to_string),
                uuid: nvidia::cell_str(row, 1).map(str::to_string),
                bus_id: bus_id.clone(),
                pci_id,
                driver_version: nvidia::cell_str(row, 3).map(str::to_string),
                temperature_c: nvidia::cell_f64(row, 4),
                utilization_percent: nvidia::cell_f64(row, 5),
                power_draw_w: nvidia::cell_f64(row, 6),
                power_limit_w: nvidia::cell_f64(row, 7),
                fan_target_percent: nvidia::cell_f64(row, 8),
                pcie: Some(pcie).filter(|p| !p.is_empty()),
                throttle: throttle.filter(|t| !t.is_empty()),
                ecc: Some(ecc).filter(|e| !e.is_empty()),
            }
        })
        .collect()
}

/// A yes/no cell; some drivers print a count (`0`/`1`) instead of `Yes`/`No`.
fn flag(row: &[Option<String>], idx: usize) -> Option<bool> {
    nvidia::cell_bool(row, idx).or_else(|| nvidia::cell_u64(row, idx).map(|n| n > 0))
}

// ---------------------------------------------------------------------------------
// Windows identity (Win32_VideoController)
// ---------------------------------------------------------------------------------

#[cfg_attr(not(windows), allow(dead_code))]
mod wmi {
    use super::*;

    /// One bounded CIM query; `-InputObject` makes an empty or single result a JSON array.
    pub const SCRIPT: &str = "$ErrorActionPreference = 'Stop'\n\
        $g = @(Get-CimInstance Win32_VideoController | ForEach-Object {\n\
        \x20 [pscustomobject]@{\n\
        \x20   name = [string]$_.Name\n\
        \x20   driver_version = [string]$_.DriverVersion\n\
        \x20   pnp_device_id = [string]$_.PNPDeviceID\n\
        \x20 }\n\
        })\n\
        ConvertTo-Json -Compress -InputObject $g";

    /// Names of adapters that are display-driver plumbing, not hardware.
    const VIRTUAL_PREFIXES: &[&str] = &["microsoft basic display", "microsoft remote display"];

    /// `(vendor id, device id)` out of `PCI\VEN_10DE&DEV_2704&SUBSYS_…`, lowercase.
    pub fn parse_pnp_id(pnp: &str) -> Option<(String, String)> {
        let upper = pnp.to_ascii_uppercase();
        let after = |key: &str| {
            let start = upper.find(key)? + key.len();
            upper.get(start..start + 4).and_then(hex4)
        };
        Some((after("VEN_")?, after("DEV_")?))
    }

    pub fn is_virtual(name: &str) -> bool {
        let lower = name.trim().to_ascii_lowercase();
        VIRTUAL_PREFIXES.iter().any(|p| lower.starts_with(p))
    }

    /// Decode the script's JSON into identities, skipping virtual adapters.
    pub fn parse_rows(value: Value) -> Vec<Gpu> {
        as_array(value)
            .into_iter()
            .filter_map(|row| {
                let text = |key: &str| {
                    row.get(key)
                        .and_then(Value::as_str)
                        .map(str::trim)
                        .filter(|s| !s.is_empty())
                };
                let name = text("name")?;
                if is_virtual(name) {
                    return None;
                }
                let ids = text("pnp_device_id").and_then(parse_pnp_id);
                let vendor = ids.as_ref().map_or("unknown", |(v, _)| vendor_from_id(v));
                let mut gpu = Gpu::identity("wmi", name.to_string(), vendor);
                gpu.pci_id = ids.map(|(v, d)| format!("{v}:{d}"));
                gpu.driver_version = text("driver_version").map(str::to_string);
                Some(gpu)
            })
            .collect()
    }

    #[cfg(windows)]
    pub fn probe(errors: &mut Vec<String>) -> Vec<Gpu> {
        match crate::telemetry::collectors::winps::run_json(SCRIPT) {
            Some(value) => parse_rows(value),
            None => {
                errors.push("wmi: Win32_VideoController query failed".to_string());
                Vec::new()
            }
        }
    }
}

/// `ConvertTo-Json` collapses one-element collections to a bare object; accept both.
#[cfg_attr(not(any(windows, test)), allow(dead_code))]
fn as_array(value: Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items,
        Value::Null => Vec::new(),
        other => vec![other],
    }
}

// ---------------------------------------------------------------------------------
// Linux identity and amdgpu facts (/sys/class/drm)
// ---------------------------------------------------------------------------------

#[cfg_attr(windows, allow(dead_code))]
mod sysfs {
    use std::fs;
    use std::path::Path;

    use super::*;

    pub const DRM_ROOT: &str = "/sys/class/drm";

    fn read(path: &Path) -> Option<String> {
        let text = fs::read_to_string(path).ok()?;
        let text = text.trim();
        (!text.is_empty()).then(|| text.to_string())
    }

    fn read_f64(path: &Path) -> Option<f64> {
        read(path)?.parse::<f64>().ok().filter(|v| v.is_finite())
    }

    fn read_u64(path: &Path) -> Option<u64> {
        read(path)?.parse().ok()
    }

    /// The `N` of a `cardN` directory name; connectors (`card0-DP-1`) and render nodes
    /// (`renderD128`) are not cards.
    fn card_index(name: &str) -> Option<u32> {
        let digits = name.strip_prefix("card")?;
        if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        digits.parse().ok()
    }

    /// `KEY=VALUE` lines of a sysfs `uevent` file.
    pub fn parse_uevent(text: &str) -> BTreeMap<&str, &str> {
        text.lines()
            .filter_map(|line| line.split_once('='))
            .map(|(k, v)| (k.trim(), v.trim()))
            .collect()
    }

    /// `ue: N` / `ce: N` lines of a `ras/<block>_err_count` file; both must be present.
    pub fn parse_ras_counts(text: &str) -> Option<RasCounts> {
        let mut ue = None;
        let mut ce = None;
        for line in text.lines() {
            if let Some((key, value)) = line.split_once(':') {
                let value = value.trim().parse::<u64>().ok();
                match key.trim() {
                    "ue" => ue = value,
                    "ce" => ce = value,
                    _ => {}
                }
            }
        }
        Some(RasCounts { ue: ue?, ce: ce? })
    }

    /// All GPUs under `root` (`/sys/class/drm`), in card order.
    pub fn probe(root: &Path) -> Vec<Gpu> {
        let Ok(entries) = fs::read_dir(root) else {
            return Vec::new();
        };
        let mut cards: Vec<(u32, std::path::PathBuf)> = entries
            .flatten()
            .filter_map(|e| {
                let index = card_index(&e.file_name().to_string_lossy())?;
                Some((index, e.path().join("device")))
            })
            .collect();
        cards.sort();
        cards
            .into_iter()
            .filter_map(|(_, device)| read_card(&device))
            .collect()
    }

    /// One card's identity (plus amdgpu facts); `None` when the card has no PCI vendor
    /// and device id (platform devices such as `simpledrm`).
    fn read_card(device: &Path) -> Option<Gpu> {
        let vendor_id = hex4(&read(&device.join("vendor"))?)?;
        let device_id = hex4(&read(&device.join("device"))?)?;
        let vendor = vendor_from_id(&vendor_id);
        let pci_id = format!("{vendor_id}:{device_id}");

        let uevent = read(&device.join("uevent")).unwrap_or_default();
        let kv = parse_uevent(&uevent);
        let bus_id = kv
            .get("PCI_SLOT_NAME")
            .and_then(|s| normalize_bus_id(s))
            .or_else(|| {
                let target = fs::canonicalize(device).ok()?;
                normalize_bus_id(&target.file_name()?.to_string_lossy())
            });

        let name = read(&device.join("label"))
            .or_else(|| read(&device.join("product_name")))
            .unwrap_or_else(|| fallback_name(vendor, &device_id, &pci_id));

        let mut gpu = Gpu::identity("sysfs", name, vendor);
        gpu.pci_id = Some(pci_id);
        gpu.bus_id = bus_id;
        gpu.driver = kv.get("DRIVER").map(|s| s.to_string());
        if vendor == "amd" {
            read_amd(&mut gpu, device);
        }
        Some(gpu)
    }

    /// `<vendor> GPU <device>`; an unknown vendor is named by its full PCI id.
    fn fallback_name(vendor: &str, device_id: &str, pci_id: &str) -> String {
        match vendor {
            "nvidia" => format!("NVIDIA GPU {device_id}"),
            "amd" => format!("AMD GPU {device_id}"),
            "intel" => format!("Intel GPU {device_id}"),
            _ => format!("GPU {pci_id}"),
        }
    }

    /// amdgpu hwmon, utilization, PCIe link and RAS counters. A runtime-suspended GPU
    /// (a laptop's idle dGPU) is left alone: reading these files would wake it.
    fn read_amd(gpu: &mut Gpu, device: &Path) {
        if read(&device.join("power/runtime_status")).as_deref() == Some("suspended") {
            return;
        }
        gpu.utilization_percent = read_f64(&device.join("gpu_busy_percent"));

        let mut hwmons: Vec<std::path::PathBuf> = fs::read_dir(device.join("hwmon"))
            .map(|rd| {
                rd.flatten()
                    .filter(|e| e.file_name().to_string_lossy().starts_with("hwmon"))
                    .map(|e| e.path())
                    .collect()
            })
            .unwrap_or_default();
        hwmons.sort();
        for hwmon in &hwmons {
            // First hwmon directory that has a value wins.
            if gpu.temperature_c.is_none() {
                gpu.temperature_c = read_f64(&hwmon.join("temp1_input")).map(|m| m / 1000.0);
            }
            if gpu.power_draw_w.is_none() {
                gpu.power_draw_w = read_f64(&hwmon.join("power1_average"))
                    .or_else(|| read_f64(&hwmon.join("power1_input")))
                    .map(|uw| uw / 1_000_000.0);
            }
            if gpu.power_limit_w.is_none() {
                gpu.power_limit_w = read_f64(&hwmon.join("power1_cap")).map(|uw| uw / 1_000_000.0);
            }
            if gpu.fan_target_percent.is_none() {
                gpu.fan_target_percent = read_f64(&hwmon.join("pwm1"))
                    .filter(|pwm| (0.0..=255.0).contains(pwm))
                    .map(|pwm| (pwm / 255.0 * 100.0).round());
            }
        }

        let pcie = Pcie {
            gen_current: read(&device.join("current_link_speed"))
                .as_deref()
                .and_then(gen_from_link_speed),
            gen_max: read(&device.join("max_link_speed"))
                .as_deref()
                .and_then(gen_from_link_speed),
            width_current: read_u64(&device.join("current_link_width")),
            width_max: read_u64(&device.join("max_link_width")),
        };
        gpu.pcie = Some(pcie).filter(|p| !p.is_empty());
        gpu.ras = read_ras(&device.join("ras"));
    }

    /// `ras/*_err_count`; `None` when the directory is absent or holds no counter file.
    fn read_ras(dir: &Path) -> Option<BTreeMap<String, RasCounts>> {
        let entries = fs::read_dir(dir).ok()?;
        let mut blocks = BTreeMap::new();
        for entry in entries.flatten() {
            let file = entry.file_name().to_string_lossy().into_owned();
            let Some(block) = file.strip_suffix("_err_count") else {
                continue;
            };
            if let Some(counts) = read(&entry.path()).as_deref().and_then(parse_ras_counts) {
                blocks.insert(block.to_string(), counts);
            }
        }
        // Sorted by name; the cap keeps the frame small on an exotic part.
        while blocks.len() > MAX_RAS_BLOCKS {
            blocks.pop_last();
        }
        Some(blocks).filter(|b| !b.is_empty())
    }
}

// ---------------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU32, Ordering};

    /// A unique scratch directory removed on drop (no `tempfile` dev-dependency).
    struct Scratch(PathBuf);

    impl Scratch {
        fn new() -> Self {
            static NEXT: AtomicU32 = AtomicU32::new(0);
            let dir = std::env::temp_dir().join(format!(
                "kenny-gpu-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            let _ = fs::remove_dir_all(&dir);
            fs::create_dir_all(&dir).unwrap();
            Scratch(dir)
        }

        fn put(&self, rel: &str, content: &str) {
            let path = self.0.join(rel);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path, content).unwrap();
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn fixture(name: &str) -> Value {
        let text = fs::read_to_string(
            Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../docs/fixtures")
                .join(name),
        )
        .expect("read the fixture");
        serde_json::from_str(&text).expect("parse the fixture")
    }

    // --- identifiers -------------------------------------------------------------

    #[test]
    fn bus_ids_normalize_to_the_four_digit_domain() {
        assert_eq!(
            normalize_bus_id("00000000:01:00.0").as_deref(),
            Some("0000:01:00.0")
        );
        assert_eq!(
            normalize_bus_id("0000:01:00.0").as_deref(),
            Some("0000:01:00.0")
        );
        assert_eq!(
            normalize_bus_id(" 00000000:0A:00.0 ").as_deref(),
            Some("0000:0a:00.0")
        );
        assert_eq!(normalize_bus_id("03:00.0").as_deref(), Some("0000:03:00.0"));
        assert_eq!(normalize_bus_id("garbage"), None);
        assert_eq!(normalize_bus_id("0000:01:00.9"), None);
        assert_eq!(normalize_bus_id(""), None);
    }

    #[test]
    fn link_speeds_map_to_pcie_generations() {
        let cases = [
            ("2.5 GT/s PCIe", 1),
            ("5.0 GT/s PCIe", 2),
            ("8.0 GT/s PCIe", 3),
            ("16.0 GT/s PCIe", 4),
            ("32.0 GT/s PCIe", 5),
            ("64.0 GT/s PCIe", 6),
        ];
        for (text, gen) in cases {
            assert_eq!(gen_from_link_speed(text), Some(gen), "{text}");
        }
        assert_eq!(gen_from_link_speed("Unknown"), None);
        assert_eq!(gen_from_link_speed(""), None);
    }

    #[test]
    fn pnp_ids_give_vendor_and_pci_id() {
        let (v, d) =
            wmi::parse_pnp_id(r"PCI\VEN_10DE&DEV_2704&SUBSYS_16F11043&REV_A1\4&1F2B&0&0008")
                .unwrap();
        assert_eq!((v.as_str(), d.as_str()), ("10de", "2704"));
        assert_eq!(vendor_from_id(&v), "nvidia");
        assert_eq!(vendor_from_id("1002"), "amd");
        assert_eq!(vendor_from_id("8086"), "intel");
        assert_eq!(vendor_from_id("15ad"), "unknown");
        assert_eq!(wmi::parse_pnp_id(r"ROOT\BASICDISPLAY\0000"), None);
    }

    // --- WMI identity ------------------------------------------------------------

    #[test]
    fn wmi_rows_skip_virtual_adapters_and_accept_a_bare_object() {
        let rows = json!([
            { "name": "NVIDIA GeForce RTX 4080", "driver_version": "32.0.15.6094",
              "pnp_device_id": r"PCI\VEN_10DE&DEV_2704&SUBSYS_1&REV_A1\4&1&0&0008" },
            { "name": "Microsoft Basic Display Adapter", "driver_version": "10.0",
              "pnp_device_id": r"ROOT\BASICDISPLAY\0000" },
            { "name": "Microsoft Remote Display Adapter", "driver_version": "10.0",
              "pnp_device_id": r"SWD\REMOTEDISPLAY\1" },
            { "name": "Intel(R) UHD Graphics 770", "driver_version": "31.0.101.4502",
              "pnp_device_id": r"PCI\VEN_8086&DEV_4680&SUBSYS_1&REV_0C\3&1&0&10" },
            { "name": "Some Virtual Thing", "pnp_device_id": null },
        ]);
        let gpus = wmi::parse_rows(rows);
        let names: Vec<&str> = gpus.iter().map(|g| g.name.as_str()).collect();
        assert_eq!(
            names,
            [
                "NVIDIA GeForce RTX 4080",
                "Intel(R) UHD Graphics 770",
                "Some Virtual Thing"
            ]
        );
        assert_eq!(gpus[0].vendor, "nvidia");
        assert_eq!(gpus[0].pci_id.as_deref(), Some("10de:2704"));
        assert_eq!(gpus[0].driver_version.as_deref(), Some("32.0.15.6094"));
        assert_eq!(gpus[1].vendor, "intel");
        assert_eq!(gpus[2].vendor, "unknown");
        assert_eq!(gpus[2].pci_id, None);
        assert_eq!(gpus[0].sources, ["wmi"]);

        let one = wmi::parse_rows(json!({ "name": "AMD Radeon RX 7800 XT",
            "pnp_device_id": r"PCI\VEN_1002&DEV_747E&SUBSYS_1&REV_C8\4&1&0&0008" }));
        assert_eq!(one.len(), 1);
        assert_eq!(one[0].pci_id.as_deref(), Some("1002:747e"));
        assert!(wmi::parse_rows(json!([])).is_empty());
        assert!(wmi::parse_rows(Value::Null).is_empty());
    }

    // --- nvidia-smi --------------------------------------------------------------

    const CORE_CSV: &str = "NVIDIA GeForce RTX 4080, GPU-4f1c2a6e-8d3b-7c59-1e20-a9b3c4d5e6f7, 00000000:01:00.0, 560.94, 47, 6, 38.52, 320.00, 30, 1, 4, 16, 16\n";
    const IDENT_CSV: &str = "00000000:01:00.0, 0x270410DE\n";
    const THROTTLE_CSV: &str = "00000000:01:00.0, Not Active, Not Active, Not Active, Not Active\n";
    const ALL_NA_CSV: &str = "00000000:01:00.0, [N/A], [N/A]\n";
    const ALL_NA_REMAP_CSV: &str = "00000000:01:00.0, [N/A], [N/A], [N/A], [N/A]\n";

    /// A fake `nvidia-smi` answering by the first distinguishing field of the query.
    /// `broken` lists substrings of field lists that make the (fake) driver fail.
    fn fake_runner<'a>(
        outputs: &'a [(&'a str, &'a str)],
        broken: &'a [&'a str],
    ) -> impl Fn(&[&str]) -> Option<Vec<Row>> + 'a {
        move |fields: &[&str]| {
            let joined = fields.join(",");
            if broken.iter().any(|b| joined.contains(b)) {
                return None;
            }
            let (_, csv) = outputs.iter().find(|(key, _)| joined.contains(key))?;
            let rows: Vec<Row> = nvidia::parse_csv(csv)
                .into_iter()
                .filter(|r| r.len() == fields.len())
                .collect();
            (!rows.is_empty()).then_some(rows)
        }
    }

    fn geforce_outputs() -> Vec<(&'static str, &'static str)> {
        vec![
            ("utilization.gpu", CORE_CSV),
            ("pci.device_id", IDENT_CSV),
            ("clocks_event_reasons", THROTTLE_CSV),
            ("ecc.errors", ALL_NA_CSV),
            ("remapped_rows", ALL_NA_REMAP_CSV),
        ]
    }

    #[test]
    fn a_geforce_card_decodes_with_ecc_null() {
        let outputs = geforce_outputs();
        let run = fake_runner(&outputs, &[]);
        let nv = collect_nvidia(&run).unwrap();
        assert_eq!(nv.len(), 1);
        let g = &nv[0];
        assert_eq!(g.name.as_deref(), Some("NVIDIA GeForce RTX 4080"));
        assert_eq!(g.bus_id.as_deref(), Some("0000:01:00.0"));
        assert_eq!(g.pci_id.as_deref(), Some("10de:2704"));
        assert_eq!(g.temperature_c, Some(47.0));
        assert_eq!(g.power_draw_w, Some(38.52));
        assert_eq!(g.ecc, None, "every ECC cell is [N/A]");
        assert_eq!(
            g.throttle,
            Some(Throttle {
                hw_slowdown: Some(false),
                hw_thermal_slowdown: Some(false),
                hw_power_brake_slowdown: Some(false),
                sw_thermal_slowdown: Some(false),
            })
        );
        assert_eq!(g.pcie.as_ref().unwrap().width_max, Some(16));
    }

    #[test]
    fn a_failing_core_group_retries_the_minimal_one() {
        let min_csv = "NVIDIA GeForce GTX 1060, GPU-abc, 00000000:02:00.0, 391.35, 61\n";
        // The full core query fails (an unknown field); only the minimal one answers.
        let outputs = vec![("temperature.gpu", min_csv)];
        let run = |fields: &[&str]| {
            if fields.len() > CORE_MIN_FIELDS.len() {
                return None;
            }
            fake_runner(&outputs, &[])(fields)
        };
        let nv = collect_nvidia(&run).unwrap();
        assert_eq!(nv.len(), 1);
        assert_eq!(nv[0].temperature_c, Some(61.0));
        assert_eq!(nv[0].utilization_percent, None);
        assert_eq!(nv[0].pcie, None);
        assert_eq!(nv[0].throttle, None);
        assert_eq!(nv[0].ecc, None);
        assert_eq!(nv[0].pci_id, None);
    }

    #[test]
    fn nothing_answering_is_none_not_an_empty_gpu() {
        let run = |_: &[&str]| None;
        assert_eq!(collect_nvidia(&run), None);
    }

    #[test]
    fn old_drivers_fall_back_to_the_throttle_reasons_prefix() {
        let old = "00000000:01:00.0, Active, Not Active, Not Active, [N/A]\n";
        let mut outputs = geforce_outputs();
        outputs.retain(|(k, _)| *k != "clocks_event_reasons");
        outputs.push(("clocks_throttle_reasons", old));
        // The new spelling is an unknown field to this driver.
        let run = fake_runner(&outputs, &["clocks_event_reasons"]);
        let nv = collect_nvidia(&run).unwrap();
        let t = nv[0]
            .throttle
            .as_ref()
            .expect("throttle from the old prefix");
        assert_eq!(t.hw_slowdown, Some(true));
        assert_eq!(t.hw_thermal_slowdown, Some(false));
        assert_eq!(t.sw_thermal_slowdown, None);
    }

    #[test]
    fn a_failing_optional_group_only_nulls_its_own_fields() {
        let outputs = geforce_outputs();
        let run = fake_runner(
            &outputs,
            &[
                "clocks_event_reasons",
                "clocks_throttle_reasons",
                "pci.device_id",
            ],
        );
        let nv = collect_nvidia(&run).unwrap();
        assert_eq!(nv[0].throttle, None);
        assert_eq!(nv[0].pci_id, None);
        assert_eq!(nv[0].temperature_c, Some(47.0));
    }

    #[test]
    fn a_datacenter_card_reports_ecc_and_remapped_rows() {
        let core = "NVIDIA A100-PCIE-40GB, GPU-a100, 00000000:3B:00.0, 535.104, 61, 97, 250.10, 250.00, [N/A], 4, 4, 16, 16\n";
        let ecc = "00000000:3B:00.0, 3, No\n";
        let remap = "00000000:3B:00.0, 2, 0, Yes, No\n";
        let outputs = vec![
            ("utilization.gpu", core),
            ("ecc.errors", ecc),
            ("remapped_rows", remap),
        ];
        let run = fake_runner(&outputs, &[]);
        let g = collect_nvidia(&run).unwrap().remove(0);
        assert_eq!(g.bus_id.as_deref(), Some("0000:3b:00.0"));
        assert_eq!(g.fan_target_percent, None);
        assert_eq!(
            g.ecc,
            Some(Ecc {
                uncorrected_volatile: Some(3),
                retired_pages_pending: Some(false),
                remapped_rows: Some(Remapped {
                    correctable: Some(2),
                    uncorrectable: Some(0),
                    pending: Some(true),
                    failure: Some(false),
                }),
            })
        );
    }

    #[test]
    fn groups_join_by_bus_id_not_by_row_order() {
        let core = "GPU A, GPU-a, 00000000:01:00.0, 560.94, 40, 1, 10, 100, 20, 1, 4, 16, 16\n\
                    GPU B, GPU-b, 00000000:02:00.0, 560.94, 60, 2, 20, 200, 40, 1, 4, 8, 8\n";
        // The throttle group lists the GPUs in the opposite order.
        let throttle = "00000000:02:00.0, Active, Not Active, Not Active, Not Active\n\
                        00000000:01:00.0, Not Active, Not Active, Not Active, Not Active\n";
        let outputs = vec![
            ("utilization.gpu", core),
            ("clocks_event_reasons", throttle),
        ];
        let run = fake_runner(&outputs, &[]);
        let nv = collect_nvidia(&run).unwrap();
        assert_eq!(nv[0].name.as_deref(), Some("GPU A"));
        assert_eq!(nv[0].throttle.as_ref().unwrap().hw_slowdown, Some(false));
        assert_eq!(nv[1].name.as_deref(), Some("GPU B"));
        assert_eq!(nv[1].throttle.as_ref().unwrap().hw_slowdown, Some(true));
    }

    // --- merging -----------------------------------------------------------------

    fn windows_identity() -> Vec<Gpu> {
        wmi::parse_rows(json!([
            { "name": "NVIDIA GeForce RTX 4080", "driver_version": "32.0.15.6094",
              "pnp_device_id": r"PCI\VEN_10DE&DEV_2704&SUBSYS_1&REV_A1\4&1&0&0008" },
            { "name": "Microsoft Basic Display Adapter",
              "pnp_device_id": r"ROOT\BASICDISPLAY\0000" },
        ]))
    }

    #[test]
    fn the_windows_fixture_is_what_the_collector_produces() {
        // The canned `nvidia-smi` answer for a card that reports row remapping (all zeros).
        let mut outputs = geforce_outputs();
        outputs.retain(|(k, _)| *k != "remapped_rows");
        outputs.push(("remapped_rows", "00000000:01:00.0, 0, 0, No, No\n"));
        let nv = collect_nvidia(&fake_runner(&outputs, &[])).unwrap();
        let (gpus, truncated) = assemble(windows_identity(), nv);
        let actual = section_from(&gpus, truncated, vec![]).into_value();
        let expected = &fixture("telemetry_snapshot.json")["snapshot"]["gpu"];
        assert_eq!(&actual, expected);
    }

    #[test]
    fn nvidia_without_a_matching_identity_is_added_on_its_own() {
        let outputs = geforce_outputs();
        let nv = collect_nvidia(&fake_runner(&outputs, &[])).unwrap();
        let (gpus, _) = assemble(Vec::new(), nv);
        assert_eq!(gpus.len(), 1);
        assert_eq!(gpus[0].sources, ["nvidia-smi"]);
        assert_eq!(gpus[0].vendor, "nvidia");
        assert_eq!(gpus[0].pci_id.as_deref(), Some("10de:2704"));
    }

    #[test]
    fn sysfs_identity_merges_by_bus_id_across_formats() {
        let mut sys = Gpu::identity("sysfs", "NVIDIA GPU 2704".into(), "nvidia");
        sys.bus_id = Some("0000:01:00.0".into());
        sys.pci_id = Some("10de:2704".into());
        sys.driver = Some("nvidia".into());
        let mut other = Gpu::identity("sysfs", "Intel GPU 4680".into(), "intel");
        other.bus_id = Some("0000:00:02.0".into());
        let outputs = geforce_outputs();
        let nv = collect_nvidia(&fake_runner(&outputs, &[])).unwrap();
        let (gpus, _) = assemble(vec![other, sys], nv);
        assert_eq!(gpus.len(), 2);
        assert_eq!(gpus[0].sources, ["sysfs"]);
        assert_eq!(gpus[1].sources, ["sysfs", "nvidia-smi"]);
        assert_eq!(gpus[1].name, "NVIDIA GeForce RTX 4080");
        assert_eq!(gpus[1].bus_id.as_deref(), Some("0000:01:00.0"));
    }

    #[test]
    fn two_identical_cards_each_claim_one_identity() {
        let id = || {
            let mut g = Gpu::identity("wmi", "NVIDIA GeForce RTX 4080".into(), "nvidia");
            g.pci_id = Some("10de:2704".into());
            g
        };
        let nv = |bus: &str, temp: f64| NvGpu {
            bus_id: Some(bus.into()),
            pci_id: Some("10de:2704".into()),
            temperature_c: Some(temp),
            ..NvGpu::default()
        };
        let (gpus, _) = assemble(
            vec![id(), id()],
            vec![nv("0000:01:00.0", 40.0), nv("0000:02:00.0", 50.0)],
        );
        assert_eq!(gpus.len(), 2);
        assert_eq!(gpus[0].bus_id.as_deref(), Some("0000:01:00.0"));
        assert_eq!(gpus[1].bus_id.as_deref(), Some("0000:02:00.0"));
        assert_eq!(gpus[1].temperature_c, Some(50.0));
    }

    #[test]
    fn the_list_is_capped_at_eight() {
        let ids: Vec<Gpu> = (0..11)
            .map(|i| Gpu::identity("sysfs", format!("GPU {i}"), "unknown"))
            .collect();
        let (gpus, truncated) = assemble(ids, Vec::new());
        assert_eq!(gpus.len(), 8);
        assert!(truncated);
        let (gpus, truncated) = assemble(
            vec![Gpu::identity("sysfs", "GPU".into(), "unknown")],
            vec![],
        );
        assert_eq!(gpus.len(), 1);
        assert!(!truncated);
    }

    #[test]
    fn nothing_readable_is_an_empty_ok_section() {
        let (gpus, truncated) = assemble(Vec::new(), Vec::new());
        let v = section_from(&gpus, truncated, vec!["wmi: failed".into()]).into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "0 GPU(s)");
        assert_eq!(v["gpus"], json!([]));
        assert_eq!(v["errors"], json!(["wmi: failed"]));
    }

    // --- sysfs -------------------------------------------------------------------

    fn amd_tree(s: &Scratch) {
        let d = "card1/device";
        s.put(&format!("{d}/vendor"), "0x1002\n");
        s.put(&format!("{d}/device"), "0x747e\n");
        s.put(&format!("{d}/label"), "AMD Radeon RX 7800 XT\n");
        s.put(
            &format!("{d}/uevent"),
            "DRIVER=amdgpu\nPCI_CLASS=30000\nPCI_ID=1002:747E\nPCI_SLOT_NAME=0000:03:00.0\n",
        );
        s.put(&format!("{d}/gpu_busy_percent"), "3\n");
        s.put(&format!("{d}/hwmon/hwmon4/temp1_input"), "52000\n");
        s.put(&format!("{d}/hwmon/hwmon4/power1_average"), "28000000\n");
        s.put(&format!("{d}/hwmon/hwmon4/power1_cap"), "263000000\n");
        s.put(&format!("{d}/current_link_speed"), "2.5 GT/s PCIe\n");
        s.put(&format!("{d}/max_link_speed"), "16.0 GT/s PCIe\n");
        s.put(&format!("{d}/current_link_width"), "16\n");
        s.put(&format!("{d}/max_link_width"), "16\n");
        s.put(&format!("{d}/ras/gfx_err_count"), "ue: 0\nce: 0\n");
        s.put(&format!("{d}/ras/umc_err_count"), "ue: 0\nce: 2\n");
        s.put(&format!("{d}/ras/sdma_err_count"), "ue: 0\nce: 0\n");
        s.put(&format!("{d}/ras/features"), "0\n");
        // Neither connectors nor render nodes are cards.
        s.put("card1-DP-1/status", "connected\n");
        s.put("renderD128/dev", "226:128\n");
        s.put("version", "x\n");
    }

    #[test]
    fn the_linux_fixture_is_what_the_collector_produces() {
        let s = Scratch::new();
        amd_tree(&s);
        let gpus = sysfs::probe(s.path());
        let actual = section_from(&gpus, false, vec![]).into_value();
        let expected = &fixture("telemetry_snapshot_linux.json")["snapshot"]["gpu"];
        assert_eq!(&actual, expected);
    }

    #[test]
    fn amd_fan_pwm_and_power_input_fallback() {
        let s = Scratch::new();
        let d = "card0/device";
        s.put(&format!("{d}/vendor"), "0x1002\n");
        s.put(&format!("{d}/device"), "0x73bf\n");
        s.put(
            &format!("{d}/uevent"),
            "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:0b:00.0\n",
        );
        s.put(&format!("{d}/hwmon/hwmon2/power1_input"), "120500000\n");
        s.put(&format!("{d}/hwmon/hwmon2/pwm1"), "128\n");
        s.put(&format!("{d}/hwmon/hwmon2/temp1_input"), "61500\n");
        let gpus = sysfs::probe(s.path());
        assert_eq!(gpus.len(), 1);
        let g = &gpus[0];
        assert_eq!(g.name, "AMD GPU 73bf", "no label file: lspci-free fallback");
        assert_eq!(g.power_draw_w, Some(120.5));
        assert_eq!(g.fan_target_percent, Some(50.0));
        assert_eq!(g.temperature_c, Some(61.5));
        assert_eq!(g.ras, None, "no ras directory");
        assert_eq!(g.pcie, None);
        assert_eq!(g.utilization_percent, None);
    }

    #[test]
    fn a_suspended_amd_gpu_is_not_read() {
        let s = Scratch::new();
        let d = "card0/device";
        s.put(&format!("{d}/vendor"), "0x1002\n");
        s.put(&format!("{d}/device"), "0x73bf\n");
        s.put(&format!("{d}/power/runtime_status"), "suspended\n");
        s.put(&format!("{d}/gpu_busy_percent"), "0\n");
        s.put(&format!("{d}/hwmon/hwmon2/temp1_input"), "30000\n");
        let g = sysfs::probe(s.path()).remove(0);
        assert_eq!(g.temperature_c, None);
        assert_eq!(g.utilization_percent, None);
        assert_eq!(g.name, "AMD GPU 73bf");
    }

    #[test]
    fn cards_without_a_pci_id_and_other_vendors_are_handled() {
        let s = Scratch::new();
        // A platform framebuffer has no PCI vendor/device files.
        s.put("card0/device/uevent", "DRIVER=simpledrm\n");
        s.put("card1/device/vendor", "0x10de\n");
        s.put("card1/device/device", "0x2704\n");
        s.put(
            "card1/device/uevent",
            "DRIVER=nvidia\nPCI_SLOT_NAME=0000:01:00.0\n",
        );
        s.put("card2/device/vendor", "0x8086\n");
        s.put("card2/device/device", "0x4680\n");
        s.put(
            "card2/device/uevent",
            "DRIVER=i915\nPCI_SLOT_NAME=0000:00:02.0\n",
        );
        s.put("card10/device/vendor", "0x1af4\n");
        s.put("card10/device/device", "0x1050\n");
        let gpus = sysfs::probe(s.path());
        let got: Vec<(&str, &str, Option<&str>)> = gpus
            .iter()
            .map(|g| (g.name.as_str(), g.vendor, g.bus_id.as_deref()))
            .collect();
        assert_eq!(
            got,
            [
                ("NVIDIA GPU 2704", "nvidia", Some("0000:01:00.0")),
                ("Intel GPU 4680", "intel", Some("0000:00:02.0")),
                ("GPU 1af4:1050", "unknown", None),
            ],
            "numeric card order: card10 after card2"
        );
        assert!(gpus[0].expects_nvidia_smi());
        assert!(!gpus[1].expects_nvidia_smi());
        assert_eq!(gpus[0].driver.as_deref(), Some("nvidia"));
    }

    #[test]
    fn ras_files_need_both_counts() {
        assert_eq!(
            sysfs::parse_ras_counts("ue: 1\nce: 7\n"),
            Some(RasCounts { ue: 1, ce: 7 })
        );
        assert_eq!(sysfs::parse_ras_counts("ue: 1\n"), None);
        assert_eq!(sysfs::parse_ras_counts("ue: x\nce: 7\n"), None);
        assert_eq!(sysfs::parse_ras_counts(""), None);
    }

    #[test]
    fn a_ras_directory_without_counter_files_is_null_not_an_empty_map() {
        let s = Scratch::new();
        amd_tree(&s);
        let with_counters = sysfs::probe(s.path())[0].to_json();
        assert_eq!(with_counters["ras"]["umc"], json!({ "ue": 0, "ce": 2 }));

        let bare = Scratch::new();
        amd_tree(&bare);
        for block in ["gfx", "umc", "sdma"] {
            fs::remove_file(
                bare.path()
                    .join(format!("card1/device/ras/{block}_err_count")),
            )
            .unwrap();
        }
        assert_eq!(sysfs::probe(bare.path())[0].to_json()["ras"], Value::Null);
    }

    #[test]
    fn a_missing_drm_directory_yields_no_gpus() {
        let s = Scratch::new();
        assert!(sysfs::probe(&s.path().join("nope")).is_empty());
    }

    // --- shape -------------------------------------------------------------------

    #[test]
    fn every_gpu_has_the_fixtures_key_set() {
        fn keys(v: &Value) -> Vec<String> {
            v.as_object().unwrap().keys().cloned().collect()
        }
        let want = keys(&fixture("telemetry_snapshot.json")["snapshot"]["gpu"]["gpus"][0]);
        assert_eq!(
            want,
            keys(&fixture("telemetry_snapshot_linux.json")["snapshot"]["gpu"]["gpus"][0])
        );
        // An identity-only GPU, a fully populated one, and an nvidia-only one.
        let bare = Gpu::identity("wmi", "x".into(), "unknown").to_json();
        assert_eq!(keys(&bare), want);
        assert_eq!(bare["pcie"], Value::Null);
        assert_eq!(bare["ras"], Value::Null);
        let outputs = geforce_outputs();
        let nv = collect_nvidia(&fake_runner(&outputs, &[])).unwrap();
        let (gpus, _) = assemble(Vec::new(), nv);
        assert_eq!(keys(&gpus[0].to_json()), want);

        let s = Scratch::new();
        amd_tree(&s);
        assert_eq!(keys(&sysfs::probe(s.path())[0].to_json()), want);
        let section = section_from(&gpus, false, vec![]).into_value();
        let top: Vec<&str> = section
            .as_object()
            .unwrap()
            .keys()
            .map(String::as_str)
            .collect();
        for key in ["status", "summary", "gpus", "truncated", "errors"] {
            assert!(top.contains(&key), "{key}");
        }
    }

    #[test]
    fn collect_is_a_valid_ok_section_on_any_host() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["gpus"].is_array());
        assert!(v["errors"].is_array());
        assert!(v["truncated"].is_boolean());
    }

    #[test]
    fn the_wmi_script_queries_the_video_controller_only() {
        assert!(wmi::SCRIPT.contains("Get-CimInstance Win32_VideoController"));
        assert!(wmi::SCRIPT.contains("ConvertTo-Json -Compress -InputObject"));
    }
}
