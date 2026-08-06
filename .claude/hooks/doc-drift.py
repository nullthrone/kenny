#!/usr/bin/env python3
"""Detect documentation drift and steer Claude to fix it.

A Claude Code hook is a deterministic shell command - it cannot reason, only
detect a condition and feed instructions back. This script runs on two events:

* ``SessionStart`` (with ``--record-base``): stamps the current HEAD as the
  session baseline so the Stop pass knows exactly what THIS session changed,
  regardless of clone depth or how far the branch has diverged from the default.
* ``Stop`` (default): diffs the working tree against that baseline and blocks the
  turn with targeted instructions when either half finds something. **Doc drift:**
  doc-relevant or GUI-relevant code changed without the mapped docs being updated.
  **Record-set drift:** the session touched the ADR set (a record, the index, or a
  file citing one) and left it inconsistent - a gap in the 0001..N numbering, an
  index that disagrees with the directory, or a citation naming no record. When
  Claude fixes what was named, the next Stop pass finds it clean and exits 0, so
  the block clears itself.

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
import re
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


_RECORD_RE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
_INDEX_ROW_RE = re.compile(r"^\| \[(\d{4})\]\((\d{4}-[a-z0-9-]+\.md)\)", re.M)
# "ADR-0009", and "ADR-0019/0020" for a run of records (the records cite each other so).
_CITE_RE = re.compile(r"\bADR-(\d{4})((?:/\d{4})*)\b")
# A link reaching a record through its directory, or relative from inside docs/adr/.
_LINK_RE = re.compile(r"(?:(?:docs/|\.\./)?adr/|\()(\d{4})-[a-z0-9-]+\.md")

_SCAN_SUFFIXES = {".md", ".py", ".rs", ".json", ".html", ".yml", ".yaml", ".toml"}
_SCAN_SKIP_DIRS = {".git", "target", "node_modules", "__pycache__", ".pytest_cache", ".venv"}
# Vendored third-party bundles are not ours to police; their README beside them is.
_SCAN_SKIP_FILES = {"kenny-server/kenny_server/webui/assets/echarts.min.js"}


def _record_files(adr_dir: Path) -> dict[str, Path]:
    return {
        m.group(1): p
        for p in sorted(adr_dir.glob("*.md"))
        if (m := _RECORD_RE.match(p.name)) and m.group(1) != "0000"
    }


def _scan_targets() -> list[Path]:
    out: list[Path] = []
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        rel = path.relative_to(REPO)
        if any(part in _SCAN_SKIP_DIRS for part in rel.parts):
            continue
        if rel.as_posix() in _SCAN_SKIP_FILES:
            continue
        out.append(path)
    return out


def _record_set_drift(adr_rule: dict) -> list[str]:
    """Check the invariants the ADR set rests on, and report what broke.

    The numbering is a gap-free 0001..N sequence, which makes a number an address:
    cite it in a module docstring and a reader can find the decision. Nothing about
    that is self-enforcing - a renumbering, a removed record or a typo'd citation
    all fail silently, and the reader who finds out is following a dead reference.
    """
    index_path = adr_rule.get("index", "docs/adr/README.md")
    adr_dir = (REPO / index_path).parent
    records = _record_files(adr_dir)
    if not records:
        return []  # nothing to check (fail-open, same as a missing map)

    findings: list[str] = []

    # 1. Dense sequence: a gap means a record left without the index catching up.
    expected = [f"{i:04d}" for i in range(1, len(records) + 1)]
    if sorted(records) != expected:
        missing = sorted(set(expected) - set(records))
        extra = sorted(set(records) - set(expected))
        detail = []
        if missing:
            detail.append(f"gap(s) at {', '.join(missing)}")
        if extra:
            detail.append(f"number(s) past the end: {', '.join(extra)}")
        findings.append(
            f"numbering is not a gap-free 0001..{len(records):04d} sequence "
            f"({'; '.join(detail)}) - renumber the records and rewrite every citation"
        )

    # 2. The index IS the record set - in both directions, filenames included.
    try:
        index_text = (REPO / index_path).read_text(encoding="utf-8")
    except Exception:
        index_text = ""
    listed = dict(_INDEX_ROW_RE.findall(index_text))
    unindexed = sorted(set(records) - set(listed))
    if unindexed:
        findings.append(
            f"{index_path} does not list: {', '.join(records[n].name for n in unindexed)}"
            " - add each record's row"
        )
    phantom = sorted(set(listed) - set(records))
    if phantom:
        findings.append(
            f"{index_path} lists record(s) that do not exist: {', '.join(phantom)}"
            " - remove the row or restore the file"
        )
    wrong_file = sorted(
        f"{n} -> {listed[n]} (actual: {records[n].name})"
        for n in set(listed) & set(records)
        if listed[n] != records[n].name
    )
    if wrong_file:
        findings.append(f"{index_path} rows point at the wrong file: {'; '.join(wrong_file)}")

    # 3. Every citation and link resolves. A dangling one is a reader sent nowhere.
    dangling: list[str] = []
    for path in _scan_targets():
        rel = path.relative_to(REPO).as_posix()
        own = m.group(1) if (m := _RECORD_RE.match(path.name)) else None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for first, run in _CITE_RE.findall(text):
            for number in [first, *(n for n in run.split("/") if n)]:
                if number in (own, "0000") or number in records:
                    continue
                dangling.append(f"{rel}: ADR-{number}")
        for number in _LINK_RE.findall(text):
            if number != "0000" and number not in records:
                dangling.append(f"{rel}: link to {number}-...")
    if dangling:
        shown = sorted(set(dangling))
        findings.append(
            "citation(s)/link(s) naming no record: "
            + "; ".join(shown[:12])
            + (f" (+{len(shown) - 12} more)" if len(shown) > 12 else "")
        )

    return findings


def _touches_records(changed: set[str], adr_rule: dict) -> bool:
    """Only police the record set when this session could have disturbed it.

    A pre-existing inconsistency should not block a session that had nothing to do
    with it; a session that edits a record, or writes a citation, gets checked.
    """
    if any(_matches_any(p, adr_rule.get("sources", [])) for p in changed):
        return True
    if adr_rule.get("index", "docs/adr/README.md") in changed:
        return True
    for rel in changed:
        path = REPO / rel
        if path.suffix not in _SCAN_SUFFIXES or not path.is_file():
            continue
        try:
            if _CITE_RE.search(path.read_text(encoding="utf-8", errors="ignore")):
                return True
        except Exception:
            continue
    return False


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

    # ADR record-set invariants (only when this session could have disturbed them).
    adr_rule = doc_map.get("adr_rule")
    record_findings: list[str] = []
    if adr_rule and _touches_records(changed, adr_rule):
        try:
            record_findings = _record_set_drift(adr_rule)
        except Exception:
            record_findings = []  # fail-open, like everything else here

    if not drifts and not record_findings:
        _allow()

    _block(_render_reason(drifts, record_findings, shots_dir, capture_cmd, screenshot_names))


def _matches_any_in(changed: set[str], globs: list[str]) -> bool:
    return any(_matches_any(c, globs) for c in changed)


def _render_reason(
    drifts: list[dict],
    record_findings: list[str],
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

    for finding in record_findings:
        lines.append(f"- [adr-records] {finding}")
    if record_findings:
        lines.append(
            "    why:     the numbering is a gap-free 0001..N sequence, so a number is "
            "an address a reader follows from a code comment. Nothing else enforces it."
        )

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
