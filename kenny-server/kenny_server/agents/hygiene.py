"""The server side of the config-hygiene agent (ADR-0071, ADR-0072).

The agent proposes — and, in ``act`` under a standing authorization scoped to
``server``, performs — the removal of reliability suppressions (ADR-0041) and
auto-ticket rules (``ticket_rules.py``) that nothing has matched for
:data:`UNUSED_AFTER_DAYS` days, and whose removal can only make kenny louder.
Which rules those are is never the model's judgement, and this docstring, not
the agent's prompt, is where the rule is stated:

* **Unused** is the server's own record (``rule_hits``): each rule notes when
  the server last applied it, its creation and the start of hit tracking on
  its table counting as a use. A rule is unused only when nothing has matched
  it for :data:`UNUSED_AFTER_DAYS` days and tracking was on for all of them.
* **Only a removal that makes kenny at least as loud is ever proposed.**
  "Unused for months" cannot tell a dead rule from one guarding a rare event,
  so a rule qualifies only where removing it cannot hide anything. A
  suppression always qualifies: removing one only lets an event count toward
  health again. An auto-ticket rule qualifies only when, for every alert it
  decides, what decides in its place opens a ticket for every severity it does
  (``ticket_rules.removal_is_louder``): a ``never`` rule always, an
  ``open_crit`` rule only where its replacement is ``open_all``, an
  ``open_all`` rule never. Hit tracking has blind spots (a chronic alert stamps
  its rule only when it is first dispatched; with the alert loop off and no
  telemetry read, a suppression records nothing), and this is what makes them
  harmless: a rule wrongly thought unused is removed only if its removal
  could not have silenced anything.

This module turns those two rules into:

* **evidence** — the providers :data:`UNUSED_SUPPRESSIONS` and
  :data:`UNUSED_TICKET_RULES`, which the runner resolves once at run start and
  freezes on the run; the spec binds each remove tool's ``rule_id`` to one of
  them, so the gate admits only an id the server computed;
* **the four rule tools** an agent run dispatches, registered on the agents'
  executor alone. They reuse the rule mirrors' own methods, so there is one
  implementation of removing a rule. A removal checks both rules again, at
  execution, against the live mirror — a rule that matched after the run
  started, or whose removal stopped being louder because the other rules
  changed, is refused, not removed — and writes an audit row naming the agent,
  the run and the authorization that let it (ADR-0072 rule 5).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ..rule_hits import last_activity, unused
from ..ticket_rules import removal_is_louder
from ..tunnel import ToolError

__all__ = [
    "EVIDENCE_NAMES",
    "UNUSED_AFTER_DAYS",
    "UNUSED_SUPPRESSIONS",
    "UNUSED_TICKET_RULES",
    "RuleHygiene",
    "register",
]

logger = logging.getLogger("kenny.agents.hygiene")

#: How long a rule must have done nothing — no match, and no creation — before
#: the agent may propose removing it. Stated in the agent's prompt, so changing
#: it changes the agent's spec hash and unbinds what was granted against it.
UNUSED_AFTER_DAYS = 90

#: The evidence providers this module registers.
UNUSED_SUPPRESSIONS = "unused_suppressions"
UNUSED_TICKET_RULES = "unused_ticket_rules"
EVIDENCE_NAMES: frozenset[str] = frozenset({UNUSED_SUPPRESSIONS, UNUSED_TICKET_RULES})


def _identity(session: Any) -> tuple[str | None, str | None]:
    """``(actor, run_id)`` of the agent run ``session`` is, or ``(None, None)``."""

    actor = getattr(session, "audit_actor", None)
    run_id = getattr(session, "agent_run_id", None)
    return (
        actor if isinstance(actor, str) and actor else None,
        run_id if isinstance(run_id, str) and run_id else None,
    )


class RuleHygiene:
    """Evidence providers and tool handlers over the two rule mirrors.

    ``suppression`` is the :class:`~kenny_server.reliability_suppression.SuppressionList`,
    ``ticket_rules`` the :class:`~kenny_server.ticket_rules.TicketRuleList`;
    ``call_log`` receives the audit rows; ``now`` is the clock "unused" is
    judged by (the runner's).
    """

    def __init__(
        self,
        *,
        suppression: Any,
        ticket_rules: Any,
        call_log: Any,
        now: Callable[[], datetime],
        days: int = UNUSED_AFTER_DAYS,
    ) -> None:
        self.suppression = suppression
        self.ticket_rules = ticket_rules
        self.call_log = call_log
        self.now = now
        self.days = days

    # -- evidence ------------------------------------------------------------

    def suppression_removable(self, rule: dict[str, Any]) -> bool:
        """Whether suppression ``rule`` may be proposed for removal now: it is unused.

        Removing a suppression only makes alerts louder, so being unused is enough.
        """

        return unused(rule, self.now(), self.days)

    def ticket_rule_removable(self, rule: dict[str, Any]) -> bool:
        """Whether auto-ticket ``rule`` may be proposed for removal now.

        It is unused, and removing it from the live rule set can only open more
        tickets (``ticket_rules.removal_is_louder``).
        """

        return unused(rule, self.now(), self.days) and removal_is_louder(
            self.ticket_rules.mapping(), rule
        )

    async def unused_suppressions(self) -> list[str]:
        """The ids of the suppressions :meth:`suppression_removable` now, sorted."""

        return sorted(
            str(r["id"]) for r in self.suppression.rules() if r.get("id") and self.suppression_removable(r)
        )

    async def unused_ticket_rules(self) -> list[str]:
        """The ids of the auto-ticket rules :meth:`ticket_rule_removable` now, sorted."""

        return sorted(
            str(r["id"]) for r in self.ticket_rules.rules() if r.get("id") and self.ticket_rule_removable(r)
        )

    # -- the read tools --------------------------------------------------------

    def _listed(
        self, rules: list[dict[str, Any]], removable: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any]:
        now = self.now()
        return {
            "rules": [
                {**r, "unused": unused(r, now, self.days), "removable": removable(r)}
                for r in rules
            ],
            "unused_after_days": self.days,
        }

    async def list_suppressions(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """``reliability_suppression_list`` for an agent run: every rule and its record."""

        return self._listed(self.suppression.rules(), self.suppression_removable)

    async def list_ticket_rules(self, args: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        """``ticket_rule_list`` for an agent run: every rule and its record."""

        return self._listed(self.ticket_rules.rules(), self.ticket_rule_removable)

    # -- the removal tools -----------------------------------------------------

    async def remove_suppression(
        self, args: dict[str, Any], *, session: Any = None, authorization_id: str | None = None
    ) -> dict[str, Any]:
        """``reliability_suppression_remove`` for an agent run."""

        return await self._remove(
            "reliability_suppression_remove",
            self.suppression,
            args,
            session=session,
            authorization_id=authorization_id,
        )

    async def remove_ticket_rule(
        self, args: dict[str, Any], *, session: Any = None, authorization_id: str | None = None
    ) -> dict[str, Any]:
        """``ticket_rule_remove`` for an agent run."""

        return await self._remove(
            "ticket_rule_remove",
            self.ticket_rules,
            args,
            session=session,
            authorization_id=authorization_id,
            louder=lambda rule: removal_is_louder(self.ticket_rules.mapping(), rule),
        )

    async def _remove(
        self,
        tool: str,
        mirror: Any,
        args: dict[str, Any],
        *,
        session: Any,
        authorization_id: str | None,
        louder: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        """Remove one rule through ``mirror``, if it still qualifies; audit either way.

        The gate has already admitted ``rule_id`` against the run's frozen
        evidence. What it cannot know is what changed *since*: the run started
        minutes ago, a rule that applied in between is in use again, and a rule
        another was added beside may no longer be one whose removal is louder.
        So the predicates the evidence was computed with are asked again, here,
        against the live mirror, immediately before the removal:
        :func:`~kenny_server.rule_hits.unused`, and ``louder`` where given.
        """

        actor, run_id = _identity(session)
        if run_id is None:
            raise ToolError("no_run", f"{tool} runs here only within a specialized agent run")
        rule_id = args.get("rule_id")

        async def refuse(code: str, message: str) -> ToolError:
            await self.call_log.record(
                None,
                tool,
                dict(args),
                ok=False,
                error=message,
                actor=actor,
                run_id=run_id,
                authorization_id=authorization_id,
            )
            return ToolError(code, message)

        if not isinstance(rule_id, str) or not rule_id:
            raise await refuse("bad_args", "rule_id is required")
        rule = mirror.get(rule_id)
        if rule is None:
            raise await refuse("not_found", f"no rule {rule_id!r}; nothing was removed")
        if not unused(rule, self.now(), self.days):
            last = last_activity(rule)
            raise await refuse(
                "in_use",
                f"rule {rule_id!r} was not removed: it matched or was created within the "
                f"last {self.days} days (at {last.isoformat() if last else 'an unknown time'}), "
                "after this run's evidence was computed",
            )
        if louder is not None and not louder(rule):
            raise await refuse(
                "not_louder",
                f"rule {rule_id!r} was not removed: with the rules as they are now, removing "
                "it could keep closed a ticket it opens",
            )
        removed, _ = await mirror.remove(rule_id)
        if not removed:
            raise await refuse("not_found", f"no rule {rule_id!r}; nothing was removed")
        logger.info("%s removed rule %s (run %s, authorization %s)", actor, rule_id, run_id, authorization_id)
        await self.call_log.record(
            None,
            tool,
            dict(args),
            ok=True,
            actor=actor,
            run_id=run_id,
            authorization_id=authorization_id,
        )
        return {"ok": True, "removed": rule_id}

    # -- wiring ----------------------------------------------------------------

    def register(self, runner: Any, executor: Any) -> None:
        """Register the evidence on ``runner`` and the four tools on ``executor``.

        ``executor`` must be the agents' own: nowhere else are these tools
        dispatched in the loop.
        """

        runner.register_evidence(UNUSED_SUPPRESSIONS, self.unused_suppressions)
        runner.register_evidence(UNUSED_TICKET_RULES, self.unused_ticket_rules)
        executor.register_server_tool("reliability_suppression_list", self.list_suppressions)
        executor.register_server_tool("reliability_suppression_remove", self.remove_suppression)
        executor.register_server_tool("ticket_rule_list", self.list_ticket_rules)
        executor.register_server_tool("ticket_rule_remove", self.remove_ticket_rule)


def register(
    runner: Any, executor: Any, *, suppression: Any, ticket_rules: Any, call_log: Any
) -> RuleHygiene:
    """Wire the config-hygiene agent's evidence and tools (``main.py``)."""

    hygiene = RuleHygiene(
        suppression=suppression, ticket_rules=ticket_rules, call_log=call_log, now=runner.now
    )
    hygiene.register(runner, executor)
    return hygiene
