//! `screen_capture` — capture the primary display to a base64 PNG.
//!
//! Windows-only via GDI (BitBlt to a DIB, then PNG-encode). Off Windows this
//! returns `unsupported` per the platform rule.
//!
//! **Session 0 routing.** A GDI `BitBlt` only sees the desktop of the *calling*
//! session. The agent normally runs as a LocalSystem service in **session 0**,
//! which has no visible desktop, so a direct grab there returns a black frame.
//! When we detect session 0 we therefore delegate to the tray helper (which runs
//! in the interactive user session) over a local named pipe; see
//! [`crate::screencap_ipc`] and ADR-0018. A foreground/dev run (session ≠ 0) grabs
//! directly.

use serde_json::Value;

use crate::protocol::ErrorCode;

/// `screen_capture` — `{image_b64, format:"png"}`.
pub fn capture(_args: Value) -> Result<Value, (ErrorCode, String)> {
    #[cfg(windows)]
    {
        windows_impl::capture()
    }
    #[cfg(not(windows))]
    {
        Err((
            ErrorCode::Unsupported,
            "screen_capture is only available on Windows".to_string(),
        ))
    }
}

/// Wrap raw PNG bytes in the `{image_b64, format:"png"}` response shape.
#[cfg_attr(not(windows), allow(dead_code))]
pub(crate) fn png_to_response(png: &[u8]) -> Value {
    use base64::Engine as _;
    let b64 = base64::engine::general_purpose::STANDARD.encode(png);
    serde_json::json!({ "image_b64": b64, "format": "png" })
}

/// Grab the primary display as raw PNG bytes (Windows, interactive session only).
/// Used by the tray's screen-capture responder ([`crate::screencap_ipc`]).
#[cfg(windows)]
pub(crate) use windows_impl::grab_primary_png;

