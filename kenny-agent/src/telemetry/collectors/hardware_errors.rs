//! `hardware_errors` section — hardware-relevant events in a rolling 14-day window.
//!
//! Windows reads the System and Application event logs with one `Get-WinEvent
//! -FilterXml` query generated from the closed query set below; Linux reads the kernel
//! journal plus the EDAC and PCIe AER counters in sysfs. The agent reports facts and
//! never grades: `status` is always `ok` (ADR-0058); component attribution, the meaning
//! of a bugcheck code and every threshold are the server's.
//!
//! Layout: [`query`] is the query set (mirrors `docs/fixtures/vectors/
//! hardware_event_query.json`, seam-tested), [`model`] is the portable core (grouping,
//! capping, section shaping), [`winevent`] turns the Windows probe's JSON into groups,
//! and [`linux`] reads the journal and sysfs. Everything that has a decision in it is
//! portable and unit-tested on Linux; only the PowerShell run itself is `#[cfg(windows)]`.

use crate::telemetry::Section;

/// Collect the `hardware_errors` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(target_os = "linux")]
    {
        linux::collect()
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        model::build_section(model::Report::default())
    }
}

// ---------------------------------------------------------------------------------
// The query set
// ---------------------------------------------------------------------------------

/// The closed set of events the agent queries. Mirrors
/// `docs/fixtures/vectors/hardware_event_query.json`; the server's attribution tables
/// must equal the same file, so an event the server attributes is an event queried here.
mod query {
    /// Rolling window the agent queries, in days.
    pub const WINDOW_DAYS: u32 = 14;

    /// One Windows provider and the events of it that are queried.
    #[derive(Debug, PartialEq, Eq)]
    pub struct WinQuery {
        /// Event log the provider writes to (`System` or `Application`).
        pub log: &'static str,
        pub provider: &'static str,
        pub event_ids: &'static [u32],
        /// Highest (least severe) level queried; 1 critical, 2 error, 3 warning, 4 info.
        pub max_level: u8,
        /// Section field the events fold into instead of being listed as groups.
        pub aggregate: Option<&'static str>,
    }

    /// One Linux kernel-journal matcher.
    #[derive(Debug, PartialEq, Eq)]
    pub struct KernelPattern {
        pub key: &'static str,
        pub regex: &'static str,
    }

    pub const APP_CRASHES: &str = "app_crashes";

    pub const WINDOWS: &[WinQuery] = &[
        WinQuery {
            log: "System",
            provider: "Microsoft-Windows-WHEA-Logger",
            event_ids: &[17, 18, 19, 47],
            max_level: 4,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "Display",
            event_ids: &[4101],
            max_level: 3,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "nvlddmkm",
            event_ids: &[13, 14, 153],
            max_level: 3,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "storahci",
            event_ids: &[129],
            max_level: 3,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "stornvme",
            event_ids: &[11, 129],
            max_level: 3,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "disk",
            event_ids: &[7, 11, 51, 153],
            max_level: 3,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "Microsoft-Windows-MemoryDiagnostics-Results",
            event_ids: &[1202],
            max_level: 4,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "Microsoft-Windows-Kernel-Power",
            event_ids: &[41],
            max_level: 1,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "BugCheck",
            event_ids: &[1001],
            max_level: 2,
            aggregate: None,
        },
        WinQuery {
            log: "System",
            provider: "Microsoft-Windows-WER-SystemErrorReporting",
            event_ids: &[1001],
            max_level: 2,
            aggregate: None,
        },
        WinQuery {
            log: "Application",
            provider: "Application Error",
            event_ids: &[1000],
            max_level: 2,
            aggregate: Some(APP_CRASHES),
        },
    ];

    pub const LINUX_KERNEL_PATTERNS: &[KernelPattern] = &[
        KernelPattern {
            key: "mce",
            regex: r"mce: \[Hardware Error\]",
        },
        KernelPattern {
            key: "edac",
            regex: r"EDAC .*(CE|UE)",
        },
        KernelPattern {
            key: "nvrm_xid",
            regex: r"NVRM: Xid \(.*\): (\d+)",
        },
        KernelPattern {
            key: "amdgpu_ras",
            regex: r"amdgpu.*(RAS|ras).*(error|uncorrectable|correctable)",
        },
        KernelPattern {
            key: "block_io",
            regex: r"I/O error, dev (\S+)",
        },
        KernelPattern {
            key: "nvme",
            regex: r"nvme\S*: .*(timeout|reset|I/O error)",
        },
        KernelPattern {
            key: "ata",
            regex: r"ata\d+(\.\d+)?: failed command",
        },
        KernelPattern {
            key: "pcie_aer",
            regex: r"AER: .*(Corrected|Uncorrected)",
        },
    ];

    /// Provider names whose events name a disk (the `disk_number` / `disk_bus_type` details).
    pub const STORAGE_PROVIDERS: &[&str] = &["disk", "storahci", "stornvme"];

    /// XML-escape text for an attribute or element body.
    pub fn xml_escape(s: &str) -> String {
        let mut out = String::with_capacity(s.len());
        for c in s.chars() {
            match c {
                '&' => out.push_str("&amp;"),
                '<' => out.push_str("&lt;"),
                '>' => out.push_str("&gt;"),
                '"' => out.push_str("&quot;"),
                '\'' => out.push_str("&apos;"),
                c => out.push(c),
            }
        }
        out
    }

    /// Build the `Get-WinEvent -FilterXml` QueryList for `entries`: one `<Select>` per
    /// provider entry, so the 22-provider limit of `-FilterHashtable` never applies.
    /// `Level >= 1` excludes level-0 (always-log) events, which no queried event uses.
    pub fn filter_xml(entries: &[&WinQuery], window_days: u32) -> String {
        let window_ms = u64::from(window_days) * 86_400_000;
        let default_path = entries.first().map_or("System", |e| e.log);
        let mut xml = format!(
            "<QueryList><Query Id=\"0\" Path=\"{}\">",
            xml_escape(default_path)
        );
        for e in entries {
            let ids = e
                .event_ids
                .iter()
                .map(|id| format!("EventID={id}"))
                .collect::<Vec<_>>()
                .join(" or ");
            xml.push_str(&format!(
                "<Select Path=\"{path}\">*[System[Provider[@Name='{provider}'] and ({ids}) \
                 and (Level&gt;=1 and Level&lt;={max}) \
                 and TimeCreated[timediff(@SystemTime) &lt;= {window_ms}]]]</Select>",
                path = xml_escape(e.log),
                provider = xml_escape(e.provider),
                max = e.max_level,
            ));
        }
        xml.push_str("</Query></QueryList>");
        xml
    }
}

// ---------------------------------------------------------------------------------
// The portable core
// ---------------------------------------------------------------------------------

/// Group selection, bounding and section shaping. OS-independent: both collectors feed it.
mod model {
    use std::collections::BTreeMap;

    use chrono::{DateTime, Utc};
    use serde_json::{json, Map, Value};

    use super::query::WINDOW_DAYS;
    use crate::protocol::Status;
    use crate::telemetry::Section;

    /// Cap on the reported groups, so the frame stays bounded.
    pub const MAX_GROUPS: usize = 24;
    /// Slots held for `level: "critical"` groups before count-based selection: a
    /// bugcheck fires once, a chatty provider thousands of times.
    pub const CRITICAL_SLOTS: usize = 4;
    /// Slots held for the most recently seen groups, so a brand-new problem that has not
    /// had time to accumulate a count still reaches the server.
    pub const RECENT_SLOTS: usize = 8;
    /// Most values kept per `details` key (the most frequent).
    pub const MAX_DETAIL_VALUES: usize = 5;
    /// Cap on the `edac` and `aer` lists.
    pub const MAX_SYSFS_ENTRIES: usize = 32;
    /// Cap on the distinct exception codes reported in `app_crashes`.
    pub const MAX_EXCEPTION_CODES: usize = 10;

    /// `details`: key -> value -> number of events (of those whose XML was read).
    pub type Details = BTreeMap<String, BTreeMap<String, u64>>;

    /// Event level name for an event level number (1 critical … 4 information).
    pub fn level_name(level: u64) -> &'static str {
        match level {
            1 => "critical",
            2 => "error",
            3 => "warning",
            _ => "information",
        }
    }

    /// One reported group: the events sharing a `(source, event_id)`.
    #[derive(Debug, Clone, PartialEq)]
    pub struct EventGroup {
        pub source: String,
        pub event_id: u32,
        pub level: &'static str,
        pub count: u64,
        /// Newest member, `yyyy-MM-ddTHH:mm:ssZ` (UTC).
        pub last_seen: String,
        /// UTC calendar date -> events that day.
        pub by_day: BTreeMap<String, u64>,
        /// Newest member's first message line, at most 200 characters.
        pub sample: String,
        pub details: Details,
    }

    impl EventGroup {
        pub fn to_value(&self) -> Value {
            json!({
                "source": self.source,
                "event_id": self.event_id,
                "level": self.level,
                "count": self.count,
                "last_seen": self.last_seen,
                "by_day": self.by_day,
                "sample": self.sample,
                "details": self.details,
            })
        }
    }

    /// Keep the [`MAX_DETAIL_VALUES`] most frequent values of every key (ties broken by
    /// value, so the result is deterministic); drop keys with no value.
    pub fn reduce_details(details: Details) -> Details {
        details
            .into_iter()
            .filter_map(|(key, values)| {
                if values.is_empty() {
                    return None;
                }
                let mut ranked: Vec<(String, u64)> = values.into_iter().collect();
                ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
                ranked.truncate(MAX_DETAIL_VALUES);
                Some((key, ranked.into_iter().collect()))
            })
            .collect()
    }

    /// Count one `value` under `key`.
    pub fn tally(details: &mut Details, key: &str, value: String) {
        *details
            .entry(key.to_string())
            .or_default()
            .entry(value)
            .or_insert(0) += 1;
    }

    /// Pick which groups to report and say how many were dropped.
    ///
    /// Three tiers: every `critical` group (largest first, up to [`CRITICAL_SLOTS`]), then
    /// the most recently seen of the rest (up to [`RECENT_SLOTS`]), then the largest until
    /// [`MAX_GROUPS`] is full. The result is ordered by count, descending.
    pub fn select_groups(groups: Vec<EventGroup>) -> (Vec<EventGroup>, usize) {
        let dropped = groups.len().saturating_sub(MAX_GROUPS);
        let mut keep = vec![false; groups.len()];
        let mut kept = 0usize;

        let mut take = |order: Vec<usize>, limit: usize, keep: &mut Vec<bool>| {
            for idx in order.into_iter().take(limit) {
                if !keep[idx] && kept < MAX_GROUPS {
                    keep[idx] = true;
                    kept += 1;
                }
            }
        };

        // `last_seen` is a fixed-width UTC string: lexicographic order is chronological.
        let mut idx: Vec<usize> = (0..groups.len()).collect();
        idx.sort_by(|&a, &b| {
            groups[b]
                .count
                .cmp(&groups[a].count)
                .then_with(|| groups[a].source.cmp(&groups[b].source))
                .then(groups[a].event_id.cmp(&groups[b].event_id))
        });
        let critical: Vec<usize> = idx
            .iter()
            .copied()
            .filter(|&i| groups[i].level == "critical")
            .collect();
        take(critical, CRITICAL_SLOTS, &mut keep);

        let mut recent: Vec<usize> = idx.iter().copied().filter(|&i| !keep[i]).collect();
        recent.sort_by(|&a, &b| groups[b].last_seen.cmp(&groups[a].last_seen));
        take(recent, RECENT_SLOTS, &mut keep);

        let rest: Vec<usize> = idx.iter().copied().filter(|&i| !keep[i]).collect();
        take(rest, MAX_GROUPS, &mut keep);

        let mut out: Vec<EventGroup> = groups
            .into_iter()
            .zip(keep)
            .filter_map(|(g, k)| k.then_some(g))
            .collect();
        out.sort_by(|a, b| {
            b.count
                .cmp(&a.count)
                .then_with(|| a.source.cmp(&b.source))
                .then(a.event_id.cmp(&b.event_id))
        });
        (out, dropped)
    }

    /// A whole-number `f64` as an integer (`14`, not `14.0`), anything else as a float.
    fn days_value(days: f64) -> Value {
        if days.fract() == 0.0 && days >= 0.0 {
            json!(days as u64)
        } else {
            json!(days)
        }
    }

    /// How far back the queried log actually reaches: `window_days` when its oldest record
    /// is older than the window, shorter (one decimal) when a wrapped log has dropped
    /// older records. `(None, None)` when the oldest record is unknown or unparsable.
    pub fn effective_window(
        oldest_utc: Option<&str>,
        now: DateTime<Utc>,
    ) -> (Option<f64>, Option<String>) {
        let Some(oldest) = oldest_utc else {
            return (None, None);
        };
        let Ok(parsed) = DateTime::parse_from_rfc3339(oldest) else {
            return (None, None);
        };
        let age_days = (now - parsed.with_timezone(&Utc)).num_seconds().max(0) as f64 / 86_400.0;
        let effective = ((age_days * 10.0).round() / 10.0).min(f64::from(WINDOW_DAYS));
        (Some(effective), Some(oldest.to_string()))
    }

    /// Everything a collector learned, ready to be shaped into the section.
    #[derive(Debug, Default)]
    pub struct Report {
        pub groups: Vec<EventGroup>,
        pub effective_window_days: Option<f64>,
        pub oldest_event_utc: Option<String>,
        pub sources: Vec<String>,
        /// `None` omits the key (Linux); `Some(Value::Null)` reports it unreadable.
        pub app_crashes: Option<Value>,
        pub edac: Vec<Value>,
        pub aer: Vec<Value>,
        pub errors: Vec<String>,
    }

    /// Shape the `hardware_errors` section. Always `ok`: the agent reports, the server judges.
    pub fn build_section(report: Report) -> Section {
        // The summary counts every event, including those in groups the cap drops.
        let total: u64 = report.groups.iter().map(|g| g.count).sum();
        let (groups, truncated_count) = select_groups(report.groups);

        let mut fields = Map::new();
        fields.insert("window_days".into(), json!(WINDOW_DAYS));
        fields.insert(
            "effective_window_days".into(),
            report.effective_window_days.map_or(Value::Null, days_value),
        );
        fields.insert("oldest_event_utc".into(), json!(report.oldest_event_utc));
        fields.insert("sources".into(), json!(report.sources));
        fields.insert(
            "groups".into(),
            Value::Array(groups.iter().map(EventGroup::to_value).collect()),
        );
        fields.insert("truncated".into(), json!(truncated_count > 0));
        fields.insert("truncated_count".into(), json!(truncated_count));
        if let Some(app) = report.app_crashes {
            fields.insert("app_crashes".into(), app);
        }
        fields.insert("edac".into(), json!(report.edac));
        fields.insert("aer".into(), json!(report.aer));
        fields.insert("errors".into(), json!(report.errors));

        Section::with_fields(
            Status::Ok,
            format!("{total} hardware-relevant events in {WINDOW_DAYS}d"),
            Value::Object(fields),
        )
    }

    #[cfg(test)]
    pub fn group(source: &str, event_id: u32, level: &'static str, count: u64) -> EventGroup {
        EventGroup {
            source: source.to_string(),
            event_id,
            level,
            count,
            last_seen: "2026-06-01T00:00:00Z".to_string(),
            by_day: BTreeMap::new(),
            sample: String::new(),
            details: Details::new(),
        }
    }
}

