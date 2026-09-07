() => {
  const TOL = 0.5
  const escapes = []

  const describe = (el) => {
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 3).join('.') : ''
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '')
  }

  const paints = (cs) =>
    parseFloat(cs.borderTopWidth) > 0 ||
    parseFloat(cs.borderRightWidth) > 0 ||
    parseFloat(cs.borderBottomWidth) > 0 ||
    parseFloat(cs.borderLeftWidth) > 0 ||
    (cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent')

  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el)
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') continue
    const r = el.getBoundingClientRect()
    if (r.width === 0 || r.height === 0) continue
    if (r.width <= 1 && r.height <= 1) continue // visually-hidden pattern

    let p = el.parentElement
    while (p && p !== document.body) {
      const ps = getComputedStyle(p)
      if (ps.overflowX !== 'visible' || ps.overflowY !== 'visible') break // already clipped
      if (paints(ps)) {
        const pr = p.getBoundingClientRect()
        const over = {
          left: pr.left - r.left,
          right: r.right - pr.right,
          top: pr.top - r.top,
          bottom: r.bottom - pr.bottom,
        }
        const worst = Object.entries(over).filter(([, v]) => v > TOL)
        if (worst.length) {
          escapes.push({
            el: describe(el),
            box: describe(p),
            by: Object.fromEntries(worst.map(([k, v]) => [k, Math.round(v)])),
            text: (el.textContent || '').trim().slice(0, 60),
          })
        }
        break
      }
      p = p.parentElement
    }
  }

  const de = document.documentElement
  return { escapes, pageOverflow: de.scrollWidth - de.clientWidth }
}
