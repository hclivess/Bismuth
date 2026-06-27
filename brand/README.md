# Bismuth brand / logo

Modernized treatments of the Bismuth mark. **All variants use only the two official brand colors** —
no off-brand palette:

- **Blue** `#5fa1ee`
- **Purple** `#b364c2`
- Brand gradient: `#5fa1ee → #b364c2`

The mark geometry is unchanged from the original `web/site/favicon.svg`; only the framing/fill treatment
varies. Sources are vector (`svg/`); 512×512 PNG previews are in `png/`. See `contact-sheet.png` for all
twelve at a glance.

| File | Treatment |
|------|-----------|
| `01-brand-original` | Canonical two-tone (blue top / purple bottom) on white |
| `02-white-on-brandgrad` | White mark on the blue→purple brand gradient |
| `03-brandgrad-mark` | Brand gradient flowing across the mark, white background |
| `04-brandgrad-on-dark` | Gradient mark on dark `#161320` |
| `05-two-tone-dark` | Brand two-tone on dark |
| `06-mono-blue` | Single brand blue |
| `07-mono-purple` | Single brand purple |
| `08-outline-grad` | Hollow gradient line-art |
| `09-circle-badge` | White mark on a gradient circle (social avatars) |
| `10-brandgrad-on-black` | Gradient mark on black (high contrast) |
| `11-depth-two-tone` | Subtle shading within the same two brand colors |
| `12-white-on-blue` | White + purple mark on solid brand blue |

All icon tiles are 1024×1024 with a 232px corner radius (Apple/Android icon-grid friendly); `09` is a
full-bleed circle. Re-render any size with `rsvg-convert -w <px> -h <px> svg/<name>.svg -o out.png`.