// ---------------------------------------------------------------------------------
// Windows event-log probe: script generation and result parsing (portable, tested)
// ---------------------------------------------------------------------------------

/// The Windows probe's PowerShell script and the parsing of its JSON result into groups.
///
/// PowerShell does what only it can (query the log, group cheaply, read the newest
/// events' XML and message); everything with a decision in it happens here in Rust so the
/// Linux tests cover it.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) mod winevent {
    use std::collections::{BTreeMap, HashMap};
    use std::sync::OnceLock;

    use regex::Regex;
    use serde_json::{json, Value};

    use super::model::{level_name, reduce_details, tally, Details, EventGroup};
    use super::query::{self, WinQuery, STORAGE_PROVIDERS, WINDOW_DAYS};
    use crate::telemetry::collectors::bus_type::bus_type_name;

    /// Cap on hardware events one query returns (newest first).
    pub const HW_EVENT_CAP: u32 = 5000;
    /// Cap on `Application Error` events one query returns (newest first).
    pub const APP_EVENT_CAP: u32 = 3000;
    /// Seconds into the probe after which no further expensive work is started, and after
    /// which running loops stop. The probe budget is 20 s (`PROBE_BUDGET`), and PowerShell
    /// itself needs a moment to start and to serialise the result.
    pub const SOFT_LIMIT_SECS: u32 = 11;
    pub const HARD_LIMIT_SECS: u32 = 16;
    /// Events per group whose XML and message are read.
    pub const DETAIL_EVENTS_PER_GROUP: usize = 10;

    const SCRIPT_TEMPLATE: &str = r#"
$ErrorActionPreference = 'Stop'
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$errs = New-Object System.Collections.ArrayList
$hwCap = @@HW_CAP@@
$appCap = @@APP_CAP@@
$soft = @@SOFT@@
$hard = @@HARD@@
$hwXml = @'
@@HW_XML@@
'@
$appXml = @'
@@APP_XML@@
'@

# "No events found" is an empty answer, not an error; anything else is reported.
function Get-Hits([string]$xml, [int]$cap, [string]$what) {
  $ok = $true
  $ev = @()
  try {
    $ev = @(Get-WinEvent -FilterXml $xml -MaxEvents $cap -ErrorAction Stop)
  } catch {
    if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*' -or $_.Exception.Message -like 'No events were found*') {
      $ev = @()
    } else {
      $ok = $false
      [void]$errs.Add("$what query failed: " + $_.Exception.Message)
    }
  }
  [pscustomobject]@{ ok = $ok; events = $ev }
}

$hw = Get-Hits $hwXml $hwCap 'hardware event'
if ($hw.events.Count -ge $hwCap) {
  [void]$errs.Add("hardware event query reached the $hwCap-event cap; older events in the window are not counted")
}

# Group on cheap fields (provider, id, time); read XML and message only for the newest
# events of each group.
$groups = New-Object System.Collections.ArrayList
foreach ($g in @($hw.events | Group-Object ProviderName, Id)) {
  $members = @($g.Group | Sort-Object TimeCreated -Descending)
  $latest = $members[0]
  $msg = ''
  try { if ($latest.Message) { $msg = ([string]$latest.Message -split "`r?`n")[0] } } catch {}
  if ($msg.Length -gt 200) { $msg = $msg.Substring(0, 200) }
  # UTC dates: a local-date key would make the number of distinct days depend on the
  # host's timezone.
  $byDay = @{}
  foreach ($e in $members) {
    $d = $e.TimeCreated.ToUniversalTime().ToString('yyyy-MM-dd')
    if ($byDay.ContainsKey($d)) { $byDay[$d]++ } else { $byDay[$d] = 1 }
  }
  $evs = New-Object System.Collections.ArrayList
  if ($sw.Elapsed.TotalSeconds -lt $soft) {
    foreach ($e in @($members | Select-Object -First @@DETAIL_EVENTS@@)) {
      $frag = ''
      $m = ''
      try {
        $x = $e.ToXml()
        if ($x -match '(?s)<(EventData|UserData)\b.*</\1>') { $frag = $matches[0] }
      } catch {}
      try { $m = [string]$e.Message } catch {}
      if ($m.Length -gt 1500) { $m = $m.Substring(0, 1500) }
      [void]$evs.Add([pscustomobject]@{ xml = $frag; msg = $m })
    }
  }
  [void]$groups.Add([pscustomobject]@{
    source    = [string]$latest.ProviderName
    event_id  = [int]$latest.Id
    level     = [int]$latest.Level
    count     = [int]$g.Count
    sample    = $msg
    last_seen = $latest.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    by_day    = $byDay
    events    = @($evs)
  })
}
if ($sw.Elapsed.TotalSeconds -ge $soft) {
  [void]$errs.Add('event details were cut short by the probe time budget')
}

# Application Error 1000 folds into counts: app name, module and exception code are
# aggregated per UTC day, keyed "app|module|code|day".
$app = $null
if ($appXml.Trim().Length -gt 0) {
  if ($sw.Elapsed.TotalSeconds -ge $soft) {
    [void]$errs.Add('Application Error aggregation skipped: probe time budget exhausted')
  } else {
    $r = Get-Hits $appXml $appCap 'Application Error'
    if ($r.ok) {
      if ($r.events.Count -ge $appCap) {
        [void]$errs.Add("Application Error query reached the $appCap-event cap; older crashes in the window are not counted")
      }
      $agg = @{}
      $n = 0
      foreach ($e in $r.events) {
        if ($sw.Elapsed.TotalSeconds -ge $hard) {
          [void]$errs.Add('Application Error aggregation cut short by the probe time budget')
          break
        }
        $n++
        $a = ''; $m = ''; $c = ''
        $p = $e.Properties
        if ($p.Count -ge 7) { $a = [string]$p[0].Value; $m = [string]$p[3].Value; $c = [string]$p[6].Value }
        $d = $e.TimeCreated.ToUniversalTime().ToString('yyyy-MM-dd')
        $k = ($a -replace '\|', '_'), ($m -replace '\|', '_'), ($c -replace '\|', '_'), $d -join '|'
        if ($agg.ContainsKey($k)) { $agg[$k]++ } else { $agg[$k] = 1 }
      }
      $app = [pscustomobject]@{ total = $n; agg = $agg }
    }
  }
}

# Disk number -> bus type, only when a storage event needs it.
$disks = @{}
$needDisks = $false
foreach ($g in $groups) {
  if (@(@@STORAGE@@) -contains $g.source) { $needDisks = $true }
}
if ($needDisks) {
  try {
    foreach ($d in @(Get-PhysicalDisk -ErrorAction Stop)) { $disks[[string]$d.DeviceId] = [string]$d.BusType }
  } catch {
    [void]$errs.Add('disk bus types unavailable: ' + $_.Exception.Message)
  }
}

