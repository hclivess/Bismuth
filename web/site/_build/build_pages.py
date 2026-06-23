# -*- coding: utf-8 -*-
"""Page content for bismuth.cz, built on the build_site template helpers."""
import sys, os, html as _h, json, glob
sys.path.insert(0, "/tmp")
import build_site as S
import gen_catalog as C   # SECTIONS / INTRO / FOOTER / TITLE / SUBTITLE / BYLINE
e = _h.escape

# catalog title -> local archive (cleaned coverage slug, or a backup PDF path)
ARCHIVE = {
    "Cryptointerview with the lead developer of Bismuth": "cryptointerview-tezosevangelist",
    "Bismuth Developer Interview — HCLivess (The First Python Crypto Currency)": "developer-interview-hclivess",
    "Bismuth — the First Python Blockchain (An Interview with Jan)": "first-python-blockchain-blackbeard",
    "teamtalks #32 — bismuth": "teamtalks-32-bismuth",
    "Bismuth Core Developer Interview": "developer-interview-hclivess",
    "The Bismuth Plugin System": "bismuth-plugin-system",
    "Dragginator.com NFT Interview": "dragginator-developer-interview",
    "BisBabble — Aravind": "bisbabble-aravind",
    "BisBabble — Endogen (Telegram bot ‘Bauer’)": "bisbabble-endogen",
    "BisBabble — ShadowCrypto (Pawer bot)": "bisbabble-shadowcrypto",
    "BisBabble — Nyzblossom (wBismuth dev)": "bisbabble-nyzblossom",
    "Bismuth — No ICO, No Premine and First Python Coin!": "no-ico-no-premine-first-python-coin",
    "Crypto Spotlight: Bismuth (BIS)": "crypto-spotlight-bismuth",
    "Bismuth [$BIS] — Valuable project. Hidden gem.": "bis-valuable-project-hidden-gem",
    "Bismuth: A Dapp Building Platform that doesn’t Bite": "dapp-building-platform",
    "Coin Report — Bismuth": "coin-report-7-bismuth",
    "Bismuth (BIS) — A Sleeping Legend that Should be Remembered": "sleeping-legend",
    "Nonlinear Feedback Control and Stability Analysis of a Proof-of-Work Blockchain": "pdf:/backups/MIC-2017-4-1.pdf",
    "Tail Removal Block Validation: Implementation and Analysis": "pdf:/backups/MIC-2018-3-1.pdf",
    "Optimizing Performance of a Blockchain (US 10,880,073)": "pdf:/backups/US10880073.pdf",
}
HAVE = set(os.path.splitext(os.path.basename(p))[0] for p in glob.glob(S.COVERAGE_MD + "/*.md"))
WWW = "/var/www/bismuth.cz"


def archive_link(title):
    a = ARCHIVE.get(title, "")
    if not a:
        return ""
    if a.startswith("pdf:"):
        path = a[4:]
        return path if os.path.exists(WWW + path) else ""
    return ("/coverage/%s.html" % a) if a in HAVE else ""


