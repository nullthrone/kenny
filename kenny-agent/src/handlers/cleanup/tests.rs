//! Cleanup tests. Every walk runs against roots injected into fresh temp trees with the
//! platform's real primitives; no test ever resolves a real catalog root.

use std::cell::Cell;
use std::sync::atomic::{AtomicU32, Ordering};

use super::*;

/// A clock that reports a fixed "now" and advances `step` per elapsed-time read.
struct TestClock {
    now: SystemTime,
    step: Duration,
    calls: Cell<u32>,
}

impl TestClock {
    /// A clock `ahead` days in the future, so files created now are that old.
    fn ahead(ahead: u32) -> Self {
        Self {
            now: SystemTime::now() + days(ahead),
            step: Duration::ZERO,
            calls: Cell::new(0),
        }
    }
}

impl Clock for TestClock {
    fn now(&self) -> SystemTime {
        self.now
    }
    fn elapsed(&self) -> Duration {
        let c = self.calls.get();
        self.calls.set(c + 1);
        self.step * c
    }
}

/// A unique temp directory, removed on drop (`remove_dir_all` never follows links).
struct Tree(PathBuf);

impl Tree {
    fn new(tag: &str) -> Self {
        static N: AtomicU32 = AtomicU32::new(0);
        let p = std::env::temp_dir().join(format!(
            "kenny-cleanup-test-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::Relaxed)
        ));
        let _ = std::fs::remove_dir_all(&p);
        std::fs::create_dir_all(&p).unwrap();
        Tree(std::fs::canonicalize(&p).unwrap())
    }

    fn path(&self, rel: &str) -> PathBuf {
        self.0.join(rel)
    }

    fn file(&self, rel: &str, len: usize) -> PathBuf {
        let p = self.path(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(&p, vec![b'x'; len]).unwrap();
        p
    }

    #[cfg_attr(not(target_os = "linux"), allow(dead_code))]
    fn dir(&self, rel: &str) -> PathBuf {
        let p = self.path(rel);
        std::fs::create_dir_all(&p).unwrap();
        p
    }
}

impl Drop for Tree {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

/// Make `p` look written `ahead` days from now (younger than a clock `< ahead` days out).
fn touch_future(p: &Path, ahead: u32) {
    let f = std::fs::File::options().write(true).open(p).unwrap();
    f.set_modified(SystemTime::now() + days(ahead)).unwrap();
}

fn unlimited() -> Budgets {
    Budgets {
        time: Duration::from_secs(600),
        max_examined: MAX_EXAMINED,
        max_deletions: MAX_DELETIONS,
    }
}

fn exists(p: &Path) -> bool {
    std::fs::symlink_metadata(p).is_ok()
}

fn sweep<'c>(
    root: &Path,
    rules: &Rules,
    dry_run: bool,
    clock: &'c dyn Clock,
    budgets: Budgets,
) -> Session<'c> {
    sweep_with(root, rules, dry_run, clock, budgets, &[])
}

fn sweep_with<'c>(
    root: &Path,
    rules: &Rules,
    dry_run: bool,
    clock: &'c dyn Clock,
    budgets: Budgets,
    excluded: &[PathBuf],
) -> Session<'c> {
    let mut s = Session::new(dry_run, clock, budgets, excluded);
    sweep_root(&platform_ops(), &mut s, root, &[], rules);
    s
}

// ---------------------------------------------------------------------------------
// Catalog and argument handling
// ---------------------------------------------------------------------------------

