//! `scheduled_tasks` section — non-Microsoft scheduled tasks.
//!
//! Real data from `Get-ScheduledTask` on Windows, keeping tasks whose `TaskPath`
//! is *not* under `\Microsoft\` (the persistence surface an operator actually
//! reviews) joined with `Get-ScheduledTaskInfo` for `LastTaskResult`/`NextRunTime`;
//! the reported action is the first action's `Execute` + `Arguments`.
//! `total_count` carries the full task count for context. Deduplicated by
//! `(path, name)`, sorted, capped at 200 with a `truncated` flag.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `scheduled_tasks` section.
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
            json!({ "tasks": [], "count": 0, "total_count": 0, "truncated": false }),
        )
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    /// Contract cap on the `tasks` list.
    pub const MAX_TASKS: usize = 200;

    /// One non-Microsoft scheduled task, as read from the probe.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Task {
        pub path: String,
        pub name: String,
        pub state: Option<String>,
        /// First action's `Execute` + `Arguments`.
        pub action: Option<String>,
        pub run_as: Option<String>,
        pub last_result: Option<i64>,
        /// RFC3339 UTC, when scheduled.
        pub next_run: Option<String>,
    }

    impl Task {
        /// Build from one probe row; rows without path/name are dropped.
        pub fn from_row(row: &Value) -> Option<Task> {
            Some(Task {
                path: row.get("path")?.as_str()?.to_string(),
                name: row.get("name")?.as_str()?.to_string(),
                state: str_field(row, "state"),
                action: str_field(row, "action"),
                run_as: str_field(row, "run_as"),
                last_result: row.get("last_result").and_then(Value::as_i64),
                next_run: str_field(row, "next_run"),
            })
        }
    }

    /// Non-empty, trimmed string field of a probe row.
    fn str_field(row: &Value, key: &str) -> Option<String> {
        row.get(key)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string)
    }

    /// Dedupe by `(path, name)` (first entry wins), sort by that key, cap at
    /// [`MAX_TASKS`]. Returns `(tasks, count_before_cap, truncated)`.
    pub fn shape(tasks: Vec<Task>) -> (Vec<Value>, usize, bool) {
        use std::collections::BTreeMap;

        let mut by_key: BTreeMap<(String, String), Task> = BTreeMap::new();
        for t in tasks {
            by_key.entry((t.path.clone(), t.name.clone())).or_insert(t);
        }
        let count = by_key.len();
        let truncated = count > MAX_TASKS;
        let out = by_key
            .into_values()
            .take(MAX_TASKS)
            .map(|t| {
                json!({
                    "path": t.path,
                    "name": t.name,
                    "state": t.state,
                    "action": t.action,
                    "run_as": t.run_as,
                    "last_result": t.last_result,
                    "next_run": t.next_run,
                })
            })
            .collect();
        (out, count, truncated)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn from_row_parses_and_requires_identity() {
            let row = json!({
                "path": "\\", "name": "OneDrive Update", "state": "Ready",
                "action": "C:\\x\\setup.exe /update", "run_as": "pc\\kid",
                "last_result": 0, "next_run": "2026-06-05T03:00:00Z"
            });
            let t = Task::from_row(&row).unwrap();
            assert_eq!(t.path, "\\");
            assert_eq!(t.name, "OneDrive Update");
            assert_eq!(t.state.as_deref(), Some("Ready"));
            assert_eq!(t.action.as_deref(), Some("C:\\x\\setup.exe /update"));
            assert_eq!(t.run_as.as_deref(), Some("pc\\kid"));
            assert_eq!(t.last_result, Some(0));
            assert_eq!(t.next_run.as_deref(), Some("2026-06-05T03:00:00Z"));
            // Nullable extras stay None; missing identity drops the row.
            let t = Task::from_row(&json!({ "path": "\\A\\", "name": "t" })).unwrap();
            assert_eq!(t.state, None);
            assert_eq!(t.last_result, None);
            assert!(Task::from_row(&json!({ "name": "no-path" })).is_none());
        }

        #[test]
        fn shape_dedupes_sorts_and_caps() {
            let task = |path: &str, name: &str| Task {
                path: path.to_string(),
                name: name.to_string(),
                state: None,
                action: None,
                run_as: None,
                last_result: None,
                next_run: None,
            };
            let (out, count, truncated) = shape(vec![
                task("\\Vendor\\", "b"),
                task("\\", "a"),
                task("\\", "a"), // duplicate collapses
            ]);
            assert_eq!(count, 2);
            assert!(!truncated);
            assert_eq!(out[0]["path"], "\\");
            assert_eq!(out[0]["name"], "a");
            assert_eq!(out[1]["path"], "\\Vendor\\");

            let many: Vec<Task> = (0..230).map(|i| task("\\", &format!("t{i:03}"))).collect();
            let (out, count, truncated) = shape(many);
            assert_eq!(out.len(), MAX_TASKS);
            assert_eq!(count, 230);
            assert!(truncated);
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::winps;
    use serde_json::Value;

    /// All tasks counted, non-Microsoft ones detailed (`Get-ScheduledTaskInfo` is
    /// only invoked for those few, keeping the probe inside the budget).
    pub fn collect() -> Section {
        let script = r#"
$all = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
$out = @()
foreach ($t in $all) {
  if ($t.TaskPath -like '\Microsoft\*') { continue }
  $info = $null
  try { $info = $t | Get-ScheduledTaskInfo -ErrorAction Stop } catch {}
  $action = $null
  $a = @($t.Actions) | Select-Object -First 1
  if ($a -and $a.Execute) {
    $action = [string]$a.Execute
    if ($a.Arguments) { $action = $action + ' ' + [string]$a.Arguments }
  }
  $next = $null
  if ($info -and $info.NextRunTime) {
    $next = (Get-Date $info.NextRunTime).ToUniversalTime().ToString('o')
  }
  $out += [pscustomobject]@{
    path = [string]$t.TaskPath
    name = [string]$t.TaskName
    state = if ($null -ne $t.State) { [string]$t.State } else { $null }
    action = $action
    run_as = if ($t.Principal -and $t.Principal.UserId) { [string]$t.Principal.UserId } else { $null }
    last_result = if ($info) { [long]$info.LastTaskResult } else { $null }
    next_run = $next
  }
}
[pscustomobject]@{ total = $all.Count; tasks = @($out) } | ConvertTo-Json -Compress -Depth 4
"#;

        let v = winps::run_json(script).unwrap_or(Value::Null);
        let total = v.get("total").and_then(Value::as_u64).unwrap_or(0);
        let rows = v
            .get("tasks")
            .cloned()
            .map(winps::as_array)
            .unwrap_or_default();
        let tasks: Vec<core::Task> = rows.iter().filter_map(core::Task::from_row).collect();
        let (tasks, count, truncated) = core::shape(tasks);

        Section::with_fields(
            Status::Ok,
            format!("{count} non-Microsoft tasks ({total} total)"),
            json!({
                "tasks": tasks,
                "count": count,
                "total_count": total,
                "truncated": truncated,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn scheduled_tasks_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["tasks"].is_array());
        assert!(v["count"].is_number());
        assert!(v["total_count"].is_number());
        assert!(v["truncated"].is_boolean());
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["tasks"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
        assert_eq!(v["total_count"], 0);
    }
}
