//! `firewall` section — Windows Firewall profile state.
//!
//! Real data from `Get-NetFirewallProfile` on Windows.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

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
    use crate::telemetry::collectors::winps;

    /// `Get-NetFirewallProfile` into `{name, enabled}` per profile
    /// (Domain/Private/Public); `warn` if any profile is disabled.
    pub fn collect() -> Section {
        let script = r#"
Get-NetFirewallProfile | ForEach-Object {
  [pscustomobject]@{ name = [string]$_.Name; enabled = [bool]$_.Enabled }
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Warn,
                "firewall state unavailable",
                json!({ "profiles": [] }),
            );
        };
        let profiles = winps::as_array(v);

        let off: Vec<String> = profiles
            .iter()
            .filter(|p| p.get("enabled").and_then(Value::as_bool) == Some(false))
            .filter_map(|p| p.get("name").and_then(Value::as_str).map(str::to_string))
            .collect();

        let (status, summary) = if off.is_empty() {
            (Status::Ok, "all profiles on".to_string())
        } else {
            (Status::Warn, format!("{} profile(s) off", off.join(", ")))
        };
        Section::with_fields(status, summary, json!({ "profiles": profiles }))
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
