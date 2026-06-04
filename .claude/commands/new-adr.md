---
description: Scaffold a new MADR architecture decision record in docs/adr/
argument-hint: <short title>
---

Create a new ADR for: **$ARGUMENTS**

1. Find the highest-numbered file in `docs/adr/` (ignore `0000-template.md`) and use the
   next zero-padded number `NNNN`.
2. Copy `docs/adr/0000-template.md` to `docs/adr/NNNN-<kebab-title>.md`.
3. Fill in the title, set `Status: proposed` and today's date, and draft the Context,
   Considered Options, and Decision Outcome sections from the conversation so far.
4. Show the path and a short summary. Do not mark it `accepted` until the user confirms.
