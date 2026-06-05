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

#[cfg(windows)]
mod windows_impl {
    use super::*;
    use serde_json::json;

    /// Real impl: `GetDC(NULL)` → `CreateCompatibleDC`/`CreateCompatibleBitmap` →
    /// `BitBlt` the virtual screen → `GetDIBits` into a buffer → PNG-encode →
    /// base64. Uses the `windows` crate (`Win32_Graphics_Gdi`).
    pub fn capture() -> Result<Value, (ErrorCode, String)> {
        // TODO(windows): implement the GDI BitBlt → PNG → base64 pipeline.
        // The scaffold returns an empty (1x1) PNG so the wire shape is exercised.
        let png_1x1 = base64::engine::general_purpose::STANDARD.encode(EMPTY_PNG);
        Ok(json!({ "image_b64": png_1x1, "format": "png" }))
    }

    /// Minimal valid 1x1 transparent PNG (placeholder until BitBlt is wired up).
    const EMPTY_PNG: &[u8] = &[
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00, 0x1F,
        0x15, 0xC4, 0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00,
        0x01, 0x00, 0x00, 0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
        0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ];

    use base64::Engine as _;
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
}
