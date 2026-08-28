# Vendored browser assets

Everything the dashboard loads from the server rather than from its own bundle is
committed here and served by the whitelisted `/assets/{name}` route: third-party
libraries that must not come from a CDN, and the brand/profile images the page and the
login screen reference by URL.

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

## `dog-*.png` — selectable profile avatars

`avatars/dog-*.svg` is the source; the sibling `dog-*.png` is what the browser gets.
`/assets/{id}.png` is public (the login screen shows an avatar before a session exists),
so the ids in `webui/users.py`'s `AVATARS`, the SVG sources and the PNGs have to stay in
step — `tests/test_rbac.py::test_avatar_sources_and_rasters_match` fails when they drift.

They are drawn from the design tokens only (`kenny-web/src/styles/tokens/colors.css`):
one paper ground, fur from the ink and brass ramps, an ink plinth, and brass for the
collar. Breeds are told apart by silhouette and value, not by hue, so the set reads as
one family at the 24px sizes the UI uses. Each is a full-bleed 128x128 square because
every frame that shows one is square.

Editing one means re-rasterizing it at 128x128 — any SVG renderer will do, e.g.

    python -m pip install cairosvg   # needs libcairo; not a project dependency
    python -c "import cairosvg,pathlib; [cairosvg.svg2png(url=str(p), \
      write_to=str(pathlib.Path(p.name).with_suffix('.png')), \
      output_width=128, output_height=128) \
      for p in sorted(pathlib.Path('avatars').glob('dog-*.svg'))]"
