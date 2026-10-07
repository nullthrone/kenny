//! `fans` section — measured fan speeds, sampled in a short burst.
//!
//! One call takes [`core::SAMPLES_PER_BURST`] RPM samples [`core::SAMPLE_INTERVAL_MS`]
//! apart, so the server can tell a stable fan from a stalled or unstable one while the
//! agent keeps no state between collections (ADR-0007). The agent reports facts and
//! never grades: `status` is always `ok`.
//!
//! - Linux reads the kernel's hwmon tree (`/sys/class/hwmon`).
//! - Windows reads only the WMI namespace of LibreHardwareMonitor / OpenHardwareMonitor
//!   when one of them is running (ADR-0035: no ring-0 driver ships with kenny). Nothing
//!   running is the normal case and yields `fans: []`.
//!
//! Row shape and field semantics: `docs/protocol.md` § Telemetry sections (`fans`).

use crate::telemetry::Section;

/// Collect the `fans` section.
pub fn collect() -> Section {
    #[cfg(windows)]
    {
        windows_impl::collect()
    }
    #[cfg(not(windows))]
    {
        hwmon::collect()
    }
}

/// Portable shaping core — compiled and tested on every platform.
#[cfg_attr(windows, allow(dead_code))]
pub mod core {
    use serde_json::{json, Value};

    use crate::protocol::Status;
    use crate::telemetry::Section;

    /// RPM samples per collection.
    pub const SAMPLES_PER_BURST: usize = 5;
    /// Gap between two samples of a burst, in milliseconds.
    pub const SAMPLE_INTERVAL_MS: u64 = 1000;
    /// Most fans one section lists.
    pub const MAX_FANS: usize = 16;
    /// Most probe errors one section carries.
    pub const MAX_ERRORS: usize = 16;

    /// How a fan is driven, as the contract's `mode` enum.
    #[derive(Debug, Clone, Copy, PartialEq, Eq)]
    pub enum Mode {
        Pwm,
        Dc,
        Auto,
        Manual,
        Unknown,
    }

    impl Mode {
        pub fn as_str(self) -> &'static str {
            match self {
                Mode::Pwm => "pwm",
                Mode::Dc => "dc",
                Mode::Auto => "auto",
                Mode::Manual => "manual",
                Mode::Unknown => "unknown",
            }
        }
    }

    /// One fan as read, before the contract's shaping rules apply.
    #[derive(Debug, Clone, PartialEq)]
    pub struct RawFan {
        pub key: String,
        pub label: Option<String>,
        pub source: &'static str,
        /// One entry per sample, in order; `None` where that read failed.
        pub samples: Vec<Option<u32>>,
        pub duty_percent: Option<f64>,
        pub mode: Mode,
    }

    /// Take `count` ticks, calling `sleep` between consecutive ticks (never after the
    /// last). `read_tick` reads every fan once, so the whole burst costs
    /// `(count - 1)` intervals no matter how many fans there are. The sleep is a
    /// parameter so tests do not wait for it.
    pub fn burst<T>(
        count: usize,
        mut read_tick: impl FnMut() -> Vec<T>,
        mut sleep: impl FnMut(),
    ) -> Vec<Vec<T>> {
        let mut ticks = Vec::with_capacity(count);
        for i in 0..count {
            if i > 0 {
                sleep();
            }
            ticks.push(read_tick());
        }
        ticks
    }

    /// Round to one decimal.
    pub fn round1(x: f64) -> f64 {
        (x * 10.0).round() / 10.0
    }

    /// A raw hwmon `pwmN` value (0-255) as a duty percentage with one decimal; values
    /// above 255 clamp to 100.
    pub fn pwm_to_percent(raw: u32) -> f64 {
        round1((f64::from(raw.min(255)) / 255.0) * 100.0)
    }

    /// A sensor's float reading as a whole RPM; negative, non-finite or absurdly large
    /// readings are failed reads.
    pub fn to_rpm(value: f64) -> Option<u32> {
        (value.is_finite() && (0.0..=f64::from(u32::MAX)).contains(&value))
            .then(|| value.round() as u32)
    }

    /// Shape raw fans into the contract section.
    ///
    /// - A fan whose every sample failed is dropped and reported in `errors`.
    /// - `idle_or_absent` is set when every sample is 0 and no duty is readable.
    /// - At most [`MAX_FANS`] are listed; `truncated` says there were more.
    pub fn shape(raw: Vec<RawFan>, sources_tried: &[&str], mut errors: Vec<String>) -> Section {
        let mut fans = Vec::new();
        for fan in raw {
            let read: Vec<u32> = fan.samples.iter().flatten().copied().collect();
            if read.is_empty() {
                errors.push(format!("{}: no readable RPM sample", fan.key));
                continue;
            }
            let idle = read.iter().all(|&r| r == 0) && fan.duty_percent.is_none();
            fans.push(json!({
                "key": fan.key,
                "label": fan.label,
                "source": fan.source,
                "rpm_samples": read,
                "duty_percent": fan.duty_percent,
                "mode": fan.mode.as_str(),
                "idle_or_absent": idle,
            }));
        }
        let truncated = fans.len() > MAX_FANS;
        fans.truncate(MAX_FANS);
        errors.truncate(MAX_ERRORS);
        let summary = format!("{} fans read", fans.len());
        Section::with_fields(
            Status::Ok,
            summary,
            json!({
                "sources_tried": sources_tried,
                "sample_interval_ms": SAMPLE_INTERVAL_MS,
                "fans": Value::Array(fans),
                "truncated": truncated,
                "errors": errors,
            }),
        )
    }
}

