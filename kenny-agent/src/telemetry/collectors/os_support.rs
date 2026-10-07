//! `os_support` section — OS edition/build and end-of-support posture.
//!
//! Portable basics (name/version) via `sysinfo`; Windows enriches with build and
//! support lifecycle. Also carries `arch` (protocol 0.13), mirroring
//! `register.meta.arch` — a periodic, self-refreshing reconfirmation of the CPU
//! architecture the update-serving path relies on (see ADR-0036). Since protocol
//! 0.17 it likewise carries `channel`, mirroring `register.meta.channel` (ADR-0048), and
//! since 0.22 `cpu`: the CPU identity and microcode revision. The agent reports the
//! facts; whether a model plus revision is a finding is the server's call.

use serde_json::{json, Value};
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `os_support` section.
pub fn collect() -> Section {
    let name = System::name().unwrap_or_else(|| "unknown".to_string());
    let version = System::os_version().unwrap_or_else(|| "unknown".to_string());
    let long = System::long_os_version().unwrap_or_else(|| name.clone());
    let cpu = cpu::read().map_or(Value::Null, |c| c.to_value());

    #[cfg(windows)]
    {
        windows_impl::collect(name, version, long, cpu)
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            long,
            json!({
                "name": name, "version": version, "build": null, "eol": null, "eol_date": null,
                "arch": crate::util::arch(),
                "channel": crate::BUILD_CHANNEL,
                "cpu": cpu,
            }),
        )
    }
}

/// CPU identity and microcode revision.
///
/// The parsers are portable and unit-tested on every platform; only the readers
/// ([`read`]: a registry probe on Windows, `/proc/cpuinfo` on Linux) are OS-specific.
// Each platform reads one source, so the other platform's parser is unused in a non-test
// build; both are compiled and tested everywhere.
#[allow(dead_code)]
mod cpu {
    use std::sync::OnceLock;

    use regex::Regex;
    use serde_json::{json, Value};

    /// What the CPU reports about itself. Every part is optional: `null` is unknown.
    #[derive(Debug, Clone, Default, PartialEq, Eq)]
    pub struct CpuIdentity {
        /// `GenuineIntel`, `AuthenticAMD`, … as the CPU reports it.
        pub vendor: Option<String>,
        pub brand: Option<String>,
        pub family: Option<u32>,
        pub model: Option<u32>,
        pub stepping: Option<u32>,
        /// Running microcode revision, lowercase hex without padding (`0x12b`).
        pub microcode: Option<String>,
        /// The revision the firmware loaded at boot, when the OS reports it separately.
        pub microcode_bios: Option<String>,
    }

    impl CpuIdentity {
        pub fn to_value(&self) -> Value {
            json!({
                "vendor": self.vendor,
                "brand": self.brand,
                "family": self.family,
                "model": self.model,
                "stepping": self.stepping,
                "microcode": self.microcode,
                "microcode_bios": self.microcode_bios,
            })
        }

        fn is_empty(&self) -> bool {
            self.vendor.is_none()
                && self.brand.is_none()
                && self.family.is_none()
                && self.model.is_none()
                && self.stepping.is_none()
                && self.microcode.is_none()
        }
    }

    /// A microcode revision as `0x…`: lowercase, no padding. Zero is "not reported"
    /// (a virtual CPU, or an Intel part before its first update) and reads as unknown, so
    /// the server never compares a bogus `0x0` against a fixed revision.
    fn revision_hex(rev: u32) -> Option<String> {
        (rev != 0).then(|| format!("0x{rev:x}"))
    }

    fn le_dword(bytes: &[u8], at: usize) -> Option<u32> {
        let raw: [u8; 4] = bytes.get(at..at + 4)?.try_into().ok()?;
        Some(u32::from_le_bytes(raw))
    }

