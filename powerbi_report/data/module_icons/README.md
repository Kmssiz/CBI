# Module icons (Odoo 13 style)

Ready to upload as the logo of a module in Administration > Serveurs (PNG, 140x140, well under the 2 MB limit).

| Module | File | Source |
|---|---|---|
| Achats | `achats.png` | Odoo 13 `purchase` icon |
| Assurances | `assurances.png` | drawn here (no Odoo community addon) |
| Comptabilité | `comptabilite.png` | Odoo 13 `account` icon |
| Entrepôt | `entrepot.png` | Odoo 13 `stock` icon |
| GEDISO | `gediso.png` | drawn here (Odoo's Documents is Enterprise only) |
| GMAO | `gmao.png` | Odoo 13 `maintenance` icon |
| GPAO | `gpao.png` | Odoo 13 `mrp` icon |
| Immobilisation | `immobilisation.png` | drawn here (Odoo's Assets is Enterprise only) |
| Parc Auto | `parc-auto.png` | Odoo 13 `fleet` icon |

The six real icons are `addons/<addon>/static/description/icon.png` of the `13.0` branch of
https://github.com/odoo/odoo (LGPL-3.0, like the addons themselves). The three drawn ones are built by
`tools/build_module_icons.py` (their `.svg` sources are next to the PNGs) from Material Design glyphs
(Apache 2.0), in the same flat tile with a long shadow.
