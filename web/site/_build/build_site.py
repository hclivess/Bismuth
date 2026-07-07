# -*- coding: utf-8 -*-
"""Unified bismuth.cz builder — one template for every page: shared SEO head (meta/OG/Twitter/JSON-LD),
unified nav + download buttons + footer, dark theme. Renders the reorganized landing page, the 10-years
retrospective, the press/coverage catalog (linking local cleaned copies), the cleaned coverage articles,
plus sitemap.xml + robots.txt. Source of truth: web/site/*.md + the catalog data below + /tmp/coverage_md.

Design: a self-contained, framework-free dark theme (no Bootstrap, no web fonts, no external requests) —
the same visual language as nadochain.com, re-palletted to Bismuth's blue -> purple crystal identity.
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

# Extensionless page URLs (nginx try_files serves the .html file; links + canonicals drop the suffix).
URL_STORY = "/10-years-of-bismuth"
URL_PRESS = "/Bismuth_Interview_Catalog"


# The entire design system — one <style> block, hand-rolled, CSP-safe (system fonts, CSS gradients only).
CSS = """
    :root{
      --bg:#0b0f14; --panel:#111823; --line:#1c2530; --txt:#e6edf3; --muted:#8b9bab; --faint:#5c6b7a;
      --accent:#5fa1ee; --accent2:#b364c2; --deep:#243b6b;
      --sans:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
      --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    }
    *{box-sizing:border-box}
    html{scroll-behavior:smooth}
    body{margin:0;background:var(--bg);color:var(--txt);font:16px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
    code{font-family:var(--mono);font-size:.92em;color:#9fd0ff}
    a{color:var(--accent);text-decoration:none;overflow-wrap:anywhere}
    a:hover{text-decoration:underline}
    .wrap{max-width:1040px;margin:0 auto;padding:0 20px}
    .glow{position:fixed;inset:0;z-index:-1;overflow:hidden;background:
      radial-gradient(60% 50% at 50% -10%,rgba(95,161,238,.13),transparent 70%),
      radial-gradient(40% 40% at 90% 10%,rgba(179,100,194,.08),transparent 70%)}
    .orb{position:absolute;border-radius:50%;filter:blur(90px);opacity:.5;will-change:transform;mix-blend-mode:screen;pointer-events:none}
    .orb.a{width:46vw;height:46vw;max-width:620px;max-height:620px;top:-12%;left:-8%;
      background:radial-gradient(circle,rgba(95,161,238,.55),transparent 68%);animation:drift1 26s ease-in-out infinite}
    .orb.b{width:40vw;height:40vw;max-width:540px;max-height:540px;top:8%;right:-10%;
      background:radial-gradient(circle,rgba(179,100,194,.42),transparent 68%);animation:drift2 32s ease-in-out infinite}
    .orb.c{width:34vw;height:34vw;max-width:460px;max-height:460px;bottom:-14%;left:28%;
      background:radial-gradient(circle,rgba(36,59,107,.65),transparent 70%);animation:drift3 38s ease-in-out infinite}
    @keyframes drift1{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(6vw,7vh) scale(1.12)}66%{transform:translate(-4vw,3vh) scale(.94)}}
    @keyframes drift2{0%,100%{transform:translate(0,0) scale(1)}50%{transform:translate(-7vw,6vh) scale(1.16)}}
    @keyframes drift3{0%,100%{transform:translate(0,0) scale(1)}40%{transform:translate(5vw,-5vh) scale(1.1)}75%{transform:translate(-3vw,-2vh) scale(.96)}}
    @media(prefers-reduced-motion:reduce){.orb{animation:none!important}}
    header.nav{display:flex;align-items:center;justify-content:space-between;padding:20px 0;gap:16px;flex-wrap:wrap}
    .brand{display:flex;align-items:center;gap:12px;font-weight:800;letter-spacing:.5px;font-size:20px;color:var(--txt)}
    .brand:hover{text-decoration:none}
    .brand img{width:30px;height:30px}
    nav a{color:var(--muted);margin-left:22px;font-size:15px;font-weight:600}
    nav a:hover{color:var(--txt);text-decoration:none}
    nav a.on{color:var(--txt)}
    .btn{display:inline-block;background:linear-gradient(135deg,var(--accent),var(--accent2));color:#08111f;
      font-weight:800;padding:14px 26px;border-radius:12px;font-size:16px;border:0;cursor:pointer;
      box-shadow:0 6px 24px rgba(95,161,238,.30);transition:transform .12s ease, box-shadow .12s ease}
    .btn:hover{text-decoration:none;transform:translateY(-1px);box-shadow:0 10px 30px rgba(95,161,238,.42)}
    .btn.ghost{background:transparent;color:var(--txt);border:1px solid var(--line);box-shadow:none;font-weight:700}
    .btn.ghost:hover{border-color:var(--accent)}
    .btn.sm{padding:9px 16px;font-size:14px;font-weight:700;border-radius:10px;box-shadow:none}
    .btn.sm.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
    .btn.sm.ghost:hover{border-color:var(--accent);color:var(--txt)}
    .btnrow{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
    .hero{text-align:center;padding:52px 0 40px}
    .hero img.mark{width:96px;height:96px;filter:drop-shadow(0 8px 30px rgba(95,161,238,.35));margin-bottom:8px}
    .hero h1{font-size:clamp(32px,6vw,56px);line-height:1.08;margin:16px 0 10px;letter-spacing:-.5px}
    .hero h1 .g{background:linear-gradient(120deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
    .hero p.lead{font-size:clamp(17px,2.4vw,21px);color:var(--muted);max-width:720px;margin:0 auto 26px}
    .cta{display:flex;gap:14px;justify-content:center;flex-wrap:wrap;margin-top:8px}
    .badges{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin:34px 0 0}
    .badge{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:7px 15px;font-size:13.5px;color:var(--muted)}
    .badge b{color:var(--txt);font-weight:700}
    .phead{padding:40px 0 6px}
    .phead .back{color:var(--muted);font-size:14px}
    .phead h1{font-size:clamp(26px,4.6vw,42px);line-height:1.12;margin:10px 0 8px;letter-spacing:-.3px}
    .phead .lead{color:var(--muted);font-size:clamp(16px,2.2vw,19px);max-width:780px;margin:0 0 16px}
    .phead .meta{color:var(--faint);font-size:14px;margin:0 0 14px}
    section{padding:44px 0;border-top:1px solid var(--line)}
    section.plain{border-top:0}
    h2{font-size:clamp(22px,3.4vw,30px);margin:0 0 6px}
    .sub{color:var(--muted);margin:0 0 26px;max-width:820px}
    .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:18px}
    .grid.two{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px;transition:border-color .15s ease, transform .15s ease}
    a.card:hover{text-decoration:none;border-color:var(--accent);transform:translateY(-2px)}
    .card h3{margin:0 0 8px;font-size:18px;color:var(--txt)}
    .card p{margin:0;color:var(--muted);font-size:15px}
    .card .ico{font-size:22px;margin-bottom:10px}
    .card .tag{display:inline-block;background:rgba(95,161,238,.12);border:1px solid rgba(95,161,238,.28);
      color:#bcd8ff;font-size:12px;font-weight:600;border-radius:999px;padding:2px 9px;margin-left:6px;vertical-align:middle}
    .card ul{list-style:none;margin:0;padding:0;font-size:14.5px}
    .card ul li{margin:0 0 7px;color:var(--muted)}
    .card ul li a{color:var(--accent)}
    .card ul li .note{color:var(--faint);font-size:13px}
    .dl{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
    pre{background:#070b10;border:1px solid var(--line);border-radius:12px;padding:16px 18px;overflow:auto;
      color:#c9d6e5;font-size:13.5px;line-height:1.55;margin:0 0 12px}
    pre code{color:#c9d6e5}
    .warn{background:rgba(179,100,194,.07);border:1px solid rgba(179,100,194,.28);border-radius:14px;padding:16px 18px;color:var(--muted);font-size:14.5px}
    .warn b{color:var(--txt)}
    /* long-form article typography (10-years + coverage md) */
    article{max-width:820px;font-size:16.5px;line-height:1.75;color:#c7d2de}
    article h2{font-size:clamp(20px,3vw,26px);margin:2rem 0 .6rem;padding-bottom:.35rem;border-bottom:1px solid var(--line);color:var(--txt)}
    article h3{font-size:1.18rem;margin:1.5rem 0 .4rem;color:var(--txt)}
    article p{margin:0 0 1rem}
    article a{color:var(--accent)}
    article ul,article ol{margin:0 0 1rem;padding-left:1.35rem}
    article li{margin:.35rem 0}
    article blockquote{border-left:3px solid var(--accent);padding:.2rem 0 .2rem 1rem;color:var(--muted);margin:0 0 1rem}
    article code{color:#9fd0ff}
    article em{color:#c9d1d9}
    article hr{border:0;border-top:1px solid var(--line);margin:1.6rem 0}
    article img{max-width:100%;height:auto;border-radius:10px}
    /* catalog tables */
    .tablewrap{overflow-x:auto;border:1px solid var(--line);border-radius:14px;margin:0 0 20px}
    table.cat{border-collapse:collapse;width:100%;font-size:14px;min-width:760px}
    table.cat th,table.cat td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);vertical-align:top}
    table.cat tbody tr:last-child td{border-bottom:0}
    table.cat thead th{background:#0e1621;color:var(--txt);font-weight:700;white-space:nowrap}
    table.cat tbody tr:hover{background:rgba(95,161,238,.05)}
    table.cat td.t{font-weight:600;color:var(--txt)}
    table.cat .pill{display:inline-block;background:var(--panel);border:1px solid var(--line);border-radius:999px;
      padding:1px 9px;font-size:12px;color:var(--muted)}
    table.cat .muted{color:var(--faint)}
    footer{border-top:1px solid var(--line);padding:30px 0 50px;color:var(--faint);font-size:14px;text-align:center;margin-top:8px}
    footer a{color:var(--muted);margin:0 9px}
    .center{text-align:center}
    @media(max-width:640px){nav{display:none}}
"""


def head(title, desc, canonical, og_type="website", jsonld=None, extra_css=""):
    j = "\n  <script type='application/ld+json'>%s</script>" % jsonld if jsonld else ""
    return f"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>{e(title)}</title>
  <meta name='description' content='{e(desc)}'>
  <link rel='canonical' href='{e(canonical)}'>
  <meta name='robots' content='index, follow, max-image-preview:large'>
  <meta name='theme-color' content='#0b0f14'>
  <meta name='color-scheme' content='dark'>
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
  <style>{CSS}{extra_css}
  </style>{j}
</head>
<body>
  <div class='glow'><span class='orb a'></span><span class='orb b'></span><span class='orb c'></span></div>
  <div class='wrap'>
"""


def nav(active=""):
    items = [("/#wallets", "Wallets", "wallets"),
             (URL_STORY, "The Story", "story"),
             (URL_PRESS, "Press", "press"),
             ("https://explorer.bismuth.cz/", "Explorer", "explorer"),
             ("/#run", "Run a node", "run"),
             ("https://bismuthcoin.org/", "Community", "community"),
             ("https://github.com/hclivess/Bismuth", "GitHub", "github")]
    links = []
    for href, label, key in items:
        cls = " class='on'" if key == active else ""
        links.append(f"<a{cls} href='{href}'>{label}</a>")
    return ("    <header class='nav'>\n"
            "      <a class='brand' href='/'><img src='/favicon.svg' alt=''> Bismuth</a>\n"
            "      <nav>" + " ".join(links) + "</nav>\n"
            "    </header>\n")


def footer():
    return ("  </div>\n  <footer>\n    <div class='wrap'>\n"
            "      <div style='margin-bottom:10px'>"
            "<a href='https://explorer.bismuth.cz/'>Explorer</a> · "
            f"<a href='#wallets'>Wallets</a> · "
            f"<a href='{URL_STORY}'>The Story</a> · "
            f"<a href='{URL_PRESS}'>Press</a> · "
            "<a href='https://bismuthcoin.org/'>Community</a> · "
            "<a href='https://github.com/hclivess/Bismuth'>GitHub</a>"
            "</div>\n"
            "      <span>Bismuth — fair-launched Python blockchain, live since 1 May 2017.</span>\n"
            "    </div>\n  </footer>\n</body>\n</html>\n")


def dlbuttons(items):
    """Unified download-button group. items = [(href,label,_ignored), ...].

    ONE consistent look for every file download across the whole site (ghost/outline button): the same
    action must never render filled in one place and hollow in another. Filled buttons (.btn / .btn.sm
    without .ghost) are reserved for navigation CTAs (Explorer, Read on site, Browse catalog). The third
    tuple element is ignored, kept only so existing callers don't break."""
    out = ["<div class='dl'>"]
    for item in items:
        href, label = item[0], item[1]
        out.append(f"<a href='{e(href)}' class='btn sm ghost'>{e(label)}</a>")
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