/// The agent's catalog is exactly the contract vector: names, order and thresholds.
#[test]
fn catalog_matches_the_contract_vector() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../docs/fixtures/vectors/fs_cleanup_targets.json");
    let raw = std::fs::read_to_string(&path).expect("read fs_cleanup_targets.json");
    let doc: Value = serde_json::from_str(&raw).expect("vector must parse");
    let from_vector = |os: &str| -> Vec<(String, u32)> {
        doc[os]
            .as_array()
            .unwrap_or_else(|| panic!("{os} must be an array"))
            .iter()
            .map(|e| {
                (
                    e["target"].as_str().unwrap().to_string(),
                    e["min_age_days"].as_u64().unwrap() as u32,
                )
            })
            .collect()
    };
    let from_agent = |cat: &[CatalogEntry]| -> Vec<(String, u32)> {
        cat.iter()
            .map(|e| (e.target.to_string(), e.min_age_days))
            .collect()
    };
    assert_eq!(from_agent(WINDOWS_CATALOG), from_vector("windows"));
    assert_eq!(from_agent(LINUX_CATALOG), from_vector("linux"));
    let keys: Vec<&String> = doc.as_object().unwrap().keys().collect();
    assert_eq!(
        keys.len(),
        2,
        "the vector names exactly windows and linux: {keys:?}"
    );
}

#[test]
fn lookup_maps_targets_to_bad_args_and_unsupported() {
    let t = |s: &str| json!({ "target": s });
    assert_eq!(
        lookup(CatalogOs::Linux, &t("tmp")).unwrap().min_age_days,
        10
    );
    assert_eq!(
        lookup(CatalogOs::Windows, &t("recycle_bin"))
            .unwrap()
            .min_age_days,
        30
    );
    // `logs_old` exists on both, with each OS's own entry.
    assert_eq!(
        lookup(CatalogOs::Linux, &t("logs_old")).unwrap().target,
        "logs_old"
    );
    assert_eq!(
        lookup(CatalogOs::Windows, &t("logs_old")).unwrap().target,
        "logs_old"
    );
    // Another OS's target is unsupported.
    assert_eq!(
        lookup(CatalogOs::Linux, &t("temp_system")).unwrap_err().0,
        ErrorCode::Unsupported
    );
    assert_eq!(
        lookup(CatalogOs::Windows, &t("journal")).unwrap_err().0,
        ErrorCode::Unsupported
    );
    // Unknown, missing or non-string targets are bad_args.
    for args in [
        t("nope"),
        t(""),
        t("TMP"),
        t("../tmp"),
        json!({}),
        json!({"target": 1}),
        json!("tmp"),
        json!(null),
    ] {
        assert_eq!(
            lookup(CatalogOs::Linux, &args).unwrap_err().0,
            ErrorCode::BadArgs,
            "{args}"
        );
        assert_eq!(
            lookup(CatalogOs::Windows, &args).unwrap_err().0,
            ErrorCode::BadArgs,
            "{args}"
        );
    }
    // macOS and friends: everything is unsupported.
    for args in [t("tmp"), t("temp_system"), t("nope"), json!({})] {
        assert_eq!(
            lookup(CatalogOs::Other, &args).unwrap_err().0,
            ErrorCode::Unsupported
        );
    }
}

#[tokio::test]
async fn handlers_reject_unknown_targets_before_any_work() {
    let err = scan(json!({"target": "nope"})).await.unwrap_err();
    let err2 = cleanup(json!({"target": "nope"})).await.unwrap_err();
    if CatalogOs::native() == CatalogOs::Other {
        assert_eq!(err.0, ErrorCode::Unsupported);
        assert_eq!(err2.0, ErrorCode::Unsupported);
    } else {
        assert_eq!(err.0, ErrorCode::BadArgs);
        assert_eq!(err2.0, ErrorCode::BadArgs);
    }
}

/// A busy host is `exec_failed`. Uses the read-only scan so a broken lock could at
/// worst read a real root, never delete from it.
#[cfg(any(windows, target_os = "linux"))]
#[allow(clippy::await_holding_lock)] // the held lock is the point: the handler must see a busy host
#[tokio::test]
async fn busy_host_is_exec_failed() {
    let _held = hold_host_lock_for_test();
    let err = scan(json!({"target": "logs_old"})).await.unwrap_err();
    assert_eq!(err.0, ErrorCode::ExecFailed);
    assert_eq!(err.1, BUSY_MESSAGE);
}

#[test]
fn agent_log_dir_and_state_files_are_excluded() {
    let ex = agent_excluded_paths();
    assert!(ex.contains(&crate::log_dir().unwrap()));
    assert!(ex.contains(&crate::control::control_path()));
    assert!(ex.contains(&crate::keys::key_path()));
}