    /// Decode a REG_BINARY `Update Revision` / `Previous Update Revision`: the revision is
    /// the **high** dword (bytes 4..8, little endian) on Intel and the **low** dword
    /// (bytes 0..4) on AMD. Any other vendor, or a value too short for its dword, is unknown.
    pub fn decode_update_revision(bytes: &[u8], vendor: &str) -> Option<String> {
        let rev = if vendor.eq_ignore_ascii_case("GenuineIntel") {
            le_dword(bytes, 4)?
        } else if vendor.eq_ignore_ascii_case("AuthenticAMD") {
            le_dword(bytes, 0)?
        } else {
            return None;
        };
        revision_hex(rev)
    }

    fn identifier_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            Regex::new(r"(?i)Family\s+(\d+)\s+Model\s+(\d+)\s+Stepping\s+(\d+)")
                .expect("static regex")
        })
    }

    /// `(family, model, stepping)` from the registry `Identifier` value
    /// (`Intel64 Family 6 Model 183 Stepping 1`; decimal numbers).
    pub fn parse_identifier(identifier: &str) -> Option<(u32, u32, u32)> {
        let c = identifier_re().captures(identifier)?;
        Some((c[1].parse().ok()?, c[2].parse().ok()?, c[3].parse().ok()?))
    }

    fn decode_hex_bytes(hex: &str) -> Option<Vec<u8>> {
        let hex = hex.trim();
        if hex.is_empty() || !hex.len().is_multiple_of(2) {
            return None;
        }
        (0..hex.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(hex.get(i..i + 2)?, 16).ok())
            .collect()
    }

    /// A revision value as the Windows probe reports it: the hex of a REG_BINARY, or
    /// `dw:<n>` for a REG_DWORD (the revision itself).
    fn registry_revision(raw: Option<&str>, vendor: &str) -> Option<String> {
        let raw = raw?.trim();
        match raw.strip_prefix("dw:") {
            Some(n) => revision_hex(n.parse().ok()?),
            None => decode_update_revision(&decode_hex_bytes(raw)?, vendor),
        }
    }

    fn clean(s: Option<&str>) -> Option<String> {
        let s = s?.split_whitespace().collect::<Vec<_>>().join(" ");
        (!s.is_empty()).then_some(s)
    }

    /// The identity from the Windows registry probe's JSON (`brand`, `vendor`,
    /// `identifier`, `update`, `previous`). `None` when it carries nothing.
    pub fn from_registry_probe(probe: &Value) -> Option<CpuIdentity> {
        let text = |k: &str| probe.get(k).and_then(Value::as_str);
        let vendor = clean(text("vendor"));
        let (family, model, stepping) = match text("identifier").and_then(parse_identifier) {
            Some((f, m, s)) => (Some(f), Some(m), Some(s)),
            None => (None, None, None),
        };
        let v = vendor.as_deref().unwrap_or("");
        let id = CpuIdentity {
            brand: clean(text("brand")),
            microcode: registry_revision(text("update"), v),
            microcode_bios: registry_revision(text("previous"), v),
            vendor,
            family,
            model,
            stepping,
        };
        (!id.is_empty()).then_some(id)
    }

    /// The first processor block of `/proc/cpuinfo` (`vendor_id`, `model name`,
    /// `cpu family`, `model`, `stepping`, `microcode`). `None` when it names nothing.
    pub fn parse_cpuinfo(text: &str) -> Option<CpuIdentity> {
        let mut id = CpuIdentity::default();
        for line in text.lines() {
            if line.trim().is_empty() {
                if id.is_empty() {
                    continue; // leading blank lines
                }
                break; // the first processor block ended
            }
            let Some((key, value)) = line.split_once(':') else {
                continue;
            };
            let value = value.trim();
            match key.trim() {
                "vendor_id" => id.vendor = clean(Some(value)),
                "model name" => id.brand = clean(Some(value)),
                "cpu family" => id.family = value.parse().ok(),
                "model" => id.model = value.parse().ok(),
                "stepping" => id.stepping = value.parse().ok(),
                "microcode" => {
                    id.microcode = value
                        .strip_prefix("0x")
                        .or_else(|| value.strip_prefix("0X"))
                        .and_then(|h| u32::from_str_radix(h, 16).ok())
                        .and_then(revision_hex);
                }
                _ => {}
            }
        }
        (!id.is_empty()).then_some(id)
    }

    /// Read the CPU identity of this host; `None` when it cannot be read.
    pub fn read() -> Option<CpuIdentity> {
        #[cfg(windows)]
        {
            read_windows()
        }
        #[cfg(target_os = "linux")]
        {
            parse_cpuinfo(&std::fs::read_to_string("/proc/cpuinfo").ok()?)
        }
        #[cfg(not(any(windows, target_os = "linux")))]
        {
            None
        }
    }

    /// One bounded PowerShell read of `HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0`.
    /// REG_BINARY revisions leave as hex so the decoding (which depends on the vendor)
    /// happens in [`from_registry_probe`].
    #[cfg(windows)]
    fn read_windows() -> Option<CpuIdentity> {
        const SCRIPT: &str = r#"
try {
  $k = Get-ItemProperty -Path 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0' -ErrorAction Stop
} catch { return }
function Rev($v) {
  if ($v -is [byte[]]) { return (($v | ForEach-Object { $_.ToString('x2') }) -join '') }
  if ($null -ne $v) { return ('dw:' + [string]$v) }
  return $null
}
[pscustomobject]@{
  brand      = [string]$k.ProcessorNameString
  vendor     = [string]$k.VendorIdentifier
  identifier = [string]$k.Identifier
  update     = (Rev $k.'Update Revision')
  previous   = (Rev $k.'Previous Update Revision')
} | ConvertTo-Json -Compress
"#;
        from_registry_probe(&crate::telemetry::collectors::winps::run_json(SCRIPT)?)
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Map the OS build number to a Microsoft end-of-servicing date and set
    /// `eol`/`eol_date`; `crit` past EOL, `warn` within 90 days.
    pub fn collect(name: String, version: String, long: String, cpu: Value) -> Section {
        // Build number from the registry (CurrentBuildNumber + UBR) is the most
        // reliable; fall back to whatever sysinfo gave us.
        let build = winps::run_text(
            "(Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion').CurrentBuildNumber",
        )
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

        let build_num: Option<u32> = build.as_deref().and_then(|b| b.parse().ok());
        let eol_date = build_num.and_then(eol_for_build);

        // Compare eol_date to now to derive status.
        let now = chrono::Utc::now();
        let (eol, status, summary) = match eol_date {
            Some(d) => {
                let parsed = chrono::DateTime::parse_from_rfc3339(d).ok();
                match parsed {
                    Some(end) => {
                        let days = (end.with_timezone(&chrono::Utc) - now).num_days();
                        if days <= 0 {
                            (true, Status::Crit, format!("{long} is end-of-life"))
                        } else if days <= 90 {
                            (
                                false,
                                Status::Warn,
                                format!("{long} end-of-life in {days}d"),
                            )
                        } else {
                            (false, Status::Ok, long.clone())
                        }
                    }
                    None => (false, Status::Ok, long.clone()),
                }
            }
            None => (false, Status::Ok, long.clone()),
        };

        Section::with_fields(
            status,
            summary,
            json!({
                "name": name,
                "version": version,
                "build": build,
                "eol": eol,
                "eol_date": eol_date,
                "arch": crate::util::arch(),
                "channel": crate::BUILD_CHANNEL,
                "cpu": cpu,
            }),
        )
    }

    /// Best-effort build-number → end-of-servicing date (Home/Pro consumer track),
    /// as published by Microsoft's lifecycle pages. Returns an ISO-8601 date.
    fn eol_for_build(build: u32) -> Option<&'static str> {
        // Windows 11 builds.
        let date = match build {
            // Windows 10 (all consumer editions retire 2025-10-14).
            10240 | 10586 | 14393 | 15063 | 16299 | 17134 | 17763 | 18362 | 18363 | 19041
            | 19042 | 19043 | 19044 | 19045 => "2025-10-14T00:00:00Z",
            // Windows 11 21H2.
            22000 => "2023-10-10T00:00:00Z",
            // Windows 11 22H2.
            22621 => "2024-10-08T00:00:00Z",
            // Windows 11 23H2.
            22631 => "2025-11-11T00:00:00Z",
            // Windows 11 24H2.
            26100 => "2026-10-13T00:00:00Z",
            _ => return None,
        };
        Some(date)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn os_support_section_is_valid() {
        let v = collect().into_value();
        assert!(v["name"].is_string());
    }

    #[test]
    fn os_support_section_reports_arch() {
        let v = collect().into_value();
        assert!(matches!(
            v["arch"].as_str(),
            Some("x86_64") | Some("aarch64")
        ));
        assert_eq!(v["arch"].as_str().unwrap(), crate::util::arch());
    }

    #[test]
    fn os_support_section_reports_channel() {
        let v = collect().into_value();
        // Test builds never set KENNY_AGENT_CHANNEL, so this asserts the
        // build.rs default (`stable`) as well as the field's presence.
        assert_eq!(v["channel"].as_str().unwrap(), crate::BUILD_CHANNEL);
        assert_eq!(v["channel"].as_str().unwrap(), "stable");
    }

    #[test]
    fn the_identifier_string_is_parsed() {
        assert_eq!(
            cpu::parse_identifier("Intel64 Family 6 Model 183 Stepping 1"),
            Some((6, 183, 1))
        );
        assert_eq!(
            cpu::parse_identifier("AMD64 Family 25 Model 80 Stepping 0"),
            Some((25, 80, 0))
        );
        assert_eq!(
            cpu::parse_identifier("x86 family 6 model 15 stepping 11"),
            Some((6, 15, 11))
        );
        assert_eq!(cpu::parse_identifier("ARMv8 (64-bit) Processor"), None);
        assert_eq!(cpu::parse_identifier(""), None);
    }

    #[test]
    fn update_revision_is_the_high_dword_on_intel_and_the_low_dword_on_amd() {
        // Intel: low dword 0, high dword 0x12b.
        let intel = [0, 0, 0, 0, 0x2b, 0x01, 0, 0];
        assert_eq!(
            cpu::decode_update_revision(&intel, "GenuineIntel").as_deref(),
            Some("0x12b")
        );
        // AMD: low dword 0x0a50000c, high dword 0.
        let amd = [0x0c, 0x00, 0x50, 0x0a, 0, 0, 0, 0];
        assert_eq!(
            cpu::decode_update_revision(&amd, "AuthenticAMD").as_deref(),
            Some("0xa50000c")
        );
        // The wrong dword for the vendor is zero, i.e. unknown, never a wrong revision.
        assert_eq!(cpu::decode_update_revision(&intel, "AuthenticAMD"), None);
        assert_eq!(cpu::decode_update_revision(&amd, "GenuineIntel"), None);
        // Too short, or a vendor whose layout is unknown.
        assert_eq!(
            cpu::decode_update_revision(&[1, 2, 3], "AuthenticAMD"),
            None
        );
        assert_eq!(
            cpu::decode_update_revision(&[0, 0, 0, 0, 1], "GenuineIntel"),
            None
        );
        assert_eq!(cpu::decode_update_revision(&intel, "Qualcomm"), None);
    }

    const WINDOWS_PROBE: &str = r#"{
        "brand": "13th Gen Intel(R) Core(TM) i7-13700K  ",
        "vendor": "GenuineIntel",
        "identifier": "Intel64 Family 6 Model 183 Stepping 1",
        "update": "000000002b010000",
        "previous": "0000000023010000"
    }"#;

    const LINUX_CPUINFO: &str = "processor\t: 0\n\
        vendor_id\t: AuthenticAMD\n\
        cpu family\t: 25\n\
        model\t\t: 80\n\
        model name\t: AMD Ryzen 5 5600G with Radeon Graphics\n\
        stepping\t: 0\n\
        microcode\t: 0xa50000c\n\
        cpu MHz\t\t: 3900.000\n\
        \n\
        processor\t: 1\n\
        vendor_id\t: AuthenticAMD\n\
        model\t\t: 99\n\
        microcode\t: 0x1\n";

    fn fixture_cpu(file: &str) -> Value {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/fixtures")
            .join(file);
        let fixture: Value =
            serde_json::from_str(&std::fs::read_to_string(path).expect("read the fixture"))
                .expect("parse the fixture");
        fixture["snapshot"]["os_support"]["cpu"].clone()
    }

    /// The Windows golden fixture's `cpu` is what the registry probe's JSON decodes to.
    #[test]
    fn the_windows_fixture_cpu_is_what_the_registry_probe_decodes_to() {
        let probe: Value = serde_json::from_str(WINDOWS_PROBE).unwrap();
        let cpu = cpu::from_registry_probe(&probe)
            .expect("identity")
            .to_value();
        assert_eq!(cpu, fixture_cpu("telemetry_snapshot.json"));
    }

    /// The Linux golden fixture's `cpu` is what `/proc/cpuinfo` parses to.
    #[test]
    fn the_linux_fixture_cpu_is_what_cpuinfo_parses_to() {
        let cpu = cpu::parse_cpuinfo(LINUX_CPUINFO)
            .expect("identity")
            .to_value();
        assert_eq!(cpu, fixture_cpu("telemetry_snapshot_linux.json"));
    }

    #[test]
    fn cpuinfo_reads_only_the_first_processor_block() {
        let id = cpu::parse_cpuinfo(LINUX_CPUINFO).unwrap();
        assert_eq!(id.model, Some(80));
        assert_eq!(id.microcode.as_deref(), Some("0xa50000c"));
        assert_eq!(id.microcode_bios, None);
    }

    #[test]
    fn a_cpuinfo_without_identity_is_unknown_and_zero_microcode_is_unreported() {
        assert_eq!(cpu::parse_cpuinfo(""), None);
        assert_eq!(
            cpu::parse_cpuinfo("processor : 0\nBogoMIPS : 50.00\n"),
            None
        );
        let id = cpu::parse_cpuinfo("vendor_id : GenuineIntel\nmicrocode : 0x0\n").unwrap();
        assert_eq!(id.vendor.as_deref(), Some("GenuineIntel"));
        assert_eq!(id.microcode, None);
        // Unreadable registry probe output carries no identity either.
        assert_eq!(cpu::from_registry_probe(&json!({})), None);
        assert_eq!(cpu::from_registry_probe(&json!({"brand": "  "})), None);
    }

    #[test]
    fn a_dword_revision_from_the_registry_is_the_revision_itself() {
        let probe = json!({"vendor": "AuthenticAMD", "update": "dw:173015052", "previous": "dw:0"});
        let id = cpu::from_registry_probe(&probe).unwrap();
        assert_eq!(id.microcode.as_deref(), Some("0xa50000c"));
        assert_eq!(id.microcode_bios, None);
    }

    /// Every field the golden fixtures carry for `os_support` is one the collector emits
    /// (a null may be absent from the payload, so only the fixture's keys are required).
    #[test]
    fn the_collector_emits_every_key_the_fixtures_carry() {
        let v = collect().into_value();
        for file in ["telemetry_snapshot.json", "telemetry_snapshot_linux.json"] {
            let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .join("../docs/fixtures")
                .join(file);
            let fixture: Value =
                serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
            for key in fixture["snapshot"]["os_support"]
                .as_object()
                .unwrap()
                .keys()
            {
                assert!(
                    v.get(key).is_some(),
                    "{file}: os_support.{key} is not emitted"
                );
            }
        }
        // `cpu` is an object when readable and null when not, never absent or a string.
        assert!(v["cpu"].is_object() || v["cpu"].is_null());
        if cfg!(target_os = "linux") {
            assert!(
                v["cpu"].is_object(),
                "/proc/cpuinfo is readable on Linux CI"
            );
        }
    }
}
