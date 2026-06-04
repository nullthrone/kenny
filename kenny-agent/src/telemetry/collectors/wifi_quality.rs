//! `wifi_quality` section — wireless signal strength / link quality.
//!
//! Real data from `netsh wlan show interfaces` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `wifi_quality` section.
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
            json!({ "connected": false }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Parse `netsh wlan show interfaces` for SSID, signal %, and band; `warn` on a
    /// weak signal (< 50%).
    pub fn collect() -> Section {
        let Some(raw) = winps::run_command("netsh", &["wlan", "show", "interfaces"]) else {
            return Section::with_fields(
                Status::Ok,
                "wifi unavailable",
                json!({ "connected": false }),
            );
        };

        // netsh prints "Key : Value" lines. Pull the ones we care about. Field names
        // are localized; we match on the English defaults present on most builds.
        let mut ssid: Option<String> = None;
        let mut signal: Option<u8> = None;
        let mut state: Option<String> = None;
        let mut band: Option<String> = None;
        for line in raw.lines() {
            let Some((k, v)) = line.split_once(':') else {
                continue;
            };
            let key = k.trim().to_lowercase();
            let val = v.trim().to_string();
            match key.as_str() {
                "ssid" => {
                    if ssid.is_none() && !val.is_empty() {
                        ssid = Some(val);
                    }
                }
                "state" => state = Some(val),
                "signal" => {
                    signal = val.trim_end_matches('%').trim().parse::<u8>().ok();
                }
                "band" => band = Some(val),
                _ => {}
            }
        }

        let connected = state
            .as_deref()
            .map(|s| s.eq_ignore_ascii_case("connected"))
            .unwrap_or(false);

        if !connected {
            return Section::with_fields(
                Status::Ok,
                "wifi not connected",
                json!({ "connected": false, "ssid": ssid, "signal_percent": signal, "band": band }),
            );
        }

        let (status, summary) = match signal {
            Some(s) if s < 50 => (
                Status::Warn,
                format!(
                    "weak wifi {}% ({})",
                    s,
                    ssid.as_deref().unwrap_or("?")
                ),
            ),
            Some(s) => (
                Status::Ok,
                format!("{} {}%", ssid.as_deref().unwrap_or("?"), s),
            ),
            None => (
                Status::Ok,
                format!("connected to {}", ssid.as_deref().unwrap_or("?")),
            ),
        };

        Section::with_fields(
            status,
            summary,
            json!({
                "connected": true,
                "ssid": ssid,
                "signal_percent": signal,
                "band": band,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn wifi_quality_section_is_valid() {
        assert!(collect().into_value()["connected"].is_boolean());
    }
}