// ---------------------------------------------------------------------------------
// Rules
// ---------------------------------------------------------------------------------

#[test]
fn rotated_log_names() {
    for yes in [
        "syslog.1",
        "x.gz",
        "a.log.2.gz",
        "b.xz",
        "c.bz2",
        "d.zst",
        "e.old",
        "kern.log.9",
    ] {
        assert!(is_rotated_log(yes), "{yes}");
    }
    for no in [
        "syslog", "x.log", "a.10", ".gz", "x.gzip", "lastlog", "wtmp", "a.1x",
    ] {
        assert!(!is_rotated_log(no), "{no}");
    }
    let cbs = windows_cbs_rules(30).want_file;
    assert!(cbs("CbsPersist_20240101.cab"));
    assert!(cbs("CbsPersist_20240101.log"));
    assert!(cbs("x.CAB"));
    assert!(!cbs("CBS.log"));
}

#[test]
fn tmp_skips_systemd_and_x11_entries() {
    let skip = tmp_rules(10).skip_entry;
    assert!(skip("systemd-private-abc-chronyd.service-x", 0, true));
    assert!(skip(".X11-unix", 0, true));
    assert!(skip(".ICE-unix", 0, true));
    assert!(!skip("foo", 0, true));
}

#[test]
fn linux_logs_stops_at_depth_three_and_never_enters_journal() {
    let t = Tree::new("logs");
    t.file("a.1", 1);
    t.file("keep.log", 1);
    t.file("d1/b.gz", 1);
    t.file("d1/d2/c.xz", 1);
    t.file("d1/d2/d3/deep.gz", 1);
    t.file("journal/x.gz", 1);
    let clock = TestClock::ahead(60);
    let s = sweep(&t.0, &linux_logs_rules(30), false, &clock, unlimited());
    assert_eq!(s.files, 3);
    assert!(!exists(&t.path("a.1")));
    assert!(!exists(&t.path("d1/b.gz")));
    assert!(!exists(&t.path("d1/d2/c.xz")));
    assert!(
        exists(&t.path("d1/d2/d3/deep.gz")),
        "depth 4 is out of reach"
    );
    assert!(exists(&t.path("journal/x.gz")), "journal/ is never entered");
    assert!(exists(&t.path("keep.log")));
}

// ---------------------------------------------------------------------------------
// Walker
// ---------------------------------------------------------------------------------

#[test]
fn age_filter_removes_old_keeps_young_and_never_the_root() {
    let t = Tree::new("age");
    t.file("old.txt", 10);
    t.file("sub/old2.txt", 20);
    t.file("sub/deeper/old3.txt", 30);
    let young = t.file("young.txt", 5);
    touch_future(&young, 29);
    let young2 = t.file("keep/young2.txt", 5);
    touch_future(&young2, 29);
    let rules = windows_tree_rules(7);

    // Everything is brand new against the real clock: nothing goes.
    let now = RealClock::start();
    let s = sweep(&t.0, &rules, false, &now, unlimited());
    assert_eq!((s.files, s.bytes), (0, 0));
    assert!(exists(&t.path("old.txt")));

    let clock = TestClock::ahead(30);
    let s = sweep(&t.0, &rules, false, &clock, unlimited());
    assert_eq!((s.files, s.bytes), (3, 60));
    assert!(s.complete());
    assert!(!exists(&t.path("old.txt")));
    assert!(
        !exists(&t.path("sub")),
        "an emptied, old subdirectory is removed"
    );
    assert!(exists(&young));
    assert!(exists(&young2), "a directory holding a young file stays");
    assert!(t.0.is_dir(), "the root is never deleted");
}

