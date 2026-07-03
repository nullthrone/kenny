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
        //
        // The whole history read is wrapped in try/catch: the WUA COM history API
        // (`GetTotalHistoryCount`/`QueryHistory`) throws terminating errors on many
        // Windows configurations (managed hosts, the newer update stack, a stopped
        // `wuauserv`). On any such throw we emit no stdout, so `run_json` returns
        // `None` and the collector takes the "unavailable" fallback below rather
        // than aborting the process with a partially-built result.
        let script = r#"
$ErrorActionPreference = 'Stop'
try {
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
} catch {
  # Emit nothing: the Rust caller treats no-JSON as "history unavailable".
}
"#;

        let Some(v) = winps::run_json(script) else {
            // Not being able to read the update history is a visibility gap, not an
            // update failure — reporting `warn` here fires a permanent false alarm
            // (the server rule can only downgrade to "ok", so worst-of pins the
            // section at warn forever). Mirror `disk_smart`'s "SMART unavailable"
            // fallback: stay `ok`; only actually-failed updates warrant a warn, and
            // that is decided from `recent` below and by the server health rule.
            return Section::with_fields(
                Status::Ok,
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
                u.get("result")
                    .and_then(Value::as_str)
                    .map(str::to_lowercase)
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
