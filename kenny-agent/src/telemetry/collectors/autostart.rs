//! `autostart` section — startup programs.
//!
//! Real data from Run keys / Startup folders / `Get-CimInstance Win32_StartupCommand`
//! on Windows. Shares the `{name, command, location}` shape with `diag_autostart`.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `autostart` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        Section::with_fields(Status::Ok, "n/a on this platform", json!({ "entries": [] }))
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// `Win32_StartupCommand` plus the HKLM/HKCU `Run` keys into
    /// `{name, command, location}`.
    pub fn collect() -> Section {
        let script = r#"
$out = @()
Get-CimInstance -ClassName Win32_StartupCommand -ErrorAction SilentlyContinue | ForEach-Object {
  $out += [pscustomobject]@{ name = [string]$_.Name; command = [string]$_.Command; location = [string]$_.Location }
}
foreach ($root in 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run','HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run') {
  try {
    $props = Get-ItemProperty -Path $root -ErrorAction Stop
    foreach ($p in $props.PSObject.Properties) {
      if ($p.Name -like 'PS*') { continue }
      $out += [pscustomobject]@{ name = [string]$p.Name; command = [string]$p.Value; location = $root }
    }
  } catch {}
}
ConvertTo-Json -Compress @($out)
"#;

        let entries = winps::run_json(script)
            .map(winps::as_array)
            .unwrap_or_default();

        let count = entries.len();
        Section::with_fields(
            Status::Ok,
            format!("{count} startup entries"),
            json!({ "entries": entries }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn autostart_section_is_valid() {
        assert!(collect().into_value()["entries"].is_array());
    }
}
