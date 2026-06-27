# Bismuth — daring graphics

Bolder, more experimental SVG art riffing on the **historical** Bismuth designs (the `graphics/` folder
on the `archive` branch: the glossy two-droplet mark, the peer-to-peer mesh, the perspective tunnel, the
"riot" concentric line-art). All vector, all on the official brand palette — blue `#5fa1ee` / purple
`#b364c2` (+ the gradient between them). Mark geometry unchanged from `web/site/favicon.svg`.

| File | Riffs on |
|------|----------|
| `A-mesh-network`     | peer-to-peer mesh (mesh2-2 / main_) — the B as a network hub |
| `B-concentric-lineart` | the "riot" nested-outline treatment |
| `C-tunnel`           | the perspective/depth tunnel (main__) |
| `D-constellation`    | the network starfield (main_) |
| `E-glossy-3d`        | the dimensional, sheened logo (logo.png / icon.jpg) |
| `F-hex-node`         | the mark as a hypernode badge |

Sources in `svg/`, 1000px PNG previews in `png/`, all six in `contact-sheet.png`.
Re-render: `rsvg-convert -w <px> svg/<name>.svg -o out.png`.
