//! `disk` section — per-volume capacity. Portable via `sysinfo`.
//!
//! The section reports only volumes with their own capacity: overlay, pseudo and image
//! filesystems and per-container mounts are dropped on non-Windows hosts.

use std::path::{Path, PathBuf};

use serde_json::{json, Value};
use sysinfo::Disks;

use crate::protocol::Status;
use crate::telemetry::Section;

/// Percent of `total` in use, given `total`/`free` bytes as `sysinfo` reports them.
///
/// `free` is not guaranteed to be `<= total`: quota-managed, thin-provisioned, or
/// network/overlay filesystems can report a larger available space than total (and a
/// resize race can do the same even for a plain local disk), so this saturates
/// instead of underflowing the `u64` subtraction.
fn percent_used(total: u64, free: u64) -> u64 {
    if total > 0 {
        ((total.saturating_sub(free) as f64 / total as f64) * 100.0).round() as u64
    } else {
        0
    }
}

/// One mount as `sysinfo` reports it. `file_system` only drives the section filter and
/// never goes on the wire.
struct Volume {
    mount: PathBuf,
    file_system: String,
    total: u64,
    free: u64,
}

impl Volume {
    fn to_json(&self) -> Value {
        json!({
            "mount": self.mount.to_string_lossy(),
            "total_bytes": self.total,
            "free_bytes": self.free,
            "percent_used": percent_used(self.total, self.free),
        })
    }
}

fn list() -> Vec<Volume> {
    let disks = Disks::new_with_refreshed_list();
    disks
        .list()
        .iter()
        .map(|d| Volume {
            mount: d.mount_point().to_path_buf(),
            file_system: d.file_system().to_string_lossy().into_owned(),
            total: d.total_space(),
            free: d.available_space(),
        })
        .collect()
}

/// Filesystem types that hold no capacity of their own: overlays share their backing
/// filesystem, the rest are kernel/memory pseudo filesystems or read-only images that
/// always read 100 % full.
const NON_CAPACITY_FILE_SYSTEMS: &[&str] = &[
    "overlay",
    "overlayfs",
    "aufs",
    "fuse-overlayfs",
    "tmpfs",
    "devtmpfs",
    "ramfs",
    "squashfs",
    "iso9660",
    "proc",
    "sysfs",
    "cgroup",
    "cgroup2",
    "devpts",
    "mqueue",
    "hugetlbfs",
    "debugfs",
    "tracefs",
    "securityfs",
    "pstore",
    "bpf",
    "configfs",
    "fusectl",
    "autofs",
    "binfmt_misc",
    "rpc_pipefs",
    "nsfs",
    "efivarfs",
    "fuse.lxcfs",
];

/// Container-runtime roots. Mounts strictly below them are per-container views of a
/// filesystem reported elsewhere; a partition mounted at a root itself is a real volume.
const CONTAINER_ROOTS: &[&str] = &[
    "/var/lib/docker",
    "/var/lib/containerd",
    "/var/lib/containers",
    "/run/containerd",
    "/run/docker",
];

/// True for a mount that carries its own capacity: not an overlay, pseudo or image
/// filesystem, and not a per-container mount under a container-runtime root.
fn is_capacity_volume(file_system: &str, mount: &Path) -> bool {
    if NON_CAPACITY_FILE_SYSTEMS
        .iter()
        .any(|fs| fs.eq_ignore_ascii_case(file_system))
    {
        return false;
    }
    !CONTAINER_ROOTS.iter().any(|root| {
        let root = Path::new(root);
        mount != root && mount.starts_with(root)
    })
}

/// Whether a volume belongs in the `disk` section. Windows keeps every volume.
fn reported_in_section(v: &Volume) -> bool {
    cfg!(windows) || is_capacity_volume(&v.file_system, &v.mount)
}

/// Per-volume `{mount, total_bytes, free_bytes, percent_used}` list of every mount,
/// unfiltered.
///
/// Shared with the `fs_disk_usage` handler; the `disk` section filters it.
pub fn volumes() -> Vec<Value> {
    list().iter().map(Volume::to_json).collect()
}

/// Collect the `disk` section.
pub fn collect() -> Section {
    let vols: Vec<Value> = list()
        .iter()
        .filter(|v| reported_in_section(v))
        .map(Volume::to_json)
        .collect();
    let worst = vols
        .iter()
        .filter_map(|v| v["percent_used"].as_u64())
        .max()
        .unwrap_or(0);
    let status = if worst >= 90 {
        Status::Crit
    } else if worst >= 80 {
        Status::Warn
    } else {
        Status::Ok
    };
    let summary = match vols
        .iter()
        .max_by_key(|v| v["percent_used"].as_u64().unwrap_or(0))
    {
        Some(v) => format!(
            "{} {}% full",
            v["mount"].as_str().unwrap_or("?"),
            v["percent_used"].as_u64().unwrap_or(0)
        ),
        None => "no volumes detected".to_string(),
    };
    Section::with_fields(status, summary, json!({ "volumes": vols, "top_dirs": [] }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_section_is_valid() {
        let s = collect();
        let v = s.into_value();
        assert!(v["status"].is_string());
        assert!(v["volumes"].is_array());
    }

    #[test]
    fn percent_used_reports_zero_for_an_empty_volume() {
        assert_eq!(percent_used(0, 0), 0);
    }

    #[test]
    fn percent_used_rounds_the_normal_case() {
        assert_eq!(percent_used(200, 50), 75);
    }

    #[test]
    fn percent_used_saturates_instead_of_underflowing_when_free_exceeds_total() {
        // Quota-managed, thin-provisioned, or network/overlay filesystems (and a
        // resize race on a plain disk) can report available_space() > total_space().
        assert_eq!(percent_used(100, 200), 0);
    }

    fn capacity(fs: &str, mount: &str) -> bool {
        is_capacity_volume(fs, Path::new(mount))
    }

    #[test]
    fn drops_docker_overlay_layers() {
        assert!(!capacity(
            "overlay",
            "/var/lib/docker/rootfs/overlayfs/0123456789abcdef"
        ));
    }

    #[test]
    fn drops_pseudo_and_image_filesystems() {
        assert!(!capacity("tmpfs", "/run"));
        assert!(!capacity("squashfs", "/snap/core/123"));
        assert!(!capacity("proc", "/proc"));
    }

    #[test]
    fn file_system_match_ignores_case() {
        assert!(!capacity("Overlay", "/mnt/merged"));
    }

    #[test]
    fn drops_any_mount_strictly_below_a_container_root() {
        assert!(!capacity("ext4", "/var/lib/docker/volumes/data/_data"));
        assert!(!capacity(
            "ext4",
            "/run/containerd/io.containerd.runtime.v2.task/k8s/x/rootfs"
        ));
    }

    #[test]
    fn keeps_real_volumes() {
        assert!(capacity("ext4", "/"));
        assert!(capacity("xfs", "/home"));
        assert!(capacity("NTFS", "C:\\"));
    }

    #[test]
    fn keeps_a_partition_mounted_at_a_container_root() {
        assert!(capacity("ext4", "/var/lib/docker"));
    }

    #[test]
    fn container_root_match_is_per_component() {
        assert!(capacity("ext4", "/var/lib/docker-data"));
    }

    #[cfg(windows)]
    #[test]
    fn windows_section_keeps_every_volume() {
        let v = Volume {
            mount: PathBuf::from("/var/lib/docker/rootfs/overlayfs/x"),
            file_system: "overlay".into(),
            total: 1,
            free: 1,
        };
        assert!(reported_in_section(&v));
    }
}
