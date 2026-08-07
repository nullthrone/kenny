---
description: Reconcile docs + regenerate dashboard screenshots after doc-relevant code or ADR changes
---

Close the gap between the code and the docs (prose **and** screenshots). The
`doc-drift` Stop hook points here when it detects drift; you can also run this
proactively after a GUI/telemetry/tooling change or a new ADR.

The file->doc->screenshot mapping is `.claude/doc-drift-map.json` — the same
source of truth the hook uses. Trust it, but sanity-check it against what you
actually changed.

1. **List what changed.** `git diff --name-only $(git merge-base HEAD origin/main)...HEAD`
   plus `git status --porcelain`. Read `.claude/doc-drift-map.json` and match your
   changed files against each rule's `sources` (and the `adr_rule`).
2. **Update the prose.** For each matched rule, open its `docs` and bring them in
   line with the current code:
   - `webui/index.html` / `webui/__init__.py` → `docs/dashboard.md` (tabs, widgets,
     KPI tiles, chart types, action buttons, **deep-link hashes** like `#/overview`).
   - `health_rules.py` → `docs/telemetry.md` (exact thresholds) + `docs/dashboard.md`.
   - `webfilter*` → `docs/parental-controls.md`; `alerting.py`/`notify.py`/`digest.py`
     → `docs/alerting.md`; `tools.py` catalog → `docs/tools.md` (and this is a
     **contract** change — do `/contract-check` first); `config.py` → `docs/setup.md`.
3. **Regenerate the affected screenshots** (only if the change is user-visible).
   Install once: `pip install -e "kenny-server[screenshots]"` (Chromium is already
   present — do **not** run `playwright install`). Then, from the repo root:
   `python scripts/screenshots/capture.py --only <names>` (the rule lists the names;
   a fleet-wide change → regenerate all). The script renders the real dashboard
   against the mock fleet and **asserts** the original fonts (Hanken Grotesk /
   JetBrains Mono) rendered — if it aborts on the font check, fix font availability
   rather than shipping fallback-font PNGs. See `scripts/screenshots/README.md`.
4. **New/changed ADR.** Add the record's row to the `docs/adr/README.md` index
   table (number, title, status), keep the numbering a gap-free `0001..N`, and
   make sure every `ADR-NNNN` citation and `adr/NNNN-` link in the repo still
   resolves — the Stop hook reports exactly which of these broke. Confirm
   `mkdocs.yml`'s `not_in_nav: adr/0*.md` still covers the file. Only
   write an ADR for an **architectural** decision (CLAUDE.md — *When (not) to write
   an ADR*); a pure UI-layout tweak is recorded in the commit message instead.
5. **Verify.** `pip install -e "kenny-server[docs]"` then `mkdocs build --strict`
   from the repo root — it fails on missing/renamed referenced images and dead
   links. Fix anything it flags.
6. **Commit** with DCO sign-off (`git commit -s`); note UI-layout-only changes in
   the message (no ADR). Everything committed is English.

If a change genuinely has no doc impact (a refactor with no visible or documented
behavior change), record that in the commit and add `[skip-doc-drift]` to the
message (or set `KENNY_SKIP_DOC_DRIFT=1`) so the hook does not block.
