# 0025. Vendor a charting library (Apache ECharts) for the fleet Overview dashboard

- Status: accepted
- Date: 2026-06-07

## Context and Problem Statement

The operator dashboard (`kenny-server/kenny_server/webui/index.html`) is a single
vanilla-JS page that was deliberately kept **dependency-light and CDN-free**: the only
data visual was a hand-rolled inline-SVG sparkline, and the code comments call out
"keep it dependency-light". We now want a high-level **Overview** dashboard that
verdichtet the whole fleet into dynamic pie/donut, stacked-bar, Sankey, ranked-bar and
time-series charts, each with click-through drill-down.

Hand-rolling all of those chart types in SVG — especially a Sankey with layout and an
interactive stacked time-series — is a large amount of fiddly, hard-to-maintain code.
The question is how to render rich, interactive charts without taking on a CDN
dependency at runtime (the deployment is self-hosted and may run without outbound
internet), and without drifting from the existing visual language.

## Considered Options

- **Keep hand-rolling vanilla SVG** for every chart type.
- **Apache ECharts, vendored locally** (a single UMD bundle served from `/assets`).
- **Chart.js + a Sankey plugin**, vendored locally.
- **D3**, vendored locally.

## Decision Outcome

Chosen option: **Apache ECharts, vendored locally**, because a single self-contained
bundle covers every chart type we need (pie, stacked bar, Sankey, line/area) with
built-in interaction (tooltips, click events for drill-down), it is Apache-2.0
licensed, and it themes cleanly from our existing CSS tokens so the charts match the
console. The bundle (`kenny_server/webui/assets/echarts.min.js`, ~1 MB) is committed and
served through the existing whitelisted `/assets/{name}` route (extended to allow `.js`),
so the runtime **never reaches for a CDN**. Chart.js needs a third-party plugin for
Sankey; D3 would mean re-implementing most of what ECharts gives for free; pure SVG was
rejected as too much bespoke code for the interactive Sankey/time-series.

This reverses the prior "no chart library" stance for the dashboard, which is why it is
recorded here rather than as an implementation detail.

### Consequences

- Good, because rich, interactive, drill-down-able charts ship with far less bespoke code.
- Good, because no runtime CDN dependency — the bundle is vendored and served locally,
  preserving offline/self-hosted operation.
- Good, because charts read their colours from the active theme's CSS custom properties,
  so dark/light and the warm palette stay consistent.
- Bad, because the repo carries a ~1 MB vendored third-party JS blob that must be updated
  manually and kept pinned (currently ECharts 5.5.1).
- Neutral: the wire contract, `PROTOCOL_VERSION`, fixtures and the agent are untouched —
  the new endpoints (`/api/fleet/overview`, `/api/fleet/trend`) are read-only aggregations
  over stored telemetry.

## More Information

- The fleet aggregation lives in `kenny-server/kenny_server/fleet_stats.py` (pure,
  unit-tested) and is exposed by the read-only routes in `kenny_server/webui/__init__.py`.
- Health thresholds remain the source of truth in `health_rules.py`; the dashboard only
  visualises their output.
- ADR-0008 (operator auth) — the new routes inherit the same `/api` operator gate.