#[test]
fn scan_reports_exactly_what_cleanup_removes() {
    let t = Tree::new("parity");
    for i in 0..7 {
        t.file(&format!("a/{i}.bin"), 100 + i);
    }
    t.file("b/c/d.bin", 50);
    let young = t.file("b/young.bin", 9);
    touch_future(&young, 29);
    t.file("top.bin", 3);
    let rules = windows_tree_rules(7);
    let clock = TestClock::ahead(30);

    let scanned = sweep(&t.0, &rules, true, &clock, unlimited());
    assert!(
        exists(&t.path("a/0.bin")) && exists(&t.path("b/c/d.bin")),
        "a scan deletes nothing"
    );
    let cleaned = sweep(&t.0, &rules, false, &clock, unlimited());

    assert_eq!(scanned.files, 9);
    assert_eq!(scanned.files, cleaned.files);
    assert_eq!(scanned.bytes, cleaned.bytes);
    assert_eq!(scanned.deletions, cleaned.deletions);
    let entry = lookup(CatalogOs::Linux, &json!({"target": "tmp"})).unwrap();
    assert_eq!(
        scan_result(&entry, &scanned)["files"],
        cleanup_result(&entry, &cleaned)["files_removed"]
    );
    assert_eq!(
        scan_result(&entry, &scanned)["bytes"],
        cleanup_result(&entry, &cleaned)["bytes_freed"]
    );
    assert!(!exists(&t.path("a")));
    assert!(exists(&young));
}

#[test]
fn count_budget_stops_and_a_second_call_continues() {
    let t = Tree::new("count");
    for i in 0..10 {
        t.file(&format!("{i}.txt"), 1);
    }
    let clock = TestClock::ahead(30);
    let rules = windows_tree_rules(7);
    let tight = Budgets {
        max_deletions: 3,
        ..unlimited()
    };
    let s = sweep(&t.0, &rules, false, &clock, tight);
    assert_eq!(s.files, 3);
    assert_eq!(s.stopped, Some(Stop::Count));
    let entry = lookup(CatalogOs::Linux, &json!({"target": "tmp"})).unwrap();
    let r = cleanup_result(&entry, &s);
    assert_eq!(r["complete"], false);
    assert_eq!(r["stopped"], "count");
    let r = scan_result(&entry, &s);
    assert_eq!(r["complete"], false);
    assert!(r.get("stopped").is_none(), "a scan never carries `stopped`");

    let s = sweep(&t.0, &rules, false, &clock, tight);
    assert_eq!(s.files, 3, "calling again continues the work");
    let left = std::fs::read_dir(&t.0).unwrap().count();
    assert_eq!(left, 4);

    let examine = Budgets {
        max_examined: 2,
        ..unlimited()
    };
    let s = sweep(&t.0, &rules, false, &clock, examine);
    assert_eq!(s.stopped, Some(Stop::Count));
    assert!(s.files <= 2);
}

#[test]
fn time_budget_stops_with_time() {
    let t = Tree::new("time");
    for i in 0..10 {
        t.file(&format!("{i}.txt"), 1);
    }
    let clock = TestClock {
        step: Duration::from_secs(1),
        ..TestClock::ahead(30)
    };
    let budget = Budgets {
        time: Duration::from_secs(4),
        ..unlimited()
    };
    let s = sweep(&t.0, &windows_tree_rules(7), false, &clock, budget);
    assert_eq!(s.stopped, Some(Stop::Time));
    assert!(s.files < 10 && s.files > 0, "{}", s.files);
    let entry = lookup(CatalogOs::Linux, &json!({"target": "tmp"})).unwrap();
    let r = cleanup_result(&entry, &s);
    assert_eq!(r["complete"], false);
    assert_eq!(r["stopped"], "time");
}

#[test]
fn complete_result_has_no_stopped_and_the_contract_keys() {
    let t = Tree::new("shape");
    let clock = TestClock::ahead(1);
    let s = sweep(&t.0, &windows_tree_rules(7), false, &clock, unlimited());
    let entry = lookup(CatalogOs::Windows, &json!({"target": "temp_system"})).unwrap();
    let r = cleanup_result(&entry, &s);
    let mut keys: Vec<&str> = r.as_object().unwrap().keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "bytes_freed",
            "complete",
            "errors",
            "files_removed",
            "skipped",
            "target"
        ]
    );
    let r = scan_result(&entry, &s);
    let mut keys: Vec<&str> = r.as_object().unwrap().keys().map(String::as_str).collect();
    keys.sort_unstable();
    assert_eq!(
        keys,
        [
            "bytes",
            "complete",
            "errors",
            "files",
            "min_age_days",
            "skipped",
            "target"
        ]
    );
    assert_eq!(r["min_age_days"], 7);
    let mut sk: Vec<&str> = r["skipped"]
        .as_object()
        .unwrap()
        .keys()
        .map(String::as_str)
        .collect();
    sk.sort_unstable();
    assert_eq!(sk, ["denied", "in_use", "other", "reparse"]);
}

