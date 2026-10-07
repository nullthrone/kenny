//! Shared `nvidia-smi` helpers for the `thermals`, `gpu` and `fans` collectors.
//!
//! `nvidia-smi` ships with the NVIDIA driver on both Windows and Linux and needs no
//! admin rights, so every collector that wants NVIDIA facts asks it through
//! [`query`]: one bounded `--query-gpu=<fields> --format=csv,noheader,nounits` run,
//! parsed into cells. A cell the driver does not report (`[N/A]`, `[Not Supported]`)
//! is `None`, never a string a caller could mistake for a value.
//!
//! Portable: the parser and the typed cell readers are pure, and [`query`] reads `None`
//! on any host without `nvidia-smi` on `PATH`.

// Consumers (`gpu`, `fans`, the Windows `thermals` source) call into this module
// selectively; not every helper is used on every platform.
#![allow(dead_code)]

use super::proc;

/// One parsed `nvidia-smi` row: a cell per queried field, `None` where the driver
/// reported nothing.
pub type Row = Vec<Option<String>>;

/// Parse `--format=csv,noheader,nounits` output: one row per non-blank line, one cell
/// per comma-separated field, trimmed. `[N/A]`, `N/A`, `[Not Supported]` and the empty
/// string read as `None`, as does any other bracketed marker nvidia-smi prints in place
/// of a value (`[Insufficient Permissions]`, `[Unknown Error]`). GPU model names and
/// the other queryable strings contain no commas.
pub fn parse_csv(out: &str) -> Vec<Row> {
    out.lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(|line| line.split(',').map(cell).collect())
        .collect()
}

/// One trimmed cell, `None` for the "no value" markers.
fn cell(raw: &str) -> Option<String> {
    let raw = raw.trim();
    let is_marker = raw.is_empty()
        || raw.eq_ignore_ascii_case("n/a")
        || raw.eq_ignore_ascii_case("not supported")
        || (raw.starts_with('[') && raw.ends_with(']'));
    (!is_marker).then(|| raw.to_string())
}

/// The cell at `idx` as a string slice; `None` when the row is shorter or the cell empty.
pub fn cell_str(row: &[Option<String>], idx: usize) -> Option<&str> {
    row.get(idx)?.as_deref()
}

/// The cell at `idx` as a finite number (`53`, `38.52`).
pub fn cell_f64(row: &[Option<String>], idx: usize) -> Option<f64> {
    cell_str(row, idx)?
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite())
}

/// The cell at `idx` as a non-negative integer (`16`, `4096`).
pub fn cell_u64(row: &[Option<String>], idx: usize) -> Option<u64> {
    cell_str(row, idx)?.parse().ok()
}

/// The cell at `idx` as a boolean. nvidia-smi spells flags `Active` / `Not Active`
/// (clock-event reasons), `Yes` / `No` and `Enabled` / `Disabled` (ECC mode, retired
/// pages pending); anything else is `None`.
pub fn cell_bool(row: &[Option<String>], idx: usize) -> Option<bool> {
    let text = cell_str(row, idx)?;
    match text.to_ascii_lowercase().as_str() {
        "active" | "yes" | "enabled" | "true" => Some(true),
        "not active" | "no" | "disabled" | "false" => Some(false),
        _ => None,
    }
}

/// Run `nvidia-smi --query-gpu=<fields> --format=csv,noheader,nounits` within
/// [`proc::PROBE_BUDGET`] and parse it: one row per GPU, one cell per field.
///
/// `None` when `nvidia-smi` is missing, times out, exits non-zero (a driver that is
/// not loaded exits 9) or prints nothing. A row whose cell count does not match
/// `fields` is dropped rather than misaligned.
pub fn query(fields: &[&str]) -> Option<Vec<Vec<Option<String>>>> {
    let query = format!("--query-gpu={}", fields.join(","));
    let out = proc::run_ok(
        "nvidia-smi",
        &[&query, "--format=csv,noheader,nounits"],
        proc::PROBE_BUDGET,
    )?;
    let rows: Vec<Row> = parse_csv(&out)
        .into_iter()
        .filter(|row| row.len() == fields.len())
        .collect();
    (!rows.is_empty()).then_some(rows)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `nvidia-smi --query-gpu=name,temperature.gpu,power.draw,pcie.link.gen.current,
    /// clocks_throttle_reasons.hw_slowdown,ecc.mode.current,fan.speed`
    /// on a consumer card and a datacenter card.
    const CANNED: &str = "\
NVIDIA GeForce RTX 4080, 47, 38.52, 1, Not Active, [N/A], [N/A]
NVIDIA A100-PCIE-40GB, 61, 74.10, 4, Active, Enabled, [Not Supported]

";

    #[test]
    fn parse_csv_maps_no_value_markers_to_none() {
        let rows = parse_csv(CANNED);
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0].len(), 7);
        assert_eq!(rows[0][0].as_deref(), Some("NVIDIA GeForce RTX 4080"));
        assert_eq!(rows[0][5], None);
        assert_eq!(rows[0][6], None);
        assert_eq!(rows[1][5].as_deref(), Some("Enabled"));
        assert_eq!(rows[1][6], None);
    }

    #[test]
    fn every_no_value_spelling_is_none() {
        let rows = parse_csv("[N/A], N/A, [Not Supported], , [Insufficient Permissions], x\n");
        assert_eq!(
            rows,
            vec![vec![None, None, None, None, None, Some("x".to_string())]]
        );
    }

    #[test]
    fn blank_lines_yield_no_rows() {
        assert!(parse_csv("").is_empty());
        assert!(parse_csv("\n  \n").is_empty());
    }

    #[test]
    fn numeric_cells_parse_and_na_is_none() {
        let rows = parse_csv(CANNED);
        assert_eq!(cell_f64(&rows[0], 1), Some(47.0));
        assert_eq!(cell_f64(&rows[0], 2), Some(38.52));
        assert_eq!(cell_u64(&rows[1], 3), Some(4));
        // A name is not a number, a `[N/A]` cell is not zero, an index past the end is none.
        assert_eq!(cell_f64(&rows[0], 0), None);
        assert_eq!(cell_f64(&rows[0], 6), None);
        assert_eq!(cell_u64(&rows[0], 6), None);
        assert_eq!(cell_u64(&rows[0], 99), None);
        // A fraction is not a u64, a negative is not either.
        assert_eq!(cell_u64(&rows[0], 2), None);
        assert_eq!(cell_u64(&parse_csv("-3\n")[0], 0), None);
    }

    #[test]
    fn boolean_cells_accept_the_driver_spellings() {
        let rows = parse_csv("Active, Not Active, Yes, No, Enabled, Disabled, [N/A], maybe\n");
        let row = &rows[0];
        let got: Vec<Option<bool>> = (0..8).map(|i| cell_bool(row, i)).collect();
        assert_eq!(
            got,
            vec![
                Some(true),
                Some(false),
                Some(true),
                Some(false),
                Some(true),
                Some(false),
                None,
                None
            ]
        );
    }

    #[test]
    fn query_is_none_without_nvidia_smi() {
        // CI hosts have no NVIDIA driver; a host that does would return rows, which is
        // equally fine — only a panic or a misshapen row would be wrong.
        if let Some(rows) = query(&["name", "temperature.gpu"]) {
            assert!(rows.iter().all(|r| r.len() == 2));
        }
    }
}
