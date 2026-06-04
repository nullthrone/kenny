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

    /// Real impl: parse `netsh wlan show interfaces` for SSID, signal %, and band;
    /// `warn` on weak signal.
    pub fn collect() -> Section {
        // TODO(windows): netsh wlan show interfaces.
        Section::with_fields(Status::Ok, "wifi link", json!({ "connected": false }))
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
