//! `screen_capture` — capture the primary display to a base64 PNG.
//!
//! Windows-only via GDI (BitBlt to a DIB, then PNG-encode). Off Windows this
//! returns `unsupported` per the platform rule.

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
    let mut rgba = vec![0u8; expected];
    for (dst, src) in rgba.chunks_exact_mut(4).zip(bgra.chunks_exact(4)) {
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
    use base64::Engine as _;
    use core::ffi::c_void;
    use serde_json::json;

    use windows::Win32::Foundation::HWND;
    use windows::Win32::Graphics::Gdi::{
        BitBlt, CreateCompatibleBitmap, CreateCompatibleDC, DeleteDC, DeleteObject, GetDC,
        GetDIBits, ReleaseDC, SelectObject, BITMAPINFO, BITMAPINFOHEADER, BI_RGB, CAPTUREBLT,
        DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ, SRCCOPY,
    };
    use windows::Win32::UI::WindowsAndMessaging::{GetSystemMetrics, SM_CXSCREEN, SM_CYSCREEN};

    /// Real impl: `GetDC(NULL)` → `CreateCompatibleDC`/`CreateCompatibleBitmap` →
    /// `BitBlt` the primary screen → `GetDIBits` into a buffer → PNG-encode →
    /// base64. Uses the `windows` crate (`Win32_Graphics_Gdi`).
    ///
    /// virtual-screen (all monitors) via SM_*VIRTUALSCREEN is a future option.
    pub fn capture() -> Result<Value, (ErrorCode, String)> {
        // SAFETY: All pointers below are validated for null before use, and every
        // GDI object created is released in `cleanup` on every return path.
        unsafe {
            let width = GetSystemMetrics(SM_CXSCREEN);
            let height = GetSystemMetrics(SM_CYSCREEN);
            if width <= 0 || height <= 0 {
                return Err((
                    ErrorCode::Internal,
                    format!("invalid screen metrics: {width}x{height}"),
                ));
            }

            // `GetDC(None)` returns the DC for the entire screen.
            let screen_dc: HDC = GetDC(HWND(std::ptr::null_mut()));
            if screen_dc.is_invalid() {
                return Err((ErrorCode::Internal, "GetDC(screen) returned null".into()));
            }

            // From here on, ensure cleanup on every error path.
            let mem_dc: HDC = CreateCompatibleDC(screen_dc);
            if mem_dc.is_invalid() {
                ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
                return Err((ErrorCode::Internal, "CreateCompatibleDC failed".into()));
            }

            let hbmp: HBITMAP = CreateCompatibleBitmap(screen_dc, width, height);
            if hbmp.is_invalid() {
                let _ = DeleteDC(mem_dc);
                ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
                return Err((ErrorCode::Internal, "CreateCompatibleBitmap failed".into()));
            }

            // Helper that frees everything; called before each fallible return below.
            let cleanup = |hbmp: HBITMAP, mem_dc: HDC, screen_dc: HDC| {
                let _ = DeleteObject(HGDIOBJ(hbmp.0));
                let _ = DeleteDC(mem_dc);
                ReleaseDC(HWND(std::ptr::null_mut()), screen_dc);
            };

            let old = SelectObject(mem_dc, HGDIOBJ(hbmp.0));
            if old.is_invalid() {
                cleanup(hbmp, mem_dc, screen_dc);
                return Err((ErrorCode::Internal, "SelectObject failed".into()));
            }

            // CAPTUREBLT includes layered/transparent windows in the grab.
            if let Err(e) = BitBlt(
                mem_dc,
                0,
                0,
                width,
                height,
                screen_dc,
                0,
                0,
                SRCCOPY | CAPTUREBLT,
            ) {
                cleanup(hbmp, mem_dc, screen_dc);
                return Err((ErrorCode::Internal, format!("BitBlt failed: {e}")));
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
                return Err((ErrorCode::Internal, "GetDIBits returned 0".into()));
            }

            // Pixels are copied out; release GDI resources before encoding.
            cleanup(hbmp, mem_dc, screen_dc);

            let png_bytes = super::encode_png(width as u32, height as u32, &buf)
                .map_err(|e| (ErrorCode::Internal, e))?;
            let b64 = base64::engine::general_purpose::STANDARD.encode(png_bytes);
            Ok(json!({ "image_b64": b64, "format": "png" }))
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

        let mut out = vec![0u8; reader.output_buffer_size()];
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
}