# ============================================================ INDEX (reorganized) ====
def build_index():
    desc = ("Bismuth — a fair-launched, from-scratch Python blockchain (no ICO, no premine) live since "
            "1 May 2017. Block explorer, ledger bootstrap, a 10-year retrospective, and a press archive.")
    jsonld = json.dumps({
        "@context": "https://schema.org", "@type": "WebSite", "name": "Bismuth", "url": S.BASE + "/",
        "description": desc,
        "publisher": {"@type": "Organization", "name": "Bismuth Foundation", "url": S.BASE + "/"},
    })
    facts = [
        ("Fair launch", "No ICO, no premine — mainnet since 1 May 2017."),
        ("Python, from scratch", "Widely cited as the first blockchain written in Python — not a fork."),
        ("Account model", "Signature-derived transaction IDs; arbitrary <code class='tok'>operation</code> + <code class='tok'>openfield</code> data."),
        ("Heavy3 PoW", "CPU/GPU-friendly proof-of-work with a peer-reviewed difficulty controller."),
        ("Plugin ecosystem", "Tokens, aliases, NFTs, DEX, naming, state channels — as building blocks."),
        ("Peer-reviewed", "Consensus analysed in <em>Modeling, Identification and Control</em> (2017–2018)."),
    ]
    fact_cards = "".join(
        "<div class='col-md-6 col-lg-4'><div class='card h-100 bg-body-tertiary border-secondary-subtle'>"
        "<div class='card-body'><h3 class='h6 card-title'>%s</h3><p class='text-secondary small mb-0'>%s</p>"
        "</div></div></div>" % (e(t), d) for t, d in facts)

    featured = ["developer-interview-hclivess", "teamtalks-32-bismuth", "sleeping-legend",
                "first-python-blockchain-blackbeard"]
    arts = S.coverage_articles()
    fcards = []
    for slug in featured:
        if slug in arts:
            m = arts[slug][0]
            fcards.append("<div class='col-md-6'><a class='card h-100 bg-body-tertiary border-secondary-subtle "
                          "text-decoration-none' href='/coverage/%s.html'><div class='card-body'>"
                          "<h3 class='h6 card-title text-info-emphasis'>%s</h3>"
                          "<p class='text-secondary small mb-0'>%s%s</p></div></a></div>"
                          % (slug, e(m.get("title", slug)), e(m.get("source", "")),
                             " · " + e(m.get("date", "")) if m.get("date") else ""))
    featured_html = ("<div class='row g-3 mb-3'>%s</div>" % "".join(fcards)) if fcards else ""

    # ---- Wallets (hosted locally on bismuth.cz; mirrored from the official release builds) ----
    def wcard(name, blurb, version, dls, note=""):
        n = ("<p class='text-secondary small mb-2'>%s</p>" % note) if note else ""
        return ("<div class='col-lg-6'><div class='card h-100 bg-body-tertiary border-secondary-subtle'>"
                "<div class='card-body'><h3 class='h6 card-title'>%s "
                "<span class='badge text-bg-secondary align-middle'>%s</span></h3>"
                "<p class='text-secondary small mb-2'>%s</p>%s%s</div></div></div>"
                % (e(name), e(version), blurb, S.dlbuttons(dls), n))
    wallets = "".join([
        wcard("Tornado Wallet", "The recommended desktop wallet — full GUI, HD accounts, tokens &amp; aliases. "
              "Runs on Windows, macOS and Linux.", "v0.1.48",
              [("/wallets/TornadoBismuthWallet-0.1.48-setup.exe", "🪟 Windows", True),
               ("/wallets/Tornado.Bismuth.Wallet.dmg", "🍎 macOS", True),
               ("/wallets/TornadoBismuthWallet-x86_64.AppImage", "🐧 Linux", True)],
              "Source &amp; release notes: <a href='https://github.com/bismuthfoundation/TornadoWallet'>github.com/bismuthfoundation/TornadoWallet</a>"),
        wcard("Tk Wallet", "Classic lightweight Tkinter wallet — small, simple, battle-tested.", "0.9.4",
              [("/wallets/Bismuth_wallet.exe", "🪟 Windows", False)],
              "Source: <a href='https://github.com/bismuthfoundation/tk-wallet'>github.com/bismuthfoundation/tk-wallet</a>"),
        wcard("Paper Wallet", "Generate an offline cold-storage key pair in your browser — air-gapped, nothing "
              "sent anywhere.", "offline",
              [("https://github.com/AngainorDev/BIS-Paper", "Get it on GitHub", False)]),
        wcard("Web wallets", "Run your own node and connect a wallet to it, or use a public wallet server.",
              "self-host",
              [("https://bismuth.im/wservers", "Active wallet servers", False)]),
    ])
    wallets_section = f"""
    <section class='mb-5' id='wallets'>
      <h2 class='h4 mb-3'>💼 Get a wallet</h2>
      <p class='text-secondary'>Hosted right here on bismuth.cz — no third-party redirects.
        Verify your download against <a href='/wallets/SHA256SUMS.txt'>SHA256SUMS.txt</a>.</p>
      <div class='row g-3'>{wallets}</div>
    </section>"""

    # ---- Ecosystem & services (scavenged from the old bismuth.cz + bismuth.im) ----
    def linkrow(title, items):
        lis = "".join("<li class='mb-1'><a href='%s'%s>%s</a>%s</li>"
                      % (e(u), "" if u.startswith("/") else " target='_blank' rel='noopener'", e(lbl),
                         (" <span class='text-secondary small'>— %s</span>" % d) if d else "")
                      for lbl, u, d in items)
        return ("<div class='col-md-6 col-lg-4'><div class='card h-100 bg-body-tertiary border-secondary-subtle'>"
                "<div class='card-body'><h3 class='h6 card-title'>%s</h3>"
                "<ul class='list-unstyled small mb-0'>%s</ul></div></div></div>" % (e(title), lis))
    eco = "".join([
        linkrow("Explorers", [
            ("explorer.bismuth.cz", "https://explorer.bismuth.cz/", "this site — full stats &amp; geomap"),
            ("bismuth.im", "https://bismuth.im/", "community explorer &amp; API"),
            ("mybismuth.com", "https://mybismuth.com/", "alternative explorer"),
        ]),
        linkrow("Mining", [
            ("Eggpool", "http://eggpool.net/", "mining pool"),
            ("kbkminer", "https://github.com/bismuthfoundation/kbkminer", "open-source miner"),
            ("Difficulty chart", "https://bismuth.im/diff_chart", ""),
        ]),
        linkrow("Infrastructure", [
            ("Hypernode", "https://github.com/bismuthfoundation/hypernode/tree/beta99", "second-tier network"),
            ("Wallet servers", "https://bismuth.im/wservers", "public node endpoints"),
            ("API help", "https://bismuth.im/apihelp", "explorer API docs"),
        ]),
        linkrow("Trading", [
            ("wBIS on Ethereum", "https://www.dextools.io/app/ether/pair-explorer/0xf4f82f8d84c529987201609cecee8ab136a50c8c", "DEXTools"),
            ("wBIS on BSC", "https://www.dextools.io/app/bsc/pair-explorer/0x731b8244f818fd488d9dc516edd976a96459ae59", "DEXTools"),
        ]),
        linkrow("Community", [
            ("Discord", "https://discord.com/invite/DsEuMQ3", ""),
            ("Telegram", "https://t.me/cryptobismuth", ""),
            ("Reddit", "https://www.reddit.com/r/cryptobismuth/", ""),
            ("Mastodon", "https://mstdn.social/@bis", ""),
            ("Bitcointalk", "https://bitcointalk.org/index.php?topic=1896497.0", "original 2017 thread"),
            ("Linktree", "https://linktr.ee/bismuthcoin", ""),
        ]),
        linkrow("Docs &amp; project", [
            ("bismuthcoin.org", "https://bismuthcoin.org/", "main site &amp; docs"),
            ("Whitepaper", "https://bismuthcoin.org/docs/papers/whitepaper/", ""),
            ("Source (GitHub)", "https://github.com/hclivess/Bismuth", ""),
        ]),
    ])
    ecosystem_section = f"""
    <section class='mb-5'>
      <h2 class='h4 mb-3'>🌐 Ecosystem &amp; services</h2>
      <p class='text-secondary'>The wider Bismuth ecosystem — explorers, pools, infrastructure and community.</p>
      <div class='row g-3'>{eco}</div>
    </section>"""

    body = f"""{S.nav('home')}
  <header class='hero py-5'>
    <div class='container py-4'>
      <h1 class='display-5 fw-bold'>Bismuth</h1>
      <p class='lead text-secondary col-lg-8'>A fair-launched, from-scratch <strong>Python blockchain</strong> —
        no ICO, no premine — running on mainnet since 1&nbsp;May&nbsp;2017. A decade-long, academically grounded
        testbed for ideas the rest of the industry named years later.</p>
      <div class='d-flex flex-wrap gap-2 mt-2'>
        <a href='https://explorer.bismuth.cz/' class='btn btn-primary btn-lg'>🔍&nbsp; Block Explorer</a>
        <a href='#wallets' class='btn btn-outline-light btn-lg'>💼&nbsp; Get a wallet</a>
        <a href='/10-years-of-bismuth.html' class='btn btn-outline-light btn-lg'>📜&nbsp; The 10-year story</a>
        <a href='#run' class='btn btn-outline-light btn-lg'>⬇&nbsp; Run a node</a>
      </div>
    </div>
  </header>
  <main class='container pb-5'>

    <section class='mb-5'>
      <h2 class='h4 mb-3'>What is Bismuth?</h2>
      <div class='row g-3'>{fact_cards}</div>
    </section>
{wallets_section}

    <section class='mb-5'>
      <h2 class='h4 mb-3'>📜 The story</h2>
      <div class='card bg-body-tertiary border-info-subtle'><div class='card-body'>
        <h3 class='h5 card-title'>10 Years of Bismuth</h3>
        <p class='text-secondary small mb-2'>A cited retrospective on the project's architectural firsts (2017–2026):
          semantic data/meaning layering, an early plugin ecosystem and NFTs, on-chain sensor data before
          &ldquo;DePIN&rdquo; had a name, and a peer-reviewed proof-of-work difficulty controller.</p>
        <div class='d-flex flex-wrap align-items-center gap-2'>
          <a href='/10-years-of-bismuth.html' class='btn btn-sm btn-info'>Read on site</a>
          {S.dlbuttons([('/10-years-of-bismuth.pdf','PDF',False),('/10-years-of-bismuth.md','Markdown',False),('/10-years-of-bismuth.bbcode.txt','BitcoinTalk',False)])}
        </div>
      </div></div>
    </section>

    <section class='mb-5'>
      <h2 class='h4 mb-3'>🎙️ Press &amp; coverage</h2>
      <p class='text-secondary'>A working bibliography of interviews, profiles, reviews and academic papers
        covering Bismuth (2017–2024) — with cleaned, link-rot-proof local copies of each piece.</p>
      {featured_html}
      <a href='/Bismuth_Interview_Catalog.html' class='btn btn-info'>Browse the full catalog →</a>
    </section>

    <section class='mb-5' id='run'>
      <h2 class='h4 mb-3'>⛏️ Run a node</h2>
      <div class='row g-4'>
        <div class='col-lg-7'>
          <div class='card bg-body-tertiary border-secondary-subtle h-100'><div class='card-body'>
            <h3 class='h6 card-title'>Ledger bootstrap</h3>
            <p class='text-secondary small mb-2'>A recent mainnet snapshot so a new node starts near the chain
              tip instead of syncing from genesis. A fresh node auto-bootstraps from this URL on first start;
              to apply it manually:</p>
            <pre class='mb-2'><code>cd Bismuth
curl -L -o static/ledger.db.tar.gz https://bismuth.cz/ledger.tar.gz
tar xzf static/ledger.db.tar.gz -C static/
python3 node.py</code></pre>
            <a href='/ledger.tar.gz' class='btn btn-sm btn-outline-info'>⬇&nbsp; Download ledger.tar.gz</a>
          </div></div>
        </div>
        <div class='col-lg-5'>
          <div class='card bg-body-tertiary border-warning-subtle h-100'><div class='card-body'>
            <h3 class='h6 card-title text-warning-emphasis'>⚠️ Full node needs the non-RSA signer libs</h3>
            <p class='text-secondary small mb-2'>mainnet carries ed25519 / ecdsa transactions; without these
              the node rejects those blocks and cannot sync:</p>
            <pre class='mb-0'><code>pip install coincurve ed25519</code></pre>
          </div></div>
        </div>
      </div>
    </section>
{ecosystem_section}

    <section>
      <h2 class='h4 mb-3'>🛠️ Build</h2>
      <div class='d-flex flex-wrap gap-2'>
        <a href='https://github.com/hclivess/Bismuth' class='btn btn-outline-light'>GitHub</a>
        <a href='https://bismuthcoin.org/' class='btn btn-outline-light'>Community &amp; docs</a>
        <a href='https://explorer.bismuth.cz/' class='btn btn-outline-light'>Block Explorer</a>
        <a href='/Bismuth_Interview_Catalog.html' class='btn btn-outline-light'>Press archive</a>
      </div>
    </section>
  </main>
{S.footer()}"""
    S.write("index.html", S.head("Bismuth — fair-launched Python blockchain (since 2017)", desc, S.BASE + "/",
                                 "website", jsonld) + body, "1.0")


