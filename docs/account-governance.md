# Account governance

kenny can manage **who may sign in** to the machines you administer — suspend an account,
take away administrator rights, restrict how it may sign in, create or remove an
account, and lock or sign out a live session.

The same controls work for **local accounts, Microsoft accounts and Linux accounts
alike**, on **Windows PCs and Linux machines alike**. That is not an abstraction kenny
maintains: a Microsoft account on a home PC *is* an entry in the machine's own account
database, with a local profile, and a Linux account is an entry in `/etc/passwd` — at the
layer these controls operate on, neither operating system draws the distinctions above
it. Where something genuinely is not possible, the dashboard **shows the control greyed
out with the reason** instead of hiding it or offering a button that cannot work.

!!! warning "Consent and scope"
    This is for machines **you** administer, in a family setting, with the knowledge and
    consent of the people who use them. kenny names accounts and can change who signs in;
    it deliberately still does **not** attribute behaviour to a person — screen time and
    web activity stay whole-machine (see [Parental controls](parental-controls.md)).
    See [ADR-0042](adr/0042-account-governance-local-and-microsoft.md) and
    [ADR-0043](adr/0043-account-governance-on-linux.md).

## Why administrator rights come first

Every other control kenny has is reversible by someone with local administrator rights:
the [web filter](parental-controls.md) writes to the hosts file, the kill switch is a
file on disk, the agent is a service. **Making a child a standard user is what makes the
rest hold.** If you do one thing on this page, do that one.

## What you can do

Everything below works identically everywhere unless the table says otherwise. One
table, because there is one panel — a Linux host and a Windows PC show the same rows,
the same badges, the same two switches and the same five buttons.

| Action | Windows: local | Windows: Microsoft | Linux | Notes |
|---|---|---|---|---|
| Suspend / restore an account | ✅ | ✅ | ✅ | Blocks sign-in on this machine. A Microsoft account itself is untouched. On Linux this expires the account, which also blocks SSH keys — a locked password alone would not. |
| Administrator ↔ standard | ✅ | ✅ | ✅ | The strongest lever. On Linux this is the `sudo`/`wheel` group. |
| Deny network sign-in | ✅ | ✅ | ❌ | Windows has a separate network sign-in plane; Linux does not — SSH is covered by the switch below. |
| Deny remote sign-in | ✅ | ✅ | ✅ | Remote Desktop on Windows, SSH on Linux. |
| Lock a session | ✅ | ✅ | desktop only | Needs a desktop session; a headless server has nothing to lock. |
| Sign an account out | ✅ | ✅ | ✅ | Ends the session — unsaved work may be lost. |
| Warn the person first | ✅ | ✅ | ❌ | Linux has no reliable way to put a message on a signed-in person's screen, so the action happens immediately. |
| Delete an account | ✅ | ✅ | ✅ | For a Microsoft account this unlinks it from **this PC** only. root cannot be deleted. |
| Create an account | ✅ | ❌ | ✅ | A Microsoft account can only be added at the PC itself. Linux has no such asymmetry. |
| Password policy | ✅ | ❌ | mostly | Microsoft accounts follow Microsoft's own cloud policy. On Linux, lockout needs the login system to already use `faillock`. |

Where an action is unavailable for a particular account, the dashboard **shows it
greyed out with the reason** rather than hiding it, so the limitation is visible instead
of mysterious. The agent refuses the same set — it reads the very list the panel shows,
so what kenny advertises and what it enforces cannot drift apart.

### Deliberately not offered

**Denying interactive (console) sign-in.** It can lock out the only person who can use
the machine, and kenny has no way to reach it to undo that.

### What differs on Linux, and why

Four honest gaps, all visible in the dashboard rather than buried here:

- **No network sign-in to deny.** Windows separates file-sharing/network sign-in from
  Remote Desktop. Linux has one remote plane and it is SSH, so the first switch is greyed
  out and the second one covers what there is.
- **Locking needs a desktop.** On a headless server kenny can *end* a session but not
  lock one, so the lock button is greyed with that reason.
- **Lockout needs `faillock`.** kenny will set the threshold where the machine's login
  configuration already uses `pam_faillock`, and reports the field as unavailable where it
  does not. kenny **never edits `/etc/pam.d`** — a mistake there locks out every form of
  sign-in at once.
