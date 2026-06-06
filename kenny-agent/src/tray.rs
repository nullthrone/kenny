//! System-tray helper: the local on/off switch for remote control.
//!
//! The agent normally runs as a Windows service in **session 0**, where a tray icon
//! would be invisible to the logged-in user. This helper is therefore a *separate*
//! process (`kenny-agent tray`) that runs in the interactive user session — `install`
//! auto-starts it at logon. It owns no connection; it only shows a notification-area
//! icon whose context menu flips the shared control file via [`crate::control`]. The
//! service (session 0) reads that file before every mutating tool. See ADR-0010.
//!
//! Two icon variants make the state legible at a glance: the normal "Kenny" badge when
//! remote control is on, and a greyed/struck-through badge when it is off.
//!
//! The menu deliberately has **no "quit"**: the tray is load-bearing (it also hosts the
//! screen-capture responder, ADR-0017), so letting the user close it would silently break
//! remote control. If it is ever killed anyway (Task Manager, a crash), a service restart
//! relaunches it into the active session — see [`crate::service`]. The menu instead
//! offers a read-only **"Protokoll anzeigen"** entry that opens the newest agent log.

/// Run the tray helper.
///
/// On Windows this registers a notification-area icon and pumps the message loop until
/// the window is destroyed (e.g. at logoff/shutdown). Elsewhere there is no tray, so this
/// is a no-op stub that returns an error (keeping `cargo build`/`cargo test` green on
/// Linux CI).
#[cfg(windows)]
pub fn run() -> anyhow::Result<()> {
    windows_impl::run()
}

/// Non-Windows stub: there is no system tray to drive.
#[cfg(not(windows))]
pub fn run() -> anyhow::Result<()> {
    anyhow::bail!("the tray helper is only supported on Windows");
}

/// Pick the agent log file to open: the most-recently-modified entry in `dir` whose
/// name starts with [`crate::LOG_FILE_PREFIX`] (the daily appender suffixes a date, so
/// there are several). Returns `None` when the directory is missing, unreadable, or holds
/// no log file — the caller then falls back to opening the directory itself.
///
/// Kept platform-neutral (used by the Windows menu, unit-tested on Linux CI).
#[cfg_attr(not(windows), allow(dead_code))]
fn newest_log_file(dir: &std::path::Path) -> Option<std::path::PathBuf> {
    let mut newest: Option<(std::time::SystemTime, std::path::PathBuf)> = None;
    for entry in std::fs::read_dir(dir).ok()?.flatten() {
        if !entry
            .file_name()
            .to_string_lossy()
            .starts_with(crate::LOG_FILE_PREFIX)
        {
            continue;
        }
        let Ok(modified) = entry.metadata().and_then(|m| m.modified()) else {
            continue;
        };
        if newest.as_ref().is_none_or(|(t, _)| modified > *t) {
            newest = Some((modified, entry.path()));
        }
    }
    newest.map(|(_, path)| path)
}

#[cfg(windows)]
mod windows_impl {
    use std::cell::RefCell;

    use anyhow::Context;
    use windows::core::{w, PCWSTR};
    use windows::Win32::Foundation::{
        GetLastError, ERROR_ALREADY_EXISTS, HINSTANCE, HWND, LPARAM, LRESULT, POINT, TRUE, WPARAM,
    };
    use windows::Win32::System::Console::FreeConsole;
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::System::Threading::CreateMutexW;
    use windows::Win32::UI::Shell::{
        ShellExecuteW, Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE,
        NIM_MODIFY, NOTIFYICONDATAW,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreateIconFromResourceEx, CreatePopupMenu, CreateWindowExW, DefWindowProcW,
        DestroyIcon, DestroyMenu, DispatchMessageW, GetCursorPos, GetMessageW, GetSystemMetrics,
        LoadCursorW, LookupIconIdFromDirectoryEx, PostMessageW, PostQuitMessage, RegisterClassW,
        SetForegroundWindow, TrackPopupMenu, TranslateMessage, CW_USEDEFAULT, HICON, HMENU,
        IDC_ARROW, IMAGE_FLAGS, MENU_ITEM_FLAGS, MF_CHECKED, MF_DISABLED, MF_GRAYED, MF_SEPARATOR,
        MF_STRING, MSG, SM_CXSMICON, SM_CYSMICON, SW_SHOWNORMAL, TPM_BOTTOMALIGN, TPM_RIGHTBUTTON,
        WINDOW_EX_STYLE, WM_APP, WM_COMMAND, WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP, WNDCLASSW,
        WS_OVERLAPPEDWINDOW,
    };

