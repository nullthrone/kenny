//! `peripherals` section — attached devices (monitors, USB, audio).
//!
//! Real inventory comes from CIM (`Win32_PnPEntity`) on Windows. Off Windows we
//! report a portable `n/a` stub; the section still carries `status`/`summary`.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `peripherals` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "devices": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Enumerate `Win32_PnPEntity` into `{name, class, status}`; `warn` when devices
    /// report an error status.
    pub fn collect() -> Section {
        // Limit to devices with a present name to avoid hundreds of phantom rows.
        let script = r#"
Get-CimInstance -ClassName Win32_PnPEntity | Where-Object { $_.Name } | ForEach-Object {
  [pscustomobject]@{
    name   = [string]$_.Name
    class  = [string]$_.PNPClass
    status = [string]$_.Status
  }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "devices unavailable",
                json!({ "devices": [] }),
            );
        };
        let devices = winps::as_array(v);

        let errored = devices
            .iter()
            .filter(|d| {
                d.get("status")
                    .and_then(Value::as_str)
                    .map(|s| !s.eq_ignore_ascii_case("OK") && !s.is_empty())
                    .unwrap_or(false)
            })
            .count();

        let total = devices.len();
        let (status, summary) = if errored > 0 {
            (
                Status::Warn,
                format!("{errored} of {total} devices with errors"),
            )
        } else {
            (Status::Ok, format!("{total} devices OK"))
        };
        Section::with_fields(status, summary, json!({ "devices": devices }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peripherals_section_is_valid() {
        let v = collect().into_value();
        assert!(v["devices"].is_array());
    }
}
