//! `win_update` section — Windows Update history.
//!
//! Real data from the Windows Update agent COM API / `Get-WUHistory`. Off Windows
//! we stub.

use serde_json::json;
#[cfg(windows)]
use serde_json::Value;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `win_update` section.
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
            json!({ "last_check": null, "recent": [] }),
        )
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;

    /// Enumerate update history via the WUA COM API (`Microsoft.Update.Session`
    /// `QueryHistory`) into `{kb, title, result, installed_at}` entries.
    pub fn collect() -> Section {
        // ResultCode: 1=InProgress 2=Succeeded 3=SucceededWithErrors 4=Failed
        // 5=Aborted. We map 2/3 -> succeeded, everything else -> failed/other.
        // The KB number is parsed out of the update Title.
        let script = r#"
$session = New-Object -ComObject Microsoft.Update.Session
$searcher = $session.CreateUpdateSearcher()
$count = $searcher.GetTotalHistoryCount()
$recent = @()
if ($count -gt 0) {
  $take = [Math]::Min($count, 25)
  foreach ($h in $searcher.QueryHistory(0, $take)) {
    $kb = $null
    if ($h.Title -match 'KB(\d+)') { $kb = "KB$($matches[1])" }
    $result = switch ($h.ResultCode) { 2 { "succeeded" } 3 { "succeeded" } default { "failed" } }
    $recent += [pscustomobject]@{
      kb           = $kb
      title        = [string]$h.Title
      result       = $result
      installed_at = if ($h.Date) { (Get-Date $h.Date).ToUniversalTime().ToString("o") } else { $null }
    }
  }
}
$lastCheck = $null
try {
  $auto = New-Object -ComObject Microsoft.Update.AutoUpdate
  if ($auto.Results.LastSearchSuccessDate) {
    $lastCheck = (Get-Date $auto.Results.LastSearchSuccessDate).ToUniversalTime().ToString("o")
  }
} catch {}
[pscustomobject]@{ last_check = $lastCheck; recent = $recent } | ConvertTo-Json -Compress -Depth 4
"#;

        let Some(v) = winps::run_json(script) else {
            return Section::with_fields(
                Status::Warn,
                "update history unavailable",
                json!({ "last_check": null, "recent": [] }),
            );
        };

        let last_check = v
            .get("last_check")
            .and_then(Value::as_str)
            .map(str::to_string);
        let recent = v
            .get("recent")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default();

        let failed = recent
            .iter()
            .filter(|u| {
                u.get("result").and_then(Value::as_str).map(str::to_lowercase)
                    == Some("failed".to_string())
            })
            .count();

        let (status, summary) = if failed > 0 {
            (
                Status::Warn,
                format!("{failed} update(s) failed in recent history"),
            )
        } else {
            (Status::Ok, "updates healthy".to_string())
        };
        Section::with_fields(
            status,
            summary,
            json!({ "last_check": last_check, "recent": recent }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn win_update_section_is_valid() {
        let v = collect().into_value();
        assert!(v["recent"].is_array());
    }
}
