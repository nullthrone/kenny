---
description: Scaffold a new MADR architecture decision record in docs/adr/
argument-hint: <short title>
---

Create a new ADR for: **$ARGUMENTS**

1. Find the highest-numbered file in `docs/adr/` (ignore `0000-template.md`) and use the
   next zero-padded number `NNNN`.
2. Copy `docs/adr/0000-template.md` to `docs/adr/NNNN-<kebab-title>.md`.
3. If the decision is genuinely hard or contested (multiple viable options, unclear
   trade-offs, high blast radius) — not for routine or obvious calls — spawn an advisor
   subagent via the Agent tool with `model: "fable"` (Anthropic's most capable available
   model) to critique the considered options before drafting. Give it the conversation
   context and ask it to stress-test the trade-offs and flag risks or overlooked options.
   Fold its input into the Considered Options / Decision Outcome sections; don't quote it
   verbatim. Skip this step for straightforward decisions.
4. Fill in the title, set `Status: proposed` and today's date, and draft the Context,
   Considered Options, and Decision Outcome sections from the conversation so far.
5. Show the path and a short summary. Do not mark it `accepted` until the user confirms.