# ============================================================ 10 YEARS ====
def build_10years():
    raw = open(S.SITE_MD + "/10-years-of-bismuth.md", encoding="utf-8").read().split("\n")
    title = subtitle = byline = ""; start = 0
    for i, ln in enumerate(raw):
        s = ln.strip()
        if s.startswith("# ") and not title: title = s[2:].strip()
        elif s.startswith("### ") and not subtitle: subtitle = s[4:].strip()
        elif s.startswith("*") and s.endswith("*") and not byline and title: byline = s.strip("*").strip()
        elif s == "---": start = i + 1; break
    import markdown2
    body_html = markdown2.markdown("\n".join(raw[start:]),
                                   extras=["cuddled-lists", "tables", "smarty-pants", "break-on-newline"])
    desc = ("A cited 10-year retrospective on Bismuth's architectural firsts (2017–2026): modular data/meaning "
            "layering, plugins & NFTs, on-chain sensor data before DePIN, and a peer-reviewed PoW difficulty controller.")
    canon = S.BASE + "/10-years-of-bismuth.html"
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                         "description": desc, "author": {"@type": "Organization", "name": "Bismuth Foundation"},
                         "publisher": {"@type": "Organization", "name": "Bismuth Foundation"},
                         "mainEntityOfPage": canon, "datePublished": "2026-06-01"})
    hero = (f"  <header class='hero py-5'><div class='container py-3'><h1 class='fw-bold'>{e(title)}</h1>"
            f"<p class='lead text-secondary col-lg-9'>{e(subtitle)}</p>"
            f"<p class='text-secondary small mb-3'>{e(byline)}</p>"
            + S.dlbuttons([("/10-years-of-bismuth.pdf", "⬇ PDF", True), ("/10-years-of-bismuth.md", "Markdown", False),
                           ("/10-years-of-bismuth.bbcode.txt", "BitcoinTalk", False)]) + "</div></header>\n")
    body = (S.nav("story") + hero + "  <main class='container pb-5'><article class='col-lg-9'>\n"
            + body_html + "\n  </article></main>\n" + S.footer())
    S.write("10-years-of-bismuth.html", S.head(title + " — Bismuth", desc, canon, "article", jsonld) + body, "0.9")


