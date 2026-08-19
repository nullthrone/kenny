"""The screenshot manifest: one :class:`Shot` per figure in ``docs/dashboard.md``
(and, for the ticket views, ``docs/itsm.md``).

Each shot names a target view (a URL hash), a capture ``mode`` (``full_page`` or
an element ``selector`` crop), a ``theme``, and an ordered list of ``actions`` the
driver runs before capturing. Actions are a tiny vocabulary interpreted by
:mod:`capture`:

* ``{"eval": "<js>"}``        — run JS in the page; awaited if it returns a
  promise. Used here to click a button/element the app renders (matched by its
  visible text or a stable attribute) — never a CSS Module class name, which is
  a build-time hash with no cross-build stability.
* ``{"wait_for": "<sel>"}``   — wait until a selector is attached + visible.
  Playwright's ``text=`` and attribute-selector syntax both work here.
* ``{"sleep": <ms>}``         — fixed settle delay (the 200ms view fade-up,
  a modal's 200ms scale-in, a stream settling).

Selector strategy (Nullthrone is CSS Modules — no hand-written class names):

* Prefer the global ``kc-*`` hooks from ``kenny-web/src/styles/global.css``
  (``kc-header``, ``kc-hostactions``, ``kc-adminnav``, ``kc-kpis``, ``kc-chat``,
  ``kc-actions``, ``kc-logline``, ``kc-stagger-row``, …) — these are the one set
  of class names the app promises to keep stable across views.
* ``Modal`` (``kenny-web/src/components/Modal/Modal.tsx``) always renders
  ``role="dialog"`` — a reliable generic selector for "whichever modal is open"
  since only one is ever mounted at a time.
* Where neither exists, a small number of ``data-shot="<name>"`` attributes were
  added directly to the view/component markup (``ticket-timeline`` on the ticket
  detail's event rail, ``history-row-<id>`` on each Ask Kenny history row,
  ``ask-kenny-transcript`` on the drawer's transcript pane) — see each of those
  files for the attribute in context.
* Clicking a button is done by matching its visible text via ``_click_button``,
  not a class name — the label copy is the one thing about a button this
  manifest can depend on staying put.

Keeping the manifest declarative makes it easy to add/adjust a figure without
touching the driver.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A short settle after layout/animation so transitions finish before we capture.
SETTLE_MS = 600


@dataclass
class Shot:
    name: str
    hash: str
    # "full_page" | "viewport" | "element".
    #
    # Use "viewport" for anything built on a fixed-position overlay — the Ask
    # kenny drawer, a modal and its backdrop. A full-page capture scrolls the
    # document while the overlay stays pinned to the viewport, so the page
    # renders undimmed below the fold and the resulting image shows a drawer
    # floating over a half-lit page, which is not what an operator ever sees.
    mode: str = "full_page"
    selector: str | None = None
    theme: str = "light"  # "dark" | "light" — Nullthrone is light-by-default.
    actions: list[dict[str, Any]] = field(default_factory=list)
    # Optional per-shot note surfaced in the run report.
    note: str = ""


# ---- reusable action fragments ------------------------------------------


def _click_button(text: str) -> dict[str, Any]:
    """Click the first ``<button>`` whose text contains ``text``.

    Matches on visible label copy rather than a CSS Module class name (which
    is a build-time hash, not a stable selector) — the same technique the old
    manifest used for its own JS-eval actions, just applied uniformly here.
    """

    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return {
        "eval": (
            "[...document.querySelectorAll('button')]"
            f".find(b => b.textContent.includes('{escaped}')).click()"
        )
    }


# Sets a React-controlled text input's value via the native property setter +
# a real `input` event — the standard trick for a script (as opposed to a real
# keystroke) to make React's onChange fire. Used once, for the Add-a-PC
# wizard's name field (`#wizard-name` is a real, stable id — no data-shot
# attribute needed).
_WIZARD_NAME_JS = (
    "const el = document.getElementById('wizard-name');"
    "const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;"
    "setter.call(el, 'tante-laptop');"
    "el.dispatchEvent(new Event('input', { bubbles: true }));"
)

# Open the Ask Kenny drawer (from a host page, so its scope chip reads
# "scope: <host>"), then switch to the History panel and wait for the seeded
# conversations to list. Shared by chat-history (stops here) and ask-kenny
# (goes on to load one).
_OPEN_ASK_KENNY_HISTORY: list[dict[str, Any]] = [
    {"wait_for": ".kc-hostactions"},
    {"sleep": 200},
    _click_button("ASK KENNY"),
    {"wait_for": ".kc-chat"},
    {"sleep": 200},
    {"eval": "document.querySelector('[title=History]').click()"},
    {"wait_for": "[data-shot^='history-row-']"},
]


MANIFEST: list[Shot] = [
    # -- Today --------------------------------------------------------------
    Shot(
        name="today",
        hash="#/today",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-kpis"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="today-dark",
        hash="#/today",
        mode="full_page",
        theme="dark",
        actions=[{"wait_for": ".kc-kpis"}, {"sleep": SETTLE_MS}],
        note="the alternate theme — Nullthrone is light-by-default (today.png).",
    ),
    Shot(
        name="header",
        hash="#/today",
        mode="element",
        selector=".kc-header",
        theme="light",
        actions=[{"wait_for": ".kc-kpis"}, {"sleep": 300}],
    ),
    # -- Fleet ----------------------------------------------------------------
    Shot(
        name="fleet",
        hash="#/fleet",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-stagger-row"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="host",
        hash="#/fleet/study-pc",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-hostactions"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="reliability",
        hash="#/fleet/grandpa-pc",
        mode="viewport",
        theme="light",
        actions=[
            {"wait_for": ".kc-hostactions"},
            {"sleep": 200},
            _click_button("Reliability"),
            {"wait_for": "[role=dialog]"},
            {"sleep": SETTLE_MS},
        ],
        note="grandpa-pc: a suppressed noisy pattern (issue #166) alongside two real events.",
    ),
    Shot(
        name="parental-controls",
        hash="#/fleet/kid-pc",
        mode="viewport",
        theme="light",
        actions=[
            {"wait_for": ".kc-hostactions"},
            {"sleep": 200},
            _click_button("Web filter"),
            {"wait_for": "[role=dialog]"},
            {"sleep": SETTLE_MS},
        ],
    ),
    # -- Ask kenny (⌘K overlay) ------------------------------------------------
    Shot(
        name="chat-history",
        hash="#/fleet/study-pc",
        mode="element",
        selector=".kc-chat",
        theme="light",
        actions=[*_OPEN_ASK_KENNY_HISTORY, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="ask-kenny",
        hash="#/fleet/study-pc",
        mode="viewport",
        theme="light",
        actions=[
            *_OPEN_ASK_KENNY_HISTORY,
            {"eval": "document.querySelector('[data-shot=history-row-conv-study-disk]').click()"},
            {"wait_for": "[data-shot=ask-kenny-transcript]"},
            {"sleep": SETTLE_MS},
        ],
    ),
    # -- Inbox ------------------------------------------------------------------
    Shot(
        name="inbox",
        hash="#/inbox",
        mode="full_page",
        theme="light",
        note="default NEEDS YOU group — the printer-driver ticket's held approval gate.",
        actions=[{"wait_for": ".kc-actions"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="ticket-detail",
        hash="#/inbox/ticket/demo-tkt-flush",
        mode="full_page",
        theme="light",
        note="the grandpa-pc Wi-Fi ticket — full lifecycle: message, tool calls, a held+approved gate, resolution.",
        actions=[{"wait_for": "[data-shot=ticket-timeline]"}, {"sleep": SETTLE_MS}],
    ),
    Shot(
        name="ticket-triage",
        hash="#/inbox/ticket/demo-tkt-phantom",
        mode="full_page",
        theme="light",
        note=(
            "an alert kenny investigated unprompted and closed out: the verdict, what it "
            "checked, and the one-click mute it proposes (ADR-0056)."
        ),
        actions=[{"wait_for": "[data-shot=triage-verdict]"}, {"sleep": SETTLE_MS}],
    ),
    # -- Log ----------------------------------------------------------------
    Shot(
        name="log",
        hash="#/log",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-logline"}, {"sleep": 300}],
    ),
    # -- Admin ----------------------------------------------------------------
    Shot(
        name="admin",
        hash="#/admin/alerting-digest",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-adminnav a"}, {"sleep": 500}],
    ),
    Shot(
        name="admin-backup",
        hash="#/admin/backup",
        mode="full_page",
        theme="light",
        actions=[{"wait_for": ".kc-adminnav a"}, {"sleep": 700}],
    ),
    # -- Profile ------------------------------------------------------------
    Shot(
        name="profile",
        hash="#/profile",
        mode="full_page",
        theme="light",
        note="signed in as the seeded 'thomas' superuser — a real account, not the legacy shared token.",
        actions=[{"wait_for": "text=SESSION"}, {"sleep": 300}],
    ),
    # -- Add a PC -------------------------------------------------------------
    # Captured LAST: minting a share link really provisions a new agent row
    # ("tante-laptop", POST /api/agents/share-link) — real state, not a mock,
    # so it must not leak a 7th fleet member into any shot captured after it.
    Shot(
        name="add-a-pc",
        hash="#/fleet",
        mode="element",
        selector="[role=dialog]",
        theme="light",
        note="drives all 3 wizard steps to the minted share-link result.",
        actions=[
            {"wait_for": ".kc-stagger-row"},
            _click_button("ADD A PC"),
            {"wait_for": "[role=dialog]"},
            {"eval": _WIZARD_NAME_JS},
            _click_button("NEXT"),
            {"sleep": 150},
            _click_button("NEXT"),
            {"sleep": 150},
            _click_button("Share a one-time link"),
            {"wait_for": "text=Expires"},
            {"sleep": SETTLE_MS},
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