/// Linux hwmon reader. The tree parsing takes the hwmon root as a parameter so tests
/// can point it at a fake tree.
#[cfg_attr(windows, allow(dead_code))]
pub mod hwmon {
    use std::fs;
    use std::io;
    use std::path::{Path, PathBuf};
    use std::time::Duration;

    use super::core::{
        burst, pwm_to_percent, shape, Mode, RawFan, SAMPLES_PER_BURST, SAMPLE_INTERVAL_MS,
    };
    use crate::telemetry::Section;

    const ROOT: &str = "/sys/class/hwmon";

    /// One fan input discovered in the tree.
    #[derive(Debug, Clone, PartialEq)]
    pub struct FanProbe {
        pub key: String,
        pub label: Option<String>,
        pub input: PathBuf,
        /// `pwmN` of the same index N, when it exists and its value is in effect.
        pub pwm: Option<PathBuf>,
        pub mode: Mode,
    }

    /// Map `pwmN_enable` / `pwmN_mode` to the contract's `mode`.
    ///
    /// The kernel ABI: `pwmN_enable` 0 = no control (fan at full speed), 1 = manual PWM
    /// control, 2+ = automatic control by the chip; `pwmN_mode` 0 = DC, 1 = PWM output.
    /// `mode` answers "who sets the speed, and through what signal", in this order:
    ///
    /// 1. enable 0 -> `manual` (the host is not regulating; the fan runs flat out)
    /// 2. enable 2+ -> `auto` (the chip regulates; its signal type does not matter)
    /// 3. otherwise (enable 1 or not exposed): `pwmN_mode` 0 -> `dc`, 1 -> `pwm`
    /// 4. enable 1 without `pwmN_mode` -> `manual`; nothing known -> `unknown`
    pub fn mode_from(enable: Option<i64>, pwm_mode: Option<i64>) -> Mode {
        match enable {
            Some(0) => return Mode::Manual,
            Some(n) if n >= 2 => return Mode::Auto,
            _ => {}
        }
        match (pwm_mode, enable) {
            (Some(0), _) => Mode::Dc,
            (Some(1), _) => Mode::Pwm,
            (_, Some(1)) => Mode::Manual,
            _ => Mode::Unknown,
        }
    }

    fn read_trimmed(path: &Path) -> io::Result<String> {
        Ok(fs::read_to_string(path)?.trim().to_string())
    }

    fn read_int(path: &Path) -> Option<i64> {
        read_trimmed(path).ok()?.parse().ok()
    }

    /// Index of an `hwmonN` directory name.
    fn chip_index(name: &str) -> Option<u32> {
        name.strip_prefix("hwmon")?.parse().ok()
    }