- **sudoers-granted admins cannot be revoked.** If an account gets administrator rights
  from a rule in `/etc/sudoers.d` rather than group membership, kenny reports it as an
  administrator and says it cannot change that. kenny **never edits sudoers** — a syntax
  error there removes `sudo` from the whole machine. Fix such a grant at the machine.

And one place Linux is *more* capable: creating an account has no asymmetry there, unlike
a Windows PC where a Microsoft account can only be added at the machine itself.

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
restrict the **last enabled administrator** on a machine, and will not delete a built-in
account — the Windows Administrator and Guest accounts, or `root` on Linux. It also
refuses any action its own inventory reports as unavailable for that account, so the
greyed-out buttons in the dashboard are not merely cosmetic. All of this is enforced on
the machine itself and cannot be overridden from the server — the same way the web filter
refuses a list that would cut off its own connection.

**Every change needs operator rights and a confirmation.** Account governance requires
the `operator` role (a scoped `user` sees the inventory read-only), and when Claude
proposes one of these actions in chat it stops and waits for you, like every other
state-changing tool.

That holds on the [Discord support surface](itsm.md) too, and by more than one
mechanism. Every account tool is classified `normal_change` — the tier that never runs
without an operator's approval — and none of them appears in any capability profile, so a
family member working a ticket is not offered them at all and is refused at dispatch if
the assistant asks for one anyway. Deciding who may sign in to a PC is not something a
support conversation can reach.

**Every call is written to the audit log** — visible in the dashboard's
[Log](dashboard.md#log) page, filtered to the TOOLS chip.

!!! note "Monitoring is the guarantee, enforcement is best-effort"
    All of these actions are refused while the person at the machine has **remote control
    switched off** ([the kill switch](user-guide.md)) — and because the switch is
    theirs to flip, a standard user can still refuse *new* changes. Changes already
    applied stay applied.

    This is the same stance as the web filter, and it is why the **drift signal**
    matters more than the enforcement: kenny reports when an account appears, gains
    administrator rights, is re-enabled, has its restrictions cleared, or is newly
    linked to a Microsoft account — whether or not the change came from kenny.

## What kenny reports

Two telemetry sections feed this page.

**`local_accounts`** — every account on the machine with its kind (local, Microsoft,
work/school), whether it is enabled, whether it is an administrator, which sign-in
restrictions are set, and which actions are unavailable for it. Plus the machine's
password policy. On Linux the account list is deliberately short: root and real people,
never the two dozen service accounts you could do nothing about anyway. Health rules warn
about an enabled built-in Administrator or Guest account **on Windows** (root being
enabled on Linux is not a finding, it is Linux), an administrator that permits a blank
password, and an administrator that also carries sign-in restrictions (a contradiction —
one of the two settings is stale).

**`logon_failures`** — failed sign-in attempts per account over the last 24 hours, split
by whether they happened at the keyboard, over the network, or remotely. On Windows this
reads the Security log; on Linux it reads the SSH and PAM failures from the journal, so a
failed `sudo` at the machine and a failed SSH login from elsewhere are told apart. A
burst against one account warns; the same number spread across the household does not.
Attempts against usernames that do not exist on the machine are counted but never named.

!!! note "An internet-facing Linux box will warn regularly"
    Anything with SSH open to the internet collects dozens of failed attempts a day
    against usernames that do not exist. That is real and worth seeing — it is the same
    signal `fail2ban` exists for — and it is counted without ever naming the probed
    usernames.

kenny takes the **account name** and the display name the user chose for themselves. It
does **not** put Microsoft account email addresses or Windows security identifiers on
the wire.

## Recovering from a mistake

If you suspend or restrict the wrong account, undo it from the same panel — the change
takes effect at the next sign-in attempt. If a machine ends up with no usable
administrator (kenny will not cause this, but a manual change might), you need physical
access to it and its own recovery options — Windows recovery, or a Linux root shell from
the boot loader. kenny cannot help from the server.

## See also

- [Parental controls](parental-controls.md) — web activity, filtering, screen time
- [Dashboard reference](dashboard.md) — where the accounts panel lives
- [Telemetry reference](telemetry.md) — the raw section shapes
- [ADR-0042](adr/0042-account-governance-local-and-microsoft.md) — why it is built this way
- [ADR-0043](adr/0043-account-governance-on-linux.md) — how Linux fits into the same surface
