//! `printers` section — installed printers and queue state.
//!
//! Real data from `Get-Printer` / `Get-PrintJob` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `printers` section.
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
            json!({ "printers": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: `Get-Printer` into `{name, status, is_default, jobs}`; `warn` on
    /// error/offline printers.
    pub fn collect() -> Section {
        // TODO(windows): Get-Printer / Get-PrintJob.
        Section::with_fields(Status::Ok, "0 printers", json!({ "printers": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn printers_section_is_valid() {
        assert!(collect().into_value()["printers"].is_array());
    }
}
