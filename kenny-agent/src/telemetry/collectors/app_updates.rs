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
    use serde_json::Value;
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

        let packages = parse_upgrade_table(&raw);
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

    /// Parse winget's fixed-width upgrade table. Columns are located by the header
    /// line (`Name  Id  Version  Available  Source`); rows below the `---` divider
    /// are sliced by those column offsets.
    fn parse_upgrade_table(raw: &str) -> Vec<Value> {
        let lines: Vec<&str> = raw.lines().collect();
        // Find the header row containing "Name" and "Id".
        let header_idx = lines.iter().position(|l| {
            let t = l.trim_start();
            t.starts_with("Name") && l.contains("Id") && l.contains("Version")
        });
        let Some(hidx) = header_idx else {
            return Vec::new();
        };
        let header = lines[hidx];
        // Column start offsets by header keyword.
        let id_col = header.find("Id");
        let ver_col = header.find("Version");
        let avail_col = header.find("Available");
        let (Some(id_col), Some(ver_col), Some(avail_col)) = (id_col, ver_col, avail_col) else {
            return Vec::new();
        };
        let src_col = header.find("Source");

        let mut packages = Vec::new();
        for line in lines.iter().skip(hidx + 1) {
            // Skip the divider and the trailing "N upgrades available" footer.
            if line.trim().is_empty() || line.trim_start().starts_with('-') {
                continue;
            }
            // Footer lines have no column structure; require the line be long
            // enough to reach the Available column and not start with a digit-word.
            if line.chars().count() < avail_col {
                continue;
            }
            let name = slice_cols(line, 0, id_col).trim().to_string();
            let id = slice_cols(line, id_col, ver_col).trim().to_string();
            let version = slice_cols(line, ver_col, avail_col).trim().to_string();
            let available = match src_col {
                Some(s) => slice_cols(line, avail_col, s).trim().to_string(),
                None => line
                    .chars()
                    .skip(avail_col)
                    .collect::<String>()
                    .trim()
                    .to_string(),
            };
            if id.is_empty() || name.is_empty() {
                continue;
            }
            packages.push(json!({
                "id": id,
                "name": name,
                "version": version,
                "available": available,
            }));
        }
        packages
    }

    /// Slice a line by character offsets `[start, end)`, tolerating short lines and
    /// multibyte characters (winget pads with Unicode where locales vary).
    fn slice_cols(line: &str, start: usize, end: usize) -> String {
        line.chars()
            .skip(start)
            .take(end.saturating_sub(start))
            .collect()
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
