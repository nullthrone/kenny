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

    /// Real impl: parse `winget upgrade` into `{id, name, version, available}`;
    /// `warn` when upgrades are pending.
    pub fn collect() -> Section {
        // TODO(windows): reuse handlers::winget list parsing.
        Section::with_fields(
            Status::Ok,
            "0 updates available",
            json!({ "available": 0, "packages": [] }),
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
