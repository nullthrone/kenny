//! `browser_extensions` section — extensions installed in the browsers we watch.
//!
//! Chromium family (Chrome, Edge): `<profile>\Extensions\<id>\<version>\manifest.json`,
//! with `__MSG_*__` names resolved best-effort via `_locales/<default_locale>/messages.json`.
//! Firefox: the profile's `extensions.json` (`addons[]`), keeping user-installed
//! (`app-profile`) extensions and skipping system/builtin locations and non-extension
//! addon types. Profiles are enumerated per user under `C:\Users\*`, the same
//! locations `web_activity` reads — the walker is local to this collector on purpose
//! (sharing it would mean refactoring the stable `web_activity` collector for little
//! gain).
//!
//! **Privacy:** deduplicated across users and profiles by `(browser, id)` — no
//! per-user attribution goes on the wire. Cap 200 with a `truncated` flag.
//!
//! The manifest/JSON parsing and the directory scanner live in [`core`] (no OS
//! specifics — the scanner takes any path) so Linux CI tests them against
//! fabricated profile trees. Only the `C:\Users` enumeration is Windows-gated.

use serde_json::json;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Collect the `browser_extensions` section.
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
                "extensions": [],
                "count": 0,
                "truncated": false,
                "profiles_read": 0,
                "errors": [],
            }),
        )
    }
}

/// Portable parsing/scanning core — compiled and tested on every platform.
#[cfg_attr(not(windows), allow(dead_code))]
pub mod core {
    use std::path::{Path, PathBuf};

    use serde_json::{json, Value};

    /// Contract cap on the `extensions` list.
    pub const MAX_EXTENSIONS: usize = 200;

