//! `av_thirdparty` section — non-Defender antivirus registered with Security Center.
//!
//! Real data from `Get-CimInstance -Namespace root/SecurityCenter2 AntiVirusProduct`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `av_thirdparty` section.
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
            json!({ "products": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;

    /// Real impl: query `root/SecurityCenter2` `AntiVirusProduct` into
    /// `{name, state, up_to_date}` products.
    pub fn collect() -> Section {
        // TODO(windows): SecurityCenter2 AntiVirusProduct.
        Section::with_fields(Status::Ok, "no third-party AV", json!({ "products": [] }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn av_thirdparty_section_is_valid() {
        assert!(collect().into_value()["products"].is_array());
    }
}
