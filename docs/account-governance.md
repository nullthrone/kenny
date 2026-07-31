# Account governance

kenny can manage **who may sign in** to the PCs you administer — suspend an account,
take away administrator rights, restrict how it may sign in, create or remove an
account, and lock or sign out a live session.

The same controls work for **local accounts and Microsoft accounts alike**. That is not
an abstraction kenny maintains: a Microsoft account on a home PC *is* an entry in the
machine's own account database, with a local profile, so Windows itself draws no
distinction at the layer these controls operate on.

!!! warning "Consent and scope"
    This is for machines **you** administer, in a family setting, with the knowledge and
    consent of the people who use them. kenny names accounts and can change who signs in;
    it deliberately still does **not** attribute behaviour to a person — screen time and
    web activity stay whole-machine (see [Parental controls](parental-controls.md)).
    See [ADR-0046](adr/0046-account-governance-local-and-microsoft.md).

## Why administrator rights come first

Every other control kenny has is reversible by someone with local administrator rights:
the [web filter](parental-controls.md) writes to the hosts file, the kill switch is a
file on disk, the agent is a service. **Making a child a standard user is what makes the
rest hold.** If you do one thing on this page, do that one.

## What you can do

Everything below works identically for both account kinds unless the table says
otherwise.

| Action | Local | Microsoft | Notes |
|---|---|---|---|
| Suspend / restore an account | ✅ | ✅ | Blocks sign-in on this PC. The Microsoft account itself is untouched. |
| Administrator ↔ standard | ✅ | ✅ | The strongest lever. |
| Deny network sign-in | ✅ | ✅ | Sign-in from other machines on the network. |
| Deny Remote Desktop sign-in | ✅ | ✅ | |
| Lock a session | ✅ | ✅ | Back to the sign-in screen; open apps keep running. |
| Sign an account out | ✅ | ✅ | Ends the session — unsaved work may be lost. |
| Delete an account | ✅ | ✅ | For a Microsoft account this unlinks it from **this PC** only. |
| Create an account | ✅ | ❌ | A Microsoft account can only be added at the PC itself. |
| Password policy | ✅ | ❌ | Microsoft accounts follow Microsoft's own cloud policy. |

Where an action is unavailable for a particular account, the dashboard **shows it
greyed out with the reason** rather than hiding it, so the limitation is visible instead
of mysterious.

### Deliberately not offered

**Denying interactive (console) sign-in.** It can lock out the only person who can use
the PC, and kenny has no way to reach the machine to undo it.

## What kenny cannot do, and why

Microsoft publishes **no administrative interface for personal Microsoft accounts**.
Microsoft Graph covers work and school identities, not the consumer accounts a family
uses, and Microsoft Family Safety has no API at all — its screen-time limits, app limits,
web restrictions, spending controls, reports and "can I have more time?" requests are
reachable only through the Family Safety app and website.

So these are out of reach entirely, with no partial version:

- The Microsoft account **password**, two-factor settings, and recovery options.
- **Everything in Microsoft Family Safety**, including reading how much screen time was
  used. kenny cannot see it and cannot change it.
- The **Windows Hello PIN**, which is tied to the person and the machine's security chip.
- **Per-account web filtering** — kenny's filter works through the hosts file, which is
  machine-wide.

!!! note "Family Safety and kenny can collide"
    If a child's Microsoft account is part of a Microsoft family group, Windows enforces
    that group's screen-time rules itself. kenny can neither read nor override them, and
    the dashboard says so on any Microsoft account.

## Safety rails

**kenny refuses to lock you out.** The agent will not disable, demote, delete, or
restrict the **last enabled administrator** on a PC, and will not delete the built-in
Administrator or Guest accounts. This is enforced on the PC itself and cannot be
overridden from the server — the same way the web filter refuses a list that would cut
off its own connection.

**Every change needs operator rights and a confirmation.** Account governance requires
the `operator` role (a scoped `user` sees the inventory read-only), and when Claude
proposes one of these actions in chat it stops and waits for you, like every other
state-changing tool.

**Every call is written to the audit log** — visible under *Activity → Tool-call audit
log* in the dashboard.

!!! note "Monitoring is the guarantee, enforcement is best-effort"
    All of these actions are refused while the person at the PC has **remote control
    switched off** ([the kill switch](user-guide.md)) — and because the switch is
    theirs to flip, a standard user can still refuse *new* changes. Changes already
    applied stay applied.

    This is the same stance as the web filter, and it is why the **drift signal**
    matters more than the enforcement: kenny reports when an account appears, gains
    administrator rights, is re-enabled, has its restrictions cleared, or is newly
    linked to a Microsoft account — whether or not the change came from kenny.

## What kenny reports

Two telemetry sections feed this page.

**`local_accounts`** — every account on the PC with its kind (local, Microsoft,
work/school), whether it is enabled, whether it is an administrator, which sign-in
restrictions are set, and which actions are unavailable for it. Plus the machine's
password policy. Health rules warn about an enabled built-in Administrator or Guest
account, an administrator that permits a blank password, and an administrator that also
carries sign-in restrictions (a contradiction — one of the two settings is stale).

**`logon_failures`** — failed sign-in attempts per account over the last 24 hours, split
by whether they happened at the keyboard, over the network, or over Remote Desktop. A
burst against one account warns; the same number spread across the household does not.
Attempts against usernames that do not exist on the PC are counted but never named.

kenny takes the **account name** and the display name the user chose for themselves. It
does **not** put Microsoft account email addresses or Windows security identifiers on
the wire.

## Recovering from a mistake

If you suspend or restrict the wrong account, undo it from the same panel — the change
takes effect at the next sign-in attempt. If a PC ends up with no usable administrator
(kenny will not cause this, but a manual change might), you need physical access to the
machine and the Windows recovery options; kenny cannot help from the server.

## See also

- [Parental controls](parental-controls.md) — web activity, filtering, screen time
- [Dashboard reference](dashboard.md) — where the accounts panel lives
- [Telemetry reference](telemetry.md) — the raw section shapes
- [ADR-0046](adr/0046-account-governance-local-and-microsoft.md) — why it is built this way
