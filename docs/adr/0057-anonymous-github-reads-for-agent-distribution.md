# 0057. Read GitHub releases anonymously for agent distribution

- Status: accepted
- Date: 2026-09-05

Boundary moved: network/trust topology — the server fetches the agent binary and release
metadata unauthenticated; a GitHub credential is no longer a precondition of the
distribution path.

## Context and Problem Statement

ADR-0015 gave the server an auto-fetch for the prebuilt agent binary and gated it on a
configured `KENNY_GITHUB_TOKEN`, "unlocks private repos and avoids anonymous API rate
limits". ADR-0044's About dialog later grew a live changelog over the same API, which sent
the same credential.

Everything both paths read is public: the release list of `nullthrone/kenny`, its assets,
and their `.sha256` siblings. The credential bought two things the canonical deployment does
not use — access to a private release repo, and a higher rate limit than a daily poll can
approach — and charged a failure mode for them.

That bill came due. On 2026-08-20 the repository moved from the `t11z` user to the
`nullthrone` organisation. The configured token's authority did not move with it, and GitHub
began answering `403` — recognised, but not authorised for this repository. Because the
fetch was gated on that credential, an authorization problem that had no bearing on the data
being read stopped the agent auto-fetch outright. The fleet sat on agent 2.1.0 for a month
while releases 2.2.0, 2.2.1 and 2.2.2 came and went, and the changelog reported nothing at
all.

Should reading public release data continue to require a credential?

## Considered Options

- **A. Read anonymously, always.** No credential on either path; a private release repo
  falls back to the manual `KENNY_AGENT_BINARY` that ADR-0015 already provides.
- **B. Keep the credential, add an anonymous fallback** when it is rejected.
- **C. Keep the gate, improve the error message** so the operator repairs the token faster.

## Decision Outcome

Chosen option: **A**. `agent_release` and `changelog` send no `Authorization` header, under
any configuration. There is one code path, not a primary and a fallback.

**B** was rejected because a fallback is a second path that only runs when the first one is
broken — the least-exercised code in the system, reached exactly when something is already
wrong. It also softens a real misconfiguration into a warning nobody has to act on, which is
the failure mode this whole area has been recovering from: kenny reporting a problem in a way
that lets it persist. **C** leaves a credential as a precondition for reading public data,
which is the defect itself rather than a symptom of it.

`KENNY_GITHUB_TOKEN` is not retired. Its scope narrows to the GHCR poll for a newer server
image (ADR-0040), where a private container package genuinely needs one.

### Consequences

- Good: the distribution path has no credential to expire, be revoked, lose authority in an
  organisation transfer, or be scoped wrong. The outage above cannot recur in this form.
- Good: one path, always exercised — the anonymous read is not a rarely-taken branch.
- Good: one less credential on outbound requests, and one less place it can leak.
- Neutral: the trust model is unchanged. Downloads are still verified against the `.sha256`
  published beside them in the same release. That check never defended against a hostile
  GitHub — it defends against a corrupted transfer — and removing a request header does not
  change what it covers.
- Bad: **a private release repo loses auto-fetch.** Anonymously it answers `404`, and the
  operator must hand-place `KENNY_AGENT_BINARY`. ADR-0015 already describes that path as the
  standing fallback; what changes is that it becomes mandatory rather than optional for a
  private fork. The 404 message says so directly.
- Bad: the API budget drops from 5000 to 60 requests per hour, **per IP**. kenny's own draw
  is far below it — the changelog is capped by a 5-minute process-local cache at 12/h, the
  update check costs 2–4/day, and asset downloads go to `objects.githubusercontent.com`
  without touching the API budget — but the limit is shared with anything else behind the
  same address, which matters behind CGNAT. A rate-limited response is now reported as one,
  naming the reset time, instead of being conflated with an authorization refusal.

## More Information

- Supersedes ADR-0015's decision that the fetch is "gated on a configured
  `KENNY_GITHUB_TOKEN`". The rest of [ADR-0015](0015-agent-binary-auto-fetch.md) stands: the
  auto-fetch itself, sha256 verification, the atomic cache write, the precedence of an
  operator-placed `KENNY_AGENT_BINARY`, and the release tag as the leading version source.
- Leaves [ADR-0040](0040-scheduled-update-detection-and-operator-approved-rollout.md)
  untouched: the GHCR image poll is a different registry with a different access model, and
  still accepts a token for a private package.
- Implementation: `kenny-server/kenny_server/agent_release.py` (`GITHUB_HEADERS`,
  `describe_http_error`), `kenny-server/kenny_server/changelog.py`. The decision is pinned by
  tests asserting that no request carries an `Authorization` header even when
  `KENNY_GITHUB_TOKEN` is set.
