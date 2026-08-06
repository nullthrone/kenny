---
description: Scaffold a new MADR architecture decision record in docs/adr/
argument-hint: <short title>
---

Create a new ADR for: **$ARGUMENTS**

0. **Run the significance test first** (`CLAUDE.md`, *When (not) to write an ADR*): name
   the structural boundary this decision moves, what breaks if it is silently reverted,
   whether it forces both sides of the contract, and whether reverting is more than a
   routine pull request. An additive contract change does not pass on its own — it still
   has to move a boundary. **If it does not pass, do not scaffold anything.** There is no
   lesser record to write instead: say which question failed, then propose the code
   comment (naming the file it belongs in) and the commit-message wording that should
   carry the reasoning. Only continue when the test passes.
1. Find the highest-numbered file in `docs/adr/` (ignore `0000-template.md`) and use the
   next zero-padded number `NNNN` — the sequence is dense, so this is always one past
   the last record.
2. Copy `docs/adr/0000-template.md` to `docs/adr/NNNN-<kebab-title>.md`.
3. If the decision is genuinely hard or contested (multiple viable options, unclear
   trade-offs, high blast radius) — not for routine or obvious calls — spawn an advisor
   subagent via the Agent tool with `model: "fable"` (Anthropic's most capable available
   model) to critique the considered options before drafting. Give it the conversation
   context and ask it to stress-test the trade-offs and flag risks or overlooked options.
   Fold its input into the Considered Options / Decision Outcome sections; don't quote it
   verbatim. Skip this step for straightforward decisions.
4. Fill in the title, set `Status: proposed` and today's date, fill `Boundary moved:`
   with the answer from step 0, and draft the Context, Considered Options, and Decision
   Outcome sections from the conversation so far. Keep it to about one page (~60 lines) —
   push implementation detail into code comments rather than into the record.
5. Add the record's row to the index table in `docs/adr/README.md`.
6. Show the path and a short summary. Do not mark it `accepted` until the user confirms.
