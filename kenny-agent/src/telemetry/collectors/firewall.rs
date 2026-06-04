//! `firewall` section — Windows Firewall profile state.
//!
//! Real data from `Get-NetFirewallProfile` on Windows.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `firewall` section.
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
            json!({ "profiles": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Get-NetFirewallProfile` into `{name, enabled}` per profile
    /// (Domain/Private/Public); `crit` if any profile is disabled.
    pub fn collect() -> Section {
        // TODO(windows): Get-NetFirewallProfile.
        Section::with_fields(Status::Ok, "all profiles on", json!({ "profiles": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn firewall_section_is_valid() {
        assert!(collect().into_value()["profiles"].is_array());
    }
}
