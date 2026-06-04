//! `os_support` section — OS edition/build and end-of-support posture.
//!
//! Portable basics (name/version) via `sysinfo`; Windows enriches with build and
//! support lifecycle.

use serde_json::json;
use sysinfo::System;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `os_support` section.
pub fn collect() -> Section {
    let name = System::name().unwrap_or_else(|| "unknown".to_string());
    let version = System::os_version().unwrap_or_else(|| "unknown".to_string());
    let long = System::long_os_version().unwrap_or_else(|| name.clone());

    #[cfg(windows)]
    {
        windows_impl::collect(name, version, long)
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(
            Status::Ok,
            long,
            json!({ "name": name, "version": version, "build": null, "eol": null, "eol_date": null }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Map the OS build number to a Microsoft end-of-servicing date and set
    /// `eol`/`eol_date`; `crit` past EOL, `warn` within 90 days.
    pub fn collect(name: String, version: String, long: String) -> Section {
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
}