    /// One installed extension, already stripped of any per-user attribution.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct Extension {
        /// `chrome`, `edge`, or `firefox`.
        pub browser: String,
        pub id: String,
        pub name: String,
        pub version: Option<String>,
    }

    /// The manifest fields needed to render a Chromium extension.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct ChromiumManifest {
        /// Raw `name`, possibly a `__MSG_*__` localization placeholder.
        pub name: Option<String>,
        pub version: String,
        pub default_locale: Option<String>,
    }

    /// Parse a Chromium `manifest.json` body. `version` is mandatory in the format;
    /// a manifest without one is rejected.
    pub fn parse_chromium_manifest(text: &str) -> Option<ChromiumManifest> {
        let v: Value = serde_json::from_str(text).ok()?;
        let version = v
            .get("version")?
            .as_str()
            .map(str::trim)
            .filter(|s| !s.is_empty())?
            .to_string();
        let name = v
            .get("name")
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(str::to_string);
        let default_locale = v
            .get("default_locale")
            .and_then(Value::as_str)
            .map(str::to_string);
        Some(ChromiumManifest {
            name,
            version,
            default_locale,
        })
    }

    /// The key inside a `__MSG_key__` localization placeholder, if `name` is one.
    pub fn msg_key(name: &str) -> Option<&str> {
        let key = name.strip_prefix("__MSG_")?.strip_suffix("__")?;
        if key.is_empty() {
            None
        } else {
            Some(key)
        }
    }

    /// Resolve a `__MSG_key__` placeholder against a `_locales/<locale>/messages.json`
    /// body (`{"<key>": {"message": "..."}}`). Chromium treats message keys as
    /// case-insensitive, so the lookup is too.
    pub fn resolve_msg(messages_text: &str, key: &str) -> Option<String> {
        let v: Value = serde_json::from_str(messages_text).ok()?;
        let obj = v.as_object()?;
        let key_lc = key.to_ascii_lowercase();
        for (k, entry) in obj {
            if k.to_ascii_lowercase() == key_lc {
                return entry
                    .get("message")
                    .and_then(Value::as_str)
                    .map(str::trim)
                    .filter(|s| !s.is_empty())
                    .map(str::to_string);
            }
        }
        None
    }

    /// Parse a Firefox `extensions.json` body: user-installed (`app-profile`)
    /// extensions only. System/builtin locations (`app-system-defaults`,
    /// `app-builtin`, ...) and non-extension addon types (themes, dictionaries,
    /// locales) are skipped.
    pub fn parse_firefox_extensions(text: &str) -> Vec<Extension> {
        let Ok(v) = serde_json::from_str::<Value>(text) else {
            return Vec::new();
        };
        let Some(addons) = v.get("addons").and_then(Value::as_array) else {
            return Vec::new();
        };
        addons
            .iter()
            .filter_map(|a| {
                if let Some(location) = a.get("location").and_then(Value::as_str) {
                    if location != "app-profile" {
                        return None;
                    }
                }
                if let Some(kind) = a.get("type").and_then(Value::as_str) {
                    if kind != "extension" {
                        return None;
                    }
                }
                let id = a.get("id")?.as_str()?.to_string();
                let name = a
                    .get("defaultLocale")
                    .and_then(|d| d.get("name"))
                    .and_then(Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| id.clone());
                let version = a.get("version").and_then(Value::as_str).map(str::to_string);
                Some(Extension {
                    browser: "firefox".to_string(),
                    id,
                    name,
                    version,
                })
            })
            .collect()
    }

    /// Scan one Chromium profile's `Extensions` directory
    /// (`<Extensions>/<id>/<version>/manifest.json`). The lexicographically last
    /// version directory that yields a parseable manifest wins (usually the newest
    /// install); `__MSG_*__` names resolve via `_locales`, else fall back to the id.
    /// Unreadable manifests are recorded in `errors` and skipped.
    pub fn scan_chromium_extensions(
        extensions_dir: &Path,
        browser: &str,
        errors: &mut Vec<String>,
    ) -> Vec<Extension> {
        let mut out = Vec::new();
        let Ok(ids) = std::fs::read_dir(extensions_dir) else {
            return out;
        };
        for id_entry in ids.flatten() {
            let id_path = id_entry.path();
            if !id_path.is_dir() {
                continue;
            }
            let id = id_entry.file_name().to_string_lossy().to_string();
            // Chromium keeps a scratch `Temp` folder alongside the extension ids.
            if id == "Temp" {
                continue;
            }
            let mut versions = subdirs(&id_path);
            versions.sort();
            for vdir in versions.iter().rev() {
                let manifest_path = vdir.join("manifest.json");
                let Ok(text) = std::fs::read_to_string(&manifest_path) else {
                    continue;
                };
                let Some(manifest) = parse_chromium_manifest(&text) else {
                    errors.push(format!("{}: invalid manifest", manifest_path.display()));
                    continue;
                };
                let name = resolve_chromium_name(&manifest, vdir, &id);
                out.push(Extension {
                    browser: browser.to_string(),
                    id: id.clone(),
                    name,
                    version: Some(manifest.version),
                });
                break;
            }
        }
        out
    }

    /// Best-effort display name for a Chromium manifest: a plain `name` as-is; a
    /// `__MSG_*__` placeholder via `_locales/<default_locale>/messages.json` (then
    /// `en`); otherwise the extension id.
    fn resolve_chromium_name(manifest: &ChromiumManifest, version_dir: &Path, id: &str) -> String {
        let raw = manifest.name.as_deref().unwrap_or("");
        let Some(key) = msg_key(raw) else {
            return if raw.is_empty() {
                id.to_string()
            } else {
                raw.to_string()
            };
        };
        let mut locales: Vec<String> = Vec::new();
        if let Some(l) = &manifest.default_locale {
            locales.push(l.clone());
        }
        locales.push("en".to_string());
        for locale in locales {
            let path = version_dir
                .join("_locales")
                .join(&locale)
                .join("messages.json");
            if let Ok(text) = std::fs::read_to_string(&path) {
                if let Some(name) = resolve_msg(&text, key) {
                    return name;
                }
            }
        }
        id.to_string()
    }

    /// Immediate subdirectories of `root`; empty when `root` does not exist.
    pub fn subdirs(root: &Path) -> Vec<PathBuf> {
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

    /// Dedupe by `(browser, id)` (first entry wins), sort by that key, cap at
    /// [`MAX_EXTENSIONS`]. Returns `(extensions, count_before_cap, truncated)`.
    pub fn shape(extensions: Vec<Extension>) -> (Vec<Value>, usize, bool) {
        use std::collections::BTreeMap;

        let mut by_key: BTreeMap<(String, String), Extension> = BTreeMap::new();
        for e in extensions {
            by_key.entry((e.browser.clone(), e.id.clone())).or_insert(e);
        }
        let count = by_key.len();
        let truncated = count > MAX_EXTENSIONS;
        let out = by_key
            .into_values()
            .take(MAX_EXTENSIONS)
            .map(|e| {
                json!({
                    "browser": e.browser,
                    "id": e.id,
                    "name": e.name,
                    "version": e.version,
                })
            })
            .collect();
        (out, count, truncated)
    }

    /// Number of distinct `browser` values among the rendered extensions.
    pub fn distinct_browsers(extensions: &[Value]) -> usize {
        let mut browsers: Vec<&str> = extensions
            .iter()
            .filter_map(|e| e.get("browser").and_then(Value::as_str))
            .collect();
        browsers.sort_unstable();
        browsers.dedup();
        browsers.len()
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn parse_chromium_manifest_reads_fields() {
            let m = parse_chromium_manifest(
                r#"{ "name": "uBlock Origin", "version": "1.58.0", "manifest_version": 3 }"#,
            )
            .unwrap();
            assert_eq!(m.name.as_deref(), Some("uBlock Origin"));
            assert_eq!(m.version, "1.58.0");
            assert_eq!(m.default_locale, None);

            let localized = parse_chromium_manifest(
                r#"{ "name": "__MSG_appName__", "version": "2.0", "default_locale": "en" }"#,
            )
            .unwrap();
            assert_eq!(localized.name.as_deref(), Some("__MSG_appName__"));
            assert_eq!(localized.default_locale.as_deref(), Some("en"));

            // No version => rejected; garbage => rejected.
            assert!(parse_chromium_manifest(r#"{ "name": "x" }"#).is_none());
            assert!(parse_chromium_manifest("not json").is_none());
        }

        #[test]
        fn msg_key_extracts_placeholder() {
            assert_eq!(msg_key("__MSG_appName__"), Some("appName"));
            assert_eq!(msg_key("plain name"), None);
            // Degenerate placeholders (empty key, wrong suffix) are not keys.
            assert_eq!(msg_key("__MSG___"), None);
            assert_eq!(msg_key("__MSG___-"), None);
        }

        #[test]
        fn resolve_msg_is_case_insensitive() {
            let messages = r#"{ "AppName": { "message": "uBlock Origin" } }"#;
            assert_eq!(
                resolve_msg(messages, "appname").as_deref(),
                Some("uBlock Origin")
            );
            assert_eq!(resolve_msg(messages, "other"), None);
            assert_eq!(resolve_msg("not json", "appname"), None);
        }

        #[test]
        fn parse_firefox_extensions_skips_system_and_non_extensions() {
            let text = r#"{ "addons": [
                { "id": "uBlock0@raymondhill.net", "version": "1.58.0",
                  "type": "extension", "location": "app-profile",
                  "defaultLocale": { "name": "uBlock Origin" } },
                { "id": "builtin@mozilla.org", "version": "1.0",
                  "type": "extension", "location": "app-system-defaults",
                  "defaultLocale": { "name": "System Thing" } },
                { "id": "theme@mozilla.org", "version": "1.0",
                  "type": "theme", "location": "app-profile",
                  "defaultLocale": { "name": "Dark" } },
                { "id": "nameless@example.org", "version": "0.1",
                  "type": "extension", "location": "app-profile" }
            ] }"#;
            let exts = parse_firefox_extensions(text);
            assert_eq!(exts.len(), 2);
            assert_eq!(exts[0].id, "uBlock0@raymondhill.net");
            assert_eq!(exts[0].name, "uBlock Origin");
            assert_eq!(exts[0].version.as_deref(), Some("1.58.0"));
            assert_eq!(exts[0].browser, "firefox");
            // Missing defaultLocale.name falls back to the id.
            assert_eq!(exts[1].name, "nameless@example.org");
            assert!(parse_firefox_extensions("garbage").is_empty());
        }

        /// Fabricate a Chromium `Extensions` tree and scan it — exercises version
        /// selection, `__MSG_*__` resolution, and error recording on Linux CI.
        #[test]
        fn scan_chromium_extensions_reads_manifests() {
            let dir = std::env::temp_dir().join(format!(
                "kenny-brext-{}-{}",
                std::process::id(),
                line!()
            ));
            let ext_root = dir.join("Extensions");
            // Plain-named extension with two versions; the newest must win.
            let plain_old = ext_root.join("aaaaplainid").join("1.0.0_0");
            let plain_new = ext_root.join("aaaaplainid").join("2.0.0_0");
            std::fs::create_dir_all(&plain_old).unwrap();
            std::fs::create_dir_all(&plain_new).unwrap();
            std::fs::write(
                plain_old.join("manifest.json"),
                r#"{ "name": "Old", "version": "1.0.0" }"#,
            )
            .unwrap();
            std::fs::write(
                plain_new.join("manifest.json"),
                r#"{ "name": "Plain Ext", "version": "2.0.0" }"#,
            )
            .unwrap();
            // Localized-name extension.
            let localized = ext_root.join("bbbblocalid").join("1.58.0_0");
            let locales = localized.join("_locales").join("en");
            std::fs::create_dir_all(&locales).unwrap();
            std::fs::write(
                localized.join("manifest.json"),
                r#"{ "name": "__MSG_extName__", "version": "1.58.0", "default_locale": "en" }"#,
            )
            .unwrap();
            std::fs::write(
                locales.join("messages.json"),
                r#"{ "extName": { "message": "uBlock Origin" } }"#,
            )
            .unwrap();
            // Broken manifest => error recorded, extension skipped.
            let broken = ext_root.join("ccccbrokenid").join("1.0_0");
            std::fs::create_dir_all(&broken).unwrap();
            std::fs::write(broken.join("manifest.json"), "not json").unwrap();
            // The scratch Temp dir must be ignored.
            std::fs::create_dir_all(ext_root.join("Temp")).unwrap();

            let mut errors = Vec::new();
            let mut exts = scan_chromium_extensions(&ext_root, "chrome", &mut errors);
            std::fs::remove_dir_all(&dir).ok();

            exts.sort_by(|a, b| a.id.cmp(&b.id));
            assert_eq!(exts.len(), 2);
            assert_eq!(exts[0].id, "aaaaplainid");
            assert_eq!(exts[0].name, "Plain Ext");
            assert_eq!(exts[0].version.as_deref(), Some("2.0.0"));
            assert_eq!(exts[1].id, "bbbblocalid");
            assert_eq!(exts[1].name, "uBlock Origin");
            assert_eq!(errors.len(), 1);
            assert!(errors[0].contains("invalid manifest"));
        }

        #[test]
        fn shape_dedupes_across_profiles_and_caps() {
            let ext = |browser: &str, id: &str| Extension {
                browser: browser.to_string(),
                id: id.to_string(),
                name: id.to_string(),
                version: Some("1.0".to_string()),
            };
            // Same (browser, id) from two users/profiles collapses to one entry;
            // the same id in another browser stays separate.
            let (out, count, truncated) = shape(vec![
                ext("chrome", "abc"),
                ext("chrome", "abc"),
                ext("edge", "abc"),
                ext("firefox", "u@x.org"),
            ]);
            assert_eq!(count, 3);
            assert!(!truncated);
            assert_eq!(out[0]["browser"], "chrome");
            assert_eq!(out[1]["browser"], "edge");
            assert_eq!(out[2]["browser"], "firefox");
            assert_eq!(distinct_browsers(&out), 3);

            let many: Vec<Extension> = (0..250)
                .map(|i| ext("chrome", &format!("id{i:04}")))
                .collect();
            let (out, count, truncated) = shape(many);
            assert_eq!(out.len(), MAX_EXTENSIONS);
            assert_eq!(count, 250);
            assert!(truncated);
        }
    }
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use std::path::Path;

    /// Walk every user's Chrome/Edge profiles and Firefox profiles — the same
    /// per-user locations `web_activity` reads — and shape the deduplicated,
    /// capped extension list.
    pub fn collect() -> Section {
        let mut extensions: Vec<core::Extension> = Vec::new();
        let mut errors: Vec<String> = Vec::new();
        let mut profiles_read = 0u64;

        if let Ok(entries) = std::fs::read_dir(Path::new(r"C:\Users")) {
            for user in entries.flatten() {
                let home = user.path();
                if !home.is_dir() {
                    continue;
                }
                let local = home.join(r"AppData\Local");
                let roaming = home.join(r"AppData\Roaming");
                // Chromium browsers: <User Data>\<Profile>\Extensions\<id>\<version>.
                for (browser, root) in [
                    ("chrome", local.join(r"Google\Chrome\User Data")),
                    ("edge", local.join(r"Microsoft\Edge\User Data")),
                ] {
                    for profile in core::subdirs(&root) {
                        let ext_dir = profile.join("Extensions");
                        if ext_dir.is_dir() {
                            profiles_read += 1;
                            extensions.extend(core::scan_chromium_extensions(
                                &ext_dir,
                                browser,
                                &mut errors,
                            ));
                        }
                    }
                }
                // Firefox: Profiles\<profile>\extensions.json.
                for profile in core::subdirs(&roaming.join(r"Mozilla\Firefox\Profiles")) {
                    let file = profile.join("extensions.json");
                    if !file.is_file() {
                        continue;
                    }
                    match std::fs::read_to_string(&file) {
                        Ok(text) => {
                            profiles_read += 1;
                            extensions.extend(core::parse_firefox_extensions(&text));
                        }
                        Err(e) => errors.push(format!("{}: {e}", file.display())),
                    }
                }
            }
        }

        let (extensions, count, truncated) = core::shape(extensions);
        let browsers = core::distinct_browsers(&extensions);

        Section::with_fields(
            Status::Ok,
            format!("{count} extensions across {browsers} browsers"),
            json!({
                "extensions": extensions,
                "count": count,
                "truncated": truncated,
                "profiles_read": profiles_read,
                "errors": errors,
            }),
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn browser_extensions_section_is_valid() {
        let v = collect().into_value();
        assert!(v["status"].is_string());
        assert!(v["summary"].is_string());
        assert!(v["extensions"].is_array());
        assert!(v["count"].is_number());
        assert!(v["truncated"].is_boolean());
        assert!(v["profiles_read"].is_number());
        assert!(v["errors"].is_array());
    }

    #[cfg(not(windows))]
    #[test]
    fn off_windows_is_ok_stub() {
        let v = collect().into_value();
        assert_eq!(v["status"], "ok");
        assert_eq!(v["summary"], "n/a on this platform");
        assert_eq!(v["extensions"].as_array().unwrap().len(), 0);
        assert_eq!(v["count"], 0);
        assert_eq!(v["profiles_read"], 0);
    }
}