# ============================================================ PRESS CATALOG ====
def build_catalog():
    canon = S.BASE + "/Bismuth_Interview_Catalog.html"
    desc = ("A working bibliography of published interviews, profiles, reviews and academic papers covering "
            "the Bismuth blockchain (BIS), 2017–2024 — with cleaned local archive copies of each source.")
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "CollectionPage", "name": C.TITLE,
                         "description": desc, "url": canon,
                         "isPartOf": {"@type": "WebSite", "name": "Bismuth", "url": S.BASE + "/"}})
    hero = ("  <header class='hero py-5'><div class='container py-3'><h1 class='fw-bold'>Interview &amp; Coverage Catalog</h1>"
            f"<p class='lead text-secondary col-lg-9'>{e(C.SUBTITLE)}</p>"
            f"<p class='text-secondary small mb-3'>{e(C.BYLINE)}</p>"
            + S.dlbuttons([("/Bismuth_Interview_Catalog.pdf", "⬇ PDF", True),
                           ("/Bismuth_Interview_Catalog.md", "Markdown", False),
                           ("/Bismuth_Interview_Catalog.bbcode.txt", "BitcoinTalk", False)]) + "</div></header>\n")
    parts = [S.nav("press"), hero, "  <main class='container pb-5'>\n"]
    for p in C.INTRO:
        parts.append("    <p class='text-secondary small col-lg-10'>%s</p>\n" % e(p))
    for stitle, note, entries in C.SECTIONS:
        parts.append("    <h2 class='h4 mt-4 mb-2 border-bottom border-secondary-subtle pb-2'>%s</h2>\n" % e(stitle))
        if note:
            parts.append("    <p class='text-secondary small'>%s</p>\n" % e(note))
        parts.append("    <div class='table-responsive'><table class='table table-dark table-striped table-hover table-sm align-middle'>\n"
                     "      <thead><tr><th>Title</th><th>By / Where</th><th>Date</th><th>Type</th><th>What it is</th><th>Archive</th></tr></thead><tbody>\n")
        for t, where, url, date, typ, what in entries:
            w = "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (e(url), e(where)) if url else e(where)
            al = archive_link(t)
            arch = ("<a href='%s'%s>%s</a>" % (e(al), "" if al.endswith(".html") else " target='_blank'",
                    "read" if al.endswith(".html") else "PDF")) if al else "<span class='text-secondary'>—</span>"
            parts.append("      <tr><td class='fw-semibold'>%s</td><td>%s</td><td class='text-nowrap'>%s</td>"
                         "<td><span class='badge text-bg-secondary'>%s</span></td>"
                         "<td class='text-secondary small'>%s</td><td>%s</td></tr>\n"
                         % (e(t), w, e(date), e(typ), e(what), arch))
        parts.append("      </tbody></table></div>\n")
    parts.append("    <p class='text-secondary small mt-4'>%s</p>\n  </main>\n" % e(C.FOOTER))
    parts.append(S.footer())
    S.write("Bismuth_Interview_Catalog.html", S.head("Bismuth — Interview & Coverage Catalog", desc, canon,
                                                     "website", jsonld) + "".join(parts), "0.8")


