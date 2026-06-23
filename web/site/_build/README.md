# bismuth.cz site builder

Static builder for bismuth.cz. One template (`build_site.py`) → consistent SEO head
(meta/OpenGraph/Twitter/JSON-LD), unified nav + download buttons + footer, dark theme.

- `gen_catalog.py` — Interview & Coverage Catalog data + md/bbcode output.
- `build_site.py` — template helpers (head/nav/footer/dlbuttons) + coverage-md parsing.
- `build_pages.py` — renders index, 10-years, catalog, coverage/* articles, sitemap.xml, robots.txt.

Coverage article sources live in `web/site/coverage/*.md` (cleaned from the PDFs in
`/var/www/bismuth.cz/backups/`). Rebuild: `python3 build_pages.py` (writes to `$SITE_OUT`,
default /tmp/site_out), then deploy to /var/www/bismuth.cz.
