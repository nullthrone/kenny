# 0054. Alert delivery channels are live settings, resolved per dispatch

- Status: accepted
- Boundary moved: the configuration boundary of the observability model's push surface.
  Which channels an alert is delivered on moves out of the host's process environment
  (changeable only with shell access and a restart) and into the settings store
  (changeable by a superuser from the dashboard, effective on the next alert). That also
  moves a class of secret — the channel URLs and the ntfy token — from the environment
  into `kenny.sqlite`, so it moves the trust topology too, in the direction this record
  has to pay for below.
- Date: 2026-08-17
- Amends: [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md)
- Touches: [ADR-0032](0032-runtime-settings-in-the-dashboard.md)

## Context and Problem Statement

[ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) made the delivery channels
env-configured, and `notify.load_notifiers()` implemented that literally: it read
`KENNY_NTFY_URL`, `KENNY_NTFY_TOKEN`, `KENNY_WEBHOOK_URL` and `KENNY_DISCORD_WEBHOOK_URL`
from `os.environ` exactly once, in `main.build_app`, and handed the resulting list to a
single long-lived `AlertEngine`. There was no reload path. Changing where alerts go meant
editing the environment and restarting the process.

[ADR-0032](0032-runtime-settings-in-the-dashboard.md) then made `lifecycle` the honesty
mechanism of the settings catalog: a key is advertised as writable only when its consumer
actually reads through the resolver. These four keys did not, so they were marked
`env_only` with a comment saying, correctly, that advertising them as writable "would be a
lie about what actually takes effect".

The comment was true and the arrangement was wrong. The redesigned Admin renders the
settings catalog generically, so Alerting shows these rows and offers no control on them —
a dead row on the one screen an operator opens when alerts are not arriving. And the
underlying asymmetry was never defensible on its own terms: the *cadence*, *cooldown*,
*offline threshold* and *digest schedule* of alerting are all editable live, while the
question that decides whether an alert reaches a human at all requires a shell on the host.
For the family-scale, single-operator deployment this project targets, that is the value
most likely to need changing and the one hardest to change.

## Considered Options

- **Keep `env_only` and special-case the four rows out of the console.** Restores honesty
  by hiding, at the cost of a page that silently omits real configuration, plus per-key
  knowledge in a renderer whose whole point is that it has none.
- **Make them writable and re-apply on write via an `APPLY_HOOKS` entry** that rebuilds the
  engine's notifier list. Rejected: it leaves two things that both believe they know the
  channels — the settings map and the list the engine holds — and they agree only for as
  long as the hook keeps firing. A hook that raises, or an engine constructed without one
  wired, keeps delivering to the old channel and reports success. That is precisely the
  failure this surface must not have, because nobody notices an alert that was never sent.
- **Make them writable with `lifecycle="restart"`.** Honest, and the operator can at least
  type the value into the dashboard — but "restart the server to change a webhook URL" is
  the problem, restated with more steps.
- **Give the engine a provider and resolve the channels at every dispatch.** Chosen.

## Decision Outcome

Chosen option: **a notifier provider, asked again for every notification**, because it
removes the possibility of a stale list rather than arranging for the list to be refreshed.
There is no cached delivery target anywhere to go out of date.

- `notify.NotifierProvider` resolves the four keys through `Settings` when one is wired —
  which already layers **DB override > env var > coded default** — and reads `os.environ`
  directly only when there is no settings layer at all. An existing env-configured
  deployment therefore keeps working with no action, exactly as before.
- The resolved values are frozen into a comparable `ChannelConfig`; the channel objects are
  rebuilt **only** when that value tuple changes. This runs on every alert, so the
  steady-state cost is four dict lookups and one dataclass comparison.
- `AlertEngine` takes `notifier_provider=` and exposes `_notifiers` as a property over it.
  A fixed `notifiers=` list is still accepted for direct construction and is wrapped in a
  constant provider; passing both raises, because two sources of truth about delivery is
  the kind of mistake that would otherwise be discovered by an alert not arriving.
- **An emptied field means the channel is off** — it does not fall back to whatever the
  environment still holds. Resurrecting an env value under a field the operator just
  cleared would be the same silent failure in the other direction. Clearing an override
  (`DELETE`) *is* the way back to the env value, and says so.
