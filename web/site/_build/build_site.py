# -*- coding: utf-8 -*-
"""Unified bismuth.cz builder — one template for every page: shared SEO head (meta/OG/Twitter/JSON-LD),
unified nav + download buttons + footer, dark theme. Renders the reorganized landing page, the 10-years
retrospective, the press/coverage catalog (linking local cleaned copies), the cleaned coverage articles,
plus sitemap.xml + robots.txt. Source of truth: web/site/*.md + the catalog data below + /tmp/coverage_md.
"""
import html as _h, os, glob, re, datetime
import markdown2

BASE = "https://bismuth.cz"
OUT = os.environ.get("SITE_OUT", "/tmp/site_out")
COVERAGE_MD = "/tmp/coverage_md"
SITE_MD = "/root/bismuth-claude/Bismuth/web/site"   # 10-years md/bbcode source
os.makedirs(OUT, exist_ok=True)
os.makedirs(OUT + "/coverage", exist_ok=True)
e = _h.escape
PAGES = []   # (relpath, lastmod_priority) for sitemap


def head(title, desc, canonical, og_type="website", jsonld=None, extra_css=""):
    j = "\n  <script type='application/ld+json'>%s</script>" % jsonld if jsonld else ""
    return f"""<!doctype html>
<html lang='en' data-bs-theme='dark'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{e(title)}</title>
  <meta name='description' content='{e(desc)}'>
  <link rel='canonical' href='{e(canonical)}'>
  <meta name='robots' content='index, follow, max-image-preview:large'>
  <meta name='theme-color' content='#0d1117'>
  <meta name='author' content='Bismuth Foundation'>
  <meta property='og:type' content='{og_type}'>
  <meta property='og:site_name' content='Bismuth'>
  <meta property='og:title' content='{e(title)}'>
  <meta property='og:description' content='{e(desc)}'>
  <meta property='og:url' content='{e(canonical)}'>
  <meta property='og:image' content='{BASE}/assets/og.png'>
  <meta property='og:image:width' content='1200'>
  <meta property='og:image:height' content='630'>
  <meta name='twitter:card' content='summary_large_image'>
  <meta name='twitter:title' content='{e(title)}'>
  <meta name='twitter:description' content='{e(desc)}'>
  <meta name='twitter:image' content='{BASE}/assets/og.png'>
  <link rel='icon' href='/favicon.svg'>
  <link href='/assets/bootstrap.min.css' rel='stylesheet'>
  <style>
    body {{ background:#0d1117; }}
    .hero {{ background:linear-gradient(180deg,#16263a 0%, transparent 100%); }}
    a {{ overflow-wrap:anywhere; }}
    article {{ font-size:1.04rem; line-height:1.7; }}
    article h2 {{ font-size:1.4rem; margin-top:2rem; margin-bottom:.6rem; padding-bottom:.3rem; border-bottom:1px solid #21262d; }}
    article h3 {{ font-size:1.15rem; margin-top:1.4rem; }}
    article p {{ margin-bottom:1rem; }} article a {{ color:#6cb4ff; }}
    article ul, article ol {{ margin-bottom:1rem; }} article li {{ margin-bottom:.4rem; }}
    article blockquote {{ border-left:3px solid #30475e; padding-left:1rem; color:#adbac7; }}
    article code {{ color:#7ee787; }} article em {{ color:#c9d1d9; }} article hr {{ border-color:#21262d; }}
    .dl .btn {{ margin:.15rem .15rem 0 0; }}
    pre {{ background:#010409; border:1px solid #21262d; border-radius:.5rem; padding:1rem; color:#c9d1d9; }}
    code.tok {{ color:#7ee787; }}
    {extra_css}
  </style>{j}
</head>
<body>
"""


def nav(active=""):
    items = [("/", "Home", "home"), ("/#wallets", "Wallets", "wallets"),
             ("/10-years-of-bismuth.html", "The Story", "story"),
             ("/Bismuth_Interview_Catalog.html", "Press", "press"),
             ("https://explorer.bismuth.cz/", "Explorer", "explorer"),
             ("/#run", "Run a node", "run"),
             ("https://bismuthcoin.org/", "Community", "community"),
             ("https://github.com/hclivess/Bismuth", "GitHub", "github")]
    links = []
    for href, label, key in items:
        cls = "navbar-text text-decoration-none me-3" + (" fw-semibold text-info" if key == active else "")
        links.append(f"<a class='{cls}' href='{href}'>{label}</a>")
    return ("  <nav class='navbar border-bottom border-secondary-subtle'>\n    <div class='container'>\n"
            "      <a class='navbar-brand fw-semibold text-decoration-none d-inline-flex align-items-center gap-2' href='/'>"
            "<img src='/favicon.svg' alt='' height='24' width='16'> Bismuth</a>\n"
            "      <div class='d-flex flex-wrap align-items-center'>\n        " + "\n        ".join(links) +
            "\n      </div>\n    </div>\n  </nav>\n")


def footer():
    return ("  <footer class='container text-center text-secondary small py-4 border-top border-secondary-subtle'>\n"
            "    Bismuth — fair-launched since 1 May 2017 · "
            "<a class='link-secondary' href='https://explorer.bismuth.cz/'>Explorer</a> · "
            "<a class='link-secondary' href='https://bismuthcoin.org/'>Community</a> · "
            "<a class='link-secondary' href='https://github.com/hclivess/Bismuth'>GitHub</a> · "
            "<a class='link-secondary' href='/Bismuth_Interview_Catalog.html'>Press</a>\n"
            "  </footer>\n</body>\n</html>\n")


def dlbuttons(items):
    """Unified download-button group. items = [(href,label,_ignored), ...].

    ONE consistent look for every file download across the whole site (btn-outline-info):
    the same action must never render filled in one place and hollow in another. Filled
    buttons (btn-info / btn-primary) are reserved for navigation CTAs (Explorer, Read on
    site, Browse catalog), never for downloads. The third tuple element is ignored, kept
    only so existing callers don't break."""
    out = ["<div class='dl d-flex flex-wrap gap-2'>"]
    for item in items:
        href, label = item[0], item[1]
        out.append(f"<a href='{e(href)}' class='btn btn-sm btn-outline-info'>{e(label)}</a>")
    out.append("</div>")
    return "".join(out)


def write(relpath, content, priority="0.6"):
    path = OUT + "/" + relpath
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(content)
    PAGES.append((relpath, priority))


# ----------------------------------------------------------------- coverage md parsing ----
def parse_md(path):
    raw = open(path, encoding="utf-8").read()
    meta, body = {}, raw
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.S)
    if m:
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                meta[k.strip()] = v.strip()
        body = m.group(2)
    return meta, body


def coverage_articles():
    """slug -> (meta, body_html) for every cleaned article on disk."""
    arts = {}
    for p in sorted(glob.glob(COVERAGE_MD + "/*.md")):
        slug = os.path.splitext(os.path.basename(p))[0]
        meta, body = parse_md(p)
        arts[slug] = (meta, markdown2.markdown(body, extras=["cuddled-lists", "tables", "smarty-pants", "break-on-newline"]))
    return arts


if __name__ == "__main__":
    import build_pages
    build_pages.run(globals())
