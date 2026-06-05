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

/// Run the tray helper.
///
/// On Windows this registers a notification-area icon and pumps the message loop until
/// the user picks **Beenden**. Elsewhere there is no tray, so this is a no-op stub that
/// returns an error (keeping `cargo build`/`cargo test` green on Linux CI).
#[cfg(windows)]
pub fn run() -> anyhow::Result<()> {
    windows_impl::run()
}

/// Non-Windows stub: there is no system tray to drive.
#[cfg(not(windows))]
pub fn run() -> anyhow::Result<()> {
    anyhow::bail!("the tray helper is only supported on Windows");
}

#[cfg(windows)]
mod windows_impl {
    use std::cell::RefCell;

    use anyhow::Context;
    use windows::core::{w, PCWSTR};
    use windows::Win32::Foundation::{HINSTANCE, HWND, LPARAM, LRESULT, POINT, TRUE, WPARAM};
    use windows::Win32::System::LibraryLoader::GetModuleHandleW;
    use windows::Win32::UI::Shell::{
        Shell_NotifyIconW, NIF_ICON, NIF_MESSAGE, NIF_TIP, NIM_ADD, NIM_DELETE, NIM_MODIFY,
        NOTIFYICONDATAW,
    };
    use windows::Win32::UI::WindowsAndMessaging::{
        AppendMenuW, CreateIconFromResourceEx, CreatePopupMenu, CreateWindowExW, DefWindowProcW,
        DestroyIcon, DestroyMenu, DestroyWindow, DispatchMessageW, GetCursorPos, GetMessageW,
        GetSystemMetrics, LoadCursorW, LookupIconIdFromDirectoryEx, PostMessageW, PostQuitMessage,
        RegisterClassW, SetForegroundWindow, TrackPopupMenu, TranslateMessage, CW_USEDEFAULT,
        HICON, HMENU, IDC_ARROW, IMAGE_FLAGS, MENU_ITEM_FLAGS, MF_CHECKED, MF_SEPARATOR, MF_STRING,
        MSG, SM_CXSMICON, SM_CYSMICON, TPM_BOTTOMALIGN, TPM_RIGHTBUTTON, WINDOW_EX_STYLE, WM_APP,
        WM_COMMAND, WM_DESTROY, WM_LBUTTONUP, WM_RBUTTONUP, WNDCLASSW, WS_OVERLAPPEDWINDOW,
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
    /// Menu command: quit the tray helper.
    const ID_QUIT: usize = 1002;
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

    /// Pop up the context menu at the cursor.
    unsafe fn show_menu(hwnd: HWND) {
        let enabled = STATE.with(|s| s.borrow().as_ref().map(|st| st.enabled).unwrap_or(true));

        let menu = CreatePopupMenu().unwrap_or_default();
        if menu.is_invalid() {
            return;
        }
        // Checkable "Fernsteuerung aktiv": checked == currently on; click toggles it.
        let toggle_flags = MF_STRING
            | if enabled {
                MF_CHECKED
            } else {
                MENU_ITEM_FLAGS(0)
            };
        let _ = AppendMenuW(menu, toggle_flags, ID_TOGGLE, w!("Fernsteuerung aktiv"));
        let _ = AppendMenuW(menu, MF_SEPARATOR, 0, PCWSTR::null());
        let _ = AppendMenuW(menu, MF_STRING, ID_QUIT, w!("Beenden"));

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
                        ID_QUIT => {
                            let _ = DestroyWindow(hwnd);
                        }
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
