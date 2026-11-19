"""
Builds the module icons that Odoo 13 does not ship, in the same flat-tile-with-long-shadow look.

    python tools/build_module_icons.py

Six of the nine module icons in powerbi_report/data/module_icons/ are the real icon.png of the Odoo 13
community addons (see the README there). Assurances, GEDISO and Immobilisation have no community
addon (Documents and Assets are Enterprise-only, there is no insurance addon), so they are drawn here
with Material Design glyphs (Apache 2.0). The SVGs are written next to the PNGs; rasterise them at
140x140 (the size of the Odoo icons), e.g. with node + @resvg/resvg-js.
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / 'powerbi_report' / 'data' / 'module_icons'

GLYPHS = {
    'shield': 'M12 1L3 5v6c0 5.55 3.84 10.74 9 12 5.16-1.26 9-6.45 9-12V5l-9-4zm-2 16l-4-4 1.41-1.41L10 14.17l6.59-6.59L18 9l-8 8z',
    'document': 'M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2zm0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z',
    'bank': 'M4 10v7h3v-7H4zm6 0v7h3v-7h-3zM2 22h19v-3H2v3zm14-12v7h3v-7h-3zm-4.5-9L2 6v2h19V6l-9.5-5z',
}

# file name -> (glyph, tile colour)
ICONS = {
    'assurances': ('shield', '#5E9E8C'),
    'gediso': ('document', '#C68B52'),
    'immobilisation': ('bank', '#9A6B87'),
}

SIZE = 140
SCALE = 4.1                                  # the 24 px glyph fills ~70 % of the tile, as in Odoo
SHADOW_STEPS = 90                            # the long shadow is the glyph repeated along the diagonal

TEMPLATE = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" role="img">
  <defs>
    <clipPath id="tile"><rect width="{size}" height="{size}" rx="9"/></clipPath>
    <linearGradient id="sheen" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>
    <linearGradient id="fade" gradientUnits="userSpaceOnUse" x1="{fade_from}" y1="{fade_from}" x2="{size}" y2="{size}">
      <stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#000"/>
    </linearGradient>
    <mask id="shadowMask"><rect width="{size}" height="{size}" fill="url(#fade)"/></mask>
    <path id="g" d="{glyph}"/>
  </defs>
  <rect width="{size}" height="{size}" rx="9" fill="{colour}"/>
  <rect width="{size}" height="{size}" rx="9" fill="url(#sheen)"/>
  <g clip-path="url(#tile)" mask="url(#shadowMask)" opacity=".24" fill="#000">
{shadow}
  </g>
  <use href="#g" transform="translate({offset} {offset}) scale({scale})" fill="#fff"/>
</svg>
'''


def build(glyph: str, colour: str) -> str:
    offset = (SIZE - 24 * SCALE) / 2
    shadow = '\n'.join(
        f'    <use href="#g" transform="translate({offset + step * 1.2:.1f} {offset + step * 1.2:.1f}) scale({SCALE})"/>'
        for step in range(SHADOW_STEPS)
    )
    return TEMPLATE.format(
        size=SIZE, glyph=glyph, colour=colour, scale=SCALE, offset=f'{offset:.1f}',
        shadow=shadow, fade_from=int(offset),
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (glyph, colour) in ICONS.items():
        (OUT / f'{name}.svg').write_text(build(GLYPHS[glyph], colour), encoding='utf-8')
    print(f'{len(ICONS)} icons written to {OUT}')


if __name__ == '__main__':
    main()