#[test]
fn errors_are_deduplicated_and_capped() {
    let clock = TestClock::ahead(0);
    let mut s = Session::new(false, &clock, unlimited(), &[]);
    for i in 0..30 {
        s.error("x", format!("m{i}"));
        s.error("x", format!("m{i}"));
    }
    assert_eq!(s.errors.len(), MAX_ERRORS);
}

#[test]
fn agent_paths_are_excluded_by_candidate_check() {
    let t = Tree::new("exclude");
    let log = t.file("kenny/logs/kenny-agent.log.2026-01-01", 10);
    let state = t.file("kenny-agent.control.json", 10);
    let other = t.file("other.txt", 10);
    let clock = TestClock::ahead(60);
    let excluded = [t.path("kenny/logs"), state.clone()];
    let s = sweep_with(
        &t.0,
        &windows_tree_rules(7),
        false,
        &clock,
        unlimited(),
        &excluded,
    );
    assert_eq!(s.files, 1);
    assert!(exists(&log));
    assert!(exists(&state));
    assert!(!exists(&other));
    assert!(
        exists(&t.path("kenny")),
        "a directory holding the log dir stays"
    );

    // An excluded root is not walked at all.
    let s = sweep_with(
        &t.path("kenny"),
        &windows_tree_rules(7),
        false,
        &clock,
        unlimited(),
        &[t.path("kenny")],
    );
    assert_eq!(s.files, 0);
    assert!(exists(&log));
}

#[test]
fn path_keys_compare_on_component_boundaries() {
    let sep = if cfg!(windows) { "\\" } else { "/" };
    let base = format!("{sep}a{sep}b");
    assert!(key_under(&base, &base));
    assert!(key_under(&format!("{base}{sep}c"), &base));
    assert!(!key_under(&format!("{base}c"), &base));
    assert!(!key_under(&format!("{sep}a"), &base));
}

#[test]
fn entry_names_that_carry_a_path_are_refused() {
    for bad in ["", ".", "..", "a/b"] {
        assert!(!is_plain_name(OsStr::new(bad)), "{bad:?}");
    }
    assert!(is_plain_name(OsStr::new("$IABC.txt")));
}

// ---------------------------------------------------------------------------------
// Recycle bin (core logic, run on a fabricated bin)
// ---------------------------------------------------------------------------------

#[test]
fn recycle_bin_removes_old_records_with_their_payload() {
    let t = Tree::new("bin");
    let sid = "S-1-5-21-1-2-3-1001";
    t.file(&format!("{sid}/$IAAA.txt"), 10);
    t.file(&format!("{sid}/$RAAA.txt"), 100);
    t.file(&format!("{sid}/$IBBB"), 10);
    t.file(&format!("{sid}/$RBBB/x/y.txt"), 20);
    t.file(&format!("{sid}/$RBBB/z.txt"), 30);
    let young_i = t.file(&format!("{sid}/$ICCC.txt"), 10);
    touch_future(&young_i, 35);
    let young_r = t.file(&format!("{sid}/$RCCC.txt"), 40);
    t.file(&format!("{sid}/$IDDD.txt"), 10); // orphan record
    let ini = t.file(&format!("{sid}/desktop.ini"), 1);
    let clock = TestClock::ahead(40);

    let mut scan = Session::new(true, &clock, unlimited(), &[]);
    sweep_recycle_bin(&platform_ops(), &mut scan, &t.0, days(30));
    assert!(
        exists(&t.path(&format!("{sid}/$RAAA.txt"))),
        "a scan deletes nothing"
    );

    let mut s = Session::new(false, &clock, unlimited(), &[]);
    sweep_recycle_bin(&platform_ops(), &mut s, &t.0, days(30));
    assert_eq!(s.files, 6, "$IAAA $RAAA y z $IBBB $IDDD");
    assert_eq!(s.bytes, 10 + 100 + 20 + 30 + 10 + 10);
    assert_eq!((scan.files, scan.bytes), (s.files, s.bytes));
    for gone in ["$IAAA.txt", "$RAAA.txt", "$IBBB", "$RBBB", "$IDDD.txt"] {
        assert!(!exists(&t.path(&format!("{sid}/{gone}"))), "{gone}");
    }
    assert!(
        exists(&young_i) && exists(&young_r),
        "a young entry stays whole"
    );
    assert!(exists(&ini));
    assert!(exists(&t.path(sid)) && t.0.is_dir());
}