    /// Fan indexes N of the `fanN_input` files in a chip directory, ascending.
    fn fan_indexes(chip: &Path) -> io::Result<Vec<u32>> {
        let mut out: Vec<u32> = fs::read_dir(chip)?
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let name = e.file_name().into_string().ok()?;
                name.strip_prefix("fan")?
                    .strip_suffix("_input")?
                    .parse()
                    .ok()
            })
            .collect();
        out.sort_unstable();
        Ok(out)
    }

    /// Walk the hwmon tree under `root` and list every fan input, plus the problems met.
    ///
    /// - `key` is `<chip name>.fan<N>`. When two chips share a name, every chip of that
    ///   name is keyed `<name>_<hwmon index>.fan<N>` instead.
    /// - `label` comes from `fanN_label`, `None` when absent.
    /// - The duty file is `pwmN` with the **same** N as `fanN_input`. Some chips wire
    ///   pwm and fan channels differently; that is not detectable from sysfs, so the
    ///   same-index pairing is an assumption.
    /// - With `pwmN_enable` = 0 the chip ignores `pwmN` (full speed), so no duty is read.
    ///
    /// A missing root is not an error (containers and non-Linux hosts have none).
    pub fn discover(root: &Path) -> (Vec<FanProbe>, Vec<String>) {
        let mut errors = Vec::new();
        let entries = match fs::read_dir(root) {
            Ok(e) => e,
            Err(e) if e.kind() == io::ErrorKind::NotFound => return (Vec::new(), errors),
            Err(e) => {
                errors.push(format!("hwmon: {}: {e}", root.display()));
                return (Vec::new(), errors);
            }
        };
        let mut chips: Vec<(u32, PathBuf)> = entries
            .filter_map(|e| e.ok())
            .filter_map(|e| {
                let idx = chip_index(e.file_name().to_str()?)?;
                Some((idx, e.path()))
            })
            .collect();
        chips.sort();

        let named: Vec<(u32, PathBuf, String)> = chips
            .into_iter()
            .map(|(idx, path)| {
                let name = read_trimmed(&path.join("name"))
                    .ok()
                    .filter(|n| !n.is_empty())
                    .unwrap_or_else(|| format!("hwmon{idx}"));
                (idx, path, name)
            })
            .collect();

        let mut probes = Vec::new();
        for (idx, path, name) in &named {
            let indexes = match fan_indexes(path) {
                Ok(i) => i,
                Err(e) => {
                    errors.push(format!("hwmon: {}: {e}", path.display()));
                    continue;
                }
            };
            if indexes.is_empty() {
                continue;
            }
            let shared = named.iter().filter(|(_, _, n)| n == name).count() > 1;
            let prefix = if shared {
                format!("{name}_{idx}")
            } else {
                name.clone()
            };
            for n in indexes {
                let enable = read_int(&path.join(format!("pwm{n}_enable")));
                let pwm_mode = read_int(&path.join(format!("pwm{n}_mode")));
                let pwm_file = path.join(format!("pwm{n}"));
                let pwm = (pwm_file.exists() && enable != Some(0)).then_some(pwm_file);
                probes.push(FanProbe {
                    key: format!("{prefix}.fan{n}"),
                    label: read_trimmed(&path.join(format!("fan{n}_label")))
                        .ok()
                        .filter(|l| !l.is_empty()),
                    input: path.join(format!("fan{n}_input")),
                    pwm,
                    mode: mode_from(enable, pwm_mode),
                });
            }
        }
        (probes, errors)
    }

    /// One reading of one fan.
    #[derive(Debug, Clone, Copy)]
    struct Reading {
        rpm: Option<u32>,
        pwm: Option<u32>,
    }

    fn read_probe(p: &FanProbe) -> Reading {
        Reading {
            rpm: read_int(&p.input).and_then(|v| u32::try_from(v).ok()),
            pwm: p
                .pwm
                .as_deref()
                .and_then(read_int)
                .and_then(|v| u32::try_from(v).ok()),
        }
    }

    /// Collect the section from the hwmon tree under `root`, taking a burst of
    /// `samples` ticks and calling `sleep` between them. Every fan of every chip is
    /// read in each tick, so the wall time is `(samples - 1)` intervals in total.
    pub fn collect_from(root: &Path, samples: usize, sleep: impl FnMut()) -> Section {
        let (probes, errors) = discover(root);
        // Nothing to read: do not spend the burst's wall time sleeping.
        let samples = if probes.is_empty() { 1 } else { samples };
        let ticks = burst(
            samples,
            || probes.iter().map(read_probe).collect::<Vec<_>>(),
            sleep,
        );
        let raw = probes
            .iter()
            .enumerate()
            .map(|(i, p)| RawFan {
                key: p.key.clone(),
                label: p.label.clone(),
                source: "hwmon",
                samples: ticks.iter().map(|t| t[i].rpm).collect(),
                // The latest readable duty of the burst.
                duty_percent: ticks
                    .iter()
                    .rev()
                    .find_map(|t| t[i].pwm)
                    .map(pwm_to_percent),
                mode: p.mode,
            })
            .collect();
        shape(raw, &["hwmon"], errors)
    }

    /// Collect the section from the live `/sys/class/hwmon`.
    pub fn collect() -> Section {
        collect_from(Path::new(ROOT), SAMPLES_PER_BURST, || {
            std::thread::sleep(Duration::from_millis(SAMPLE_INTERVAL_MS));
        })
    }
}

