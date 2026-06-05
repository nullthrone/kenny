//! `app_updates` section — count of available application upgrades.
//!
//! Real data from `winget upgrade` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `app_updates` section.
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
            json!({ "available": 0, "packages": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::process::Command;

    /// Parse `winget upgrade` into `{id, name, version, available}` rows; `warn`
    /// when upgrades are pending.
    pub fn collect() -> Section {
        let output = Command::new("winget")
            .args(["upgrade", "--include-unknown", "--accept-source-agreements"])
            .output();

        let raw = match output {
            Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).into_owned(),
            _ => {
                return Section::with_fields(
                    Status::Ok,
                    "winget upgrade unavailable",
                    json!({ "available": 0, "packages": [] }),
                );
            }
        };

        let packages = crate::handlers::winget::parse_table(&raw);
        let available = packages.len();
        let (status, summary) = if available == 0 {
            (Status::Ok, "0 updates available".to_string())
        } else {
            (Status::Warn, format!("{available} app update(s) available"))
        };
        Section::with_fields(
            status,
            summary,
            json!({ "available": available, "packages": packages }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn app_updates_section_is_valid() {
        assert!(collect().into_value()["packages"].is_array());
    }
}
