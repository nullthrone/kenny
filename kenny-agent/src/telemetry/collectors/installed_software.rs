//! `installed_software` section — machine-wide program inventory.
//!
//! Real data from the registry Uninstall keys (HKLM 64-bit + WOW6432Node 32-bit)
//! via `Get-ItemProperty` on Windows — deliberately **not** `Win32_Product` (its
//! enumeration triggers MSI reconfiguration) and not `winget list` (too slow for
//! the probe budget). Per-user (HKCU) installs are invisible to the session-0
//! service and are a documented blind spot (docs/protocol.md v0.10). Entries
//! without a `DisplayName` and system components (`SystemComponent=1`) are
//! filtered out in the probe.
//!
//! Deduplicated by name, sorted by name, capped at 300 with a `truncated` flag;
//! `count` is the deduplicated total *before* the cap.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `installed_software` section.
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
            Status::Ok,
            "n/a on this platform",
            json!({ "apps": [], "count": 0, "truncated": false }),
        )
    }
}

/// Portable shaping core — compiled and tested on every platform.
///
/// Row parsing, `InstallDate` normalization, and the dedupe/sort/cap live here so
/// Linux CI exercises them; the only non-test consumer is the Windows collector.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    /// Contract cap on the `apps` list.
    pub const MAX_APPS: usize = 300;

    /// One installed program, as read from an Uninstall registry key.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct App {
        pub name: String,
        pub version: Option<String>,
        pub publisher: Option<String>,
        /// Raw registry `InstallDate` (usually `yyyyMMdd`), normalized on render.
        pub install_date: Option<String>,
    }

    impl App {
        /// Build from one probe row; rows without a non-empty `name` are dropped.
        pub fn from_row(row: &Value) -> Option<App> {
            let name = str_field(row, "name")?;
            Some(App {
                name,
                version: str_field(row, "version"),
                publisher: str_field(row, "publisher"),
                install_date: str_field(row, "install_date"),
            })
        }
    }

    /// Non-empty, trimmed string field of a probe row.
    fn str_field(row: &Value, key: &str) -> Option<String> {
        row.get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    }

    /// Normalize a registry `InstallDate` to `yyyy-MM-dd`, or `None` when unusable.
    ///
    /// The value is unvalidated vendor input: usually `yyyyMMdd`, sometimes already
    /// dashed, occasionally garbage. Only plausible dates pass.
    pub fn normalize_install_date(raw: &str) -> Option<String> {
        let s = raw.trim();
        if !s.is_ascii() {
            return None;
        }
        // Accept an already-dashed `yyyy-MM-dd` by compacting it first.
        let compact = if s.len() == 10 && s.as_bytes()[4] == b'-' && s.as_bytes()[7] == b'-' {
            format!("{}{}{}", &s[0..4], &s[5..7], &s[8..10])
        } else {
            s.to_string()
        };
        if compact.len() != 8 || !compact.bytes().all(|b| b.is_ascii_digit()) {
            return None;
        }
        let month: u32 = compact[4..6].parse().ok()?;
        let day: u32 = compact[6..8].parse().ok()?;
        if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
            return None;
        }
        Some(format!(
            "{}-{}-{}",
            &compact[0..4],
            &compact[4..6],
            &compact[6..8]
        ))
    }

    /// Dedupe by name (first entry wins), sort by name, cap at [`MAX_APPS`].
    /// Returns `(apps, count_before_cap, truncated)`.
    pub fn shape(apps: Vec<App>) -> (Vec<Value>, usize, bool) {
        use std::collections::BTreeMap;

        let mut by_name: BTreeMap<String, App> = BTreeMap::new();
        for app in apps {
            by_name.entry(app.name.clone()).or_insert(app);
        }
        let count = by_name.len();
        let truncated = count > MAX_APPS;
        let out = by_name
            .into_values()
            .take(MAX_APPS)
            .map(|a| {
                json!({
                    "name": a.name,
                    "version": a.version,
                    "publisher": a.publisher,
                    "install_date": a.install_date.as_deref().and_then(normalize_install_date),
                })
            })
            .collect();
        (out, count, truncated)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn normalize_install_date_handles_common_shapes() {
            assert_eq!(
                normalize_install_date("20260311").as_deref(),
                Some("2026-03-11")
            );
            assert_eq!(
                normalize_install_date("2026-03-11").as_deref(),
                Some("2026-03-11")
            );
            assert_eq!(
                normalize_install_date(" 20260311 ").as_deref(),
                Some("2026-03-11")
            );
            // Garbage in, null out.
            assert_eq!(normalize_install_date(""), None);
            assert_eq!(normalize_install_date("11/03/2026"), None);
            assert_eq!(normalize_install_date("20261341"), None); // month 13
            assert_eq!(normalize_install_date("20260300"), None); // day 0
            assert_eq!(normalize_install_date("not-a-date"), None);
            assert_eq!(normalize_install_date("2026年03月11"), None);
        }

        #[test]
        fn from_row_requires_name_and_trims() {
            let row = json!({ "name": " 7-Zip ", "version": "24.08", "publisher": "", "install_date": "20260311" });
            let app = App::from_row(&row).unwrap();
            assert_eq!(app.name, "7-Zip");
            assert_eq!(app.version.as_deref(), Some("24.08"));
            assert_eq!(app.publisher, None, "empty string becomes None");
            assert_eq!(app.install_date.as_deref(), Some("20260311"));
            assert!(App::from_row(&json!({ "version": "1.0" })).is_none());
            assert!(App::from_row(&json!({ "name": "  " })).is_none());
        }

        #[test]
        fn shape_dedupes_sorts_and_normalizes_dates() {
            let apps = vec![
                App {
                    name: "Zed".into(),
                    version: None,
                    publisher: None,
                    install_date: Some("bogus".into()),
                },
                App {
                    name: "App".into(),
                    version: Some("1".into()),
                    publisher: Some("Acme".into()),
                    install_date: Some("20260311".into()),
                },
                App {
                    name: "App".into(),
                    version: Some("2".into()),
                    publisher: None,
                    install_date: None,
                },
            ];
            let (out, count, truncated) = shape(apps);
            assert_eq!(count, 2);
            assert!(!truncated);
            assert_eq!(out[0]["name"], "App");
            assert_eq!(out[0]["version"], "1", "first duplicate wins");
            assert_eq!(out[0]["install_date"], "2026-03-11");
            assert_eq!(out[1]["name"], "Zed");
            assert_eq!(out[1]["install_date"], Value::Null);
        }

        #[test]
        fn shape_caps_at_300_and_reports_precap_count() {
            let apps: Vec<App> = (0..320)
                .map(|i| App {
                    name: format!("app-{i:04}"),
                    version: None,
                    publisher: None,
                    install_date: None,
                })
                .collect();
            let (out, count, truncated) = shape(apps);
            assert_eq!(out.len(), MAX_APPS);
            assert_eq!(count, 320, "count is the deduped total before the cap");
            assert!(truncated);
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Read both Uninstall hives (64-bit + WOW6432Node) into
    /// `{name, version, publisher, install_date}` rows.
    pub fn collect() -> Section {
        let script = r#"
$out = @()
$roots = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
foreach ($root in $roots) {
  Get-ItemProperty -Path $root -ErrorAction SilentlyContinue | ForEach-Object {
    if (-not $_.DisplayName) { return }
    if ($_.SystemComponent -eq 1) { return }
    $out += [pscustomobject]@{
      name         = [string]$_.DisplayName
      version      = if ($_.DisplayVersion) { [string]$_.DisplayVersion } else { $null }
      publisher    = if ($_.Publisher) { [string]$_.Publisher } else { $null }
      install_date = if ($_.InstallDate) { [string]$_.InstallDate } else { $null }
    }
  }
}
ConvertTo-Json -Compress @($out)
"#;

        let rows = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();
        let apps: Vec<core::App> = rows.iter().filter_map(core::App::from_row).collect();
        let (apps, count, truncated) = core::shape(apps);

        Section::with_fields(
            Status::Ok,
            format!("{count} programs installed"),
            json!({ "apps": apps, "count": count, "truncated": truncated }),
        )
    }
}

#[cfg(target_os = "linux")]
mod linux_impl {
    use super::*;
    use std::process::Command;

    /// Parse `Package\tVersion\tMaintainer` rows (one per line) into apps.
    ///
    /// Works for both the `dpkg-query` and `rpm` layouts: three tab-separated
    /// columns where the third is a maintainer (dpkg) or vendor (rpm). Rows with
    /// an empty package name are dropped; empty version/publisher become `None`.
    fn parse_packages(raw: &str) -> Vec<core::App> {
        raw.lines()
            .filter_map(|line| {
                let mut cols = line.split('\t');
                let name = cols.next().unwrap_or("").trim();
                if name.is_empty() {
                    return None;
                }
                let version = cols.next().map(str::trim).filter(|s| !s.is_empty());
                let publisher = cols.next().map(str::trim).filter(|s| !s.is_empty());
                Some(core::App {
                    name: name.to_string(),
                    version: version.map(str::to_string),
                    publisher: publisher.map(str::to_string),
                    install_date: None,
                })
            })
            .collect()
    }

    /// Run one package-database query, returning its stdout on success.
    fn query(cmd: &str, args: &[&str]) -> Option<String> {
        let out = Command::new(cmd).args(args).output().ok()?;
        if !out.status.success() {
            return None;
        }
        Some(String::from_utf8_lossy(&out.stdout).into_owned())
    }

    /// Query dpkg first, then rpm; `None` when neither package manager is present.
    fn read_packages() -> Option<String> {
        query(
            "dpkg-query",
            &["-W", "-f=${Package}\t${Version}\t${Maintainer}\n"],
        )
        .or_else(|| query("rpm", &["-qa", "--qf", "%{NAME}\t%{VERSION}\t%{VENDOR}\n"]))
    }

    /// Read the package database and shape it into the documented section.
    pub fn collect() -> Section {
        let Some(raw) = read_packages() else {
            return Section::with_fields(
                Status::Ok,
                "n/a on this platform",
                json!({ "apps": [], "count": 0, "truncated": false }),
            );
        };
        let apps = parse_packages(&raw);
        let (apps, count, truncated) = core::shape(apps);

        Section::with_fields(
            Status::Ok,
            format!("{count} packages installed"),
            json!({ "apps": apps, "count": count, "truncated": truncated }),
        )
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn parse_packages_reads_tab_columns() {
            let raw = "\
7zip\t23.01+dfsg-1\tYSTV <ystv@example.org>
bash\t5.2.21-2\tMatthias Klose <doko@debian.org>
\t1.0\tNo Name
coreutils\t\t
";
            let apps = parse_packages(raw);
            assert_eq!(apps.len(), 3, "the nameless row is dropped");
            assert_eq!(apps[0].name, "7zip");
            assert_eq!(apps[0].version.as_deref(), Some("23.01+dfsg-1"));
            assert_eq!(
                apps[0].publisher.as_deref(),
                Some("YSTV <ystv@example.org>")
            );
            assert_eq!(apps[0].install_date, None);
            assert_eq!(apps[1].name, "bash");
            // Empty version/publisher collapse to None.
            assert_eq!(apps[2].name, "coreutils");
            assert_eq!(apps[2].version, None);
            assert_eq!(apps[2].publisher, None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installed_software_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["apps"].is_array());
        assert!(v["count"].is_number());
        assert!(v["truncated"].is_boolean());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn linux_reports_real_packages() {
        // The Linux arm reads the package database; assert the documented shape
        // without pinning a machine-specific count.
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert!(v["summary"].is_string());
        assert!(v["apps"].is_array());
        assert!(v["count"].is_number());
        assert!(v["truncated"].is_boolean());
    }

    #[cfg(all(not(windows), not(target_os = "linux")))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["apps"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
        assert_eq!(v["truncated"], false);
    }
}