    /// Embedded icon for the "remote control on" state (multi-resolution `.ico`).
    const ICON_ON: &[u8] = include_bytes!("../assets/kenny-on.ico");
    /// Embedded icon for the "remote control off" state.
    const ICON_OFF: &[u8] = include_bytes!("../assets/kenny-off.ico");

    /// Private window message the shell posts for tray-icon mouse events.
    const WM_TRAY_CALLBACK: u32 = WM_APP + 1;
    /// Stable id of our single tray icon.
    const TRAY_UID: u32 = 1;
    /// Menu command: toggle remote control on/off.
    const ID_TOGGLE: usize = 1001;
    /// Menu command: open the newest local agent log in the default editor.
    const ID_OPEN_LOGS: usize = 1003;
    /// `CreateIconFromResourceEx` version word for modern (3.0) icon resources.
    const ICON_RESOURCE_VERSION: u32 = 0x0003_0000;

    /// Per-process tray state, owned by the message-loop thread.
    struct TrayState {
        hwnd: HWND,
        icon_on: HICON,
        icon_off: HICON,
        enabled: bool,
    }

    thread_local! {
        static STATE: RefCell<Option<TrayState>> = const { RefCell::new(None) };
    }

    pub fn run() -> anyhow::Result<()> {
        unsafe {
            // Detach the inherited console (the binary is a console subsystem app) so
            // the tray runs silently — no flashing console window. Harmless if there
            // is no console attached. Does NOT affect `run`/CLI, which keep their console.
            let _ = FreeConsole();

            // Single-instance guard, scoped to this session (fast-user-switching safe).
            // `install` starts the tray immediately *and* registers a logon autostart,
            // so a second copy can race; the first one to create the named mutex wins.
            let _singleton =
                CreateMutexW(None, false, w!("Local\\kenny-agent-tray")).context("CreateMutexW")?;
            if GetLastError() == ERROR_ALREADY_EXISTS {
                // Another tray already owns the notification icon in this session.
                return Ok(());
            }

            // Host the screen-capture responder for the session-0 service. Runs for
            // the life of the process; the OS reclaims the thread on exit (ADR-0017).
            std::thread::spawn(crate::screencap_ipc::serve);

            let hmodule = GetModuleHandleW(None).context("GetModuleHandleW")?;
            let hinstance = HINSTANCE(hmodule.0);

            // Register a window class for the (hidden) message window.
            let class_name = w!("kenny_tray_window");
            let wnd_class = WNDCLASSW {
                lpfnWndProc: Some(wndproc),
                hInstance: hinstance,
                lpszClassName: class_name,
                hCursor: LoadCursorW(None, IDC_ARROW).unwrap_or_default(),
                ..Default::default()
            };
            if RegisterClassW(&wnd_class) == 0 {
                anyhow::bail!("RegisterClassW failed");
            }

            // A normal top-level window that we never show: it owns the tray icon and
            // receives the callback + menu messages.
            let hwnd = CreateWindowExW(
                WINDOW_EX_STYLE::default(),
                class_name,
                w!("kenny"),
                WS_OVERLAPPEDWINDOW,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                CW_USEDEFAULT,
                HWND::default(),
                HMENU::default(),
                hinstance,
                None,
            )
            .context("CreateWindowExW")?;

            // Load both icon variants at the small (notification-area) size.
            let cx = GetSystemMetrics(SM_CXSMICON);
            let cy = GetSystemMetrics(SM_CYSMICON);
            let icon_on = load_icon(ICON_ON, cx, cy).context("load on-icon")?;
            let icon_off = load_icon(ICON_OFF, cx, cy).context("load off-icon")?;

            let enabled = crate::control::remote_control_enabled();

            STATE.with(|s| {
                *s.borrow_mut() = Some(TrayState {
                    hwnd,
                    icon_on,
                    icon_off,
                    enabled,
                });
            });

            // Add the icon to the notification area.
            let mut nid = base_nid(hwnd);
            nid.hIcon = if enabled { icon_on } else { icon_off };
            set_tip(&mut nid, tip_for(enabled));
            if !Shell_NotifyIconW(NIM_ADD, &nid).as_bool() {
                anyhow::bail!("Shell_NotifyIconW(NIM_ADD) failed");
            }

            // Pump messages until WM_QUIT (posted by the Quit menu item / WM_DESTROY).
            let mut msg = MSG::default();
            while GetMessageW(&mut msg, HWND::default(), 0, 0).as_bool() {
                let _ = TranslateMessage(&msg);
                DispatchMessageW(&msg);
            }

            // Best-effort cleanup.
            let nid = base_nid(hwnd);
            let _ = Shell_NotifyIconW(NIM_DELETE, &nid);
            let _ = DestroyIcon(icon_on);
            let _ = DestroyIcon(icon_off);
        }
        Ok(())
    }