# Oldest System record, so a wrapped log never reads as a quiet one.
$oldest = $null
try {
  $o = @(Get-WinEvent -LogName System -MaxEvents 1 -Oldest -ErrorAction Stop)[0]
  if ($o) { $oldest = $o.TimeCreated.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ') }
} catch {
  [void]$errs.Add('oldest System record unavailable: ' + $_.Exception.Message)
}

[pscustomobject]@{
  groups = @($groups)
  app    = $app
  disks  = $disks
  oldest = $oldest
  errors = @($errs)
} | ConvertTo-Json -Depth 6 -Compress
"#;

    /// The probe script: the QueryList(s) are generated from the query set, so the script
    /// cannot drift from the contract's vector file.
    pub fn probe_script() -> String {
        let (hw, app): (Vec<&WinQuery>, Vec<&WinQuery>) = query::WINDOWS
            .iter()
            .partition(|q| q.aggregate != Some(query::APP_CRASHES));
        let storage = STORAGE_PROVIDERS
            .iter()
            .map(|p| format!("'{p}'"))
            .collect::<Vec<_>>()
            .join(",");
        let app_xml = if app.is_empty() {
            String::new()
        } else {
            query::filter_xml(&app, WINDOW_DAYS)
        };
        SCRIPT_TEMPLATE
            .replace("@@HW_CAP@@", &HW_EVENT_CAP.to_string())
            .replace("@@APP_CAP@@", &APP_EVENT_CAP.to_string())
            .replace("@@SOFT@@", &SOFT_LIMIT_SECS.to_string())
            .replace("@@HARD@@", &HARD_LIMIT_SECS.to_string())
            .replace("@@DETAIL_EVENTS@@", &DETAIL_EVENTS_PER_GROUP.to_string())
            .replace("@@STORAGE@@", &storage)
            .replace("@@HW_XML@@", &query::filter_xml(&hw, WINDOW_DAYS))
            .replace("@@APP_XML@@", &app_xml)
    }

    // ---- EventData parsing --------------------------------------------------------

    /// One `<Data>` element of an event's `EventData`.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Datum {
        pub name: Option<String>,
        pub value: String,
    }

    fn data_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            Regex::new(
                r#"(?s)<Data(?:\s+Name\s*=\s*(?:'([^']*)'|"([^"]*)"))?\s*(?:/>|>(.*?)</Data>)"#,
            )
            .expect("static regex")
        })
    }

    fn xml_unescape(s: &str) -> String {
        let mut out = String::with_capacity(s.len());
        let mut rest = s;
        while let Some(i) = rest.find('&') {
            out.push_str(&rest[..i]);
            rest = &rest[i..];
            let Some(end) = rest.find(';').filter(|&e| e <= 10) else {
                out.push('&');
                rest = &rest[1..];
                continue;
            };
            let entity = &rest[1..end];
            let decoded = match entity {
                "amp" => Some('&'),
                "lt" => Some('<'),
                "gt" => Some('>'),
                "quot" => Some('"'),
                "apos" => Some('\''),
                e => e
                    .strip_prefix("#x")
                    .and_then(|h| u32::from_str_radix(h, 16).ok())
                    .or_else(|| e.strip_prefix('#').and_then(|d| d.parse().ok()))
                    .and_then(char::from_u32),
            };
            match decoded {
                Some(c) => {
                    out.push(c);
                    rest = &rest[end + 1..];
                }
                None => {
                    out.push('&');
                    rest = &rest[1..];
                }
            }
        }
        out.push_str(rest);
        out
    }

    /// Every `<Data>` element in `xml` (an `EventData` fragment or a whole event), in order.
    pub fn parse_event_data(xml: &str) -> Vec<Datum> {
        data_re()
            .captures_iter(xml)
            .map(|c| Datum {
                name: c
                    .get(1)
                    .or_else(|| c.get(2))
                    .map(|m| xml_unescape(m.as_str())),
                value: xml_unescape(c.get(3).map_or("", |m| m.as_str()).trim()),
            })
            .collect()
    }

    fn named<'a>(data: &'a [Datum], names: &[&str]) -> Option<&'a str> {
        names.iter().find_map(|n| {
            data.iter()
                .find(|d| {
                    d.name
                        .as_deref()
                        .is_some_and(|dn| dn.eq_ignore_ascii_case(n))
                        && !d.value.is_empty()
                })
                .map(|d| d.value.as_str())
        })
    }

    /// `Label: value` from a rendered (English) event message.
    fn message_field(msg: &str, label: &str) -> Option<String> {
        msg.lines().find_map(|line| {
            let line = line.trim();
            let rest = line.strip_prefix(label)?.trim_start();
            let value = rest.strip_prefix(':')?.trim();
            (!value.is_empty()).then(|| value.to_string())
        })
    }

    fn parse_num(s: &str) -> Option<u64> {
        let t = s.trim();
        match t.strip_prefix("0x").or_else(|| t.strip_prefix("0X")) {
            Some(h) => u64::from_str_radix(h, 16).ok(),
            None => t.parse().ok(),
        }
    }

    fn is_numeric(s: &str) -> bool {
        parse_num(s).is_some()
    }

    /// A bugcheck code as `0x%08x`; `decimal` says how a bare number without `0x` reads.
    fn bugcheck_hex(s: &str, decimal: bool) -> Option<String> {
        let t = s.trim();
        let n = if t.starts_with("0x") || t.starts_with("0X") || decimal {
            parse_num(t)?
        } else {
            u64::from_str_radix(t, 16).ok()?
        };
        Some(format!("0x{n:08x}"))
    }

    fn hex_token_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| Regex::new(r"(?i)0x[0-9a-f]+").expect("static regex"))
    }

    fn harddisk_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| Regex::new(r"(?i)\\Device\\Harddisk(\d+)").expect("static regex"))
    }

    fn disk_msg_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| Regex::new(r"\bDisk (\d+)\b").expect("static regex"))
    }

    fn xid_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            Regex::new(r"(?i)\bXid\b\s*(?:\([^)]*\))?\s*:?\s*(\d+)").expect("static regex")
        })
    }

    fn display_driver_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| Regex::new(r"(?i)Display driver (\S+) stopped").expect("static regex"))
    }

    fn pci_ven_dev_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            Regex::new(r"(?i)VEN_([0-9a-f]{4})&DEV_([0-9a-f]{4})").expect("static regex")
        })
    }

    fn pci_bdf_re() -> &'static Regex {
        static RE: OnceLock<Regex> = OnceLock::new();
        RE.get_or_init(|| {
            Regex::new(
                r"(?i)Primary Bus:Device:Function:\s*(0x[0-9a-f]+):(0x[0-9a-f]+):(0x[0-9a-f]+)",
            )
            .expect("static regex")
        })
    }

    /// Prefer a descriptive text over a bare number: the event XML may carry an enum value
    /// where the rendered message names it.
    fn text_or_message(xml_value: Option<&str>, msg_value: Option<String>) -> Option<String> {
        match (xml_value, msg_value) {
            (Some(x), _) if !is_numeric(x) => Some(x.to_string()),
            (_, Some(m)) => Some(m),
            (Some(x), None) => Some(x.to_string()),
            (None, None) => None,
        }
    }

    /// `WHEA-Logger` details.
    ///
    /// The `EventData` field names assumed (they vary by Windows version, so several
    /// spellings are tried, and the English rendered message is the fallback):
    /// - `error_source`: `ErrorSourceName`, `ErrorSource`; message `Error Source:`.
    /// - `error_type`: `ErrorTypeName`, `ErrorType`; message `Error Type:`.
    /// - `processor_apic_id`: `ApicId`, `ProcessorApicId`, `LocalApicId`, `Processor`;
    ///   message `Processor APIC ID:`.
    /// - `pci_vendor_device`: `VendorId`/`VendorID` + `DeviceId`/`DeviceID`; message
    ///   `PCI\VEN_vvvv&DEV_dddd` in the device name. Rendered `vvvv:dddd`, lowercase hex.
    /// - `pci_location`: `PrimaryBusNumber`/`Bus` + `PrimaryDeviceNumber`/`Device` +
    ///   `PrimaryFunctionNumber`/`Function`; message `Primary Bus:Device:Function:`.
    ///   Rendered `bb:dd.f` in lowercase hex (the `lspci` form).
    fn whea_details(data: &[Datum], msg: &str, out: &mut Vec<(&'static str, String)>) {
        if let Some(v) = text_or_message(
            named(data, &["ErrorSourceName", "ErrorSource"]),
            message_field(msg, "Error Source"),
        ) {
            out.push(("error_source", v));
        }
        if let Some(v) = text_or_message(
            named(data, &["ErrorTypeName", "ErrorType"]),
            message_field(msg, "Error Type"),
        ) {
            out.push(("error_type", v));
        }
        if let Some(v) = named(
            data,
            &["ApicId", "ProcessorApicId", "LocalApicId", "Processor"],
        )
        .map(str::to_string)
        .or_else(|| message_field(msg, "Processor APIC ID"))
        {
            out.push(("processor_apic_id", v));
        }

        let vendor = named(data, &["VendorId", "VendorID"]).and_then(parse_num);
        let device = named(data, &["DeviceId", "DeviceID"]).and_then(parse_num);
        let vendor_device = match (vendor, device) {
            (Some(v), Some(d)) => Some(format!("{v:04x}:{d:04x}")),
            _ => pci_ven_dev_re().captures(msg).map(|c| {
                format!(
                    "{}:{}",
                    c[1].to_ascii_lowercase(),
                    c[2].to_ascii_lowercase()
                )
            }),
        };
        if let Some(v) = vendor_device {
            out.push(("pci_vendor_device", v));
        }

        let bus = named(data, &["PrimaryBusNumber", "Bus", "BusNumber"]).and_then(parse_num);
        let dev =
            named(data, &["PrimaryDeviceNumber", "Device", "DeviceNumber"]).and_then(parse_num);
        let func = named(
            data,
            &["PrimaryFunctionNumber", "Function", "FunctionNumber"],
        )
        .and_then(parse_num);
        let location = match (bus, dev, func) {
            (Some(b), Some(d), Some(f)) => Some(format!("{b:02x}:{d:02x}.{f:x}")),
            _ => pci_bdf_re().captures(msg).and_then(|c| {
                Some(format!(
                    "{:02x}:{:02x}.{:x}",
                    parse_num(&c[1])?,
                    parse_num(&c[2])?,
                    parse_num(&c[3])?
                ))
            }),
        };
        if let Some(v) = location {
            out.push(("pci_location", v));
        }
    }

    /// The disk number a storage event names, best effort: a named `DiskNumber`-style
    /// field, a `\Device\HarddiskN\…` device name in any field, then `Disk N` in the English
    /// message. A `\Device\RaidPortN` (storahci/stornvme 129) names a port, not a disk,
    /// and cannot be mapped to a disk number: no number then.
    fn disk_number(data: &[Datum], msg: &str) -> Option<u64> {
        if let Some(n) = named(data, &["DiskNumber", "Disk", "DeviceNumber"]).and_then(parse_num) {
            return Some(n);
        }
        for d in data {
            if let Some(c) = harddisk_re().captures(&d.value) {
                return c[1].parse().ok();
            }
        }
        if let Some(c) = harddisk_re()
            .captures(msg)
            .or_else(|| disk_msg_re().captures(msg))
        {
            return c[1].parse().ok();
        }
        None
    }

    /// The details one event contributes: `(key, value)` pairs. `disks` maps a disk number
    /// to its raw `BusType`.
    pub fn event_details(
        source: &str,
        event_id: u32,
        xml: &str,
        msg: &str,
        disks: &HashMap<String, String>,
    ) -> Vec<(&'static str, String)> {
        let data = parse_event_data(xml);
        let is = |p: &str| source.eq_ignore_ascii_case(p);
        let mut out: Vec<(&'static str, String)> = Vec::new();

        if is("Microsoft-Windows-WHEA-Logger") {
            whea_details(&data, msg, &mut out);
        } else if is("Microsoft-Windows-Kernel-Power") && event_id == 41 {
            // `BugcheckCode` is stored decimal.
            if let Some(code) = named(&data, &["BugcheckCode"]).and_then(|v| bugcheck_hex(v, true))
            {
                out.push(("bugcheck_code", code));
            }
            if let Some(ts) = named(&data, &["PowerButtonTimestamp"]).and_then(parse_num) {
                out.push(("power_button", (ts != 0).to_string()));
            }
        } else if (is("BugCheck") || is("Microsoft-Windows-WER-SystemErrorReporting"))
            && event_id == 1001
        {
            // Parameter 1 reads `0x00000124 (0x…, 0x…, 0x…, 0x…)`.
            let first = named(&data, &["param1", "BugcheckCode", "BugCheckCode"])
                .or_else(|| data.first().map(|d| d.value.as_str()))
                .and_then(|v| hex_token_re().find(v).map(|m| m.as_str().to_string()))
                .or_else(|| {
                    msg.split("bugcheck was:")
                        .nth(1)
                        .and_then(|rest| hex_token_re().find(rest).map(|m| m.as_str().to_string()))
                });
            if let Some(code) = first.and_then(|v| bugcheck_hex(&v, false)) {
                out.push(("bugcheck_code", code));
            }
        } else if STORAGE_PROVIDERS.iter().any(|p| is(p)) {
            let number = disk_number(&data, msg);
            if let Some(n) = number {
                out.push(("disk_number", n.to_string()));
            }
            let bus = number
                .and_then(|n| disks.get(&n.to_string()))
                .map_or("Unknown", |b| bus_type_name(b));
            out.push(("disk_bus_type", bus.to_string()));
        } else if is("nvlddmkm") {
            let xid = named(&data, &["Xid", "XidCode"])
                .and_then(|v| v.trim().parse::<u64>().ok().map(|n| n.to_string()))
                .or_else(|| {
                    data.iter()
                        .find_map(|d| xid_re().captures(&d.value).map(|c| c[1].to_string()))
                })
                .or_else(|| xid_re().captures(msg).map(|c| c[1].to_string()));
            if let Some(x) = xid {
                out.push(("xid", x));
            }
        } else if is("Display") && event_id == 4101 {
            let ident = |s: &str| {
                !s.is_empty()
                    && !is_numeric(s)
                    && s.chars()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-'))
            };
            let driver = named(&data, &["DisplayDriver", "Driver", "DriverName"])
                .map(str::to_string)
                .or_else(|| {
                    data.iter()
                        .map(|d| d.value.as_str())
                        .find(|v| ident(v))
                        .map(str::to_string)
                })
                .or_else(|| display_driver_re().captures(msg).map(|c| c[1].to_string()));
            if let Some(d) = driver {
                let d = d.strip_suffix(".sys").unwrap_or(&d).to_string();
                out.push(("driver", d));
            }
        }
        out
    }

    // ---- Probe result -> groups ---------------------------------------------------

    fn items(v: Option<&Value>) -> Vec<&Value> {
        match v {
            Some(Value::Array(a)) => a.iter().collect(),
            Some(Value::Null) | None => Vec::new(),
            Some(other) => vec![other],
        }
    }

    /// The disk number -> raw `BusType` map the probe returned.
    pub fn disk_map(probe: &Value) -> HashMap<String, String> {
        probe
            .get("disks")
            .and_then(Value::as_object)
            .map(|o| {
                o.iter()
                    .filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string())))
                    .collect()
            })
            .unwrap_or_default()
    }

    /// Turn the probe's `groups` into [`EventGroup`]s, extracting `details` from the newest
    /// events' XML and message.
    pub fn groups_from_probe(probe: &Value) -> Vec<EventGroup> {
        let disks = disk_map(probe);
        let mut out = Vec::new();
        for g in items(probe.get("groups")) {
            let Some(source) = g.get("source").and_then(Value::as_str) else {
                continue;
            };
            let event_id = g.get("event_id").and_then(Value::as_u64).unwrap_or(0) as u32;
            let mut details = Details::new();
            for ev in items(g.get("events")) {
                let xml = ev.get("xml").and_then(Value::as_str).unwrap_or("");
                let msg = ev.get("msg").and_then(Value::as_str).unwrap_or("");
                for (key, value) in event_details(source, event_id, xml, msg, &disks) {
                    tally(&mut details, key, value);
                }
            }
            let by_day: BTreeMap<String, u64> = g
                .get("by_day")
                .and_then(Value::as_object)
                .map(|o| {
                    o.iter()
                        .filter_map(|(k, v)| v.as_u64().map(|n| (k.clone(), n)))
                        .collect()
                })
                .unwrap_or_default();
            out.push(EventGroup {
                source: source.to_string(),
                event_id,
                level: level_name(g.get("level").and_then(Value::as_u64).unwrap_or(4)),
                count: g.get("count").and_then(Value::as_u64).unwrap_or(0),
                last_seen: g
                    .get("last_seen")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                by_day,
                sample: g
                    .get("sample")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .chars()
                    .take(200)
                    .collect(),
                details: reduce_details(details),
            });
        }
        out
    }

    // ---- Application Error aggregate ----------------------------------------------

    /// The last path component of a module name (`C:\Windows\x\ntdll.dll` -> `ntdll.dll`):
    /// never a directory, so a user profile path can not reach the wire.
    pub fn basename(s: &str) -> &str {
        s.rsplit(['\\', '/']).next().unwrap_or(s).trim()
    }

    /// An exception code as `0x%08x` lowercase (`c0000005`, `0xC0000005` -> `0xc0000005`).
    pub fn exception_code(s: &str) -> Option<String> {
        let t = s.trim();
        let h = t
            .strip_prefix("0x")
            .or_else(|| t.strip_prefix("0X"))
            .unwrap_or(t);
        u64::from_str_radix(h, 16)
            .ok()
            .map(|n| format!("0x{n:08x}"))
    }

    /// The `app_crashes` aggregate from the probe's `app` object (`total` and an `agg` map
    /// keyed `app|module|code|day`). `Value::Null` when the Application log could not be
    /// read. Only the [`MAX_EXCEPTION_CODES`] most frequent codes are listed.
    pub fn app_crashes_from_probe(probe: &Value) -> Value {
        let Some(app) = probe.get("app").filter(|a| a.is_object()) else {
            return Value::Null;
        };
        let total = app.get("total").and_then(Value::as_u64).unwrap_or(0);
        let mut apps = std::collections::BTreeSet::new();
        let mut modules = std::collections::BTreeSet::new();
        let mut codes: BTreeMap<String, u64> = BTreeMap::new();
        let mut by_day: BTreeMap<String, u64> = BTreeMap::new();
        if let Some(agg) = app.get("agg").and_then(Value::as_object) {
            for (key, n) in agg {
                let n = n.as_u64().unwrap_or(0);
                let mut parts = key.splitn(4, '|');
                let (a, m, c, d) = (
                    parts.next().unwrap_or(""),
                    parts.next().unwrap_or(""),
                    parts.next().unwrap_or(""),
                    parts.next().unwrap_or(""),
                );
                if !a.trim().is_empty() {
                    apps.insert(a.trim().to_ascii_lowercase());
                }
                let m = basename(m);
                if !m.is_empty() {
                    modules.insert(m.to_ascii_lowercase());
                }
                if let Some(code) = exception_code(c) {
                    *codes.entry(code).or_insert(0) += n;
                }
                if !d.is_empty() {
                    *by_day.entry(d.to_string()).or_insert(0) += n;
                }
            }
        }
        let mut ranked: Vec<(String, u64)> = codes.into_iter().collect();
        ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        ranked.truncate(super::model::MAX_EXCEPTION_CODES);
        let exception_codes: BTreeMap<String, u64> = ranked.into_iter().collect();
        json!({
            "total": total,
            "distinct_apps": apps.len(),
            "distinct_modules": modules.len(),
            "exception_codes": exception_codes,
            "by_day": by_day,
        })
    }
}

