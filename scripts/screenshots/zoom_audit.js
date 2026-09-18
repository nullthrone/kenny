() => {
  // The floor, and the controls it has to hold. WebKit zooms for text entry of
  // every kind and for <select>; an <input> with no type attribute is a text
  // field, so the selector subtracts the types that do not zoom rather than
  // listing the ones that do. Kept character-for-character in step with the
  // rule in kenny-web/src/styles/global.css.
  const FLOOR = 16
  const ZOOMABLE =
    "input:not([type='checkbox']):not([type='radio']):not([type='button'])" +
    ":not([type='submit']):not([type='reset']):not([type='range'])" +
    ":not([type='color']):not([type='file']), textarea, select"

  const describe = (el) => {
    const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/).slice(0, 2).join('.') : ''
    return el.tagName.toLowerCase() + (cls ? '.' + cls : '')
  }

  // Whatever lets a human find the field again on the page.
  const label = (el) =>
    el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || ''

  const findings = []
  let measured = 0

  // Not filtered by visibility: a control inside a collapsed panel resolves the
  // same font-size it will have when shown, and skipping it would quietly
  // shrink what this audit claims to cover.
  for (const el of document.querySelectorAll(ZOOMABLE)) {
    measured++
    const px = parseFloat(getComputedStyle(el).fontSize)
    if (px < FLOOR - 0.01) findings.push({ el: describe(el), px, label: label(el) })
  }

  return { measured, findings }
}