    /// The notification-area tooltip for a given state.
    fn tip_for(enabled: bool) -> &'static str {
        if enabled {
            "kenny – Fernsteuerung aktiv"
        } else {
            "kenny – Fernsteuerung AUS"
        }
    }

    /// Build a zeroed `NOTIFYICONDATAW` addressing our single icon.
    fn base_nid(hwnd: HWND) -> NOTIFYICONDATAW {
        NOTIFYICONDATAW {
            cbSize: std::mem::size_of::<NOTIFYICONDATAW>() as u32,
            hWnd: hwnd,
            uID: TRAY_UID,
            uFlags: NIF_MESSAGE | NIF_ICON | NIF_TIP,
            uCallbackMessage: WM_TRAY_CALLBACK,
            ..Default::default()
        }
    }

    /// Encode a string as a NUL-terminated UTF-16 buffer for the wide Win32 APIs.
    /// The returned `Vec` must outlive the call that reads its pointer.
    fn to_wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// Copy a tooltip string into the fixed `szTip` buffer (NUL-terminated).
    fn set_tip(nid: &mut NOTIFYICONDATAW, tip: &str) {
        let buf = &mut nid.szTip;
        let mut i = 0;
        for unit in tip.encode_utf16() {
            if i + 1 >= buf.len() {
                break;
            }
            buf[i] = unit;
            i += 1;
        }
        buf[i] = 0;
    }

    /// Create an `HICON` from an in-memory `.ico` file, picking the best-fit image.
    unsafe fn load_icon(ico: &[u8], cx: i32, cy: i32) -> anyhow::Result<HICON> {
        // Find the offset of the directory entry that best matches the desired size.
        let offset =
            LookupIconIdFromDirectoryEx(ico.as_ptr(), TRUE, cx, cy, IMAGE_FLAGS(0)) as usize;
        if offset == 0 || offset >= ico.len() {
            anyhow::bail!("no matching icon image in .ico");
        }
        // `CreateIconFromResourceEx` takes the icon image as a slice starting at the
        // best-fit entry the lookup pointed us at.
        let icon = CreateIconFromResourceEx(
            &ico[offset..],
            TRUE,
            ICON_RESOURCE_VERSION,
            cx,
            cy,
            IMAGE_FLAGS(0),
        )
        .context("CreateIconFromResourceEx")?;
        Ok(icon)
    }

    /// Toggle the kill switch, persist it, and refresh the icon + tooltip.
    unsafe fn toggle() {
        STATE.with(|s| {
            let mut guard = s.borrow_mut();
            let Some(state) = guard.as_mut() else {
                return;
            };
            let new_enabled = !state.enabled;
            if let Err(e) = crate::control::set_remote_control_enabled(new_enabled) {
                tracing::error!(error = %e, "failed to persist remote-control state");
                return;
            }
            state.enabled = new_enabled;
            let mut nid = base_nid(state.hwnd);
            nid.hIcon = if new_enabled {
                state.icon_on
            } else {
                state.icon_off
            };
            set_tip(&mut nid, tip_for(new_enabled));
            let _ = Shell_NotifyIconW(NIM_MODIFY, &nid);
        });
    }

    /// Open the newest local agent log file in the user's default editor.
    ///
    /// `.log` files are associated with Notepad on a stock Windows, so a `ShellExecute`
    /// "open" pops the log in an editor. Falls back to opening the log *directory* (e.g.
    /// before the first log has rolled, or if none match), and is a no-op if there is no
    /// log directory at all. Best-effort: failures are logged, never fatal.
    unsafe fn open_logs() {
        let Some(dir) = crate::log_dir() else {
            tracing::warn!("no log directory configured; nothing to open");
            return;
        };
        // Prefer the newest concrete log file; otherwise open the directory itself.
        let target = super::newest_log_file(&dir).unwrap_or(dir);
        let wide = to_wide(&target.to_string_lossy());
        // SAFETY: `wide` is a valid NUL-terminated wide string that outlives the call;
        // the verb/dir/params are static or null.
        let hinst = ShellExecuteW(
            None,
            w!("open"),
            PCWSTR(wide.as_ptr()),
            PCWSTR::null(),
            PCWSTR::null(),
            SW_SHOWNORMAL,
        );
        // ShellExecuteW returns an HINSTANCE > 32 on success.
        if hinst.0 as usize <= 32 {
            tracing::warn!(path = %target.display(), "could not open log via ShellExecute");
        }
    }

    /// Pop up the context menu at the cursor.
    unsafe fn show_menu(hwnd: HWND) {
        let enabled = STATE.with(|s| s.borrow().as_ref().map(|st| st.enabled).unwrap_or(true));

        let menu = CreatePopupMenu().unwrap_or_default();
        if menu.is_invalid() {
            return;
        }
        // Version header (disabled/greyed, no command id): the agent version is led by
        // the GitHub release tag at build time (see `build.rs`/`crate::BUILD_VERSION`),
        // shown inline so there is no separate "About" window.
        let version_label = to_wide(&format!("kenny v{}", crate::BUILD_VERSION));
        let _ = AppendMenuW(
            menu,
            MF_STRING | MF_DISABLED | MF_GRAYED,
            0,
            PCWSTR(version_label.as_ptr()),
        );
        let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
        // Checkable "Fernsteuerung aktiv": checked == currently on; click toggles it.
        let toggle_flags = MF_STRING
            | if enabled {
                MF_CHECKED
            } else {
                MENU_ITEM_FLAGS(0)
            };
        let _ = AppendMenuW(menu, toggle_flags, ID_TOGGLE, w!("Fernsteuerung aktiv"));
        let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
        // Read-only convenience: open the local agent log so the person at the PC can see
        // what kenny is doing. There is intentionally no "quit" — see the module docs.
        let _ = AppendMenuW(menu, MF_STRING, ID_OPEN_LOGS, w!("Protokoll anzeigen"));

        let mut pt = POINT::default();
        let _ = GetCursorPos(&mut pt);
        // Required so the menu dismisses when the user clicks elsewhere.
        let _ = SetForegroundWindow(hwnd);
        let _ = TrackPopupMenu(
            menu,
            TPM_RIGHTBUTTON | TPM_BOTTOMALIGN,
            pt.x,
            pt.y,
            0,
            hwnd,
            None,
        );
        // MSDN workaround so the menu closes reliably.
        let _ = PostMessageW(hwnd, 0, WPARAM(0), LPARAM(0));
        let _ = DestroyMenu(menu);
    }

    /// Window procedure for the hidden tray window.
    extern "system" fn wndproc(hwnd: HWND, msg: u32, wparam: WPARAM, lparam: LPARAM) -> LRESULT {
        unsafe {
            match msg {
                WM_TRAY_CALLBACK => {
                    // The mouse event is in the low word of lParam.
                    let event = (lparam.0 as u32) & 0xffff;
                    if event == WM_RBUTTONUP || event == WM_LBUTTONUP {
                        show_menu(hwnd);
                    }
                    LRESULT(0)
                }
                WM_COMMAND => {
                    match wparam.0 & 0xffff {
                        ID_TOGGLE => toggle(),
                        ID_OPEN_LOGS => open_logs(),
                        _ => {}
                    }
                    LRESULT(0)
                }
                WM_DESTROY => {
                    PostQuitMessage(0);
                    LRESULT(0)
                }
                _ => DefWindowProcW(hwnd, msg, wparam, lparam),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::newest_log_file;
    use std::fs;
    use std::time::{Duration, SystemTime};

    #[test]
    fn newest_log_file_picks_most_recent_match() {
        let dir = std::env::temp_dir().join(format!("kenny-tray-logs-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        // A non-log file must be ignored even if it is the newest thing in the dir.
        fs::write(dir.join("unrelated.txt"), b"x").unwrap();
        let old = dir.join("kenny-agent.log.2026-06-05");
        let new = dir.join("kenny-agent.log.2026-06-06");
        fs::write(&old, b"old").unwrap();
        fs::write(&new, b"new").unwrap();

        // Make `new` distinctly newer regardless of write granularity.
        let later = SystemTime::now() + Duration::from_secs(5);
        let f = fs::File::open(&new).unwrap();
        f.set_modified(later).unwrap();

        assert_eq!(newest_log_file(&dir).as_deref(), Some(new.as_path()));

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn newest_log_file_none_when_no_match_or_dir() {
        let missing = std::env::temp_dir().join("kenny-tray-does-not-exist-xyz");
        let _ = fs::remove_dir_all(&missing);
        assert_eq!(newest_log_file(&missing), None);

        let empty = std::env::temp_dir().join(format!("kenny-tray-empty-{}", std::process::id()));
        let _ = fs::remove_dir_all(&empty);
        fs::create_dir_all(&empty).unwrap();
        fs::write(empty.join("readme.md"), b"x").unwrap();
        assert_eq!(newest_log_file(&empty), None);
        let _ = fs::remove_dir_all(&empty);
    }
}