// ---------------------------------------------------------------------------------
// Windows: run the probe
// ---------------------------------------------------------------------------------

#[cfg(windows)]
mod windows_impl {
    use super::model::{build_section, effective_window, Report};
    use super::winevent;
    use crate::telemetry::collectors::winps::{self, PROBE_BUDGET};
    use crate::telemetry::Section;
    use serde_json::Value;

    /// One bounded PowerShell probe (`PROBE_BUDGET`); a probe that fails or times out adds
    /// an `errors` entry and an otherwise empty section, never a clean-looking zero count
    /// with `sources: ["event_log"]`.
    pub fn collect() -> Section {
        let mut report = Report::default();
        match winps::run_json_within(&winevent::probe_script(), PROBE_BUDGET) {
            Ok(probe) => {
                report.groups = winevent::groups_from_probe(&probe);
                report.app_crashes = Some(winevent::app_crashes_from_probe(&probe));
                let oldest = probe.get("oldest").and_then(Value::as_str);
                let (effective, oldest) = effective_window(oldest, chrono::Utc::now());
                report.effective_window_days = effective;
                report.oldest_event_utc = oldest;
                report.errors = probe
                    .get("errors")
                    .and_then(Value::as_array)
                    .map(|a| {
                        a.iter()
                            .filter_map(Value::as_str)
                            .map(|s| s.chars().take(300).collect())
                            .collect()
                    })
                    .unwrap_or_default();
                report.sources = vec!["event_log".to_string()];
            }
            Err(failure) => {
                report.app_crashes = Some(Value::Null);
                report
                    .errors
                    .push(format!("event log probe failed: {failure:?}"));
            }
        }
        build_section(report)
    }
}

// ---------------------------------------------------------------------------------
// Linux: kernel journal, EDAC, PCIe AER
// ---------------------------------------------------------------------------------

/// The Linux readers are portable code (plain file reads and a bounded `journalctl`), so
/// they are unit-tested on every platform; only [`collect`] is `target_os = "linux"`.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
mod linux {
    use std::collections::BTreeMap;
    use std::path::Path;
    use std::time::Duration;

    use chrono::{DateTime, Utc};
    use regex::{Regex, RegexSet};
    use serde_json::{json, Value};

    use super::model::{
        build_section, effective_window, reduce_details, tally, Details, EventGroup, Report,
        MAX_SYSFS_ENTRIES,
    };
    use super::query::{KernelPattern, LINUX_KERNEL_PATTERNS};
    use crate::telemetry::collectors::proc::{self, PROBE_BUDGET};
    use crate::telemetry::collectors::ProbeFailure;
    use crate::telemetry::Section;

    /// Newest journal entries read at most; more is flagged in `errors`, never silent.
    const MAX_JOURNAL_ENTRIES: usize = 250_000;

    /// A matcher key and the `details` key its first capture group feeds, if any.
    const CAPTURE_DETAIL: &[(&str, &str)] = &[("nvrm_xid", "xid"), ("block_io", "device")];

    pub struct Matchers {
        set: RegexSet,
        each: Vec<(&'static str, Regex)>,
    }

    impl Matchers {
        /// Compile the vector's patterns. `None` if one does not compile (a build bug the
        /// unit test pins).
        pub fn compile(patterns: &'static [KernelPattern]) -> Option<Self> {
            let set = RegexSet::new(patterns.iter().map(|p| p.regex)).ok()?;
            let each = patterns
                .iter()
                .map(|p| Regex::new(p.regex).ok().map(|r| (p.key, r)))
                .collect::<Option<Vec<_>>>()?;
            Some(Self { set, each })
        }

        /// The first pattern (in table order) matching `text`, and its first capture group.
        fn first_match(&self, text: &str) -> Option<(&'static str, Option<String>)> {
            self.each.iter().find_map(|(key, re)| {
                re.captures(text)
                    .map(|c| (*key, c.get(1).map(|m| m.as_str().to_string())))
            })
        }
    }

    #[derive(Default)]
    struct Acc {
        count: u64,
        newest_us: i64,
        level: &'static str,
        sample: String,
        by_day: BTreeMap<String, u64>,
        details: Details,
    }

    /// `PRIORITY` -> level: 0-2 critical, 3 error, 4 warning, else information.
    fn priority_level(p: u64) -> &'static str {
        match p {
            0..=2 => "critical",
            3 => "error",
            4 => "warning",
            _ => "information",
        }
    }

    fn json_message(v: &Value) -> Option<String> {
        match v.get("MESSAGE")? {
            Value::String(s) => Some(s.clone()),
            // journalctl renders a message with non-UTF-8 bytes as an array of byte values.
            Value::Array(a) => {
                let bytes: Vec<u8> = a
                    .iter()
                    .filter_map(|b| b.as_u64().and_then(|n| u8::try_from(n).ok()))
                    .collect();
                Some(String::from_utf8_lossy(&bytes).into_owned())
            }
            _ => None,
        }
    }

    /// Group the kernel-journal JSON lines (`journalctl -o json`, one object per line) by
    /// matcher key. Returns the groups and how many journal lines were read.
    pub fn groups_from_journal(stdout: &str, matchers: &Matchers) -> (Vec<EventGroup>, usize) {
        let mut accs: BTreeMap<&'static str, Acc> = BTreeMap::new();
        let mut lines = 0usize;
        for line in stdout.lines() {
            if line.is_empty() {
                continue;
            }
            lines += 1;
            // Cheap pre-filter on the raw line before paying for a JSON parse. A message with
            // non-UTF-8 bytes is an array of numbers there, so it always goes on.
            if !matchers.set.is_match(line) && !line.contains("\"MESSAGE\":[") {
                continue;
            }
            let Ok(v) = serde_json::from_str::<Value>(line) else {
                continue;
            };
            let Some(message) = json_message(&v) else {
                continue;
            };
            let Some((key, capture)) = matchers.first_match(&message) else {
                continue;
            };
            let ts_us: i64 = v
                .get("__REALTIME_TIMESTAMP")
                .and_then(Value::as_str)
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            let Some(ts) = DateTime::<Utc>::from_timestamp(ts_us.div_euclid(1_000_000), 0) else {
                continue;
            };
            let priority = v
                .get("PRIORITY")
                .and_then(Value::as_str)
                .and_then(|s| s.parse().ok())
                .unwrap_or(6);

            let acc = accs.entry(key).or_default();
            acc.count += 1;
            *acc.by_day
                .entry(ts.format("%Y-%m-%d").to_string())
                .or_insert(0) += 1;
            if let Some((_, detail_key)) = CAPTURE_DETAIL.iter().find(|(k, _)| *k == key) {
                // `I/O error, dev (\S+)` also captures the comma that follows the name.
                if let Some(value) =
                    capture.map(|c| c.trim_end_matches([',', ';', ':']).to_string())
                {
                    tally(&mut acc.details, detail_key, value);
                }
            }
            if acc.count == 1 || ts_us >= acc.newest_us {
                acc.newest_us = ts_us;
                acc.level = priority_level(priority);
                acc.sample = message
                    .lines()
                    .next()
                    .unwrap_or("")
                    .chars()
                    .take(200)
                    .collect();
            }
        }
        let groups = accs
            .into_iter()
            .map(|(key, acc)| EventGroup {
                source: key.to_string(),
                event_id: 0,
                level: acc.level,
                count: acc.count,
                last_seen: DateTime::<Utc>::from_timestamp(acc.newest_us.div_euclid(1_000_000), 0)
                    .map(|t| t.format("%Y-%m-%dT%H:%M:%SZ").to_string())
                    .unwrap_or_default(),
                by_day: acc.by_day,
                sample: acc.sample,
                details: reduce_details(acc.details),
            })
            .collect();
        (groups, lines)
    }