/// LibreHardwareMonitor / OpenHardwareMonitor WMI row handling — portable so the
/// pairing logic is tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod lhm {
    use serde_json::Value;

    use super::core::{round1, to_rpm, Mode, RawFan};

    /// A `Sensor` row of type `Fan` or `Control`.
    #[derive(Debug, Clone, PartialEq)]
    pub struct SensorRow {
        pub identifier: String,
        pub name: String,
        pub parent: String,
        pub sensor_type: String,
        pub value: Option<f64>,
    }

    /// Parse one tick's `rows` array (a bare object stands for a one-row array).
    pub fn parse_rows(rows: &Value) -> Vec<SensorRow> {
        let list: Vec<&Value> = match rows {
            Value::Array(a) => a.iter().collect(),
            Value::Object(_) => vec![rows],
            _ => Vec::new(),
        };
        list.into_iter()
            .filter_map(|r| {
                let s = |k: &str| r.get(k).and_then(Value::as_str).map(str::to_string);
                Some(SensorRow {
                    identifier: s("identifier").filter(|i| !i.is_empty())?,
                    name: s("name").unwrap_or_default(),
                    parent: s("parent").unwrap_or_default(),
                    sensor_type: s("type").unwrap_or_default(),
                    value: r.get("value").and_then(Value::as_f64),
                })
            })
            .collect()
    }

    /// Parse the probe's whole output: `Ok(None)` when no LHM/OHM namespace answered
    /// (the normal case), otherwise one row list per tick.
    pub fn parse_burst(v: &Value) -> Result<Option<Vec<Vec<SensorRow>>>, String> {
        let obj = v.as_object().ok_or("unexpected output shape")?;
        if obj.get("ns").is_none_or(Value::is_null) {
            return Ok(None);
        }
        let ticks = match obj.get("ticks") {
            Some(Value::Array(a)) => a.iter().collect::<Vec<_>>(),
            Some(t @ Value::Object(_)) => vec![t],
            _ => return Err("no ticks in output".to_string()),
        };
        Ok(Some(
            ticks
                .into_iter()
                .map(|t| parse_rows(t.get("rows").unwrap_or(&Value::Null)))
                .collect(),
        ))
    }

    /// The parent hardware of a sensor: its `Parent`, else its identifier minus the
    /// last two segments (`/lpc/nct6798d/fan/1` -> `/lpc/nct6798d`).
    fn parent_of(row: &SensorRow) -> String {
        if !row.parent.is_empty() {
            return row.parent.clone();
        }
        let mut parts: Vec<&str> = row.identifier.split('/').collect();
        parts.truncate(parts.len().saturating_sub(2));
        parts.join("/")
    }

    /// The trailing index of an identifier (`/lpc/nct6798d/fan/1` -> 1).
    fn index_of(row: &SensorRow) -> Option<u32> {
        row.identifier.rsplit('/').next()?.parse().ok()
    }

    /// Build fans from a burst. A `Fan` sensor is paired with the `Control` sensor of
    /// the same parent hardware and trailing identifier index (`.../fan/1` with
    /// `.../control/1`) — an assumption that holds for the Super-I/O boards LHM
    /// supports. Samples follow tick order; a tick missing the sensor, or reading it as
    /// null, is a failed sample. The duty is the latest readable `Control` value. The
    /// mode is `unknown`: WMI does not say how the channel is driven.
    pub fn fans_from_ticks(ticks: &[Vec<SensorRow>]) -> Vec<RawFan> {
        // Fan identifiers in a stable order: by parent hardware, index, identifier.
        let mut fans: Vec<&SensorRow> = Vec::new();
        for row in ticks.iter().flatten() {
            if row.sensor_type == "Fan" && !fans.iter().any(|f| f.identifier == row.identifier) {
                fans.push(row);
            }
        }
        fans.sort_by_key(|f| (parent_of(f), index_of(f), f.identifier.clone()));

        fans.into_iter()
            .map(|fan| {
                let parent = parent_of(fan);
                let index = index_of(fan);
                let samples = ticks
                    .iter()
                    .map(|tick| {
                        tick.iter()
                            .find(|r| r.sensor_type == "Fan" && r.identifier == fan.identifier)
                            .and_then(|r| r.value)
                            .and_then(to_rpm)
                    })
                    .collect();
                let duty_percent = index.and_then(|idx| {
                    ticks.iter().rev().find_map(|tick| {
                        tick.iter()
                            .find(|r| {
                                r.sensor_type == "Control"
                                    && parent_of(r) == parent
                                    && index_of(r) == Some(idx)
                            })
                            .and_then(|r| r.value)
                            .filter(|v| v.is_finite())
                            .map(|v| round1(v.clamp(0.0, 100.0)))
                    })
                });
                RawFan {
                    key: fan.identifier.clone(),
                    label: Some(fan.name.clone()).filter(|n| !n.is_empty()),
                    source: "lhm",
                    samples,
                    duty_percent,
                    mode: Mode::Unknown,
                }
            })
            .collect()
    }
}

#[cfg(windows)]
mod windows_impl {
    use serde_json::Value;

    use super::core::{shape, SAMPLES_PER_BURST, SAMPLE_INTERVAL_MS};
    use super::lhm;
    use crate::telemetry::collectors::winps;
    use crate::telemetry::Section;

    /// One bounded PowerShell call for the whole burst (not one spawn per sample).
    /// It picks the first of the two namespaces that answers, then loops, sleeping
    /// between ticks. No namespace answering prints `{"ns":null}` and exits 0: the
    /// monitor not running is normal.
    const SCRIPT: &str = r#"
$ErrorActionPreference = 'Stop'
$ns = $null
foreach ($candidate in 'root/LibreHardwareMonitor', 'root/OpenHardwareMonitor') {
  try {
    $null = Get-CimInstance -Namespace $candidate -ClassName Sensor -ErrorAction Stop | Select-Object -First 1
    $ns = $candidate
    break
  } catch {}
}
if ($null -eq $ns) { '{"ns":null,"ticks":[]}'; exit 0 }
$ticks = @()
for ($i = 0; $i -lt __SAMPLES__; $i++) {
  if ($i -gt 0) { Start-Sleep -Milliseconds __INTERVAL__ }
  $rows = @()
  try {
    $rows = @(Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop |
      Where-Object { $_.SensorType -eq 'Fan' -or $_.SensorType -eq 'Control' } |
      ForEach-Object {
        [pscustomobject]@{
          identifier = [string]$_.Identifier
          name       = [string]$_.Name
          parent     = [string]$_.Parent
          type       = [string]$_.SensorType
          value      = $_.Value
        }
      })
  } catch {}
  $ticks += [pscustomobject]@{ rows = $rows }
}
[pscustomobject]@{ ns = $ns; ticks = $ticks } | ConvertTo-Json -Depth 5 -Compress
"#;

