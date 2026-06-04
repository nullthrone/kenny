//! `time_sync` section — system clock synchronization state.
//!
//! Real data from `w32tm /query /status` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `time_sync` section.
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
            json!({ "synchronized": null, "source": null, "offset_secs": null }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Parse `w32tm /query /status` for source and last-sync error (offset); `warn`
    /// when the offset is large or the time service is not running.
    pub fn collect() -> Section {
        let Some(raw) = winps::run_command("w32tm", &["/query", "/status"]) else {
            return Section::with_fields(
                Status::Warn,
                "time service not running",
                json!({ "synchronized": false, "source": null, "offset_secs": null }),
            );
        };

        let mut source: Option<String> = None;
        let mut offset_secs: Option<f64> = None;
        for line in raw.lines() {
            let Some((k, v)) = line.split_once(':') else {
                continue;
            };
            let key = k.trim().to_lowercase();
            let val = v.trim();
            match key.as_str() {
                "source" => source = Some(val.to_string()),
                // e.g. "Last Successful Sync Time" or "Phase Offset: 0.0123456s"
                "phase offset" => {
                    offset_secs = val
                        .trim_end_matches('s')
                        .trim()
                        .parse::<f64>()
                        .ok();
                }
                _ => {}
            }
        }

        // Treat large skew (>5s) or a missing source as a warning.
        let big_skew = offset_secs.map(|o| o.abs() > 5.0).unwrap_or(false);
        let synchronized = source
            .as_deref()
            .map(|s| !s.eq_ignore_ascii_case("Local CMOS Clock") && !s.is_empty())
            .unwrap_or(false);

        let (status, summary) = if big_skew {
            (
                Status::Warn,
                format!("clock offset {:.2}s", offset_secs.unwrap_or(0.0)),
            )
        } else if !synchronized {
            (Status::Warn, "clock not network-synchronized".to_string())
        } else {
            (Status::Ok, "clock synchronized".to_string())
        };

        Section::with_fields(
            status,
            summary,
            json!({
                "synchronized": synchronized,
                "source": source,
                "offset_secs": offset_secs,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn time_sync_section_is_valid() {
        assert!(collect().into_value()["status"].is_string());
    }
}