    /// The oldest journal entry (RFC 3339 UTC) from `journalctl --list-boots -o json`
    /// (systemd 252+; older versions print text, which reads as unknown).
    pub fn oldest_from_list_boots(stdout: &str) -> Option<String> {
        let boots: Value = serde_json::from_str(stdout.trim()).ok()?;
        let first_us = boots
            .as_array()?
            .iter()
            .filter_map(|b| b.get("first_entry").and_then(Value::as_i64))
            .filter(|&us| us > 0)
            .min()?;
        DateTime::<Utc>::from_timestamp(first_us.div_euclid(1_000_000), 0)
            .map(|t| t.format("%Y-%m-%dT%H:%M:%SZ").to_string())
    }

    /// `mc*` controllers in numeric order.
    fn numbered(dir: &Path, prefix: &str) -> std::io::Result<Vec<(u64, std::path::PathBuf)>> {
        let mut out: Vec<(u64, std::path::PathBuf)> = std::fs::read_dir(dir)?
            .filter_map(Result::ok)
            .filter_map(|e| {
                let name = e.file_name().into_string().ok()?;
                let n = name.strip_prefix(prefix)?.parse().ok()?;
                Some((n, e.path()))
            })
            .collect();
        out.sort_by_key(|(n, _)| *n);
        Ok(out)
    }

    fn read_u64(path: &Path) -> Option<u64> {
        std::fs::read_to_string(path).ok()?.trim().parse().ok()
    }

    /// One entry per memory controller from `<root>/mc*/{ce_count,ue_count}`. `Ok(None)`
    /// when there is no EDAC (no such directory: not an error), `Err` when it exists but
    /// cannot be read.
    pub fn read_edac(root: &Path) -> Result<Option<Vec<Value>>, String> {
        let controllers = match numbered(root, "mc") {
            Ok(c) => c,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(format!("edac unreadable: {e}")),
        };
        Ok(Some(
            controllers
                .into_iter()
                .take(MAX_SYSFS_ENTRIES)
                .map(|(n, path)| {
                    json!({
                        "controller": format!("mc{n}"),
                        "ce_count": read_u64(&path.join("ce_count")).unwrap_or(0),
                        "ue_count": read_u64(&path.join("ue_count")).unwrap_or(0),
                    })
                })
                .collect(),
        ))
    }

    /// The total of an `aer_dev_*` file: its `TOTAL_ERR_*` line, else the sum of the
    /// per-error lines (`RxErr 0`, …). `None` when the file has no counters.
    pub fn parse_aer_total(text: &str) -> Option<u64> {
        let mut sum = 0u64;
        let mut any = false;
        for line in text.lines() {
            let mut parts = line.split_whitespace();
            let (Some(name), Some(n)) = (parts.next(), parts.next()) else {
                continue;
            };
            let Ok(n) = n.parse::<u64>() else { continue };
            if name.starts_with("TOTAL_ERR_") {
                return Some(n);
            }
            sum = sum.saturating_add(n);
            any = true;
        }
        any.then_some(sum)
    }

    /// PCIe devices with a non-zero AER error total, from `<root>/*/aer_dev_*`, most errors
    /// first. `Ok(None)` when there is no PCI bus directory.
    pub fn read_aer(root: &Path) -> Result<Option<Vec<Value>>, String> {
        let dirs = match std::fs::read_dir(root) {
            Ok(d) => d,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
            Err(e) => return Err(format!("aer unreadable: {e}")),
        };
        let mut found: Vec<(u64, Value)> = Vec::new();
        for entry in dirs.filter_map(Result::ok) {
            let path = entry.path();
            let read = |file: &str| {
                std::fs::read_to_string(path.join(file))
                    .ok()
                    .and_then(|t| parse_aer_total(&t))
            };
            let (cor, nonfatal, fatal) = (
                read("aer_dev_correctable"),
                read("aer_dev_nonfatal"),
                read("aer_dev_fatal"),
            );
            if cor.is_none() && nonfatal.is_none() && fatal.is_none() {
                continue;
            }
            let (cor, nonfatal, fatal) =
                (cor.unwrap_or(0), nonfatal.unwrap_or(0), fatal.unwrap_or(0));
            let total = cor.saturating_add(nonfatal).saturating_add(fatal);
            if total == 0 {
                continue;
            }
            found.push((
                total,
                json!({
                    "device": entry.file_name().to_string_lossy(),
                    "correctable": cor,
                    "nonfatal": nonfatal,
                    "fatal": fatal,
                }),
            ));
        }
        found.sort_by(|a, b| {
            b.0.cmp(&a.0)
                .then_with(|| a.1["device"].as_str().cmp(&b.1["device"].as_str()))
        });
        Ok(Some(
            found
                .into_iter()
                .take(MAX_SYSFS_ENTRIES)
                .map(|(_, v)| v)
                .collect(),
        ))
    }

    fn failure_text(f: ProbeFailure) -> String {
        match f {
            ProbeFailure::Spawn => "could not start journalctl".to_string(),
            ProbeFailure::Timeout(d) => format!("journalctl timed out after {}s", d.as_secs()),
            other => format!("journalctl failed: {other:?}"),
        }
    }

    /// What reading the journal produced.
    struct JournalRead {
        groups: Vec<EventGroup>,
        oldest: Option<String>,
        /// Partial-coverage notes for `errors`; the groups are still facts.
        notes: Vec<String>,
    }

    /// Read the kernel journal for the window. `Err` is the text for `errors`.
    fn read_journal(matchers: &Matchers) -> Result<JournalRead, String> {
        let since = format!("--since=-{}d", super::query::WINDOW_DAYS);
        let lines = format!("--lines={MAX_JOURNAL_ENTRIES}");
        let out = proc::run(
            "journalctl",
            &[
                "_TRANSPORT=kernel",
                &since,
                &lines,
                "-o",
                "json",
                "--no-pager",
            ],
            PROBE_BUDGET,
        )
        .map_err(failure_text)?;
        if !out.success && out.stdout.trim().is_empty() {
            return Err(format!(
                "journalctl exited with code {}",
                out.code.map_or_else(|| "?".to_string(), |c| c.to_string())
            ));
        }
        let (groups, read) = groups_from_journal(&out.stdout, matchers);
        let mut notes = Vec::new();
        if read >= MAX_JOURNAL_ENTRIES {
            notes.push(format!(
                "journal read capped at the newest {MAX_JOURNAL_ENTRIES} kernel entries"
            ));
        }
        // Cheap and optional: how far back the journal reaches.
        let oldest = proc::run(
            "journalctl",
            &["--list-boots", "-o", "json", "--no-pager"],
            Duration::from_secs(5),
        )
        .ok()
        .and_then(|o| oldest_from_list_boots(&o.stdout));
        Ok(JournalRead {
            groups,
            oldest,
            notes,
        })
    }

    pub fn collect() -> Section {
        let mut report = Report::default();
        let Some(matchers) = Matchers::compile(LINUX_KERNEL_PATTERNS) else {
            report
                .errors
                .push("kernel patterns failed to compile".to_string());
            return build_section(report);
        };

        match read_journal(&matchers) {
            Ok(read) => {
                report.groups = read.groups;
                report.sources.push("journal".to_string());
                let (effective, oldest) = effective_window(read.oldest.as_deref(), Utc::now());
                report.effective_window_days = effective;
                report.oldest_event_utc = oldest;
                report.errors.extend(read.notes);
            }
            Err(e) => report.errors.push(e),
        }
        match read_edac(Path::new("/sys/devices/system/edac/mc")) {
            Ok(Some(edac)) => {
                report.sources.push("edac".to_string());
                report.edac = edac;
            }
            Ok(None) => {}
            Err(e) => report.errors.push(e),
        }
        match read_aer(Path::new("/sys/bus/pci/devices")) {
            Ok(Some(aer)) => {
                report.sources.push("aer".to_string());
                report.aer = aer;
            }
            Ok(None) => {}
            Err(e) => report.errors.push(e),
        }
        build_section(report)
    }
}

#[cfg(test)]
mod tests {
    use std::collections::{BTreeMap, HashMap};
    use std::path::{Path, PathBuf};

    use chrono::{DateTime, Utc};
    use serde_json::{json, Value};

    use super::linux;
    use super::model::{self, group, EventGroup};
    use super::query::{self, KernelPattern, WinQuery};
    use super::winevent;

    // ---- seam: the Rust constant equals the contract's vector file ----------------

