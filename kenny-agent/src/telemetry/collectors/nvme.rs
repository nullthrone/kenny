//! NVMe SMART / health information log (log page `0x02`) — pure, portable decoder.
//!
//! The raw 512-byte log is read per platform by `disk_smart` (a `DeviceIoControl` query
//! on Windows, the admin-command ioctl on Linux); this module only turns the bytes into
//! the contract's `nvme` object (`docs/protocol.md`, the `disk_smart` section), so both
//! readers share one decoder that is tested without hardware.

use serde_json::{json, Value};

/// Size of the SMART / health information log page in bytes.
pub const HEALTH_LOG_LEN: usize = 512;

/// The NVMe log page identifier of the SMART / health information log.
pub const HEALTH_LOG_ID: u8 = 0x02;

/// The decoded SMART / health log. The 128-bit counters of the log saturate to `u64`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct NvmeHealth {
    /// Bitfield: bit 0 spare below threshold, 1 temperature, 2 reliability degraded,
    /// 3 read-only, 4 volatile-memory backup failed.
    pub critical_warning: u8,
    /// Composite temperature in °C; `None` when the controller reports 0 K (unset).
    pub temperature_c: Option<i64>,
    pub available_spare: u8,
    pub available_spare_threshold: u8,
    /// Percent of rated endurance used; may exceed 100.
    pub percentage_used: u8,
    pub data_units_written: u64,
    pub power_on_hours: u64,
    pub unsafe_shutdowns: u64,
    pub media_errors: u64,
    pub error_log_entries: u64,
}

/// A 128-bit little-endian counter at `offset`, saturated to `u64::MAX`.
fn counter128(buf: &[u8; HEALTH_LOG_LEN], offset: usize) -> u64 {
    let (lo, hi) = buf[offset..offset + 16].split_at(8);
    let hi = u64::from_le_bytes(hi.try_into().expect("8 bytes"));
    if hi != 0 {
        u64::MAX
    } else {
        u64::from_le_bytes(lo.try_into().expect("8 bytes"))
    }
}

/// Decode the 512-byte SMART / health information log.
pub fn decode_health_log(buf: &[u8; HEALTH_LOG_LEN]) -> NvmeHealth {
    let kelvin = u16::from_le_bytes([buf[1], buf[2]]);
    NvmeHealth {
        critical_warning: buf[0],
        temperature_c: (kelvin != 0).then(|| i64::from(kelvin) - 273),
        available_spare: buf[3],
        available_spare_threshold: buf[4],
        percentage_used: buf[5],
        data_units_written: counter128(buf, 48),
        power_on_hours: counter128(buf, 128),
        unsafe_shutdowns: counter128(buf, 144),
        media_errors: counter128(buf, 160),
        error_log_entries: counter128(buf, 176),
    }
}

impl NvmeHealth {
    /// The contract's `nvme` object.
    pub fn to_json(self) -> Value {
        json!({
            "critical_warning": self.critical_warning,
            "available_spare": self.available_spare,
            "available_spare_threshold": self.available_spare_threshold,
            "percentage_used": self.percentage_used,
            "media_errors": self.media_errors,
            "unsafe_shutdowns": self.unsafe_shutdowns,
            "error_log_entries": self.error_log_entries,
            "data_units_written": self.data_units_written,
            "power_on_hours": self.power_on_hours,
            "temperature_c": self.temperature_c,
        })
    }
}