// ---------------------------------------------------------------------------------
// Links, special files and races (Linux)
// ---------------------------------------------------------------------------------

#[cfg(target_os = "linux")]
mod linux_only {
    use super::*;
    use std::os::unix::fs::symlink;

    #[test]
    fn symlinked_file_and_dir_are_never_followed() {
        let t = Tree::new("links");
        let outside = Tree::new("links-outside");
        let secret = outside.file("secret.txt", 10);
        let inner = outside.file("secret_dir/inner.txt", 10);
        symlink(&secret, t.path("link_file")).unwrap();
        symlink(outside.path("secret_dir"), t.path("link_dir")).unwrap();
        t.file("plain.txt", 1);
        let clock = TestClock::ahead(60);
        let s = sweep(&t.0, &tmp_rules(10), false, &clock, unlimited());
        assert_eq!(s.files, 1);
        assert_eq!(s.skipped.reparse, 2);
        assert!(exists(&secret) && exists(&inner), "link targets survive");
        assert!(
            exists(&t.path("link_file")) && exists(&t.path("link_dir")),
            "links are skipped, not deleted"
        );
    }

    #[test]
    fn a_root_that_is_a_link_is_refused() {
        let t = Tree::new("rootlink");
        let real = t.dir("real");
        let victim = t.file("real/old.txt", 1);
        symlink(&real, t.path("link")).unwrap();
        let clock = TestClock::ahead(60);
        let s = sweep(&t.path("link"), &tmp_rules(10), false, &clock, unlimited());
        assert_eq!(s.files, 0);
        assert_eq!(s.skipped.reparse, 1);
        assert!(s.errors.iter().any(|e| e.code == "root_refused"));
        assert!(exists(&victim));

        // A link anywhere on the chain below the base is refused too.
        symlink(&real, t.path("chainlink")).unwrap();
        let mut s = Session::new(false, &clock, unlimited(), &[]);
        sweep_root(
            &platform_ops(),
            &mut s,
            &t.0,
            &["chainlink"],
            &tmp_rules(10),
        );
        assert_eq!(s.files, 0);
        assert!(exists(&victim));
    }

    #[test]
    fn emptied_directory_younger_than_threshold_is_kept() {
        let t = Tree::new("youngdir");
        t.file("d/old.txt", 1);
        let d = t.path("d");
        std::fs::File::open(&d)
            .unwrap()
            .set_modified(SystemTime::now() + days(29))
            .unwrap();
        let clock = TestClock::ahead(30);
        let s = sweep(&t.0, &tmp_rules(7), false, &clock, unlimited());
        assert_eq!(s.files, 1, "the old file goes");
        assert!(
            d.is_dir(),
            "its directory is younger than the threshold and stays"
        );
    }