# ============================================================ COVERAGE ARTICLES ====
def build_articles():
    arts = S.coverage_articles()
    for slug, (m, body_html) in arts.items():
        title = m.get("title", slug)
        src = m.get("source", ""); date = m.get("date", ""); author = m.get("author", "")
        url = m.get("original_url", "")
        canon = "%s/coverage/%s.html" % (S.BASE, slug)
        desc = ("%s — Bismuth coverage%s%s. Archived on bismuth.cz." %
                (title, (" by " + author) if author else "", (" (" + src + ")") if src else ""))[:300]
        jsonld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                             "author": {"@type": "Person", "name": author} if author else
                                       {"@type": "Organization", "name": "Bismuth Foundation"},
                             "publisher": {"@type": "Organization", "name": "Bismuth Foundation"},
                             "mainEntityOfPage": canon})
        meta_line = " · ".join(x for x in [e(author), e(src), e(date)] if x)
        orig = ("<a class='btn btn-sm btn-outline-secondary' href='%s' target='_blank' rel='noopener'>Original ↗</a>" % e(url)) if url else ""
        hero = ("  <header class='hero py-4'><div class='container py-2'>"
                "<p class='small mb-1'><a href='/Bismuth_Interview_Catalog.html' class='link-secondary text-decoration-none'>← Press &amp; coverage</a></p>"
                f"<h1 class='fw-bold h2'>{e(title)}</h1>"
                f"<p class='text-secondary small mb-2'>{meta_line}</p>"
                "<div class='d-flex flex-wrap gap-2'>"
                f"<a class='btn btn-sm btn-outline-info' href='/coverage/{slug}.md'>Markdown</a>{orig}"
                "</div></div></header>\n")
        body = (S.nav("press") + hero + "  <main class='container pb-5'><article class='col-lg-9'>\n"
                + body_html + "\n  </article></main>\n" + S.footer())
        S.write("coverage/%s.html" % slug, S.head(title + " — Bismuth coverage", desc, canon, "article", jsonld) + body, "0.6")
        # also publish the cleaned markdown next to it
        import shutil
        shutil.copy(S.COVERAGE_MD + "/%s.md" % slug, S.OUT + "/coverage/%s.md" % slug)


# ============================================================ SITEMAP + ROBOTS ====
def build_sitemap():
    today = os.environ.get("BUILD_DATE", "2026-06-23")
    urls = []
    for rel, pri in S.PAGES:
        loc = S.BASE + "/" + ("" if rel == "index.html" else rel)
        urls.append("  <url><loc>%s</loc><lastmod>%s</lastmod><priority>%s</priority></url>" % (e(loc), today, pri))
    sm = ("<?xml version='1.0' encoding='UTF-8'?>\n"
          "<urlset xmlns='http://www.sitemap.org/schemas/sitemap/0.9'>\n" + "\n".join(urls) + "\n</urlset>\n")
    sm = sm.replace("www.sitemap.org", "www.sitemaps.org")
    open(S.OUT + "/sitemap.xml", "w").write(sm)
    open(S.OUT + "/robots.txt", "w").write("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % S.BASE)


build_index()
build_10years()
build_catalog()
build_articles()
build_sitemap()
print("built %d pages -> %s" % (len(S.PAGES), S.OUT))
for rel, _ in S.PAGES:
    print("  ", rel)
print("  sitemap.xml, robots.txt")