/// Decode a log returned as a byte slice; `None` unless it is at least 512 bytes.
#[cfg_attr(not(windows), allow(dead_code))] // the Windows reader hands over a byte buffer
pub fn decode_health_slice(bytes: &[u8]) -> Option<NvmeHealth> {
    let buf: &[u8; HEALTH_LOG_LEN] = bytes.get(..HEALTH_LOG_LEN)?.try_into().ok()?;
    Some(decode_health_log(buf))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn put128(buf: &mut [u8; HEALTH_LOG_LEN], offset: usize, lo: u64, hi: u64) {
        buf[offset..offset + 8].copy_from_slice(&lo.to_le_bytes());
        buf[offset + 8..offset + 16].copy_from_slice(&hi.to_le_bytes());
    }

    /// A log shaped like the contract's `WD_BLACK SN850X` example.
    fn sample() -> [u8; HEALTH_LOG_LEN] {
        let mut b = [0u8; HEALTH_LOG_LEN];
        b[0] = 0b0000_0010;
        b[1..3].copy_from_slice(&314u16.to_le_bytes()); // 314 K = 41 °C
        b[3] = 100;
        b[4] = 10;
        b[5] = 2;
        put128(&mut b, 48, 18_734_512, 0);
        put128(&mut b, 128, 1520, 0);
        put128(&mut b, 144, 14, 0);
        put128(&mut b, 160, 3, 0);
        put128(&mut b, 176, 7, 0);
        // Fields the decoder must not mistake for the above.
        put128(&mut b, 32, 111, 0); // data units read
        put128(&mut b, 64, 222, 0); // host read commands
        put128(&mut b, 112, 333, 0); // controller busy time
        b
    }

    #[test]
    fn decodes_every_field_from_its_offset() {
        let h = decode_health_log(&sample());
        assert_eq!(h.critical_warning, 2);
        assert_eq!(h.temperature_c, Some(41));
        assert_eq!(h.available_spare, 100);
        assert_eq!(h.available_spare_threshold, 10);
        assert_eq!(h.percentage_used, 2);
        assert_eq!(h.data_units_written, 18_734_512);
        assert_eq!(h.power_on_hours, 1520);
        assert_eq!(h.unsafe_shutdowns, 14);
        assert_eq!(h.media_errors, 3);
        assert_eq!(h.error_log_entries, 7);
    }

    #[test]
    fn json_form_is_the_contract_object() {
        let mut b = sample();
        b[0] = 0;
        put128(&mut b, 160, 0, 0);
        put128(&mut b, 176, 0, 0);
        let v = decode_health_log(&b).to_json();
        assert_eq!(
            v,
            json!({
                "critical_warning": 0, "available_spare": 100,
                "available_spare_threshold": 10, "percentage_used": 2,
                "media_errors": 0, "unsafe_shutdowns": 14, "error_log_entries": 0,
                "data_units_written": 18_734_512, "power_on_hours": 1520,
                "temperature_c": 41,
            })
        );
    }

    #[test]
    fn a_counter_with_high_bits_set_saturates() {
        let mut b = sample();
        put128(&mut b, 160, 5, 1); // 2^64 + 5
        put128(&mut b, 144, u64::MAX, 0); // exactly u64::MAX does not overflow
        put128(&mut b, 128, 0, u64::MAX);
        let h = decode_health_log(&b);
        assert_eq!(h.media_errors, u64::MAX);
        assert_eq!(h.unsafe_shutdowns, u64::MAX);
        assert_eq!(h.power_on_hours, u64::MAX);
        // Neighbouring counters are unaffected.
        assert_eq!(h.error_log_entries, 7);
        assert_eq!(h.data_units_written, 18_734_512);
    }

    #[test]
    fn counters_are_little_endian_across_all_eight_low_bytes() {
        let mut b = [0u8; HEALTH_LOG_LEN];
        b[176..184].copy_from_slice(&[0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08]);
        assert_eq!(
            decode_health_log(&b).error_log_entries,
            0x0807_0605_0403_0201
        );
    }

    #[test]
    fn temperature_is_kelvin_minus_273_and_zero_is_unset() {
        let mut b = [0u8; HEALTH_LOG_LEN];
        b[1..3].copy_from_slice(&273u16.to_le_bytes());
        assert_eq!(decode_health_log(&b).temperature_c, Some(0));
        b[1..3].copy_from_slice(&0x0161u16.to_le_bytes()); // 353 K, high byte used
        assert_eq!(decode_health_log(&b).temperature_c, Some(80));
        b[1..3].copy_from_slice(&200u16.to_le_bytes()); // below freezing
        assert_eq!(decode_health_log(&b).temperature_c, Some(-73));
        b[1..3].copy_from_slice(&0u16.to_le_bytes());
        let h = decode_health_log(&b);
        assert_eq!(h.temperature_c, None);
        assert_eq!(h.to_json()["temperature_c"], Value::Null);
    }

    #[test]
    fn percentage_used_above_100_is_kept() {
        let mut b = [0u8; HEALTH_LOG_LEN];
        b[5] = 255;
        assert_eq!(decode_health_log(&b).percentage_used, 255);
    }

    #[test]
    fn an_all_zero_log_decodes_to_zeros() {
        let h = decode_health_log(&[0u8; HEALTH_LOG_LEN]);
        assert_eq!(h.critical_warning, 0);
        assert_eq!(h.media_errors, 0);
        assert_eq!(h.temperature_c, None);
    }

    #[test]
    fn slice_decoding_needs_the_full_page() {
        assert!(decode_health_slice(&[0u8; 511]).is_none());
        assert_eq!(
            decode_health_slice(&sample()).map(|h| h.power_on_hours),
            Some(1520)
        );
        let mut longer = sample().to_vec();
        longer.extend_from_slice(&[0xFF; 16]);
        assert_eq!(
            decode_health_slice(&longer).map(|h| h.power_on_hours),
            Some(1520)
        );
    }
}
