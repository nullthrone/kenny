//! `web_activity` section — host names a PC has been reaching in the last 24 h.
//!
//! Observed from two sources that cover each other's blind spots (ADR-0024):
//! the OS DNS client cache (cheap, all apps, but blind to DoH and short-lived) and
//! per-user browser history (Chromium `History`, Firefox `places.sqlite`; catches
//! DoH-resolved visits and carries real timestamps). The agent extracts **host names
//! only** — never full URLs, titles, or which user visited — dedups/merges into a
//! bounded list (cap 250, `truncated` flag), and always reports `status: "ok"`; it
//! holds no list and does not judge. The server accumulates the rolling window and
//! matches against the per-host list.
//!
//! The parsing/merging core in [`core`] is `#[cfg(windows)]`-free and unit-tested on
//! Linux CI against fabricated SQLite DBs. Only the OS probes (DNS cache, real profile
//! paths) are Windows-gated. Off Windows the section is the standard `n/a` stub.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Rolling observation window reported to the server.
const WINDOW_HOURS: i64 = 24;

/// Collect the `web_activity` section.
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
            json!({
                "window_hours": WINDOW_HOURS,
                "sources": [],
                "domains": [],
                "truncated": false,
                "browser_profiles_read": 0,
                "errors": [],
            }),
        )
    }
}

