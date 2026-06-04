//! `av_thirdparty` section — non-Defender antivirus registered with Security Center.
//!
//! Real data from `Get-CimInstance -Namespace root/SecurityCenter2 AntiVirusProduct`.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

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
    use crate::telemetry::collectors::winps;

    /// Query `root/SecurityCenter2 AntiVirusProduct` into `{name, state, up_to_date}`.
    ///
    /// `productState` is a packed bitmask; bit `0x1000` (in the second byte)
    /// indicates the product is enabled, and the low byte `0x10` means signatures
    /// are out of date. Windows Defender also registers here; we keep all products
    /// and let the operator distinguish.
    pub fn collect() -> Section {
        // Decode productState in PowerShell where the hex math is clearest.
        let script = r#"
Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop | ForEach-Object {
  $ps = [int]$_.productState
  $enabled = (($ps -band 0x1000) -ne 0)
  $uptodate = (($ps -band 0x10) -eq 0)
  [pscustomobject]@{
    name       = [string]$_.displayName
    state      = if ($enabled) { "enabled" } else { "disabled" }
    up_to_date = [bool]$uptodate
  }
} | ConvertTo-Json -Compress
"#;

        let products: Vec<Value> = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();

        // Third-party = anything not named like Microsoft Defender.
        let third_party: Vec<&Value> = products
            .iter()
            .filter(|p| {
                let name = p
                    .get("name")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_lowercase();
                !name.contains("defender") && !name.contains("windows security")
            })
            .collect();

        let stale = third_party.iter().any(|p| {
            p.get("up_to_date").and_then(Value::as_bool) == Some(false)
        });

        let (status, summary) = if third_party.is_empty() {
            (Status::Ok, "no third-party AV".to_string())
        } else if stale {
            (
                Status::Warn,
                format!("{} third-party AV (signatures stale)", third_party.len()),
            )
        } else {
            (
                Status::Ok,
                format!("{} third-party AV product(s)", third_party.len()),
            )
        };
        Section::with_fields(status, summary, json!({ "products": products }))
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
