#!/usr/bin/env python3
"""Detect documentation drift and steer Claude to fix it.

A Claude Code hook is a deterministic shell command - it cannot reason, only
detect a condition and feed instructions back. This script runs on two events:

* ``SessionStart`` (with ``--record-base``): stamps the current HEAD as the
  session baseline so the Stop pass knows exactly what THIS session changed,
  regardless of clone depth or how far the branch has diverged from the default.
* ``Stop`` (default): diffs the working tree against that baseline and - if
  doc-relevant or GUI-relevant code changed, or an ADR was added, WITHOUT the
  mapped docs being updated - blocks the turn with targeted instructions telling
  Claude to reconcile the docs and regenerate the affected screenshots. When
  Claude edits the mapped files, the next Stop pass finds them in the diff and
  exits 0, so the block clears itself.

The file->doc->screenshot mapping lives in ``.claude/doc-drift-map.json`` (shared
with the ``/doc-sync`` command), so this script carries no policy of its own.

Design guarantees:
* **Fail-open.** Any error (not a git repo, no baseline, bad JSON, ...) exits 0
  so a tooling hiccup never wedges a session.
* **Loop-safe.** Honors ``stop_hook_active`` and exits 0 on the second pass.
* **Escapable.** ``KENNY_SKIP_DOC_DRIFT`` in the env, or ``[skip-doc-drift]`` in
  HEAD's commit message, suppresses the check (for pure refactors with no visible
  change).

See ``.claude/settings.json`` for registration and ``.claude/commands/doc-sync.md``
for the reconcile recipe this hook points Claude at.
"""

from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
MAP_PATH = HERE.parent / "doc-drift-map.json"


def _allow() -> None:
    """Let Claude stop normally (no drift, or fail-open)."""
    sys.exit(0)


def _block(reason: str) -> None:
    """Keep Claude working, feeding it the reconcile instructions."""
    json.dump({"decision": "block", "reason": reason}, sys.stdout)
    sys.exit(0)


def _git(*args: str) -> str | None:
    """Run a read-only git command; return stdout or None on any failure."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _rev_exists(ref: str) -> bool:
    return _git("rev-parse", "--verify", "--quiet", ref) is not None


def _base_file(session_id: str) -> Path | None:
    """Path to this session's baseline-SHA file, inside the git dir (untracked)."""
    git_dir = _git("rev-parse", "--absolute-git-dir")
    if not git_dir:
        return None
    sid = session_id or "default"
    # Keep it filesystem-safe.
    sid = "".join(c if c.isalnum() or c in "-_" else "_" for c in sid)
    base_dir = Path(git_dir.strip()) / "kenny-doc-drift"
    return base_dir / f"base-{sid}"


def record_base(session_id: str) -> None:
    """SessionStart: stamp current HEAD as the session baseline (best-effort)."""
    head = _git("rev-parse", "HEAD")
    path = _base_file(session_id)
    if not head or path is None:
        _allow()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(head.strip(), encoding="utf-8")
    except Exception:
        pass
    _allow()


def _read_base(session_id: str) -> str | None:
    path = _base_file(session_id)
    if path is None or not path.exists():
        return None
    try:
        sha = path.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    return sha if sha and _rev_exists(sha) else None


def _parse_status(status: str, into: set[str]) -> None:
    for line in status.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        # Renames/copies show as "old -> new"; the new path is what exists.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        cleaned = path.strip().strip('"')
        if cleaned:
            into.add(cleaned)


def _changed_files(session_id: str) -> set[str]:
    """Files this session changed, repo-relative POSIX.

    Primary path: diff the working tree against the SessionStart baseline SHA -
    this captures everything changed since the session began (committed OR still
    uncommitted) and nothing from the branch's pre-existing divergence, so it is
    robust to shallow clones and long-lived branches. Untracked files are added
    from ``git status`` (diff does not list them).

    Fallback (no baseline, e.g. a resumed session): uncommitted changes vs HEAD.
    This never floods; at worst it misses work already committed before the
    baseline existed. On any git failure the part is skipped (fail-open).
    """
    changed: set[str] = set()
    base = _read_base(session_id)
    if base:
        diff = _git("diff", "--name-only", base)
        if diff:
            changed.update(p for p in diff.splitlines() if p.strip())
    else:
        diff = _git("diff", "--name-only", "HEAD")
        if diff:
            changed.update(p for p in diff.splitlines() if p.strip())

    status = _git("status", "--porcelain")
    if status:
        _parse_status(status, changed)

    return {p for p in changed if p}


def _matches_any(path: str, globs: list[str]) -> bool:
    # fnmatch's '*' spans '/', so "webui/*" also covers nested files - intended.
    return any(fnmatch.fnmatch(path, g) for g in globs)