/// Encode a GDI top-down 32-bit BGRA buffer as an RGBA PNG.
///
/// Portable (not cfg-gated) so it can be unit-tested on Linux. GDI `BitBlt`
/// produces BGRA with the alpha channel left at 0, so we swap B/R and force
/// alpha to 255 (fully opaque) before encoding.
///
/// `bgra` must be exactly `width * height * 4` bytes, laid out top-down.
///
/// On non-Windows builds `windows_impl` is compiled out, so the only callers are
/// the unit tests; `allow(dead_code)` keeps the portable `cargo build` clean.
#[cfg_attr(not(windows), allow(dead_code))]
fn encode_png(width: u32, height: u32, bgra: &[u8]) -> Result<Vec<u8>, String> {
    let expected = (width as usize)
        .checked_mul(height as usize)
        .and_then(|p| p.checked_mul(4))
        .ok_or_else(|| "image dimensions overflow".to_string())?;
    if bgra.len() != expected {
        return Err(format!(
            "buffer size mismatch: got {} bytes, expected {} ({}x{}x4)",
            bgra.len(),
            expected,
            width,
            height
        ));
    }

    // BGRA -> RGBA, alpha forced opaque. Source layout is [B, G, R, A].
    // Both remainders are empty by construction: `expected` is width*height*4,
    // and `bgra.len()` was checked equal to it above.
    let mut rgba = vec![0u8; expected];
    let (dst_pixels, _) = rgba.as_chunks_mut::<4>();
    let (src_pixels, _) = bgra.as_chunks::<4>();
    for (dst, src) in dst_pixels.iter_mut().zip(src_pixels) {
        dst[0] = src[2]; // R
        dst[1] = src[1]; // G
        dst[2] = src[0]; // B
        dst[3] = 255; // A (GDI BitBlt leaves this 0)
    }

    let mut out: Vec<u8> = Vec::new();
    {
        let mut encoder = png::Encoder::new(&mut out, width, height);
        encoder.set_color(png::ColorType::Rgba);
        encoder.set_depth(png::BitDepth::Eight);
        let mut writer = encoder
            .write_header()
            .map_err(|e| format!("png write_header failed: {e}"))?;
        writer
            .write_image_data(&rgba)
            .map_err(|e| format!("png write_image_data failed: {e}"))?;
    }
    Ok(out)
}

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use core::ffi::c_void;

    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT,
        DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ, SRCCOPY,
    };
    use windows::Win32::System::RemoteDesktop::ProcessIdToSessionId;
    use windows::Win32::System::Threading::GetCurrentProcessId;
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

    /// True if this process runs in **session 0** (the non-interactive services
    /// session). Such a process cannot grab the user's desktop directly.
    fn in_session_zero() -> bool {
        let mut session: u32 = 0;
        // SAFETY: writes a single u32 we own; GetCurrentProcessId is infallible.
        let ok = unsafe { ProcessIdToSessionId(GetCurrentProcessId(), &mut session) };
        // If the lookup fails, assume interactive (direct grab) rather than block.
        ok.is_ok() && session == 0
    }

    /// `screen_capture`: grab directly when interactive, else delegate to the tray.
    pub fn capture() -> Result<Value, (ErrorCode, String)> {
        if in_session_zero() {
            // Running as the session-0 service: the desktop lives in the user's
            // session, so ask the tray helper to capture it (ADR-0018).
            return crate::screencap_ipc::capture_via_tray();
        }
        let png = grab_primary_png().map_err(|e| (ErrorCode::Internal, e))?;
        Ok(png_to_response(&png))
    }

    /// Grab the primary display via GDI and return raw PNG bytes.
    ///
    /// `GetDC(NULL)` → `CreateCompatibleDC`/`CreateCompatibleBitmap` → `BitBlt` →
    /// `GetDIBits` into a BGRA buffer → `encode_png`. Only meaningful when called
    /// from a session with a visible desktop (the tray, or a foreground run).
    ///
    /// virtual-screen (all monitors) via SM_*VIRTUALSCREEN is a future option.
    pub(crate) fn grab_primary_png() -> Result<Vec<u8>, String> {
        // SAFETY: All pointers below are validated for null before use, and every
        // GDI object created is released in `cleanup` on every return path.
        unsafe {
            let width = GetSystemMetrics(SM_CXSCREEN);
            let height = GetSystemMetrics(SM_CYSCREEN);
            if width <= 0 || height <= 0 {
                return Err(format!("invalid screen metrics: {width}x{height}"));
            }

            // `GetDC(None)` returns the DC for the entire screen.
            let screen_dc: HDC = GetDC(None);
            if screen_dc.is_invalid() {
                return Err("GetDC(screen) returned null".into());
            }

            // From here on, ensure cleanup on every error path.
            let mem_dc: HDC = CreateCompatibleDC(Some(screen_dc));
            if mem_dc.is_invalid() {
                ReleaseDC(None, screen_dc);
                return Err("CreateCompatibleDC failed".into());
            }

            let hbmp: HBITMAP = CreateCompatibleBitmap(screen_dc, width, height);
            if hbmp.is_invalid() {
                let _ = DeleteDC(mem_dc);
                ReleaseDC(None, screen_dc);
                return Err("CreateCompatibleBitmap failed".into());
            }

            // Helper that frees everything; called before each fallible return below.
            let cleanup = |hbmp: HBITMAP, mem_dc: HDC, screen_dc: HDC| {
                let _ = DeleteObject(HGDIOBJ(hbmp.0));
                let _ = DeleteDC(mem_dc);
                ReleaseDC(None, screen_dc);
            };

            let old = SelectObject(mem_dc, HGDIOBJ(hbmp.0));
            if old.is_invalid() {
                cleanup(hbmp, mem_dc, screen_dc);
                return Err("SelectObject failed".into());
            }

            // CAPTUREBLT includes layered/transparent windows in the grab.
            if let Err(e) = BitBlt(
                mem_dc,
                0,
                0,
                width,
                height,
                Some(screen_dc),
                0,
                0,
                SRCCOPY | CAPTUREBLT,
            ) {
                cleanup(hbmp, mem_dc, screen_dc);
                return Err(format!("BitBlt failed: {e}"));
            }

            // Negative height => top-down rows; 32 bpp, uncompressed BGRA.
            let mut bmi = BITMAPINFO {
                bmiHeader: BITMAPINFOHEADER {
                    biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                    biWidth: width,
                    biHeight: -height,
                    biPlanes: 1,
                    biBitCount: 32,
                    biCompression: BI_RGB.0,
                    ..Default::default()
                },
                ..Default::default()
            };

            let pixel_count = (width as usize) * (height as usize);
            let mut buf = vec![0u8; pixel_count * 4];

            let scanned = GetDIBits(
                mem_dc,
                hbmp,
                0,
                height as u32,
                Some(buf.as_mut_ptr() as *mut c_void),
                &mut bmi,
                DIB_RGB_COLORS,
            );
            if scanned == 0 {
                cleanup(hbmp, mem_dc, screen_dc);
                return Err("GetDIBits returned 0".into());
            }

            // Pixels are copied out; release GDI resources before encoding.
            cleanup(hbmp, mem_dc, screen_dc);

            super::encode_png(width as u32, height as u32, &buf)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(not(windows))]
    #[test]
    fn capture_unsupported_off_windows() {
        let err = capture(serde_json::json!({})).unwrap_err();
        assert_eq!(err.0, ErrorCode::Unsupported);
    }

    /// PNG file signature: every PNG starts with these 8 bytes.
    const PNG_SIGNATURE: [u8; 8] = [0x89, b'P', b'N', b'G', 0x0D, 0x0A, 0x1A, 0x0A];

    #[test]
    fn encode_png_produces_valid_png() {
        // 2x2 top-down BGRA buffer (4 pixels). Values are arbitrary; alpha is 0
        // to prove encode_png forces it opaque.
        let bgra: Vec<u8> = vec![
            10, 20, 30, 0, // px (0,0): B=10 G=20 R=30 A=0
            40, 50, 60, 0, // px (1,0)
            70, 80, 90, 0, // px (0,1)
            100, 110, 120, 0, // px (1,1)
        ];
        let png = encode_png(2, 2, &bgra).expect("encode_png should succeed");

        assert_eq!(&png[..8], &PNG_SIGNATURE, "output must be a PNG");

        // Round-trip: decode and confirm dimensions and the B/R swap + opaque alpha.
        let decoder = png::Decoder::new(std::io::Cursor::new(&png));
        let mut reader = decoder.read_info().expect("readable PNG");
        let info = reader.info();
        assert_eq!(info.width, 2);
        assert_eq!(info.height, 2);
        assert_eq!(info.color_type, png::ColorType::Rgba);

        let mut out = vec![
            0u8;
            reader
                .output_buffer_size()
                .expect("decoded PNG frame size is known")
        ];
        let frame = reader.next_frame(&mut out).expect("decode frame");
        let bytes = &out[..frame.buffer_size()];
        // First pixel: BGRA (10,20,30,0) -> RGBA (30,20,10,255).
        assert_eq!(&bytes[0..4], &[30, 20, 10, 255]);
    }

    #[test]
    fn encode_png_rejects_size_mismatch() {
        // Claims 2x2 (needs 16 bytes) but only provides 4.
        let err = encode_png(2, 2, &[0u8; 4]).unwrap_err();
        assert!(err.contains("mismatch"), "unexpected error: {err}");
    }

    /// Zero-area (width or height 0, matching empty buffer) and a lopsided-but-matching
    /// buffer must not panic either — random buffer lengths in the loop below almost never
    /// happen to exactly match `width*height*4`, so this exercises that path directly.
    #[test]
    fn encode_png_handles_zero_dimensions_and_matching_lopsided_buffer() {
        assert!(std::panic::catch_unwind(|| encode_png(0, 0, &[])).is_ok());
        assert!(std::panic::catch_unwind(|| encode_png(0, 5000, &[])).is_ok());
        assert!(std::panic::catch_unwind(|| encode_png(5000, 0, &[])).is_ok());
        let buf = vec![0u8; 100_000 * 4];
        assert!(std::panic::catch_unwind(|| encode_png(100_000, 1, &buf)).is_ok());
    }

    /// Randomized dimensions/buffer-length combinations, including zero, huge, and
    /// overflow-triggering sizes, must only ever return `Err`, never panic.
    #[test]
    fn encode_png_never_panics_on_random_dimensions_and_buffers() {
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
        let mut rng = Rng(0xDEADBEEF);
        let interesting: [u32; 7] = [0, 1, 2, 0xFFFF_FFFF, 0x7FFF_FFFF, 65536, 100_000];
        for iter in 0..2000u32 {
            let width = if rng.next().is_multiple_of(2) {
                interesting[(rng.next() as usize) % interesting.len()]
            } else {
                (rng.next() % 5000) as u32
            };
            let height = if rng.next().is_multiple_of(2) {
                interesting[(rng.next() as usize) % interesting.len()]
            } else {
                (rng.next() % 5000) as u32
            };
            // Buffer length: sometimes correct, sometimes wildly off.
            let buf_len = (rng.next() % 20_000) as usize;
            let buf = vec![0u8; buf_len];
            let result = std::panic::catch_unwind(|| encode_png(width, height, &buf));
            if result.is_err() {
                panic!("iter {iter}: panic on width={width} height={height} buf_len={buf_len}");
            }
        }
    }
}