/// Portable parsing/merging core — compiled and tested on every platform.
///
/// Holds no OS-specific code: URL host extraction, noise filtering, epoch math, the
/// merge/dedup/cap, and the SQLite history readers (which work against any file path,
/// so they are exercised on Linux CI against temp DBs). Its only non-test consumer is
/// the Windows collector; in a non-test Linux `cargo build` those consumers are absent,
/// so allow dead code there (the readers stay live via the unit tests on every platform).
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use std::path::Path;

    use serde_json::{json, Value};

    /// Microseconds between 1601-01-01 (Chromium/Win32 FILETIME epoch) and the Unix
    /// epoch (1970-01-01). `11644473600` seconds.
    const EPOCH_1601_TO_1970_US: i64 = 11_644_473_600 * 1_000_000;

    /// Upper bound on domains reported in one snapshot (contract: cap 250, `last_seen`
    /// desc, `truncated` beyond).
    pub const MAX_DOMAINS: usize = 250;

    /// One observed host, merged across sources.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Observation {
        pub domain: String,
        /// RFC3339 UTC (`...Z`).
        pub first_seen: String,
        /// RFC3339 UTC (`...Z`).
        pub last_seen: String,
        pub hits: u64,
        /// Observation sources, e.g. `dns_cache`, `browser_history`.
        pub sources: Vec<String>,
    }

    impl Observation {
        /// Render into the wire object for the section's `domains` array.
        pub fn into_value(self) -> Value {
            json!({
                "domain": self.domain,
                "first_seen": self.first_seen,
                "last_seen": self.last_seen,
                "hits": self.hits,
                "sources": self.sources,
            })
        }
    }

    /// Extract the lowercase host from an `http(s)` URL, hand-rolled (no `url` crate).
    ///
    /// Strips scheme (`scheme://`), userinfo (`user:pass@`), port (`:443`), and any
    /// path/query/fragment. Returns `None` for non-`http(s)` schemes (`chrome://`,
    /// `about:`, `file:`, `data:`), for a missing/empty host, or for anything without a
    /// `scheme://` authority.
    pub fn host_from_url(u: &str) -> Option<String> {
        let (scheme, rest) = u.split_once("://")?;
        let scheme = scheme.to_ascii_lowercase();
        if scheme != "http" && scheme != "https" {
            return None;
        }
        // Authority ends at the first path/query/fragment delimiter.
        let authority = rest.split(['/', '?', '#']).next().unwrap_or(rest);
        // Drop userinfo (everything up to and including the last '@').
        let host_port = authority.rsplit('@').next().unwrap_or(authority);
        // Strip a trailing :port. IPv6 literals (bracketed) are unusual in browser
        // history hosts; take the substring before the first ':' which is correct for
        // the ordinary `host:port` case.
        let host = host_port.split(':').next().unwrap_or(host_port).trim();
        let host = host.trim_end_matches('.').to_ascii_lowercase();
        if host.is_empty() {
            None
        } else {
            Some(host)
        }
    }

    /// True when a domain is uninteresting reverse-DNS / link-local / OS-chatter noise
    /// that should never reach the server. Kept deliberately small and documented.
    pub fn is_noise(domain: &str) -> bool {
        let d = domain.trim_end_matches('.');
        // Reverse-DNS PTR lookups and link-local / single-label names carry no browsing
        // signal.
        if d.ends_with(".in-addr.arpa") || d.ends_with(".ip6.arpa") || d.ends_with(".local") {
            return true;
        }
        if !d.contains('.') {
            // Single-label (e.g. `wpad`, `localhost`, a NetBIOS name).
            return true;
        }
        // Background OS connectivity/update/telemetry chatter — not user browsing. Small
        // allowlist of substrings; extend consciously.
        const OS_CHATTER: &[&str] = &[
            "msftconnecttest.com",
            "msftncsi.com",
            "windowsupdate.com",
            "update.microsoft.com",
            "delivery.mp.microsoft.com",
            "settings-win.data.microsoft.com",
            "watson.telemetry.microsoft.com",
            "events.data.microsoft.com",
        ];
        OS_CHATTER
            .iter()
            .any(|s| d == *s || d.ends_with(&format!(".{s}")))
    }

    /// Convert Chromium `last_visit_time` (microseconds since 1601-01-01 UTC) to RFC3339.
    ///
    /// `last_visit_time` comes straight out of a browser's `History` SQLite file, which is
    /// untrusted (corrupted or adversarially crafted) input: it can be any `i64`, including
    /// values so far from a real timestamp that subtracting the epoch offset would overflow.
    /// A `checked_sub` failure falls back to the Unix epoch, matching
    /// [`unix_micros_to_rfc3339`]'s own out-of-range fallback below.
    pub fn chrome_epoch_to_rfc3339(micros_since_1601: i64) -> String {
        match micros_since_1601.checked_sub(EPOCH_1601_TO_1970_US) {
            Some(micros_since_1970) => unix_micros_to_rfc3339(micros_since_1970),
            None => unix_micros_to_rfc3339(0),
        }
    }

    /// Convert microseconds since the Unix epoch (Firefox `visit_date`) to RFC3339 UTC.
    pub fn unix_micros_to_rfc3339(micros_since_1970: i64) -> String {
        let secs = micros_since_1970.div_euclid(1_000_000);
        let nanos = (micros_since_1970.rem_euclid(1_000_000) as u32) * 1_000;
        chrono::DateTime::from_timestamp(secs, nanos)
            .unwrap_or_else(|| chrono::DateTime::from_timestamp(0, 0).expect("epoch is valid"))
            .to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
    }

    /// The Chromium `last_visit_time` threshold (micros since 1601) for a lookback of
    /// `hours` before `now_unix_secs`.
    pub fn chrome_since(now_unix_secs: i64, hours: i64) -> i64 {
        (now_unix_secs - hours * 3600) * 1_000_000 + EPOCH_1601_TO_1970_US
    }

    /// The Firefox `visit_date` threshold (micros since Unix epoch) for a lookback of
    /// `hours` before `now_unix_secs`.
    pub fn firefox_since(now_unix_secs: i64, hours: i64) -> i64 {
        (now_unix_secs - hours * 3600) * 1_000_000
    }

    /// Merge observations: dedup by domain (`first_seen` = min, `last_seen` = max, `hits`
    /// summed, `sources` unioned), sort by `last_seen` desc, and cap at [`MAX_DOMAINS`].
    /// Returns the capped list and whether truncation occurred.
    pub fn merge_observations(observations: Vec<Observation>) -> (Vec<Observation>, bool) {
        use std::collections::BTreeMap;

        let mut by_domain: BTreeMap<String, Observation> = BTreeMap::new();
        for obs in observations {
            match by_domain.get_mut(&obs.domain) {
                None => {
                    by_domain.insert(obs.domain.clone(), obs);
                }
                Some(existing) => {
                    if obs.first_seen < existing.first_seen {
                        existing.first_seen = obs.first_seen;
                    }
                    if obs.last_seen > existing.last_seen {
                        existing.last_seen = obs.last_seen;
                    }
                    existing.hits += obs.hits;
                    for s in obs.sources {
                        if !existing.sources.contains(&s) {
                            existing.sources.push(s);
                        }
                    }
                }
            }
        }

        let mut merged: Vec<Observation> = by_domain.into_values().collect();
        // Keep source ordering deterministic within each entry.
        for o in &mut merged {
            o.sources.sort();
        }
        // Newest first; break ties by domain for a stable, deterministic order (the
        // RFC3339 seconds format sorts lexicographically in chronological order).
        merged.sort_by(|a, b| {
            b.last_seen
                .cmp(&a.last_seen)
                .then_with(|| a.domain.cmp(&b.domain))
        });

        let truncated = merged.len() > MAX_DOMAINS;
        merged.truncate(MAX_DOMAINS);
        (merged, truncated)
    }

    /// Read a Chromium `History` SQLite DB (opened read-only) for visits newer than
    /// `since_micros_1601`. Maps each URL to its host; one `Observation` per row with
    /// source `browser_history`. Rows whose URL yields no host (or is noise) are dropped.
    pub fn read_chromium_history(
        db: &Path,
        since_micros_1601: i64,
    ) -> Result<Vec<Observation>, String> {
        let conn = open_readonly(db)?;
        let mut stmt = conn
            .prepare(
                "SELECT url, last_visit_time, visit_count \
                 FROM urls WHERE last_visit_time > ?1",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([since_micros_1601], |row| {
                let url: String = row.get(0)?;
                let last_visit: i64 = row.get(1)?;
                let visit_count: i64 = row.get(2)?;
                Ok((url, last_visit, visit_count))
            })
            .map_err(|e| e.to_string())?;

        let mut out = Vec::new();
        for row in rows {
            let (url, last_visit, visit_count) = row.map_err(|e| e.to_string())?;
            let Some(domain) = host_from_url(&url) else {
                continue;
            };
            if is_noise(&domain) {
                continue;
            }
            let ts = chrome_epoch_to_rfc3339(last_visit);
            out.push(Observation {
                domain,
                first_seen: ts.clone(),
                last_seen: ts,
                hits: visit_count.max(1) as u64,
                sources: vec!["browser_history".to_string()],
            });
        }
        Ok(out)
    }

    /// Read a Firefox `places.sqlite` DB (opened read-only) for visits newer than
    /// `since_micros_1970`, joining `moz_places` to `moz_historyvisits`. One
    /// `Observation` per visit row with source `browser_history`.
    pub fn read_firefox_places(
        db: &Path,
        since_micros_1970: i64,
    ) -> Result<Vec<Observation>, String> {
        let conn = open_readonly(db)?;
        let mut stmt = conn
            .prepare(
                "SELECT p.url, h.visit_date \
                 FROM moz_places p JOIN moz_historyvisits h ON h.place_id = p.id \
                 WHERE h.visit_date > ?1",
            )
            .map_err(|e| e.to_string())?;
        let rows = stmt
            .query_map([since_micros_1970], |row| {
                let url: String = row.get(0)?;
                let visit_date: i64 = row.get(1)?;
                Ok((url, visit_date))
            })
            .map_err(|e| e.to_string())?;

        let mut out = Vec::new();
        for row in rows {
            let (url, visit_date) = row.map_err(|e| e.to_string())?;
            let Some(domain) = host_from_url(&url) else {
                continue;
            };
            if is_noise(&domain) {
                continue;
            }
            let ts = unix_micros_to_rfc3339(visit_date);
            out.push(Observation {
                domain,
                first_seen: ts.clone(),
                last_seen: ts,
                hits: 1,
                sources: vec!["browser_history".to_string()],
            });
        }
        Ok(out)
    }

    /// Open a SQLite database read-only. Kept private; the readers use it so a locked or
    /// missing DB surfaces as an `Err` string the collector records in `errors` without
    /// failing the section.
    fn open_readonly(db: &Path) -> Result<rusqlite::Connection, String> {
        use rusqlite::OpenFlags;
        rusqlite::Connection::open_with_flags(
            db,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|e| e.to_string())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn host_from_url_extracts_host() {
            assert_eq!(
                host_from_url("https://www.example.com/path?q=1#frag").as_deref(),
                Some("www.example.com")
            );
            assert_eq!(
                host_from_url("http://Example.COM").as_deref(),
                Some("example.com")
            );
            assert_eq!(
                host_from_url("https://host.example:8443/x").as_deref(),
                Some("host.example")
            );
            assert_eq!(
                host_from_url("https://user:pass@secure.example.com/a").as_deref(),
                Some("secure.example.com")
            );
            assert_eq!(
                host_from_url("https://trailing.example.com./p").as_deref(),
                Some("trailing.example.com")
            );
            // Non-http(s) schemes and non-URLs yield no host.
            assert_eq!(host_from_url("chrome://settings"), None);
            assert_eq!(host_from_url("about:blank"), None);
            assert_eq!(host_from_url("file:///C:/x.txt"), None);
            assert_eq!(host_from_url("data:text/plain,hi"), None);
            assert_eq!(host_from_url("not a url"), None);
            assert_eq!(host_from_url("https://"), None);
        }

        #[test]
        fn is_noise_filters_reverse_dns_and_chatter() {
            assert!(is_noise("1.0.0.127.in-addr.arpa"));
            assert!(is_noise("a.b.ip6.arpa"));
            assert!(is_noise("printer.local"));
            assert!(is_noise("wpad"));
            assert!(is_noise("localhost"));
            assert!(is_noise("www.msftconnecttest.com"));
            assert!(is_noise("windowsupdate.com"));
            assert!(is_noise("fe2.update.microsoft.com"));
            // Real browsing hosts are kept.
            assert!(!is_noise("example.com"));
            assert!(!is_noise("cdn.example.net"));
            assert!(!is_noise("news.ycombinator.com"));
        }

        #[test]
        fn chrome_epoch_math() {
            // 13350000000000000 us since 1601 == 2023-05-12T14:40:00Z (a known value).
            // Verify round-trip against the Unix-epoch converter instead of a magic string.
            let unix_us = 1_600_000_000_000_000i64; // 2020-09-13T12:26:40Z
            let chrome_us = unix_us + super::EPOCH_1601_TO_1970_US;
            assert_eq!(
                chrome_epoch_to_rfc3339(chrome_us),
                unix_micros_to_rfc3339(unix_us)
            );
            assert_eq!(
                unix_micros_to_rfc3339(1_600_000_000_000_000),
                "2020-09-13T12:26:40Z"
            );
            // Unix epoch zero.
            assert_eq!(unix_micros_to_rfc3339(0), "1970-01-01T00:00:00Z");
        }

        #[test]
        fn merge_dedups_unions_and_sorts() {
            let obs = vec![
                Observation {
                    domain: "a.example".to_string(),
                    first_seen: "2026-06-04T10:00:00Z".to_string(),
                    last_seen: "2026-06-04T10:00:00Z".to_string(),
                    hits: 2,
                    sources: vec!["dns_cache".to_string()],
                },
                Observation {
                    domain: "a.example".to_string(),
                    first_seen: "2026-06-04T09:00:00Z".to_string(),
                    last_seen: "2026-06-04T12:00:00Z".to_string(),
                    hits: 3,
                    sources: vec!["browser_history".to_string()],
                },
                Observation {
                    domain: "b.example".to_string(),
                    first_seen: "2026-06-04T08:00:00Z".to_string(),
                    last_seen: "2026-06-04T08:00:00Z".to_string(),
                    hits: 1,
                    sources: vec!["dns_cache".to_string()],
                },
            ];
            let (merged, truncated) = merge_observations(obs);
            assert!(!truncated);
            assert_eq!(merged.len(), 2);
            // a.example is newest (last_seen 12:00) => first.
            assert_eq!(merged[0].domain, "a.example");
            assert_eq!(merged[0].first_seen, "2026-06-04T09:00:00Z");
            assert_eq!(merged[0].last_seen, "2026-06-04T12:00:00Z");
            assert_eq!(merged[0].hits, 5);
            assert_eq!(
                merged[0].sources,
                vec!["browser_history".to_string(), "dns_cache".to_string()]
            );
            assert_eq!(merged[1].domain, "b.example");
        }

        #[test]
        fn merge_caps_at_250_and_flags_truncated() {
            let mut obs = Vec::new();
            for i in 0..300 {
                obs.push(Observation {
                    domain: format!("d{i:04}.example"),
                    first_seen: "2026-06-04T00:00:00Z".to_string(),
                    // Distinct last_seen so ordering is well-defined.
                    last_seen: format!("2026-06-04T00:{:02}:{:02}Z", i / 60, i % 60),
                    hits: 1,
                    sources: vec!["dns_cache".to_string()],
                });
            }
            let (merged, truncated) = merge_observations(obs);
            assert!(truncated);
            assert_eq!(merged.len(), MAX_DOMAINS);
        }

        /// Build a throwaway Chromium `History` DB and assert the lookback filter +
        /// host extraction. Exercises the real SQLite reader on Linux CI.
        #[test]
        fn read_chromium_history_applies_lookback() {
            let dir = std::env::temp_dir().join(format!(
                "kenny-webact-chrome-{}-{}",
                std::process::id(),
                line!()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            let db = dir.join("History");
            {
                let conn = rusqlite::Connection::open(&db).unwrap();
                conn.execute_batch(
                    "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, \
                     last_visit_time INTEGER, visit_count INTEGER);",
                )
                .unwrap();
                let now_unix = 1_700_000_000i64;
                let recent = now_unix * 1_000_000 + EPOCH_1601_TO_1970_US;
                let old = (now_unix - 48 * 3600) * 1_000_000 + EPOCH_1601_TO_1970_US;
                conn.execute(
                    "INSERT INTO urls (url, last_visit_time, visit_count) VALUES (?1, ?2, ?3)",
                    rusqlite::params!["https://recent.example.com/a", recent, 4i64],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO urls (url, last_visit_time, visit_count) VALUES (?1, ?2, ?3)",
                    rusqlite::params!["https://old.example.com/b", old, 9i64],
                )
                .unwrap();
                // A chrome:// internal page must be dropped (no host).
                conn.execute(
                    "INSERT INTO urls (url, last_visit_time, visit_count) VALUES (?1, ?2, ?3)",
                    rusqlite::params!["chrome://settings", recent, 1i64],
                )
                .unwrap();
            }
            let since = chrome_since(1_700_000_000, 24);
            let obs = read_chromium_history(&db, since).unwrap();
            std::fs::remove_dir_all(&dir).ok();

            assert_eq!(obs.len(), 1, "only the recent, host-bearing row survives");
            assert_eq!(obs[0].domain, "recent.example.com");
            assert_eq!(obs[0].hits, 4);
            assert_eq!(obs[0].sources, vec!["browser_history".to_string()]);
        }

        /// Build a throwaway Firefox `places.sqlite` and assert the join + lookback.
        #[test]
        fn read_firefox_places_applies_lookback() {
            let dir = std::env::temp_dir().join(format!(
                "kenny-webact-ff-{}-{}",
                std::process::id(),
                line!()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            let db = dir.join("places.sqlite");
            {
                let conn = rusqlite::Connection::open(&db).unwrap();
                conn.execute_batch(
                    "CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT); \
                     CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, \
                     place_id INTEGER, visit_date INTEGER);",
                )
                .unwrap();
                let now_unix = 1_700_000_000i64;
                let recent = now_unix * 1_000_000;
                let old = (now_unix - 48 * 3600) * 1_000_000;
                conn.execute(
                    "INSERT INTO moz_places (id, url) VALUES (1, 'https://ff.example.org/x')",
                    [],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO moz_places (id, url) VALUES (2, 'https://oldff.example.org/y')",
                    [],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO moz_historyvisits (place_id, visit_date) VALUES (1, ?1)",
                    [recent],
                )
                .unwrap();
                conn.execute(
                    "INSERT INTO moz_historyvisits (place_id, visit_date) VALUES (2, ?1)",
                    [old],
                )
                .unwrap();
            }
            let since = firefox_since(1_700_000_000, 24);
            let obs = read_firefox_places(&db, since).unwrap();
            std::fs::remove_dir_all(&dir).ok();

            assert_eq!(obs.len(), 1);
            assert_eq!(obs[0].domain, "ff.example.org");
            assert_eq!(obs[0].sources, vec!["browser_history".to_string()]);
        }

        /// Regression test: a corrupted/adversarial `History` file can carry any `i64` in
        /// `last_visit_time`, not just plausible timestamps. A value far enough from the
        /// epoch previously overflowed the `i64` subtraction in `chrome_epoch_to_rfc3339`
        /// and panicked (`attempt to subtract with overflow`) instead of returning a
        /// degraded-but-valid result. See `chrome_epoch_to_rfc3339`.
        #[test]
        fn read_chromium_history_survives_extreme_last_visit_time() {
            let dir = std::env::temp_dir().join(format!(
                "kenny-webact-overflow-repro-{}-{}",
                std::process::id(),
                line!()
            ));
            std::fs::create_dir_all(&dir).unwrap();
            let db = dir.join("History");
            {
                let conn = rusqlite::Connection::open(&db).unwrap();
                conn.execute_batch(
                    "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, \
                     last_visit_time INTEGER, visit_count INTEGER);",
                )
                .unwrap();
                for extreme in [i64::MIN, i64::MIN + 1, i64::MAX] {
                    conn.execute(
                        "INSERT INTO urls (url, last_visit_time, visit_count) VALUES (?1, ?2, ?3)",
                        rusqlite::params![
                            format!("https://evil-{extreme}.example.com/a"),
                            extreme,
                            1i64
                        ],
                    )
                    .unwrap();
                }
            }
            // since = i64::MIN so every row (including last_visit_time == i64::MIN) is
            // in-window; must not panic, and every row must still parse to an Observation.
            let obs = read_chromium_history(&db, i64::MIN).unwrap();
            std::fs::remove_dir_all(&dir).ok();
            assert_eq!(obs.len(), 2, "only last_visit_time > since survives");
        }

        #[test]
        fn chrome_epoch_to_rfc3339_does_not_panic_on_extreme_input() {
            // Falls back to the Unix epoch rather than overflowing.
            assert_eq!(chrome_epoch_to_rfc3339(i64::MIN), "1970-01-01T00:00:00Z");
            // A merely-large-but-in-range value still converts normally (no fallback).
            assert_eq!(
                chrome_epoch_to_rfc3339(EPOCH_1601_TO_1970_US),
                "1970-01-01T00:00:00Z"
            );
        }

        #[test]
        fn read_missing_db_is_err_not_panic() {
            let missing = std::env::temp_dir().join("kenny-webact-does-not-exist.sqlite");
            assert!(read_chromium_history(&missing, 0).is_err());
            assert!(read_firefox_places(&missing, 0).is_err());
        }

        /// Randomized/corrupted/truncated raw bytes written straight to disk as a would-be
        /// `History`/`places.sqlite` file (not a well-formed DB at all — the untrusted case
        /// of a hand-corrupted or adversarial browser history file) must only ever surface
        /// as an `Err`, never panic, when read through the real readers.
        #[test]
        fn readers_never_panic_on_random_corrupted_files() {
            struct Rng(u64);
            impl Rng {
                fn next(&mut self) -> u64 {
                    self.0 = self.0.wrapping_add(0x9E3779B97F4A7C15);
                    let mut z = self.0;
                    z = (z ^ (z >> 30)).wrapping_mul(0xBF58476D1CE4E5B9);
                    z = (z ^ (z >> 27)).wrapping_mul(0x94D049BB133111EB);
                    z ^ (z >> 31)
                }
            }
            let mut rng = Rng(0x5CA1AB1E_5CA1AB1E);
            let dir = std::env::temp_dir().join(format!(
                "kenny-webact-corrupt-fuzz-{}-{}",
                std::process::id(),
                line!()
            ));
            std::fs::create_dir_all(&dir).unwrap();

            // A real (empty-schema) DB, whose bytes are then randomly truncated/mutated —
            // this is far more likely to reach interesting SQLite parsing states than pure
            // random bytes, which mostly bail out at the "not a database" header check.
            let seed_db = dir.join("seed.sqlite");
            {
                let conn = rusqlite::Connection::open(&seed_db).unwrap();
                conn.execute_batch(
                    "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, \
                     last_visit_time INTEGER, visit_count INTEGER); \
                     INSERT INTO urls (url, last_visit_time, visit_count) \
                     VALUES ('https://example.com/', 13300000000000000, 1); \
                     CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT); \
                     CREATE TABLE moz_historyvisits (id INTEGER PRIMARY KEY, \
                     place_id INTEGER, visit_date INTEGER); \
                     INSERT INTO moz_places (id, url) VALUES (1, 'https://example.org/'); \
                     INSERT INTO moz_historyvisits (place_id, visit_date) VALUES (1, 1700000000000000);",
                )
                .unwrap();
            }
            let seed_bytes = std::fs::read(&seed_db).unwrap();

            for iter in 0..1500u32 {
                let mut bytes = seed_bytes.clone();
                let choice = rng.next() % 4;
                match choice {
                    0 => {
                        // Truncate to a random length.
                        let cut = (rng.next() as usize) % (bytes.len() + 1);
                        bytes.truncate(cut);
                    }
                    1 => {
                        // Fully random bytes, random length.
                        let len = (rng.next() % 4096) as usize;
                        bytes = (0..len).map(|_| rng.next() as u8).collect();
                    }
                    2 => {
                        // Flip a handful of random bytes in the real DB (bit rot / adversarial
                        // tampering of an otherwise well-formed file).
                        let flips = 1 + (rng.next() % 20) as usize;
                        for _ in 0..flips {
                            if bytes.is_empty() {
                                break;
                            }
                            let idx = (rng.next() as usize) % bytes.len();
                            bytes[idx] = rng.next() as u8;
                        }
                    }
                    _ => {
                        // Empty file.
                        bytes.clear();
                    }
                }
                let path = dir.join(format!("corrupt-{iter}.sqlite"));
                std::fs::write(&path, &bytes).unwrap();

                let since = (rng.next() as i64).wrapping_sub(i64::MAX / 2);
                let r1 = std::panic::catch_unwind(|| read_chromium_history(&path, since));
                let r2 = std::panic::catch_unwind(|| read_firefox_places(&path, since));
                let _ = std::fs::remove_file(&path);

                if r1.is_err() {
                    panic!("iter {iter}: read_chromium_history panicked");
                }
                if r2.is_err() {
                    panic!("iter {iter}: read_firefox_places panicked");
                }
            }
            std::fs::remove_dir_all(&dir).ok();
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use crate::telemetry::collectors::web_activity::core::{
        self, chrome_since, firefox_since, merge_observations, read_chromium_history,
        read_firefox_places, Observation,
    };
    use crate::telemetry::collectors::winps;
    use std::path::{Path, PathBuf};

    pub fn collect() -> Section {
        let now = chrono::Utc::now();
        let now_unix = now.timestamp();
        let now_rfc = now.to_rfc3339_opts(chrono::SecondsFormat::Secs, true);

        let mut observations: Vec<Observation> = Vec::new();
        let mut errors: Vec<String> = Vec::new();

        // (a) DNS client cache — one hit per unique A/AAAA entry, timestamped "now"
        // (the cache carries no first-seen). Blind to DoH, hence source (b).
        observations.extend(collect_dns_cache(&now_rfc));

        // (b) Browser history across every user profile.
        let mut browser_profiles_read = 0u64;
        for db in enumerate_browser_dbs() {
            match read_profile(&db, now_unix) {
                Ok(mut obs) => {
                    browser_profiles_read += 1;
                    observations.append(&mut obs);
                }
                Err(e) => errors.push(format!("{}: {e}", db.path.display())),
            }
        }

        let (domains, truncated) = merge_observations(observations);
        let n = domains.len();
        let domain_values: Vec<_> = domains.into_iter().map(Observation::into_value).collect();

        Section::with_fields(
            Status::Ok,
            format!("{n} domains observed (24h)"),
            json!({
                "window_hours": WINDOW_HOURS,
                "sources": ["dns_cache", "browser_history"],
                "domains": domain_values,
                "truncated": truncated,
                "browser_profiles_read": browser_profiles_read,
                "errors": errors,
            }),
        )
    }

    /// Read the DNS client cache A/AAAA entries into observations (source `dns_cache`).
    fn collect_dns_cache(now_rfc: &str) -> Vec<Observation> {
        let script = "Get-DnsClientCache -Type A,AAAA -ErrorAction SilentlyContinue | \
             Select-Object -ExpandProperty Entry -Unique | ConvertTo-Json -Compress";
        let Some(v) = winps::run_json(script) else {
            return Vec::new();
        };
        winps::as_array(v)
            .into_iter()
            .filter_map(|entry| entry.as_str().map(str::to_string))
            .map(|e| e.trim_end_matches('.').to_ascii_lowercase())
            .filter(|d| !core::is_noise(d))
            .map(|domain| Observation {
                domain,
                first_seen: now_rfc.to_string(),
                last_seen: now_rfc.to_string(),
                hits: 1,
                sources: vec!["dns_cache".to_string()],
            })
            .collect()
    }

    /// A browser history DB on disk and the engine that reads it.
    struct BrowserDb {
        path: PathBuf,
        engine: Engine,
    }

    enum Engine {
        Chromium,
        Firefox,
    }

    /// Enumerate every user's Chromium (Chrome/Edge) `History` and Firefox
    /// `places.sqlite` across all profiles under `C:\Users\*`.
    fn enumerate_browser_dbs() -> Vec<BrowserDb> {
        let mut dbs = Vec::new();
        let users = Path::new(r"C:\Users");
        let Ok(entries) = std::fs::read_dir(users) else {
            return dbs;
        };
        for user in entries.flatten() {
            let home = user.path();
            if !home.is_dir() {
                continue;
            }
            let local = home.join(r"AppData\Local");
            let roaming = home.join(r"AppData\Roaming");
            // Chromium browsers: <User Data>\<Profile>\History.
            for chromium_root in [
                local.join(r"Google\Chrome\User Data"),
                local.join(r"Microsoft\Edge\User Data"),
            ] {
                for profile in profile_dirs(&chromium_root) {
                    let db = profile.join("History");
                    if db.is_file() {
                        dbs.push(BrowserDb {
                            path: db,
                            engine: Engine::Chromium,
                        });
                    }
                }
            }
            // Firefox: Profiles\<profile>\places.sqlite.
            let ff_root = roaming.join(r"Mozilla\Firefox\Profiles");
            if let Ok(profiles) = std::fs::read_dir(&ff_root) {
                for profile in profiles.flatten() {
                    let db = profile.path().join("places.sqlite");
                    if db.is_file() {
                        dbs.push(BrowserDb {
                            path: db,
                            engine: Engine::Firefox,
                        });
                    }
                }
            }
        }
        dbs
    }

    /// Immediate subdirectories of `root` (the Chromium per-profile folders such as
    /// `Default`, `Profile 1`). Returns empty if `root` does not exist.
    fn profile_dirs(root: &Path) -> Vec<PathBuf> {
        let mut out = Vec::new();
        if let Ok(entries) = std::fs::read_dir(root) {
            for e in entries.flatten() {
                let p = e.path();
                if p.is_dir() {
                    out.push(p);
                }
            }
        }
        out
    }

    /// Copy a browser DB (plus any `-wal`/`-shm` siblings) to a private temp file and
    /// read it read-only with the 24 h lookback. Copying dodges SQLite locks held by a
    /// running browser. The temp copies are removed before returning.
    fn read_profile(db: &BrowserDb, now_unix: i64) -> Result<Vec<Observation>, String> {
        let temp = copy_to_temp(&db.path)?;
        let result = match db.engine {
            Engine::Chromium => {
                read_chromium_history(&temp.main, chrome_since(now_unix, WINDOW_HOURS))
            }
            Engine::Firefox => {
                read_firefox_places(&temp.main, firefox_since(now_unix, WINDOW_HOURS))
            }
        };
        temp.cleanup();
        result
    }

    /// A temp copy of a SQLite DB and its WAL/SHM siblings, cleaned up on drop-equivalent.
    struct TempDb {
        main: PathBuf,
        siblings: Vec<PathBuf>,
    }

    impl TempDb {
        fn cleanup(self) {
            let _ = std::fs::remove_file(&self.main);
            for s in &self.siblings {
                let _ = std::fs::remove_file(s);
            }
        }
    }

    fn copy_to_temp(src: &Path) -> Result<TempDb, String> {
        let file_name = src
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or_else(|| "invalid db file name".to_string())?;
        let unique = format!(
            "kenny-webact-{}-{}",
            std::process::id(),
            uuid::Uuid::new_v4()
        );
        let dir = std::env::temp_dir().join(unique);
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;

        let main = dir.join(file_name);
        std::fs::copy(src, &main).map_err(|e| e.to_string())?;

        // Copy -wal/-shm siblings if present so a WAL-mode DB reads consistently.
        let mut siblings = Vec::new();
        for suffix in ["-wal", "-shm"] {
            let sib_src = with_suffix(src, suffix);
            if sib_src.is_file() {
                let sib_dst = dir.join(format!("{file_name}{suffix}"));
                if std::fs::copy(&sib_src, &sib_dst).is_ok() {
                    siblings.push(sib_dst);
                }
            }
        }
        Ok(TempDb { main, siblings })
    }

    fn with_suffix(path: &Path, suffix: &str) -> PathBuf {
        let mut s = path.as_os_str().to_owned();
        s.push(suffix);
        PathBuf::from(s)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn section_shape_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert_eq!(v["window_hours"], WINDOW_HOURS);
        assert!(v["sources"].is_array());
        assert!(v["domains"].is_array());
        assert!(v["truncated"].is_boolean());
        assert!(v["browser_profiles_read"].is_number());
        assert!(v["errors"].is_array());
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["sources"].as_array().unwrap().len(), 0);
        assert_eq!(v["domains"].as_array().unwrap().len(), 0);
        assert_eq!(v["browser_profiles_read"], 0);
    }
}