    fn vector() -> Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/fixtures/vectors/hardware_event_query.json");
        serde_json::from_str(&std::fs::read_to_string(path).expect("read the vector file"))
            .expect("parse the vector file")
    }

    #[test]
    fn the_query_constant_equals_the_contract_vector() {
        let v = vector();
        assert_eq!(v["window_days"], query::WINDOW_DAYS);

        let windows = v["windows"].as_array().expect("windows");
        assert_eq!(windows.len(), query::WINDOWS.len(), "provider entry count");
        for (json, rust) in windows.iter().zip(query::WINDOWS) {
            let ids: Vec<u32> = json["event_ids"]
                .as_array()
                .expect("event_ids")
                .iter()
                .map(|i| i.as_u64().expect("id") as u32)
                .collect();
            let expected = WinQuery {
                log: leak(json["log"].as_str().expect("log")),
                provider: leak(json["provider"].as_str().expect("provider")),
                event_ids: Box::leak(ids.into_boxed_slice()),
                max_level: json["max_level"].as_u64().expect("max_level") as u8,
                aggregate: json.get("aggregate").and_then(Value::as_str).map(leak),
            };
            assert_eq!(&expected, rust);
        }

        let patterns = v["linux_kernel_patterns"].as_array().expect("patterns");
        assert_eq!(patterns.len(), query::LINUX_KERNEL_PATTERNS.len());
        for (json, rust) in patterns.iter().zip(query::LINUX_KERNEL_PATTERNS) {
            let expected = KernelPattern {
                key: leak(json["key"].as_str().expect("key")),
                regex: leak(json["regex"].as_str().expect("regex")),
            };
            assert_eq!(&expected, rust);
        }
    }

    fn leak(s: &str) -> &'static str {
        Box::leak(s.to_string().into_boxed_str())
    }

    #[test]
    fn every_kernel_pattern_compiles() {
        assert!(linux::Matchers::compile(query::LINUX_KERNEL_PATTERNS).is_some());
    }

    // ---- the FilterXml builder ----------------------------------------------------

    #[test]
    fn filter_xml_has_one_select_per_entry_and_escapes() {
        let entries: Vec<&WinQuery> = query::WINDOWS.iter().collect();
        let xml = query::filter_xml(&entries, 14);
        assert_eq!(xml.matches("<Select ").count(), query::WINDOWS.len());
        assert_eq!(xml.matches("</Select>").count(), query::WINDOWS.len());
        assert!(xml.starts_with("<QueryList><Query Id=\"0\" Path=\"System\">"));
        assert!(xml.ends_with("</Query></QueryList>"));
        // No raw comparison operator may survive inside the XPath.
        assert!(!xml.contains("<="));
        assert!(!xml.contains(">="));
        assert!(xml.contains("timediff(@SystemTime) &lt;= 1209600000"));
        assert!(xml.contains(
            "<Select Path=\"System\">*[System[Provider[@Name='Microsoft-Windows-WHEA-Logger'] \
             and (EventID=17 or EventID=18 or EventID=19 or EventID=47) \
             and (Level&gt;=1 and Level&lt;=4) and TimeCreated"
        ));
        assert!(xml.contains(
            "<Select Path=\"Application\">*[System[Provider[@Name='Application Error'] \
             and (EventID=1000) and (Level&gt;=1 and Level&lt;=2)"
        ));
    }

    #[test]
    fn filter_xml_escapes_provider_names() {
        let q = WinQuery {
            log: "System",
            provider: "A&B'<C>",
            event_ids: &[1],
            max_level: 3,
            aggregate: None,
        };
        let xml = query::filter_xml(&[&q], 1);
        assert!(xml.contains("@Name='A&amp;B&apos;&lt;C&gt;'"));
        assert!(xml.contains("&lt;= 86400000"));
    }

    #[test]
    fn the_probe_script_embeds_both_queries_and_no_placeholder() {
        let script = winevent::probe_script();
        assert!(!script.contains("@@"), "unreplaced placeholder");
        let hw = query::WINDOWS
            .iter()
            .filter(|q| q.aggregate.is_none())
            .count();
        // The hardware here-string holds every non-aggregate provider and not the app one.
        let (hw_part, app_part) = script
            .split_once("$appXml = @'")
            .expect("both here-strings");
        assert_eq!(hw_part.matches("<Select ").count(), hw);
        assert_eq!(app_part.matches("<Select ").count(), 1);
        assert!(!hw_part.contains("Application Error"));
        assert!(app_part.contains("Application Error"));
        assert!(script.contains("NoMatchingEventsFound"));
        assert!(script.contains("ConvertTo-Json -Depth 6 -Compress"));
        // A here-string terminator must start its line.
        assert_eq!(script.matches("\n'@\n").count(), 2);
    }

    // ---- EventData -> details -----------------------------------------------------

    fn details(source: &str, id: u32, xml: &str, msg: &str) -> Vec<(&'static str, String)> {
        winevent::event_details(source, id, xml, msg, &HashMap::new())
    }

    fn get<'a>(d: &'a [(&'static str, String)], key: &str) -> Option<&'a str> {
        d.iter().find(|(k, _)| *k == key).map(|(_, v)| v.as_str())
    }

    #[test]
    fn event_data_parses_named_unnamed_empty_and_escaped_elements() {
        let xml = "<EventData><Data Name='A'>1</Data><Data Name=\"B\">x &amp; y</Data>\
                   <Data>plain</Data><Data Name='E' /><Data Name='F'></Data></EventData>";
        let d = winevent::parse_event_data(xml);
        let pairs: Vec<(Option<&str>, &str)> = d
            .iter()
            .map(|d| (d.name.as_deref(), d.value.as_str()))
            .collect();
        assert_eq!(
            pairs,
            vec![
                (Some("A"), "1"),
                (Some("B"), "x & y"),
                (None, "plain"),
                (Some("E"), ""),
                (Some("F"), ""),
            ]
        );
    }

    #[test]
    fn whea_details_from_named_fields() {
        let xml = "<EventData><Data Name='ErrorSource'>Corrected Machine Check</Data>\
                   <Data Name='ErrorType'>Bus/Interconnect Error</Data>\
                   <Data Name='ApicId'>6</Data></EventData>";
        let d = details("Microsoft-Windows-WHEA-Logger", 19, xml, "");
        assert_eq!(get(&d, "error_source"), Some("Corrected Machine Check"));
        assert_eq!(get(&d, "error_type"), Some("Bus/Interconnect Error"));
        assert_eq!(get(&d, "processor_apic_id"), Some("6"));
        assert_eq!(get(&d, "pci_vendor_device"), None);
    }

    #[test]
    fn whea_numeric_source_falls_back_to_the_message_text() {
        let xml = "<EventData><Data Name='ErrorSource'>1</Data></EventData>";
        let msg = "A corrected hardware error has occurred.\r\n\r\nReported by component: Processor Core\r\n\
                   Error Source: Corrected Machine Check\r\nError Type: Cache Hierarchy Error\r\nProcessor APIC ID: 14\r\n";
        let d = details("Microsoft-Windows-WHEA-Logger", 19, xml, msg);
        assert_eq!(get(&d, "error_source"), Some("Corrected Machine Check"));
        assert_eq!(get(&d, "error_type"), Some("Cache Hierarchy Error"));
        assert_eq!(get(&d, "processor_apic_id"), Some("14"));
        // And a numeric value with no message to explain it is reported as it came.
        let d = details("Microsoft-Windows-WHEA-Logger", 19, xml, "");
        assert_eq!(get(&d, "error_source"), Some("1"));
    }

    #[test]
    fn whea_pci_ids_and_location_from_event_data() {
        let xml = "<EventData><Data Name='VendorId'>0x1022</Data><Data Name='DeviceId'>5251</Data>\
                   <Data Name='PrimaryBusNumber'>0</Data><Data Name='PrimaryDeviceNumber'>1</Data>\
                   <Data Name='PrimaryFunctionNumber'>2</Data></EventData>";
        let d = details("Microsoft-Windows-WHEA-Logger", 17, xml, "");
        // 5251 decimal = 0x1483.
        assert_eq!(get(&d, "pci_vendor_device"), Some("1022:1483"));
        assert_eq!(get(&d, "pci_location"), Some("00:01.2"));
    }

    #[test]
    fn whea_pci_ids_and_location_from_the_message() {
        let msg =
            "A corrected hardware error has occurred.\r\n\r\nComponent: PCI Express Root Port\r\n\
                   Error Source: Advanced Error Reporting (PCI Express)\r\n\r\n\
                   Primary Bus:Device:Function: 0x3:0x1:0x0\r\n\
                   Primary Device Name:PCI\\VEN_10DE&DEV_2704&SUBSYS_00000000&REV_A1\r\n";
        let d = details("Microsoft-Windows-WHEA-Logger", 17, "", msg);
        assert_eq!(get(&d, "pci_vendor_device"), Some("10de:2704"));
        assert_eq!(get(&d, "pci_location"), Some("03:01.0"));
        assert_eq!(
            get(&d, "error_source"),
            Some("Advanced Error Reporting (PCI Express)")
        );
    }

    #[test]
    fn kernel_power_41_converts_the_decimal_bugcheck_and_reads_the_power_button() {
        let xml = "<EventData><Data Name='BugcheckCode'>292</Data>\
                   <Data Name='BugcheckParameter1'>0x0</Data>\
                   <Data Name='PowerButtonTimestamp'>0</Data></EventData>";
        let d = details("Microsoft-Windows-Kernel-Power", 41, xml, "");
        assert_eq!(get(&d, "bugcheck_code"), Some("0x00000124"));
        assert_eq!(get(&d, "power_button"), Some("false"));

        let xml = "<EventData><Data Name='BugcheckCode'>0</Data>\
                   <Data Name='PowerButtonTimestamp'>133600000000000000</Data></EventData>";
        let d = details("Microsoft-Windows-Kernel-Power", 41, xml, "");
        assert_eq!(get(&d, "bugcheck_code"), Some("0x00000000"));
        assert_eq!(get(&d, "power_button"), Some("true"));
    }

    #[test]
    fn bugcheck_1001_takes_the_first_parameter_hex() {
        let xml = "<EventData><Data Name='param1'>0x00000116 (0xffffd0, 0x1, 0x2, 0x3)</Data>\
                   <Data Name='param2'>C:\\Windows\\MEMORY.DMP</Data></EventData>";
        for source in ["BugCheck", "Microsoft-Windows-WER-SystemErrorReporting"] {
            let d = details(source, 1001, xml, "");
            assert_eq!(get(&d, "bugcheck_code"), Some("0x00000116"), "{source}");
        }
        // Unnamed parameters, upper-case hex.
        let xml = "<EventData><Data>0x0000009C (0x0, 0x0, 0x0, 0x0)</Data></EventData>";
        let d = details("BugCheck", 1001, xml, "");
        assert_eq!(get(&d, "bugcheck_code"), Some("0x0000009c"));
        // No XML at all: the message still carries it.
        let d = details(
            "BugCheck",
            1001,
            "",
            "The computer has rebooted from a bugcheck.  The bugcheck was: 0x00000124 (0x0).",
        );
        assert_eq!(get(&d, "bugcheck_code"), Some("0x00000124"));
    }

    #[test]
    fn storage_events_resolve_the_disk_number_and_bus_type() {
        let disks: HashMap<String, String> = [("0", "NVMe"), ("1", "USB")]
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect();
        let xml = "<EventData><Data>\\Device\\Harddisk1\\DR1</Data></EventData>";
        let d = winevent::event_details("disk", 11, xml, "", &disks);
        assert_eq!(get(&d, "disk_number"), Some("1"));
        assert_eq!(get(&d, "disk_bus_type"), Some("USB"));

        // Event 153 names the disk in its message when the data does not.
        let d = winevent::event_details(
            "disk",
            153,
            "<EventData><Data Name='LogicalBlockAddress'>0x1a2b3c</Data></EventData>",
            "The IO operation at logical block address 0x1a2b3c for Disk 0 was retried.",
            &disks,
        );
        assert_eq!(get(&d, "disk_number"), Some("0"));
        assert_eq!(get(&d, "disk_bus_type"), Some("NVMe"));

        // A RaidPort names a port, not a disk: no number, bus Unknown.
        let xml = "<EventData><Data>\\Device\\RaidPort0</Data></EventData>";
        let d = winevent::event_details("stornvme", 129, xml, "", &disks);
        assert_eq!(get(&d, "disk_number"), None);
        assert_eq!(get(&d, "disk_bus_type"), Some("Unknown"));

        // A disk the bus map does not know is Unknown too.
        let xml = "<EventData><Data>\\Device\\Harddisk7\\DR7</Data></EventData>";
        let d = winevent::event_details("disk", 7, xml, "", &disks);
        assert_eq!(get(&d, "disk_number"), Some("7"));
        assert_eq!(get(&d, "disk_bus_type"), Some("Unknown"));
    }

    #[test]
    fn nvlddmkm_xid_from_data_or_message() {
        let d = details(
            "nvlddmkm",
            14,
            "<EventData><Data Name='Xid'>79</Data></EventData>",
            "",
        );
        assert_eq!(get(&d, "xid"), Some("79"));
        let d = details(
            "nvlddmkm",
            13,
            "",
            "NVRM: Xid (PCI:0000:01:00): 31, pid=4242, Ch 00000008",
        );
        assert_eq!(get(&d, "xid"), Some("31"));
        let d = details("nvlddmkm", 153, "", "Xid 62 occurred");
        assert_eq!(get(&d, "xid"), Some("62"));
        assert!(details("nvlddmkm", 13, "", "Graphics Exception: ESR 0x404600").is_empty());
    }

    #[test]
    fn display_4101_names_the_driver() {
        let d = details(
            "Display",
            4101,
            "<EventData><Data>nvlddmkm</Data><Data></Data></EventData>",
            "",
        );
        assert_eq!(get(&d, "driver"), Some("nvlddmkm"));
        let d = details(
            "Display",
            4101,
            "",
            "Display driver amdkmdag.sys stopped responding and has successfully recovered.",
        );
        assert_eq!(get(&d, "driver"), Some("amdkmdag"));
    }

    // ---- Application Error aggregate ----------------------------------------------

    #[test]
    fn basenames_and_exception_codes_are_normalized() {
        assert_eq!(
            winevent::basename("C:\\Users\\bob\\AppData\\x\\bad.dll"),
            "bad.dll"
        );
        assert_eq!(winevent::basename("/opt/x/y.so"), "y.so");
        assert_eq!(winevent::basename("ntdll.dll"), "ntdll.dll");
        assert_eq!(
            winevent::exception_code("c0000005").as_deref(),
            Some("0xc0000005")
        );
        assert_eq!(
            winevent::exception_code("0XC000001D").as_deref(),
            Some("0xc000001d")
        );
        assert_eq!(winevent::exception_code("5").as_deref(), Some("0x00000005"));
        assert_eq!(winevent::exception_code("oops"), None);
    }

    #[test]
    fn app_crashes_never_carry_paths_or_users() {
        let probe = json!({"app": {"total": 3, "agg": {
            "chrome.exe|C:\\Users\\alice\\AppData\\Local\\x\\bad.dll|c0000005|2026-06-01": 2,
            "Chrome.exe|BAD.DLL|c0000005|2026-06-02": 1,
        }}});
        let v = winevent::app_crashes_from_probe(&probe);
        assert_eq!(v["distinct_apps"], 1);
        assert_eq!(v["distinct_modules"], 1);
        assert_eq!(v["exception_codes"], json!({"0xc0000005": 3}));
        assert!(!v.to_string().to_lowercase().contains("alice"));
        assert!(!v.to_string().contains('\\'));
        assert_eq!(
            winevent::app_crashes_from_probe(&json!({"app": null})),
            Value::Null
        );
        assert_eq!(winevent::app_crashes_from_probe(&json!({})), Value::Null);
    }

    #[test]
    fn app_crash_exception_codes_are_bounded() {
        let agg: serde_json::Map<String, Value> = (0..30)
            .map(|i| (format!("a|m|c{i:07x}|2026-06-01"), json!(1)))
            .collect();
        let v = winevent::app_crashes_from_probe(&json!({"app": {"total": 30, "agg": agg}}));
        assert_eq!(
            v["exception_codes"].as_object().unwrap().len(),
            model::MAX_EXCEPTION_CODES
        );
    }

    // ---- the portable core --------------------------------------------------------

    fn dated(source: &str, count: u64, level: &'static str, last_seen: &str) -> EventGroup {
        EventGroup {
            last_seen: last_seen.to_string(),
            ..group(source, 1, level, count)
        }
    }

    fn sources(groups: &[EventGroup]) -> Vec<&str> {
        groups.iter().map(|g| g.source.as_str()).collect()
    }

    #[test]
    fn a_short_group_list_is_kept_whole_and_sorted_by_count() {
        let (out, dropped) = model::select_groups(vec![
            group("small", 1, "warning", 1),
            group("big", 1, "warning", 99),
        ]);
        assert_eq!(dropped, 0);
        assert_eq!(sources(&out), vec!["big", "small"]);
    }

    #[test]
    fn the_cap_is_24_and_a_lone_critical_group_survives_noise() {
        let mut raw: Vec<EventGroup> = (0..model::MAX_GROUPS + 20)
            .map(|i| {
                dated(
                    &format!("noise{i}"),
                    5000 + i as u64,
                    "warning",
                    "2026-06-01T00:00:00Z",
                )
            })
            .collect();
        raw.push(dated("Kernel-Power", 1, "critical", "2026-06-04T18:00:00Z"));
        let (out, dropped) = model::select_groups(raw);
        assert_eq!(model::MAX_GROUPS, 24);
        assert_eq!(out.len(), 24);
        assert_eq!(dropped, 21);
        assert!(sources(&out).contains(&"Kernel-Power"));
    }

    #[test]
    fn a_new_low_count_group_survives_on_recency() {
        let mut raw: Vec<EventGroup> = (0..model::MAX_GROUPS + 10)
            .map(|i| {
                dated(
                    &format!("noise{i}"),
                    900 + i as u64,
                    "warning",
                    "2026-06-01T00:00:00Z",
                )
            })
            .collect();
        raw.push(dated("BrandNew", 2, "warning", "2026-06-04T17:59:00Z"));
        let (out, _) = model::select_groups(raw);
        assert!(sources(&out).contains(&"BrandNew"));
    }

    #[test]
    fn no_tier_can_consume_the_whole_budget() {
        let mut raw: Vec<EventGroup> = (0..20)
            .map(|i| dated(&format!("crit{i}"), 1, "critical", "2026-06-04T18:00:00Z"))
            .collect();
        raw.extend((0..model::MAX_GROUPS).map(|i| {
            dated(
                &format!("loud{i}"),
                1000 + i as u64,
                "warning",
                "2026-06-01T00:00:00Z",
            )
        }));
        let (out, _) = model::select_groups(raw);
        assert_eq!(out.len(), model::MAX_GROUPS);
        let crit = sources(&out)
            .iter()
            .filter(|s| s.starts_with("crit"))
            .count();
        assert!(
            crit <= model::CRITICAL_SLOTS + model::RECENT_SLOTS,
            "{crit}"
        );
    }

    #[test]
    fn details_keep_the_five_most_frequent_values() {
        let mut d = model::Details::new();
        for (value, n) in [
            ("a", 1),
            ("b", 9),
            ("c", 3),
            ("d", 3),
            ("e", 2),
            ("f", 5),
            ("g", 4),
        ] {
            for _ in 0..n {
                model::tally(&mut d, "k", value.to_string());
            }
        }
        d.insert("empty".into(), BTreeMap::new());
        let reduced = model::reduce_details(d);
        assert!(!reduced.contains_key("empty"));
        let kept: Vec<&str> = reduced["k"].keys().map(String::as_str).collect();
        assert_eq!(kept, vec!["b", "c", "d", "f", "g"]);
    }

    #[test]
    fn the_summary_counts_events_in_dropped_groups_too() {
        let groups: Vec<EventGroup> = (0..30)
            .map(|i| group(&format!("g{i}"), 1, "warning", 2))
            .collect();
        let v = model::build_section(model::Report {
            groups,
            ..Default::default()
        })
        .into_value();
        assert_eq!(v["summary"], "60 hardware-relevant events in 14d");
        assert_eq!(v["groups"].as_array().unwrap().len(), 24);
        assert_eq!(v["truncated"], true);
        assert_eq!(v["truncated_count"], 6);
        assert_eq!(v["status"], "ok");
    }

    fn at(s: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(s).unwrap().with_timezone(&Utc)
    }

    #[test]
    fn the_effective_window_is_shorter_for_a_wrapped_log() {
        let now = at("2026-06-04T18:00:00Z");
        // Older than the window: the full window, as an integer.
        let (days, oldest) = model::effective_window(Some("2026-05-12T03:41:09Z"), now);
        assert_eq!(days, Some(14.0));
        assert_eq!(oldest.as_deref(), Some("2026-05-12T03:41:09Z"));
        // A log that wrapped 3.5 days ago.
        let (days, _) = model::effective_window(Some("2026-06-01T06:00:00Z"), now);
        assert_eq!(days, Some(3.5));
        // Unknown or garbled.
        assert_eq!(model::effective_window(None, now), (None, None));
        assert_eq!(
            model::effective_window(Some("yesterday"), now),
            (None, None)
        );
        // The whole-number case is an integer on the wire, the fraction a float.
        let v = model::build_section(model::Report {
            effective_window_days: Some(14.0),
            ..Default::default()
        })
        .into_value();
        assert!(v["effective_window_days"].is_u64());
    }

    // ---- Linux readers ------------------------------------------------------------

    fn matchers() -> linux::Matchers {
        linux::Matchers::compile(query::LINUX_KERNEL_PATTERNS).unwrap()
    }

    fn us(s: &str) -> String {
        (at(s).timestamp() * 1_000_000).to_string()
    }

    fn jline(ts: &str, priority: &str, message: &str) -> String {
        json!({
            "__REALTIME_TIMESTAMP": us(ts),
            "PRIORITY": priority,
            "_TRANSPORT": "kernel",
            "MESSAGE": message,
        })
        .to_string()
    }

    #[test]
    fn the_journal_matcher_groups_by_key_with_captures() {
        let journal = [
            jline(
                "2026-07-27T10:00:00Z",
                "3",
                "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.",
            ),
            jline(
                "2026-07-29T22:41:03Z",
                "3",
                "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.",
            ),
            jline(
                "2026-07-29T23:00:00Z",
                "3",
                "NVRM: Xid (PCI:0000:01:00): 31, pid=1",
            ),
            jline(
                "2026-07-28T01:00:00Z",
                "3",
                "blk_update_request: I/O error, dev sda, sector 4 op 0x0:(READ)",
            ),
            jline(
                "2026-07-28T02:00:00Z",
                "4",
                "nvme nvme0: I/O 12 QID 3 timeout, reset controller",
            ),
            jline(
                "2026-07-28T03:00:00Z",
                "2",
                "mce: [Hardware Error]: CPU 3: Machine Check: 0 Bank 5",
            ),
            jline("2026-07-28T04:00:00Z", "6", "unrelated message about eth0"),
            "this is not json but contains I/O error, dev x".to_string(),
        ]
        .join("\n");
        let (groups, lines) = linux::groups_from_journal(&journal, &matchers());
        assert_eq!(lines, 8);
        let by_key: HashMap<&str, &EventGroup> =
            groups.iter().map(|g| (g.source.as_str(), g)).collect();

        let xid = by_key["nvrm_xid"];
        assert_eq!((xid.count, xid.event_id, xid.level), (3, 0, "error"));
        assert_eq!(xid.last_seen, "2026-07-29T23:00:00Z");
        assert_eq!(xid.by_day["2026-07-29"], 2);
        assert_eq!(xid.details["xid"]["79"], 2);
        assert_eq!(xid.details["xid"]["31"], 1);
        assert_eq!(xid.sample, "NVRM: Xid (PCI:0000:01:00): 31, pid=1");

        assert_eq!(by_key["block_io"].details["device"]["sda"], 1);
        // `nvme ... I/O 12 QID 3 timeout` is the nvme matcher, not block_io.
        assert_eq!(by_key["nvme"].count, 1);
        assert_eq!(by_key["nvme"].level, "warning");
        assert!(by_key["nvme"].details.is_empty());
        assert_eq!(by_key["mce"].level, "critical");
        assert_eq!(groups.len(), 4);
    }

    #[test]
    fn the_first_matching_pattern_wins_so_a_line_is_counted_once() {
        let line = jline(
            "2026-07-28T01:00:00Z",
            "3",
            "nvme0n1: I/O error, dev nvme0n1, sector 0",
        );
        let (groups, _) = linux::groups_from_journal(&line, &matchers());
        // Both block_io and nvme would match; the table order decides.
        assert_eq!(groups.len(), 1);
        assert_eq!(groups[0].source, "block_io");
    }

    #[test]
    fn a_binary_journal_message_is_decoded_lossily() {
        let line = json!({
            "__REALTIME_TIMESTAMP": us("2026-07-28T01:00:00Z"),
            "PRIORITY": "3",
            "MESSAGE": b"ata1.00: failed command: READ DMA".to_vec(),
        })
        .to_string();
        let (groups, _) = linux::groups_from_journal(&line, &matchers());
        assert_eq!(groups[0].source, "ata");
    }

    #[test]
    fn priorities_map_to_levels() {
        let m = matchers();
        for (prio, want) in [
            ("0", "critical"),
            ("2", "critical"),
            ("3", "error"),
            ("4", "warning"),
            ("5", "information"),
            ("6", "information"),
        ] {
            let line = jline("2026-07-28T01:00:00Z", prio, "ata1: failed command: READ");
            let (groups, _) = linux::groups_from_journal(&line, &m);
            assert_eq!(groups[0].level, want, "priority {prio}");
        }
    }

    #[test]
    fn the_oldest_journal_entry_comes_from_list_boots() {
        let out = json!([
            {"index": -1, "boot_id": "a", "first_entry": 1_780_000_000_000_000_i64, "last_entry": 1_780_100_000_000_000_i64},
            {"index": 0, "boot_id": "b", "first_entry": 1_780_200_000_000_000_i64, "last_entry": 1_780_300_000_000_000_i64},
        ])
        .to_string();
        assert_eq!(
            linux::oldest_from_list_boots(&out).as_deref(),
            Some("2026-05-28T20:26:40Z")
        );
        // Older systemd prints text.
        assert_eq!(
            linux::oldest_from_list_boots("-1 abc Mon 2026-05-28 20:26:40 UTC"),
            None
        );
        assert_eq!(linux::oldest_from_list_boots("[]"), None);
    }

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            use std::sync::atomic::{AtomicU32, Ordering};
            static N: AtomicU32 = AtomicU32::new(0);
            let dir = std::env::temp_dir().join(format!(
                "kenny-hwerr-{tag}-{}-{}",
                std::process::id(),
                N.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir_all(&dir).unwrap();
            Self(dir)
        }

        fn write(&self, rel: &str, content: &str) {
            let p = self.0.join(rel);
            std::fs::create_dir_all(p.parent().unwrap()).unwrap();
            std::fs::write(p, content).unwrap();
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn edac_lists_every_controller_in_numeric_order() {
        let t = TempDir::new("edac");
        t.write("mc0/ce_count", "3\n");
        t.write("mc0/ue_count", "0\n");
        t.write("mc10/ce_count", "0\n");
        t.write("mc10/ue_count", "2\n");
        t.write("mc2/ce_count", "1\n");
        t.write("mc2/ue_count", "0\n");
        t.write("power/runtime", "x");
        let edac = linux::read_edac(&t.0).unwrap().unwrap();
        let names: Vec<&str> = edac
            .iter()
            .map(|e| e["controller"].as_str().unwrap())
            .collect();
        assert_eq!(names, vec!["mc0", "mc2", "mc10"]);
        assert_eq!(
            edac[0],
            json!({"controller": "mc0", "ce_count": 3, "ue_count": 0})
        );
        assert_eq!(edac[2]["ue_count"], 2);
        // No EDAC at all is not an error.
        assert_eq!(linux::read_edac(&t.0.join("missing")), Ok(None));
    }

    #[test]
    fn aer_totals_come_from_the_total_line_or_the_sum() {
        let multi = "RxErr 1\nBadTLP 2\nBadDLLP 0\nRollover 0\nTimeout 0\nNonFatalErr 0\nCorrIntErr 0\nHeaderOF 0\nTOTAL_ERR_COR 3\n";
        assert_eq!(linux::parse_aer_total(multi), Some(3));
        assert_eq!(linux::parse_aer_total("RxErr 1\nBadTLP 2\n"), Some(3));
        assert_eq!(linux::parse_aer_total("TOTAL_ERR_FATAL 0\n"), Some(0));
        assert_eq!(linux::parse_aer_total(""), None);
        assert_eq!(linux::parse_aer_total("garbage\n"), None);
    }

    // PCIe device directories in sysfs are named by bus address (`0000:01:00.0`); a
    // colon is not a valid file-name character on Windows, so the fake tree this
    // reads can only be built where the reader itself runs.
    #[cfg(unix)]
    #[test]
    fn aer_lists_only_devices_with_errors_largest_first() {
        let t = TempDir::new("aer");
        t.write(
            "0000:01:00.0/aer_dev_correctable",
            "RxErr 12\nTOTAL_ERR_COR 12\n",
        );
        t.write("0000:01:00.0/aer_dev_nonfatal", "TOTAL_ERR_NONFATAL 0\n");
        t.write("0000:01:00.0/aer_dev_fatal", "TOTAL_ERR_FATAL 0\n");
        t.write("0000:02:00.0/aer_dev_correctable", "TOTAL_ERR_COR 0\n");
        t.write("0000:02:00.0/aer_dev_nonfatal", "TOTAL_ERR_NONFATAL 0\n");
        t.write("0000:02:00.0/aer_dev_fatal", "TOTAL_ERR_FATAL 0\n");
        t.write("0000:03:00.0/aer_dev_fatal", "TOTAL_ERR_FATAL 5\n");
        t.write("0000:04:00.0/vendor", "0x1022\n");
        let aer = linux::read_aer(&t.0).unwrap().unwrap();
        assert_eq!(aer.len(), 2);
        assert_eq!(
            aer[0],
            json!({"device": "0000:01:00.0", "correctable": 12, "nonfatal": 0, "fatal": 0})
        );
        assert_eq!(aer[1]["device"], "0000:03:00.0");
        assert_eq!(aer[1]["fatal"], 5);
        assert_eq!(linux::read_aer(&t.0.join("missing")), Ok(None));
    }

    // PCIe device directories in sysfs are named by bus address (`0000:01:00.0`); a
    // colon is not a valid file-name character on Windows, so the fake tree this
    // reads can only be built where the reader itself runs.
    #[cfg(unix)]
    #[test]
    fn the_sysfs_lists_are_capped_at_32() {
        let t = TempDir::new("cap");
        for i in 0..40 {
            t.write(&format!("mc{i}/ce_count"), "1\n");
            t.write(
                &format!("0000:{i:02x}:00.0/aer_dev_correctable"),
                "TOTAL_ERR_COR 1\n",
            );
        }
        assert_eq!(linux::read_edac(&t.0).unwrap().unwrap().len(), 32);
        assert_eq!(linux::read_aer(&t.0).unwrap().unwrap().len(), 32);
    }

    // ---- the section the collector produces ---------------------------------------

    #[test]
    fn the_collector_returns_the_contract_shape_on_this_host() {
        let v = super::collect().into_value();
        assert_eq!(v["status"], "ok", "the agent never grades");
        assert_eq!(v["window_days"], 14);
        for list in ["sources", "groups", "edac", "aer", "errors"] {
            assert!(v[list].is_array(), "{list} must be a list");
        }
        assert!(v["truncated"].is_boolean());
        assert!(v["truncated_count"].is_u64());
    }

    fn fixture_section(file: &str) -> Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/fixtures")
            .join(file);
        let fixture: Value =
            serde_json::from_str(&std::fs::read_to_string(path).expect("read the fixture"))
                .expect("parse the fixture");
        fixture["snapshot"]["hardware_errors"].clone()
    }

    /// Sort `groups` by `(source, event_id)` so the comparison does not depend on how the
    /// golden fixture happens to order them.
    fn normalized(mut v: Value) -> Value {
        v["groups"].as_array_mut().unwrap().sort_by_key(|g| {
            (
                g["source"].as_str().unwrap().to_string(),
                g["event_id"].as_u64(),
            )
        });
        v
    }

    fn win_event(xml: &str, msg: &str) -> Value {
        json!({"xml": xml, "msg": msg})
    }

    /// The Windows golden fixture is what the real pipeline produces from the probe's JSON
    /// (raw event XML, counts, the bus-type map), shaped by the real section builder.
    #[test]
    fn the_windows_fixture_is_what_the_collector_produces() {
        let whea = |apic: &str| {
            win_event(
                &format!(
                    "<EventData><Data Name='ErrorSource'>Corrected Machine Check</Data>\
                     <Data Name='ErrorType'>Bus/Interconnect Error</Data>\
                     <Data Name='ApicId'>{apic}</Data></EventData>"
                ),
                "",
            )
        };
        let display = || win_event("<EventData><Data>nvlddmkm</Data></EventData>", "");
        let disk = || {
            win_event(
                "<EventData><Data Name='LogicalBlockAddress'>0x1a2b3c</Data></EventData>",
                "The IO operation at logical block address 0x1a2b3c for Disk 1 was retried.",
            )
        };
        let probe = json!({
            "groups": [
                {"source": "Microsoft-Windows-WHEA-Logger", "event_id": 19, "level": 3, "count": 3,
                 "sample": "A corrected hardware error has occurred.",
                 "last_seen": "2026-06-03T21:07:44Z",
                 "by_day": {"2026-05-29": 1, "2026-06-01": 1, "2026-06-03": 1},
                 "events": [whea("6"), whea("6"), whea("14")]},
                {"source": "Display", "event_id": 4101, "level": 3, "count": 6,
                 "sample": "Display driver nvlddmkm stopped responding and has successfully recovered.",
                 "last_seen": "2026-06-04T11:52:30Z",
                 "by_day": {"2026-06-01": 1, "2026-06-02": 2, "2026-06-04": 3},
                 "events": [display(), display(), display(), display(), display(), display()]},
                {"source": "disk", "event_id": 153, "level": 3, "count": 4,
                 "sample": "The IO operation at logical block address 0x1a2b3c for Disk 1 was retried.",
                 "last_seen": "2026-06-03T08:15:02Z",
                 "by_day": {"2026-06-02": 1, "2026-06-03": 3},
                 "events": [disk(), disk(), disk(), disk()]},
            ],
            "app": {"total": 17, "agg": {
                "chrome.exe|ntdll.dll|c0000005|2026-06-01": 2,
                "chrome.exe|KERNELBASE.dll|c0000005|2026-06-02": 3,
                "outlook.exe|mso.dll|c0000005|2026-06-03": 3,
                "game.exe|C:\\Games\\x\\engine.dll|c0000005|2026-06-04": 1,
                "game.exe|engine.dll|c0000005|2026-06-04": 1,
                "code.exe|ntdll.dll|c0000005|2026-06-03": 1,
                "teams.exe|ntdll.dll|c0000409|2026-06-02": 2,
                "code.exe|ntdll.dll|c0000409|2026-06-03": 1,
                "code.exe|ntdll.dll|c0000409|2026-06-01": 1,
                "chrome.exe|ntdll.dll|c000001d|2026-06-01": 1,
                "chrome.exe|ntdll.dll|c000001d|2026-06-02": 1,
            }},
            "disks": {"0": "NVMe", "1": "SATA"},
            "oldest": "2026-05-12T03:41:09Z",
            "errors": [],
        });
        let (effective, oldest) =
            model::effective_window(probe["oldest"].as_str(), at("2026-06-04T18:00:00Z"));
        let section = model::build_section(model::Report {
            groups: winevent::groups_from_probe(&probe),
            effective_window_days: effective,
            oldest_event_utc: oldest,
            sources: vec!["event_log".into()],
            app_crashes: Some(winevent::app_crashes_from_probe(&probe)),
            ..Default::default()
        })
        .into_value();

        let expected = fixture_section("telemetry_snapshot.json");
        // Compare the pieces separately first so a failure names the field.
        assert_eq!(section["summary"], expected["summary"]);
        assert_eq!(
            section["app_crashes"]["total"],
            expected["app_crashes"]["total"]
        );
        assert_eq!(
            section["app_crashes"]["distinct_modules"],
            expected["app_crashes"]["distinct_modules"]
        );
        assert_eq!(normalized(section), normalized(expected));
    }

    // PCIe device directories in sysfs are named by bus address (`0000:01:00.0`); a
    // colon is not a valid file-name character on Windows, so the fake tree this
    // reads can only be built where the reader itself runs.
    #[cfg(unix)]
    #[test]
    fn the_linux_fixture_is_what_the_collector_produces() {
        let journal = [
            jline(
                "2026-07-27T18:30:12Z",
                "3",
                "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.",
            ),
            jline(
                "2026-07-29T22:41:03Z",
                "3",
                "NVRM: Xid (PCI:0000:01:00): 79, GPU has fallen off the bus.",
            ),
        ]
        .join("\n");
        let (groups, _) = linux::groups_from_journal(&journal, &matchers());

        let edac = TempDir::new("fx-edac");
        edac.write("mc0/ce_count", "3\n");
        edac.write("mc0/ue_count", "0\n");
        let aer = TempDir::new("fx-aer");
        aer.write("0000:01:00.0/aer_dev_correctable", "TOTAL_ERR_COR 12\n");
        aer.write("0000:01:00.0/aer_dev_nonfatal", "TOTAL_ERR_NONFATAL 0\n");
        aer.write("0000:01:00.0/aer_dev_fatal", "TOTAL_ERR_FATAL 0\n");

        let (effective, oldest) =
            model::effective_window(Some("2026-06-02T03:11:09Z"), at("2026-07-30T00:00:00Z"));
        let section = model::build_section(model::Report {
            groups,
            effective_window_days: effective,
            oldest_event_utc: oldest,
            sources: vec!["journal".into(), "edac".into(), "aer".into()],
            app_crashes: None,
            edac: linux::read_edac(&edac.0).unwrap().unwrap(),
            aer: linux::read_aer(&aer.0).unwrap().unwrap(),
            errors: vec![],
        })
        .into_value();

        let mut expected = fixture_section("telemetry_snapshot_linux.json");
        // The fixture's last_seen/by_day reflect its own sample times; the pipeline must
        // reproduce them exactly from the journal lines above.
        let g = &mut expected["groups"][0];
        g["by_day"] = json!({"2026-07-27": 1, "2026-07-29": 1});
        assert_eq!(section, expected);
    }
}
