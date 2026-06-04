//! `battery` section — battery charge/health (laptops).
//!
//! Real data from `Win32_Battery` / battery report on Windows. Desktops report
//! `present: false`.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `battery` section.
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
            json!({ "present": false }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Battery state into `{present, charge_percent, health_percent, status}`.
    ///
    /// `health_percent` is full-charge capacity ÷ design capacity, read from the
    /// `root/wmi` `BatteryFullChargedCapacity` + `BatteryStaticData` classes; the
    /// live charge comes from `Win32_Battery`.
    pub fn collect() -> Section {
        let script = r#"
$batt = Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $batt) { '{"present":false}' ; exit }
$design = $null; $full = $null
try { $design = (Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData -ErrorAction Stop | Select-Object -First 1).DesignedCapacity } catch {}
try { $full = (Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity -ErrorAction Stop | Select-Object -First 1).FullChargedCapacity } catch {}
$health = $null
if ($design -and $design -gt 0 -and $full) { $health = [Math]::Round(($full / $design) * 100.0, 0) }
[pscustomobject]@{
  present        = $true
  charge_percent = [int]$batt.EstimatedChargeRemaining
  health_percent = $health
  status         = [string]$batt.BatteryStatus
} | ConvertTo-Json -Compress
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Ok,
                "battery status unavailable",
                json!({ "present": false }),
            );
        };

        if v.get("present").and_then(Value::as_bool) != Some(true) {
            return Section::with_fields(Status::Ok, "no battery", json!({ "present": false }));
        }

        let health = v.get("health_percent").and_then(Value::as_f64);
        let charge = v.get("charge_percent").and_then(Value::as_i64);
        let battery_status = v.get("status").cloned().unwrap_or(Value::Null);

        let (status, summary) = match health {
            Some(h) if h < 50.0 => (Status::Crit, format!("battery health {h:.0}% (<50%)")),
            Some(h) if h < 70.0 => (Status::Warn, format!("battery health {h:.0}% (<70%)")),
            Some(h) => (Status::Ok, format!("battery health {h:.0}%")),
            None => (
                Status::Ok,
                match charge {
                    Some(c) => format!("battery {c}% charged"),
                    None => "battery present".to_string(),
                },
            ),
        };

        Section::with_fields(
            status,
            summary,
            json!({
                "present": true,
                "charge_percent": charge,
                "health_percent": health,
                "status": battery_status,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn battery_section_is_valid() {
        assert!(collect().into_value()["present"].is_boolean());
    }
}