def _adr_drift(changed: set[str], adr_rule: dict, index_text: str) -> list[str]:
    """Return the basenames of changed ADR records not linked from the index."""
    ignore = set(adr_rule.get("ignore", []))
    unindexed: list[str] = []
    for path in sorted(changed):
        if path in ignore:
            continue
        if not _matches_any(path, adr_rule.get("sources", [])):
            continue
        # A deleted record won't exist on disk; only flag records still present.
        if not (REPO / path).exists():
            continue
        name = Path(path).name
        if name not in index_text:
            unindexed.append(path)
    return unindexed


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session_id = str(payload.get("session_id", "") or "")

    # SessionStart mode: record the baseline and get out of the way.
    if "--record-base" in sys.argv[1:]:
        record_base(session_id)

    # 1. Loop guard: never block twice in a row.
    if payload.get("stop_hook_active"):
        _allow()

    # 2. Escape hatches.
    if os.environ.get("KENNY_SKIP_DOC_DRIFT", "").strip():
        _allow()
    head_msg = _git("log", "-1", "--format=%B") or ""
    if "[skip-doc-drift]" in head_msg:
        _allow()

    # 3. Load the mapping. Missing/broken map -> fail-open.
    try:
        doc_map = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    except Exception:
        _allow()

    changed = _changed_files(session_id)
    if not changed:
        _allow()

    shots_dir = doc_map.get("screenshots_dir", "docs/assets/screenshots")
    capture_cmd = doc_map.get(
        "capture_cmd", "python scripts/screenshots/capture.py --only {names}"
    )

    drifts: list[dict] = []
    screenshot_names: list[str] = []

    for rule in doc_map.get("rules", []):
        if not _matches_any_in(changed, rule.get("sources", [])):
            continue
        docs = rule.get("docs", [])
        # Satisfied when at least one mapped doc is in the changed set.
        if any(d in changed for d in docs):
            continue
        touched = sorted(
            {c for c in changed if _matches_any(c, rule.get("sources", []))}
        )
        drifts.append(
            {
                "id": rule.get("id", "?"),
                "why": rule.get("why", ""),
                "touched": touched,
                "docs": docs,
                "screenshots": rule.get("screenshots", []),
                "adr": rule.get("adr", ""),
            }
        )
        for name in rule.get("screenshots", []):
            if name not in screenshot_names:
                screenshot_names.append(name)

    # ADR index rule.
    adr_rule = doc_map.get("adr_rule")
    adr_unindexed: list[str] = []
    if adr_rule:
        index_path = adr_rule.get("index", "")
        try:
            index_text = (REPO / index_path).read_text(encoding="utf-8")
        except Exception:
            index_text = ""
        adr_unindexed = _adr_drift(changed, adr_rule, index_text)

    if not drifts and not adr_unindexed:
        _allow()

    _block(_render_reason(drifts, adr_unindexed, adr_rule, shots_dir, capture_cmd, screenshot_names))


def _matches_any_in(changed: set[str], globs: list[str]) -> bool:
    return any(_matches_any(c, globs) for c in changed)


def _render_reason(
    drifts: list[dict],
    adr_unindexed: list[str],
    adr_rule: dict | None,
    shots_dir: str,
    capture_cmd: str,
    screenshot_names: list[str],
) -> str:
    lines: list[str] = [
        "Documentation drift detected. This session changed doc-relevant code "
        "or ADRs without updating the docs they map to. Reconcile before "
        "finishing - follow the `/doc-sync` recipe (.claude/commands/doc-sync.md).",
        "",
    ]

    for d in drifts:
        lines.append(f"- [{d['id']}] {d['why']}")
        lines.append(f"    changed: {', '.join(d['touched'])}")
        lines.append(f"    update:  {', '.join(d['docs'])}")
        if d["screenshots"]:
            shots = ", ".join(f"{shots_dir}/{n}.png" for n in d["screenshots"])
            lines.append(f"    screenshots that may need a refresh: {shots}")
        if d["adr"]:
            lines.append(f"    ADR: {d['adr']}")

    if adr_unindexed:
        index = adr_rule.get("index", "docs/adr/README.md") if adr_rule else "docs/adr/README.md"
        lines.append(
            f"- [adr-index] New ADR record(s) not linked from {index}: "
            f"{', '.join(adr_unindexed)}"
        )
        lines.append(f"    update:  add each record's row to {index}")

    lines.append("")
    lines.append("Then:")
    if screenshot_names:
        cmd = capture_cmd.replace("{names}", ",".join(screenshot_names))
        lines.append(
            "  1. If a change is user-visible, regenerate the affected screenshots "
            "with the mock fleet + original fonts (Hanken Grotesk / JetBrains Mono):"
        )
        lines.append(f"       {cmd}")
        lines.append(
            "     (install once: pip install -e \"kenny-server[screenshots]\"; "
            "Chromium is already present - do not run `playwright install`)."
        )
        lines.append("  2. Run `mkdocs build --strict` and fix any broken image/link refs.")
    else:
        lines.append("  1. Run `mkdocs build --strict` and fix any broken image/link refs.")
    lines.append(
        "If the code change is NOT user-visible (a pure refactor with no doc "
        "impact), record that in the commit message and add `[skip-doc-drift]` "
        "to it, or set KENNY_SKIP_DOC_DRIFT=1 - then this check will pass."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Never wedge a session on a hook bug.
        _allow()
