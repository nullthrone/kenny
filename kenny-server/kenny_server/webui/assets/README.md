# Vendored browser assets

The dashboard is one hand-written page: no build step, no framework, no package manager.
Anything it needs from outside the repository is committed here and served by the
whitelisted `/assets/{name}` route.

## `echarts.min.js` — Apache ECharts 5.6.0, Apache-2.0

**Never load this from a CDN.** A kenny deployment may sit on a box with no outbound
internet; the console has to render from the machine it runs on.

It earns its ~1 MB because the Overview needs pie/donut, stacked bar, Sankey and an
interactive stacked time series, each with click-through drill-down — inline SVG is the
right call for a sparkline and the wrong one for a Sankey with layout. Chart.js needs a
third-party plugin for Sankey; D3 would mean re-implementing most of this.

Charts read their colours from the active theme's CSS custom properties, so dark/light
needs no second theme definition.

Upgrading is manual: replace the file, update the version above, check the Overview in
both themes.