    pub fn collect() -> Section {
        let script = SCRIPT
            .replace("__SAMPLES__", &SAMPLES_PER_BURST.to_string())
            .replace("__INTERVAL__", &SAMPLE_INTERVAL_MS.to_string());
        let sources = ["lhm_wmi"];
        let out: Result<Value, _> = winps::run_json_within(&script, winps::PROBE_BUDGET);
        let parsed = out
            .map_err(|why| format!("lhm_wmi: probe failed: {why:?}"))
            .and_then(|v| lhm::parse_burst(&v).map_err(|e| format!("lhm_wmi: {e}")));
        match parsed {
            Ok(Some(ticks)) => shape(lhm::fans_from_ticks(&ticks), &sources, Vec::new()),
            Ok(None) => shape(Vec::new(), &sources, Vec::new()),
            Err(e) => shape(Vec::new(), &sources, vec![e]),
        }
    }
}

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU32, Ordering};

    use serde_json::{json, Value};

    use super::core::*;
    use super::hwmon::{collect_from, discover, mode_from};
    use super::lhm;

    // ---- fake hwmon tree -------------------------------------------------------

    /// A unique temp directory removed on drop.
    struct Tree(PathBuf);

    impl Tree {
        fn new() -> Self {
            static N: AtomicU32 = AtomicU32::new(0);
            let dir = std::env::temp_dir().join(format!(
                "kenny-fans-test-{}-{}",
                std::process::id(),
                N.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&dir).unwrap();
            Tree(dir)
        }
        fn put(&self, rel: &str, content: &str) {
            let p = self.0.join(rel);
            fs::create_dir_all(p.parent().unwrap()).unwrap();
            fs::write(p, content).unwrap();
        }
        fn root(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for Tree {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn collect_tree(t: &Tree) -> Value {
        collect_from(t.root(), SAMPLES_PER_BURST, || {}).into_value()
    }

    fn fan_by_key<'a>(v: &'a Value, key: &str) -> &'a Value {
        v["fans"]
            .as_array()
            .unwrap()
            .iter()
            .find(|f| f["key"] == key)
            .unwrap_or_else(|| panic!("no fan {key} in {v}"))
    }

    // ---- pure functions --------------------------------------------------------

    #[test]
    fn pwm_scales_to_percent_with_one_decimal() {
        assert_eq!(pwm_to_percent(0), 0.0);
        assert_eq!(pwm_to_percent(255), 100.0);
        assert_eq!(pwm_to_percent(128), 50.2);
        assert_eq!(pwm_to_percent(115), 45.1);
        assert_eq!(pwm_to_percent(9999), 100.0);
    }

    #[test]
    fn mode_mapping_follows_the_kernel_abi() {
        assert_eq!(mode_from(Some(0), None), Mode::Manual);
        assert_eq!(mode_from(Some(0), Some(1)), Mode::Manual);
        assert_eq!(mode_from(Some(2), Some(1)), Mode::Auto);
        assert_eq!(mode_from(Some(5), None), Mode::Auto);
        assert_eq!(mode_from(Some(1), Some(1)), Mode::Pwm);
        assert_eq!(mode_from(Some(1), Some(0)), Mode::Dc);
        assert_eq!(mode_from(Some(1), None), Mode::Manual);
        assert_eq!(mode_from(None, Some(1)), Mode::Pwm);
        assert_eq!(mode_from(None, Some(0)), Mode::Dc);
        assert_eq!(mode_from(None, None), Mode::Unknown);
        assert_eq!(mode_from(Some(-1), None), Mode::Unknown);
    }

    #[test]
    fn rpm_conversion_rejects_garbage() {
        assert_eq!(to_rpm(1180.4), Some(1180));
        assert_eq!(to_rpm(0.0), Some(0));
        assert_eq!(to_rpm(-1.0), None);
        assert_eq!(to_rpm(f64::NAN), None);
        assert_eq!(to_rpm(1e12), None);
    }

    #[test]
    fn burst_sleeps_between_ticks_only() {
        let log = RefCell::new(Vec::new());
        let ticks = burst(
            3,
            || {
                log.borrow_mut().push("read");
                vec![1]
            },
            || log.borrow_mut().push("sleep"),
        );
        assert_eq!(ticks.len(), 3);
        assert_eq!(
            *log.borrow(),
            ["read", "sleep", "read", "sleep", "read"],
            "no sleep before the first or after the last read"
        );
    }

    fn raw(key: &str, samples: Vec<Option<u32>>, duty: Option<f64>) -> RawFan {
        RawFan {
            key: key.into(),
            label: None,
            source: "hwmon",
            samples,
            duty_percent: duty,
            mode: Mode::Unknown,
        }
    }

    #[test]
    fn idle_needs_all_zero_samples_and_no_duty() {
        let v = shape(
            vec![
                raw("a", vec![Some(0); 5], None),
                raw("b", vec![Some(0); 5], Some(40.0)),
                raw(
                    "c",
                    vec![Some(0), Some(0), Some(900), Some(0), Some(0)],
                    None,
                ),
            ],
            &["hwmon"],
            vec![],
        )
        .into_value();
        assert_eq!(fan_by_key(&v, "a")["idle_or_absent"], true);
        assert_eq!(fan_by_key(&v, "b")["idle_or_absent"], false);
        assert_eq!(fan_by_key(&v, "c")["idle_or_absent"], false);
        assert_eq!(v["status"], "ok");
    }

    #[test]
    fn failed_samples_are_skipped_and_all_failed_fans_dropped() {
        let v = shape(
            vec![
                raw(
                    "partial",
                    vec![Some(100), None, Some(102), None, Some(101)],
                    None,
                ),
                raw("dead", vec![None; 5], Some(50.0)),
            ],
            &["hwmon"],
            vec![],
        )
        .into_value();
        assert_eq!(v["summary"], "1 fans read");
        assert_eq!(
            fan_by_key(&v, "partial")["rpm_samples"],
            json!([100, 102, 101])
        );
        assert_eq!(v["fans"].as_array().unwrap().len(), 1);
        assert_eq!(v["errors"], json!(["dead: no readable RPM sample"]));
    }

    #[test]
    fn list_is_capped_at_sixteen() {
        let fans = (0..20)
            .map(|i| raw(&format!("f{i}"), vec![Some(1000); 5], None))
            .collect();
        let v = shape(fans, &["hwmon"], vec![]).into_value();
        assert_eq!(v["fans"].as_array().unwrap().len(), 16);
        assert_eq!(v["truncated"], true);
        assert_eq!(v["summary"], "16 fans read");

        let v = shape(vec![raw("f", vec![Some(1)], None)], &["hwmon"], vec![]).into_value();
        assert_eq!(v["truncated"], false);
    }

    // ---- hwmon tree ------------------------------------------------------------

    #[test]
    fn hwmon_tree_is_parsed_into_fans() {
        let t = Tree::new();
        t.put("hwmon0/name", "coretemp\n");
        t.put("hwmon0/temp1_input", "41000\n");
        t.put("hwmon2/name", "nct6798\n");
        t.put("hwmon2/fan1_input", "1180\n");
        t.put("hwmon2/fan1_label", "CPU_FAN\n");
        t.put("hwmon2/pwm1", "115\n");
        t.put("hwmon2/pwm1_enable", "1\n");
        t.put("hwmon2/pwm1_mode", "1\n");
        t.put("hwmon2/fan2_input", "820\n");
        t.put("hwmon2/pwm2_enable", "5\n");
        t.put("hwmon2/fan10_input", "700\n");

        let v = collect_tree(&t);
        assert_eq!(v["sources_tried"], json!(["hwmon"]));
        assert_eq!(v["sample_interval_ms"], 1000);
        assert_eq!(v["errors"], json!([]));
        let keys: Vec<_> = v["fans"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f["key"].as_str().unwrap())
            .collect();
        assert_eq!(keys, ["nct6798.fan1", "nct6798.fan2", "nct6798.fan10"]);

        let f1 = fan_by_key(&v, "nct6798.fan1");
        assert_eq!(f1["label"], "CPU_FAN");
        assert_eq!(f1["source"], "hwmon");
        assert_eq!(f1["rpm_samples"], json!([1180, 1180, 1180, 1180, 1180]));
        assert_eq!(f1["duty_percent"], 45.1);
        assert_eq!(f1["mode"], "pwm");
        assert_eq!(f1["idle_or_absent"], false);

        let f2 = fan_by_key(&v, "nct6798.fan2");
        assert_eq!(f2["label"], Value::Null);
        assert_eq!(f2["duty_percent"], Value::Null);
        assert_eq!(f2["mode"], "auto");
        assert_eq!(fan_by_key(&v, "nct6798.fan10")["mode"], "unknown");
    }

    #[test]
    fn chips_sharing_a_name_are_keyed_with_the_hwmon_index() {
        let t = Tree::new();
        for idx in [1, 3] {
            t.put(&format!("hwmon{idx}/name"), "amdgpu\n");
            t.put(&format!("hwmon{idx}/fan1_input"), "900\n");
        }
        t.put("hwmon4/name", "thinkpad\n");
        t.put("hwmon4/fan1_input", "2000\n");
        let (probes, errors) = discover(t.root());
        assert!(errors.is_empty());
        let keys: Vec<_> = probes.iter().map(|p| p.key.as_str()).collect();
        assert_eq!(keys, ["amdgpu_1.fan1", "amdgpu_3.fan1", "thinkpad.fan1"]);
    }

    #[test]
    fn unnamed_chip_falls_back_to_its_directory_name() {
        let t = Tree::new();
        t.put("hwmon7/fan1_input", "900\n");
        let (probes, _) = discover(t.root());
        assert_eq!(probes[0].key, "hwmon7.fan1");
    }

    #[test]
    fn manual_enable_zero_reads_no_duty() {
        let t = Tree::new();
        t.put("hwmon0/name", "it8688\n");
        t.put("hwmon0/fan1_input", "1500\n");
        t.put("hwmon0/pwm1", "40\n");
        t.put("hwmon0/pwm1_enable", "0\n");
        let v = collect_tree(&t);
        let f = fan_by_key(&v, "it8688.fan1");
        assert_eq!(f["duty_percent"], Value::Null);
        assert_eq!(f["mode"], "manual");
    }

    #[test]
    fn pwm_pairs_by_same_index_only() {
        let t = Tree::new();
        t.put("hwmon0/name", "chip\n");
        t.put("hwmon0/fan2_input", "1000\n");
        t.put("hwmon0/pwm1", "255\n");
        let v = collect_tree(&t);
        assert_eq!(fan_by_key(&v, "chip.fan2")["duty_percent"], Value::Null);
    }

    #[test]
    fn one_burst_reads_all_fans_per_tick_and_keeps_order() {
        let t = Tree::new();
        t.put("hwmon0/name", "a\n");
        t.put("hwmon0/fan1_input", "100\n");
        t.put("hwmon1/name", "b\n");
        t.put("hwmon1/fan1_input", "200\n");
        t.put("hwmon1/pwm1", "255\n");
        let sleeps = RefCell::new(0u32);
        let v = collect_from(t.root(), 5, || {
            let n = {
                let mut s = sleeps.borrow_mut();
                *s += 1;
                *s
            };
            t.put("hwmon0/fan1_input", &format!("{}\n", 100 + n));
            t.put("hwmon1/fan1_input", &format!("{}\n", 200 + n));
        })
        .into_value();
        assert_eq!(*sleeps.borrow(), 4, "four intervals for five samples");
        assert_eq!(
            fan_by_key(&v, "a.fan1")["rpm_samples"],
            json!([100, 101, 102, 103, 104])
        );
        assert_eq!(
            fan_by_key(&v, "b.fan1")["rpm_samples"],
            json!([200, 201, 202, 203, 204])
        );
        assert_eq!(fan_by_key(&v, "b.fan1")["duty_percent"], 100.0);
    }

    #[test]
    fn unreadable_inputs_are_dropped_and_reported() {
        let t = Tree::new();
        t.put("hwmon0/name", "chip\n");
        t.put("hwmon0/fan1_input", "1000\n");
        t.put("hwmon0/fan2_input", "not a number\n");
        t.put("hwmon0/fan3_input", "0\n");
        let v = collect_tree(&t);
        assert_eq!(v["fans"].as_array().unwrap().len(), 2);
        assert_eq!(v["errors"], json!(["chip.fan2: no readable RPM sample"]));
        assert_eq!(fan_by_key(&v, "chip.fan3")["idle_or_absent"], true);
    }

    #[test]
    fn missing_hwmon_root_is_an_empty_list_without_error() {
        let t = Tree::new();
        let v = collect_from(&t.root().join("absent"), 5, || {}).into_value();
        assert_eq!(v["fans"], json!([]));
        assert_eq!(v["errors"], json!([]));
        assert_eq!(v["sources_tried"], json!(["hwmon"]));
        assert_eq!(v["summary"], "0 fans read");
    }

    #[test]
    fn unreadable_root_is_reported() {
        let t = Tree::new();
        t.put("not_a_dir", "x");
        let v = collect_from(&t.root().join("not_a_dir"), 5, || {}).into_value();
        assert_eq!(v["fans"], json!([]));
        assert_eq!(v["errors"].as_array().unwrap().len(), 1);
    }

    // ---- LibreHardwareMonitor rows -----------------------------------------------

    fn row(id: &str, name: &str, parent: &str, ty: &str, value: Value) -> Value {
        json!({"identifier": id, "name": name, "parent": parent, "type": ty, "value": value})
    }

    fn canned_burst() -> Value {
        let tick = |rpm1: f64, duty1: f64| {
            json!({"rows": [
                row("/lpc/nct6798d/fan/1", "CPU Fan", "/lpc/nct6798d", "Fan", json!(rpm1)),
                row("/lpc/nct6798d/fan/2", "Fan #2", "/lpc/nct6798d", "Fan", json!(0.0)),
                row("/lpc/nct6798d/control/1", "CPU Fan", "/lpc/nct6798d", "Control", json!(duty1)),
                row("/gpu-nvidia/0/fan/1", "GPU Fan", "/gpu-nvidia/0", "Fan", json!(1500.0)),
                row("/gpu-nvidia/0/control/1", "GPU Fan", "/gpu-nvidia/0", "Control", json!(33.33)),
                row("/lpc/it8688e/control/1", "Other", "/lpc/it8688e", "Control", json!(99.0)),
            ]})
        };
        json!({"ns": "root/LibreHardwareMonitor", "ticks": [
            tick(1180.0, 44.0), tick(1176.0, 44.0), tick(1182.0, 44.0),
            tick(1179.0, 44.0), tick(1181.0, 45.04),
        ]})
    }

    #[test]
    fn lhm_pairs_fan_and_control_by_parent_and_index() {
        let ticks = lhm::parse_burst(&canned_burst()).unwrap().unwrap();
        assert_eq!(ticks.len(), 5);
        let v = shape(lhm::fans_from_ticks(&ticks), &["lhm_wmi"], vec![]).into_value();
        assert_eq!(v["sources_tried"], json!(["lhm_wmi"]));
        assert_eq!(v["summary"], "3 fans read");

        let cpu = fan_by_key(&v, "/lpc/nct6798d/fan/1");
        assert_eq!(cpu["label"], "CPU Fan");
        assert_eq!(cpu["source"], "lhm");
        assert_eq!(cpu["rpm_samples"], json!([1180, 1176, 1182, 1179, 1181]));
        assert_eq!(cpu["duty_percent"], 45.0);
        assert_eq!(cpu["mode"], "unknown");
        assert_eq!(cpu["idle_or_absent"], false);

        // No control with index 2 on that chip: no duty, and idle at 0 RPM.
        let f2 = fan_by_key(&v, "/lpc/nct6798d/fan/2");
        assert_eq!(f2["duty_percent"], Value::Null);
        assert_eq!(f2["idle_or_absent"], true);

        // Same index on another chip does not leak across.
        assert_eq!(fan_by_key(&v, "/gpu-nvidia/0/fan/1")["duty_percent"], 33.3);
    }

    #[test]
    fn lhm_missing_namespace_is_an_empty_list() {
        assert_eq!(
            lhm::parse_burst(&json!({"ns": null, "ticks": []})).unwrap(),
            None
        );
        let v = shape(Vec::new(), &["lhm_wmi"], vec![]).into_value();
        assert_eq!(v["fans"], json!([]));
        assert_eq!(v["errors"], json!([]));
    }

    #[test]
    fn lhm_tolerates_powershell_single_object_collapsing() {
        let v = json!({"ns": "root/OpenHardwareMonitor", "ticks": {"rows":
            row("/lpc/x/fan/0", "F", "/lpc/x", "Fan", json!(900.0))}});
        let ticks = lhm::parse_burst(&v).unwrap().unwrap();
        let fans = lhm::fans_from_ticks(&ticks);
        assert_eq!(fans.len(), 1);
        assert_eq!(fans[0].samples, vec![Some(900)]);
    }

    #[test]
    fn lhm_null_values_are_failed_samples_and_parent_falls_back_to_identifier() {
        let tick = |v: Value| {
            json!({"rows": [
                {"identifier": "/lpc/x/fan/0", "name": "F", "parent": "", "type": "Fan", "value": v},
                {"identifier": "/lpc/x/control/0", "name": "F", "parent": "", "type": "Control", "value": 80.0},
            ]})
        };
        let v = json!({"ns": "root/LibreHardwareMonitor",
            "ticks": [tick(json!(900.0)), tick(Value::Null)]});
        let fans = lhm::fans_from_ticks(&lhm::parse_burst(&v).unwrap().unwrap());
        assert_eq!(fans[0].samples, vec![Some(900), None]);
        assert_eq!(fans[0].duty_percent, Some(80.0));
    }

    #[test]
    fn lhm_garbage_output_is_an_error() {
        assert!(lhm::parse_burst(&json!([1, 2])).is_err());
        assert!(lhm::parse_burst(&json!({"ns": "x"})).is_err());
    }

    // ---- the fixtures ----------------------------------------------------------

    fn fixture_fans(file: &str) -> Value {
        let path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../docs/fixtures")
            .join(file);
        let text = fs::read_to_string(&path).unwrap();
        let v: Value = serde_json::from_str(&text).unwrap();
        v["snapshot"]["fans"].clone()
    }

    fn mode_of(s: &str) -> Mode {
        [Mode::Pwm, Mode::Dc, Mode::Auto, Mode::Manual, Mode::Unknown]
            .into_iter()
            .find(|m| m.as_str() == s)
            .unwrap_or_else(|| panic!("fixture mode {s} is not in the contract enum"))
    }

    #[test]
    fn shaping_reproduces_the_linux_fixture() {
        let fixture = fixture_fans("telemetry_snapshot_linux.json");
        let raw: Vec<RawFan> = fixture["fans"]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| RawFan {
                key: f["key"].as_str().unwrap().to_string(),
                label: f["label"].as_str().map(str::to_string),
                source: "hwmon",
                samples: f["rpm_samples"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|s| Some(s.as_u64().unwrap() as u32))
                    .collect(),
                duty_percent: f["duty_percent"].as_f64(),
                mode: mode_of(f["mode"].as_str().unwrap()),
            })
            .collect();
        // The fixture is the section as pushed: status and summary included.
        assert_eq!(shape(raw, &["hwmon"], vec![]).into_value(), fixture);
    }

    #[test]
    fn empty_windows_section_matches_the_windows_fixture() {
        let fixture = fixture_fans("telemetry_snapshot.json");
        assert_eq!(
            shape(Vec::new(), &["lhm_wmi"], vec![]).into_value(),
            fixture
        );
    }

    #[test]
    fn collect_returns_a_contract_section() {
        // Reads the live host: only the shape is asserted. On Linux this takes a real
        // burst only when the host has fans.
        let v = super::collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["sample_interval_ms"], 1000);
        assert!(v["fans"].is_array());
        assert!(v["sources_tried"].is_array());
        assert!(v["errors"].is_array());
    }
}