- Each channel's `send` is now additionally wrapped at the dispatch site. `Notifier.send`
  already swallows its own transport errors (ADR-0027); the guard covers everything else a
  channel could throw, so one misconfigured channel cannot cost the others their delivery.
- Zero configured channels remains a legitimate state: the loop evaluates, records history
  and opens tickets as before, it just pushes nothing.
- The four specs become `lifecycle="live"`, stay `sensitive=True`, and `KENNY_NTFY_URL`
  becomes type `secret` alongside the other three — a `sensitive` row's serialised value is
  the mask text, and only a `secret`-typed row opens its editor blank instead of offering
  that mask as the draft.

**The delivery model itself is untouched.** Transitions only, persisted flap suppression in
`alert_state`, cooldown keyed by `(agent_id, scope)`, escalations to crit always firing —
all exactly as ADR-0027 decided. This record changes only where the channel configuration
comes from. There is no frontend work: Admin renders the catalog generically, so flipping
`lifecycle` is what makes the rows editable.

### Consequences

- Good, because the dead rows become real controls, and the setting an operator reaches for
  when alerts are not arriving is the one they can now actually change — from the same
  screen, without a shell, effective on the next evaluation pass (~60 s).
- Good, because no component holds a delivery target across a change; a stale channel list
  is not a bug that can be reintroduced, because there is no list to keep.
- Good, because the environment remains a first-class way to configure this. Nothing about
  an existing deployment changes, and its values keep showing as the effective ones.
- **Bad, because a webhook URL is bearer-equivalent and is now reachable from the
  dashboard.** Before, redirecting kenny's alerts required host access; now a superuser
  session or PAT is enough to point them at a URL the holder controls (leaking host names,
  section names and rule reasons — the only content alert bodies carry, by ADR-0027's
  deliberate design) or to switch alerting off by clearing a field. The settings routes are
  superuser-gated and the values are never serialised back out — `describe()` reports
  `set`/`not set`, so an existing channel cannot be *read* from the dashboard, only
  replaced. That narrows the exposure to whoever already holds the highest role, and it is
  a genuine reduction in the privilege needed to misdirect alerting.
- **Bad, because these secrets now live in `kenny.sqlite` as raw strings**, which
  [ADR-0032](0032-runtime-settings-in-the-dashboard.md) explicitly avoided when it said
  secrets stay `env_only`. They consequently travel in every DB backup and to every
  configured backup target ([ADR-0039](0039-server-database-backup-and-restore.md)). This
  record amends that boundary knowingly for these four keys only: the auth and identity
  secrets (operator tokens, agent tokens, the Ed25519 key material, the Anthropic and
  Discord bot tokens) stay `env_only`, and `sensitive` is now explicitly the orthogonal
  axis — never serialised out — rather than a synonym for `env_only`.
- **Known gap: a channel change leaves no audit trail.** `PUT`/`DELETE /api/settings/{key}`
  write no event, so an operator who redirects or disables alert delivery does it silently,
  and the events table — the one place ADR-0027 guarantees an alert is recorded even when
  no channel exists — cannot show that the channels changed underneath it. It is recorded
  here rather than fixed in the same change, because settings writes are not specific to
  alerting and auditing them belongs with the settings surface, not inside this one.
- Neutral, because a deployment that sets these in its environment will still see the
  console render the rows read-only: the frontend derives its edit control from
  `source !== 'env'`, uniformly for every setting, so an env value must be dropped before
  the dashboard takes over the key. The server accepts the write either way.

## More Information

- [ADR-0027](0027-push-alerting-ntfy-webhook-and-weekly-digest.md) — the alerting model
  this amends the configuration of, and the best-effort delivery contract it preserves.
- [ADR-0032](0032-runtime-settings-in-the-dashboard.md) — the `lifecycle` taxonomy and the
  DB > env > default precedence this relies on.
- Code: `kenny-server/kenny_server/notify.py` (`NotifierProvider`, `resolve_channels`),
  `alerting.py` (`notifier_provider`, per-channel isolation in `_dispatch`), `config.py`
  (the four specs), `main.py` (composition).
- Tests: `tests/test_notify.py` (settings over env, env alone, a change seen by the next
  resolution, clearing means off, memoisation), `tests/test_alerting.py` (delivery on a
  channel configured after construction, one raising channel not costing the others, a
  provider that raises not breaking the pass), `tests/test_console_endpoints.py` (the rows
  are editable over the API and the value is never echoed back).
