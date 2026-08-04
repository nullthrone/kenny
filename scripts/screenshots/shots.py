"""The screenshot manifest: one :class:`Shot` per figure in ``docs/dashboard.md``
(and, for the ticket views, ``docs/itsm.md``).

Each shot names a target view (a URL hash), a capture ``mode`` (``full_page`` or
an element ``selector`` crop), a ``theme``, and an ordered list of ``actions`` the
driver runs before capturing. Actions are a tiny vocabulary interpreted by
:mod:`capture`:

* ``{"eval": "<js>"}``        — run JS in the page (call a dashboard global, e.g.
  ``selectAgent('study-pc')``); awaited if it returns a promise.
* ``{"wait_for": "<sel>"}``   — wait until a selector is attached + visible.
* ``{"wait_charts": True}``   — wait until every Overview ECharts SVG has size.
* ``{"sleep": <ms>}``         — fixed settle delay (chart animations, streams).

Keeping the manifest declarative makes it easy to add/adjust a figure without
touching the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A short settle after charts/layout so animations finish before we capture.
SETTLE_MS = 600


@dataclass
class Shot:
    name: str
    hash: str
    mode: str = "full_page"  # "full_page" | "element"
    selector: str | None = None
    theme: str = "dark"  # "dark" | "light"
    actions: list[dict[str, Any]] = field(default_factory=list)
    # Optional per-shot note surfaced in the run report.
    note: str = ""


# ---- reusable action fragments ------------------------------------------

def _select(agent_id: str) -> list[dict[str, Any]]:
    """Go to an agent on the Fleet tab and wait for its detail to render."""

    return [
        {"eval": f"selectAgent({agent_id!r})"},
        {"wait_for": "#detail .kc-tiles"},
        {"sleep": 300},
    ]


# The copilot confirm-gate is reconstructed from the real transcript renderers
# (bubble / toolRun / renderPending) — the same DOM a live turn produces — since
# driving a real state-changing turn needs an API key and a live agent.
_COPILOT_CONFIRM_JS = (
    "(() => {"
    "  const t = document.getElementById('transcript'); if (t) t.innerHTML='';"
    "  bubble('user', 'Update all packages on living-room-pc');"
    "  bubble('assistant', \"I'll check what upgrades are pending, then apply them. \""
    "    + 'Let me list the available packages first.');"
    "  toolRun('winget_list', true);"
    "  renderPending({tool: 'winget_update', args: {id: 'Microsoft.PowerToys'},"
    "    agent_id: 'living-room-pc'});"
    "})()"
)

# The AI Recommendation block only renders with an Anthropic API key; inject the
# documented Diagnosis/Action/Urgency advisory + Auto-Remediate button so the
# figure matches docs/dashboard.md when no key is configured. Built from the
# page's own icon() + component classes (the .k-airec__body is pre-wrap, so the
# HTML fragment is kept on one line to avoid stray whitespace).
_AI_REC_BODY = (
    "<b>Diagnosis.</b> Drive C: is 96% full and rising about 0.5%/day — roughly 9 days "
    "to full.\\n\\n<b>Action.</b> Clear the Windows temp + update caches and move the "
    "Videos folder to secondary storage.\\n\\n<b>Urgency.</b> High — act within the week "
    "to avoid a full system drive."
)
_AI_REC_JS = (
    "(() => {"
    "  const body = document.querySelector('#modal-overlay .k-modal__body'); if (!body) return;"
    "  if (body.querySelector('.k-airec')) return;"
    "  const sec = document.createElement('section'); sec.className='k-airec';"
    "  sec.innerHTML = '<div class=\"k-airec__head\">' + icon('sparkles', 15)"
    "    + '<span>AI Recommendation</span></div>'"
    f"    + '<div class=\"k-airec__body\">{_AI_REC_BODY}</div>'"
    "    + '<div class=\"k-airec__foot\"><button class=\"k-btn k-btn--primary k-btn--sm\">'"
    "    + 'Auto-Remediate</button></div>';"
    "  body.insertBefore(sec, body.firstChild);"
    "})()"
)


MANIFEST: list[Shot] = [
    # -- full-page dashboards ---------------------------------------------
    Shot(
        name="overview",
        hash="#/overview",
        mode="full_page",
        actions=[{"wait_charts": True}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="overview-light",
        hash="#/overview",
        mode="full_page",
        theme="light",
        actions=[{"wait_charts": True}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="fleet-console",
        hash="#/fleet",
        mode="full_page",
        actions=[{"wait_for": "#detail .kc-tiles"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="flagged",
        hash="#/flagged/warn",
        mode="full_page",
        actions=[{"wait_for": ".kc-flagged"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="drilldown",
        hash="#/overview",
        mode="full_page",
        actions=[
            {"wait_charts": True},
            {"sleep": 300},
            # KPI tiles carry data-kpi=<index>; click the first non-empty one to
            # open its host drill-down table (the documented drilldown figure).
            {"eval": "document.querySelector('.kc-kpi[data-empty=\"0\"]').click()"},
            {"wait_for": "#modal-overlay .k-modal"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="reliability",
        hash="#/fleet",
        mode="full_page",
        actions=[
            *_select("grandpa-pc"),
            {"eval": "openSectionDetail('reliability')"},
            {"wait_for": "#modal-overlay #k-reliab-heat"},
            # The alarm suppression panel (ADR-0045 / issue #166) mounts async
            # after the heatmap; wait for it too so the seeded suppressed
            # pattern and its rule row are visible in the capture.
            {"wait_for": "#modal-overlay #k-relsup-panel .kwf-list, #modal-overlay #k-relsup-panel .kwf-row"},
            {"sleep": SETTLE_MS},
        ],
    ),
    # -- element / modal crops --------------------------------------------
    Shot(
        name="header",
        hash="#/overview",
        mode="element",
        selector=".kc-header",
        actions=[{"wait_charts": True}, {"sleep": 300}],
    ),
    Shot(
        name="agent-detail",
        hash="#/fleet",
        mode="element",
        selector="#detail",
        actions=[*_select("study-pc"), {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="activity-audit",
        hash="#/activity/audit",
        mode="element",
        selector="#app",
        actions=[{"wait_for": ".k-audit__row"}, {"sleep": 300}],
    ),
    Shot(
        name="activity-events",
        hash="#/activity/events",
        mode="element",
        selector="#app",
        actions=[{"wait_for": ".k-events__row"}, {"sleep": 300}],
    ),
    Shot(
        name="parental-controls",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal",
        actions=[
            *_select("kid-pc"),
            {"eval": "openSectionDetail('web_activity')"},
            {"wait_for": "#modal-overlay #kwf-panel .kwf-list, #modal-overlay #kwf-panel .kwf-row"},
            {"sleep": SETTLE_MS},
        ],
    ),
    Shot(
        name="reliability-modal",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal",
        note="element crop of the reliability section detail (companion to full-page)",
        actions=[
            *_select("grandpa-pc"),
            {"eval": "openSectionDetail('reliability')"},
            {"wait_for": "#modal-overlay #k-reliab-heat"},
            {"wait_for": "#modal-overlay #k-relsup-panel .kwf-list, #modal-overlay #k-relsup-panel .kwf-row"},
            {"sleep": SETTLE_MS},
        ],
    ),
    Shot(
        name="ai-recommendation",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal",
        actions=[
            *_select("study-pc"),
            {"eval": "openSectionDetail('disk')"},
            {"wait_for": "#modal-overlay .k-modal__body"},
            {"eval": _AI_REC_JS},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="screenshot-modal",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal--media",
        actions=[
            *_select("papa-pc"),
            {
                "eval": "screenshotModal('/api/agent/papa-pc/screenshot?t=' + Date.now())",
            },
            {"wait_for": "#modal-overlay .k-modal--media img"},
            {"sleep": SETTLE_MS},
        ],
    ),
    Shot(
        name="chat-history",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal",
        actions=[
            {"wait_for": "#detail .kc-tiles"},
            {"eval": "openHistoryPanel()"},
            {"wait_for": "#modal-overlay .kc-history-row"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="copilot-confirm",
        hash="#/fleet",
        mode="element",
        selector=".kc-copilot",
        actions=[
            *_select("living-room-pc"),
            {"eval": _COPILOT_CONFIRM_JS},
            {"wait_for": ".kc-copilot .k-gate"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="share-link",
        hash="#/fleet",
        mode="element",
        selector="#modal-overlay .k-modal",
        actions=[
            {"wait_for": "#detail .kc-tiles"},
            {
                "eval": "shareLinkModal('study-pc', "
                "'https://kenny.example/d/9f3c1a2b8e7d6c5f4a3b2c1d0e9f8a7b', 3600)",
            },
            {"wait_for": "#modal-overlay #km-share-url"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="tickets",
        hash="#/tickets",
        mode="full_page",
        # Check the first row so the bulk-action bar (state picker + Apply,
        # operator+ only) renders in the figure alongside the list itself.
        actions=[
            {"wait_for": "table.kacc-tbl tbody tr"},
            {"eval": "document.querySelector('table.kacc-tbl tbody tr td input[type=checkbox]').click()"},
            {"wait_for": "#tk-bulk-bar"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="ticket-detail",
        hash="#/tickets/demo-tkt-flush",
        mode="full_page",
        actions=[{"wait_for": ".kc-timeline"}, {"sleep": 300}],
    ),
    Shot(
        name="settings",
        hash="#/settings",
        mode="full_page",
        actions=[
            {"wait_for": ".kc-navitem"},
            {"wait_for": "#set-KENNY_ALERT_COOLDOWN_SECS"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="settings-backup",
        hash="#/settings/backup",
        mode="full_page",
        actions=[
            # The catalog row is always present regardless of whether any demo
            # backup has been seeded (the backup list itself may be empty).
            {"wait_for": "#set-KENNY_BACKUP_INTERVAL_SECS"},
            {"sleep": 300},
        ],
    ),
    Shot(
        name="discord-settings",
        hash="#/settings/discord-tickets",
        mode="element",
        selector="#discord-panel",
        actions=[{"wait_for": "#discord-panel .kacc-tbl"}, {"sleep": 300}],
    ),
    Shot(
        name="about",
        hash="#/overview",
        mode="element",
        selector="#modal-overlay .k-modal",
        actions=[
            {"wait_charts": True},
            {"eval": "openAboutModal()"},
            {"wait_for": "#modal-overlay .k-deflist"},
            {"sleep": 300},
        ],
    ),
]


def by_names(names: list[str]) -> list[Shot]:
    """Filter the manifest to ``names`` (preserving manifest order)."""

    wanted = set(names)
    picked = [s for s in MANIFEST if s.name in wanted]
    missing = wanted - {s.name for s in picked}
    if missing:
        raise SystemExit(f"unknown shot(s): {', '.join(sorted(missing))}")
    return picked