    #[test]
    fn sockets_and_fifos_are_never_removed() {
        let t = Tree::new("special");
        let fifo = t.path("fifo");
        let c = std::ffi::CString::new(fifo.as_os_str().as_encoded_bytes()).unwrap();
        // SAFETY: NUL-terminated path.
        assert_eq!(unsafe { libc::mkfifo(c.as_ptr(), 0o600) }, 0);
        let sock = t.path("sock");
        let _listener = std::os::unix::net::UnixListener::bind(&sock).unwrap();
        let clock = TestClock::ahead(60);
        let s = sweep(&t.0, &tmp_rules(10), false, &clock, unlimited());
        assert_eq!(s.files, 0);
        assert!(exists(&fifo) && exists(&sock));
    }

    /// Delegates to the real primitives, running `hook` once around `open_dir` of
    /// `victim` — `before` it to swap the entry, `after` it to move the opened dir.
    struct Racing<F: Fn()> {
        victim: &'static str,
        before: bool,
        hook: F,
        fired: Cell<bool>,
    }

    impl<F: Fn()> Ops for Racing<F> {
        type Dir = <linux::FdOps as Ops>::Dir;
        fn open_root(&self, base: &Path, chain: &[&str]) -> Result<(Self::Dir, PathBuf), FsError> {
            linux::FdOps.open_root(base, chain)
        }
        fn list(&self, dir: &Self::Dir) -> Result<Vec<OsString>, FsError> {
            linux::FdOps.list(dir)
        }
        fn stat(&self, dir: &Self::Dir, name: &OsStr) -> Result<Meta, FsError> {
            linux::FdOps.stat(dir, name)
        }
        fn open_dir(
            &self,
            parent: &Self::Dir,
            name: &OsStr,
            expect: &Meta,
        ) -> Result<Self::Dir, FsError> {
            let fire = name == self.victim && !self.fired.replace(true);
            if fire && self.before {
                (self.hook)();
            }
            let r = linux::FdOps.open_dir(parent, name, expect);
            if fire && !self.before {
                (self.hook)();
            }
            r
        }
        fn remove_file(
            &self,
            dir: &Self::Dir,
            name: &OsStr,
            expect: &Meta,
            dry_run: bool,
        ) -> Result<(), FsError> {
            linux::FdOps.remove_file(dir, name, expect, dry_run)
        }
        fn remove_dir(
            &self,
            dir: &Self::Dir,
            name: &OsStr,
            expect: &Meta,
            dry_run: bool,
        ) -> Result<(), FsError> {
            linux::FdOps.remove_dir(dir, name, expect, dry_run)
        }
    }

    /// A directory swapped for a symlink between enumeration and descent is not
    /// followed: the walk never reaches the link's target.
    #[test]
    fn dir_swapped_for_symlink_before_descent_is_not_followed() {
        let t = Tree::new("race1");
        let outside = Tree::new("race1-outside");
        let precious = outside.file("precious.txt", 10);
        t.file("victim/old.txt", 1);
        let (root, out) = (t.0.clone(), outside.0.clone());
        let ops = Racing {
            victim: "victim",
            before: true,
            hook: move || {
                std::fs::rename(root.join("victim"), out.join("moved-away")).unwrap();
                symlink(&out, root.join("victim")).unwrap();
            },
            fired: Cell::new(false),
        };
        let clock = TestClock::ahead(60);
        let mut s = Session::new(false, &clock, unlimited(), &[]);
        sweep_root(&ops, &mut s, &t.0, &[], &tmp_rules(10));
        assert!(ops.fired.get());
        assert!(exists(&precious), "nothing outside the root is deleted");
        assert!(exists(&outside.path("moved-away/old.txt")));
        assert_eq!(s.files, 0);
        assert!(s.skipped.reparse >= 1);
    }

    /// A directory moved out of the root after it was opened (and replaced with a
    /// symlink) is caught by the containment re-check before anything in it goes.
    #[test]
    fn dir_moved_out_after_open_is_caught_by_containment() {
        if !Path::new("/proc/self/fd").is_dir() {
            return;
        }
        let t = Tree::new("race2");
        let outside = Tree::new("race2-outside");
        t.file("victim/old.txt", 1);
        let (root, out) = (t.0.clone(), outside.0.clone());
        let ops = Racing {
            victim: "victim",
            before: false,
            hook: move || {
                std::fs::rename(root.join("victim"), out.join("moved")).unwrap();
                symlink(&out, root.join("victim")).unwrap();
            },
            fired: Cell::new(false),
        };
        let clock = TestClock::ahead(60);
        let mut s = Session::new(false, &clock, unlimited(), &[]);
        sweep_root(&ops, &mut s, &t.0, &[], &tmp_rules(10));
        assert!(ops.fired.get());
        assert!(
            exists(&outside.path("moved/old.txt")),
            "the moved directory's file survives"
        );
        assert_eq!(s.files, 0);
        assert!(
            s.errors.iter().any(|e| e.code == "containment"),
            "{:?}",
            s.errors
        );
    }

    #[test]
    fn recycle_payload_that_is_a_link_keeps_its_record() {
        let t = Tree::new("binlink");
        let outside = Tree::new("binlink-outside");
        let precious = outside.file("precious.txt", 1);
        let sid = "S-1-5-21-9";
        let rec = t.file(&format!("{sid}/$IEEE"), 1);
        symlink(&outside.0, t.path(&format!("{sid}/$REEE"))).unwrap();
        let clock = TestClock::ahead(60);
        let mut s = Session::new(false, &clock, unlimited(), &[]);
        sweep_recycle_bin(&platform_ops(), &mut s, &t.0, days(30));
        assert!(exists(&precious) && exists(&rec));
        assert_eq!(s.skipped.reparse, 1);
    }

    #[test]
    fn commands_run_with_a_cleared_environment() {
        if !Path::new("/usr/bin/env").is_file() {
            return;
        }
        std::env::set_var("KENNY_CLEANUP_LEAK_PROBE", "1");
        let out = run_fixed("/usr/bin/env", &[], Duration::from_secs(10)).unwrap();
        std::env::remove_var("KENNY_CLEANUP_LEAK_PROBE");
        let mut vars: Vec<&str> = out.stdout.lines().collect();
        vars.sort_unstable();
        assert_eq!(
            vars,
            ["LANG=C", "LC_ALL=C", "PATH=/usr/sbin:/usr/bin:/sbin:/bin"]
        );
    }
}

// ---------------------------------------------------------------------------------
// Junctions (Windows)
// ---------------------------------------------------------------------------------

#[cfg(windows)]
mod windows_only {
    use super::*;

    /// Create a junction with `mklink /J` (no privilege needed). `cmd` does not take
    /// verbatim `\\?\` paths, so they are handed over without the prefix.
    fn junction(link: &Path, target: &Path) -> bool {
        let plain = |p: &Path| {
            let s = p.to_string_lossy().into_owned();
            s.strip_prefix(r"\\?\").map(str::to_string).unwrap_or(s)
        };
        let made = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(plain(link))
            .arg(plain(target))
            .output()
            .is_ok_and(|o| o.status.success());
        made && exists(link)
    }

    #[test]
    fn junctions_are_never_followed_or_deleted() {
        let t = Tree::new("junction");
        let outside = Tree::new("junction-outside");
        let precious = outside.file("precious.txt", 10);
        t.file("plain.txt", 1);
        assert!(junction(&t.path("j"), &outside.0), "mklink /J failed");
        let clock = TestClock::ahead(60);
        let s = sweep(&t.0, &windows_tree_rules(7), false, &clock, unlimited());
        assert_eq!(s.files, 1);
        assert_eq!(s.skipped.reparse, 1);
        assert!(exists(&precious));
        assert!(exists(&t.path("j")));
    }

    #[test]
    fn a_root_that_is_a_junction_is_refused() {
        let t = Tree::new("rootjunction");
        let outside = Tree::new("rootjunction-outside");
        let precious = outside.file("precious.txt", 10);
        assert!(junction(&t.path("j"), &outside.0), "mklink /J failed");
        let clock = TestClock::ahead(60);
        let s = sweep(
            &t.path("j"),
            &windows_tree_rules(7),
            false,
            &clock,
            unlimited(),
        );
        assert_eq!(s.files, 0);
        assert_eq!(s.skipped.reparse, 1);
        assert!(exists(&precious));
    }
}
