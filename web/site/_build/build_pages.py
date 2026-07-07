# -*- coding: utf-8 -*-
"""Page content for bismuth.cz, built on the build_site template helpers (framework-free NADO-style theme)."""
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
    """Returns ('page', '/coverage/slug') for a local cleaned article, ('pdf', '/backups/x.pdf') for a
    PDF, or ('', '') when nothing is archived. Coverage pages are extensionless (served via try_files)."""
    a = ARCHIVE.get(title, "")
    if not a:
        return "", ""
    if a.startswith("pdf:"):
        path = a[4:]
        return ("pdf", path) if os.path.exists(WWW + path) else ("", "")
    return ("page", "/coverage/%s" % a) if a in HAVE else ("", "")


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
        ("🎯", "Fair launch", "No ICO, no premine — mainnet since 1 May 2017. Everyone started from the same block zero."),
        ("🐍", "Python, from scratch", "Widely cited as the first blockchain written in Python — an original codebase, not a fork."),
        ("🧾", "Account model", "Signature-derived transaction IDs, with arbitrary <code>operation</code> + <code>openfield</code> data on every tx."),
        ("⛏️", "Heavy3 PoW", "CPU/GPU-friendly proof-of-work with a peer-reviewed difficulty controller."),
        ("🧩", "Plugin ecosystem", "Tokens, aliases, NFTs, DEX, naming and state channels — as composable building blocks."),
        ("🎓", "Peer-reviewed", "Consensus analysed in <em>Modeling, Identification and Control</em> (2017–2018)."),
    ]
    fact_cards = "".join(
        "<div class='card'><div class='ico'>%s</div><h3>%s</h3><p>%s</p></div>" % (ic, e(t), d)
        for ic, t, d in facts)

    featured = ["developer-interview-hclivess", "teamtalks-32-bismuth", "sleeping-legend",
                "first-python-blockchain-blackbeard"]
    arts = S.coverage_articles()
    fcards = []
    for slug in featured:
        if slug in arts:
            m = arts[slug][0]
            sub = e(m.get("source", "")) + (" · " + e(m.get("date", "")) if m.get("date") else "")
            fcards.append("<a class='card' href='/coverage/%s'><h3>%s</h3><p>%s</p></a>"
                          % (slug, e(m.get("title", slug)), sub))
    featured_html = ("<div class='grid two'>%s</div>" % "".join(fcards)) if fcards else ""

    # ---- Wallets (hosted locally on bismuth.cz; mirrored from the official release builds) ----
    def wcard(name, blurb, version, dls, note=""):
        n = ("<p style='margin-top:12px;font-size:13.5px'>%s</p>" % note) if note else ""
        return ("<div class='card'><h3>%s <span class='tag'>%s</span></h3><p>%s</p>%s%s</div>"
                % (e(name), e(version), blurb, S.dlbuttons(dls), n))
    wallets = "".join([
        wcard("Tornado Wallet", "The recommended desktop wallet — full GUI, HD accounts, tokens &amp; aliases. "
              "Runs on Windows, macOS and Linux.", "v0.1.48",
              [("/wallets/TornadoBismuthWallet-0.1.48-setup.exe", "🪟 Windows", True),
               ("/wallets/Tornado.Bismuth.Wallet.dmg", "🍎 macOS", True),
               ("/wallets/TornadoBismuthWallet-x86_64.AppImage", "🐧 Linux", True)],
              "Source &amp; release notes: <a href='https://github.com/bismuthfoundation/TornadoWallet'>TornadoWallet</a>"),
        wcard("Tk Wallet", "Classic lightweight Tkinter wallet — small, simple, battle-tested.", "0.9.4",
              [("/wallets/Bismuth_wallet.exe", "🪟 Windows", False)],
              "Source: <a href='https://github.com/bismuthfoundation/tk-wallet'>tk-wallet</a>"),
        wcard("Paper Wallet", "Generate an offline cold-storage key pair in your browser — air-gapped, nothing "
              "sent anywhere.", "offline",
              [("https://github.com/AngainorDev/BIS-Paper", "Get it on GitHub", False)]),
        wcard("Web wallets", "Run your own node and connect a wallet to it, or use a public wallet server.",
              "self-host",
              [("https://bismuth.im/wservers", "Active wallet servers", False)]),
    ])
    wallets_section = f"""    <section id='wallets'>
      <h2>💼 Get a wallet</h2>
      <p class='sub'>Hosted right here on bismuth.cz — no third-party redirects. Verify your download against
        <a href='/wallets/SHA256SUMS.txt'>SHA256SUMS.txt</a>.</p>
      <div class='grid two'>{wallets}</div>
    </section>
"""

    # ---- Ecosystem & services ----
    def linkrow(title, items):
        lis = "".join("<li><a href='%s'%s>%s</a>%s</li>"
                      % (e(u), "" if u.startswith("/") else " target='_blank' rel='noopener'", e(lbl),
                         (" <span class='note'>— %s</span>" % d) if d else "")
                      for lbl, u, d in items)
        return "<div class='card'><h3>%s</h3><ul>%s</ul></div>" % (e(title), lis)
    eco = "".join([
        linkrow("Explorers", [
            ("explorer.bismuth.cz", "https://explorer.bismuth.cz/", "full stats &amp; geomap"),
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
    ecosystem_section = f"""    <section>
      <h2>🌐 Ecosystem &amp; services</h2>
      <p class='sub'>The wider Bismuth ecosystem — explorers, pools, infrastructure and community.</p>
      <div class='grid'>{eco}</div>
    </section>
"""

    body = f"""{S.nav('home')}
    <div class='hero'>
      <img class='mark' src='/favicon.svg' alt='Bismuth logo'>
      <h1>The fair-launched<br><span class='g'>Python blockchain</span></h1>
      <p class='lead'>Bismuth is a from-scratch blockchain — <b>no ICO, no premine</b> — running on mainnet
        since <b>1&nbsp;May&nbsp;2017</b>. A decade-long, academically grounded testbed for ideas the rest of
        the industry named years later.</p>
      <div class='cta'>
        <a class='btn' href='https://explorer.bismuth.cz/'>🔍&nbsp; Block Explorer</a>
        <a class='btn ghost' href='#wallets'>💼&nbsp; Get a wallet</a>
        <a class='btn ghost' href='{S.URL_STORY}'>📜&nbsp; The 10-year story</a>
      </div>
      <div class='badges'>
        <span class='badge'>🎯 <b>Fair launch</b>, no premine</span>
        <span class='badge'>🐍 <b>Python</b>, from scratch</span>
        <span class='badge'>📅 Live since <b>2017</b></span>
        <span class='badge'>🎓 <b>Peer-reviewed</b> consensus</span>
      </div>
    </div>

    <section id='what'>
      <h2>What is Bismuth?</h2>
      <p class='sub'>An account-based chain widely cited as the first blockchain written in Python — not a
        fork, built from the ground up with an unusually broad plugin ecosystem.</p>
      <div class='grid'>{fact_cards}</div>
    </section>

{wallets_section}
    <section id='story'>
      <h2>📜 The story</h2>
      <div class='card'>
        <h3>10 Years of Bismuth</h3>
        <p>A cited retrospective on the project's architectural firsts (2017–2026): semantic data/meaning
          layering, an early plugin ecosystem and NFTs, on-chain sensor data before &ldquo;DePIN&rdquo; had a
          name, and a peer-reviewed proof-of-work difficulty controller.</p>
        <div class='btnrow'>
          <a class='btn sm' href='{S.URL_STORY}'>Read on site →</a>
          {S.dlbuttons([('/10-years-of-bismuth.pdf','PDF',False),('/10-years-of-bismuth.md','Markdown',False),('/10-years-of-bismuth.bbcode.txt','BitcoinTalk',False)])}
        </div>
      </div>
    </section>

    <section id='press'>
      <h2>🎙️ Press &amp; coverage</h2>
      <p class='sub'>A working bibliography of interviews, profiles, reviews and academic papers covering
        Bismuth (2017–2024) — with cleaned, link-rot-proof local copies of each piece.</p>
      {featured_html}
      <div class='btnrow'><a class='btn sm' href='{S.URL_PRESS}'>Browse the full catalog →</a></div>
    </section>

    <section id='run'>
      <h2>⛏️ Run a node</h2>
      <p class='sub'>One command. The installer pulls every dependency (including the ed25519 / ecdsa signer
        libs mainnet needs), registers a <code>systemd</code> service that starts on boot, and the node
        <b>auto-bootstraps a recent ledger snapshot on first run</b> — so it starts near the chain tip, not
        from genesis. No manual downloads, no pip juggling.</p>
      <div class='grid two'>
        <div class='card'>
          <h3>Install &amp; run (Linux)</h3>
          <pre><code>git clone https://github.com/hclivess/Bismuth
cd Bismuth
sudo ./install_node.sh</code></pre>
          <p>That's it — deps, the <code>bismuth-node</code> service and the ledger bootstrap are handled for
            you. Check it with <code>systemctl status bismuth-node</code>.</p>
        </div>
        <div class='card'>
          <h3>Options</h3>
          <ul>
            <li><code>--tor</code> <span class='note'>— also install Tor for private onion routing</span></li>
            <li><code>--regnet</code> <span class='note'>— a local regtest node for development</span></li>
            <li><code>--no-start</code> / <code>--restart</code> <span class='note'>— control the service</span></li>
            <li>Idempotent &amp; re-runnable; won't disturb a running node unless you ask.</li>
          </ul>
        </div>
      </div>
      <p class='sub' style='margin-top:16px'>Advanced: the fresh-sync bootstrap pulls
        <a href='/ledger.tar.gz'>ledger.tar.gz</a> automatically; to seed from your own snapshot set
        <code>bootstrap_file</code> / <code>bootstrap_url</code> in <code>config.toml</code>. All settings:
        <a href='https://github.com/hclivess/Bismuth/blob/main/doc/11-configuration.md'>doc/11</a>.</p>
    </section>

{ecosystem_section}
    <section>
      <h2>🛠️ Build</h2>
      <div class='btnrow'>
        <a class='btn sm ghost' href='https://github.com/hclivess/Bismuth'>GitHub</a>
        <a class='btn sm ghost' href='https://bismuthcoin.org/'>Community &amp; docs</a>
        <a class='btn sm ghost' href='https://explorer.bismuth.cz/'>Block Explorer</a>
        <a class='btn sm ghost' href='{S.URL_PRESS}'>Press archive</a>
      </div>
    </section>
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
    canon = S.BASE + S.URL_STORY
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                         "description": desc, "author": {"@type": "Organization", "name": "Bismuth Foundation"},
                         "publisher": {"@type": "Organization", "name": "Bismuth Foundation"},
                         "mainEntityOfPage": canon, "datePublished": "2026-06-01"})
    dl = S.dlbuttons([("/10-years-of-bismuth.pdf", "⬇ PDF", True), ("/10-years-of-bismuth.md", "Markdown", False),
                      ("/10-years-of-bismuth.bbcode.txt", "BitcoinTalk", False)])
    phead = (f"    <div class='phead'><h1>{e(title)}</h1>"
             f"<p class='lead'>{e(subtitle)}</p><p class='meta'>{e(byline)}</p>{dl}</div>\n")
    body = (S.nav("story") + phead + "    <section class='plain'><article>\n"
            + body_html + "\n    </article></section>\n" + S.footer())
    S.write("10-years-of-bismuth.html", S.head(title + " — Bismuth", desc, canon, "article", jsonld) + body, "0.9")


# ============================================================ PRESS CATALOG ====
def build_catalog():
    canon = S.BASE + S.URL_PRESS
    desc = ("A working bibliography of published interviews, profiles, reviews and academic papers covering "
            "the Bismuth blockchain (BIS), 2017–2024 — with cleaned local archive copies of each source.")
    jsonld = json.dumps({"@context": "https://schema.org", "@type": "CollectionPage", "name": C.TITLE,
                         "description": desc, "url": canon,
                         "isPartOf": {"@type": "WebSite", "name": "Bismuth", "url": S.BASE + "/"}})
    dl = S.dlbuttons([("/Bismuth_Interview_Catalog.pdf", "⬇ PDF", True),
                      ("/Bismuth_Interview_Catalog.md", "Markdown", False),
                      ("/Bismuth_Interview_Catalog.bbcode.txt", "BitcoinTalk", False)])
    phead = ("    <div class='phead'><p class='back'><a href='/'>← Home</a></p>"
             "<h1>Interview &amp; Coverage Catalog</h1>"
             f"<p class='lead'>{e(C.SUBTITLE)}</p><p class='meta'>{e(C.BYLINE)}</p>{dl}</div>\n")
    parts = [S.nav("press"), phead, "    <section class='plain'>\n"]
    for p in C.INTRO:
        parts.append("      <p class='sub'>%s</p>\n" % e(p))
    for stitle, note, entries in C.SECTIONS:
        parts.append("      <h2 style='margin-top:2rem'>%s</h2>\n" % e(stitle))
        if note:
            parts.append("      <p class='sub'>%s</p>\n" % e(note))
        parts.append("      <div class='tablewrap'><table class='cat'>\n"
                     "        <thead><tr><th>Title</th><th>By / Where</th><th>Date</th><th>Type</th><th>What it is</th><th>Archive</th></tr></thead><tbody>\n")
        for t, where, url, date, typ, what in entries:
            w = "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (e(url), e(where)) if url else e(where)
            kind, al = archive_link(t)
            if kind == "page":
                arch = "<a href='%s'>read</a>" % e(al)
            elif kind == "pdf":
                arch = "<a href='%s' target='_blank'>PDF</a>" % e(al)
            else:
                arch = "<span class='muted'>—</span>"
            parts.append("        <tr><td class='t'>%s</td><td>%s</td><td class='muted' style='white-space:nowrap'>%s</td>"
                         "<td><span class='pill'>%s</span></td><td>%s</td><td>%s</td></tr>\n"
                         % (e(t), w, e(date), e(typ), e(what), arch))
        parts.append("        </tbody></table></div>\n")
    parts.append("      <p class='sub' style='margin-top:1.5rem'>%s</p>\n    </section>\n" % e(C.FOOTER))
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
        canon = "%s/coverage/%s" % (S.BASE, slug)
        desc = ("%s — Bismuth coverage%s%s. Archived on bismuth.cz." %
                (title, (" by " + author) if author else "", (" (" + src + ")") if src else ""))[:300]
        jsonld = json.dumps({"@context": "https://schema.org", "@type": "Article", "headline": title,
                             "author": {"@type": "Person", "name": author} if author else
                                       {"@type": "Organization", "name": "Bismuth Foundation"},
                             "publisher": {"@type": "Organization", "name": "Bismuth Foundation"},
                             "mainEntityOfPage": canon})
        meta_line = " · ".join(x for x in [e(author), e(src), e(date)] if x)
        orig = ("<a class='btn sm ghost' href='%s' target='_blank' rel='noopener'>Original ↗</a>" % e(url)) if url else ""
        phead = ("    <div class='phead'><p class='back'><a href='%s'>← Press &amp; coverage</a></p>"
                 "<h1>%s</h1><p class='meta'>%s</p>"
                 "<div class='btnrow'><a class='btn sm ghost' href='/coverage/%s.md'>Markdown</a>%s</div></div>\n"
                 % (S.URL_PRESS, e(title), meta_line, slug, orig))
        body = (S.nav("press") + phead + "    <section class='plain'><article>\n"
                + body_html + "\n    </article></section>\n" + S.footer())
        S.write("coverage/%s.html" % slug, S.head(title + " — Bismuth coverage", desc, canon, "article", jsonld) + body, "0.6")
        # also publish the cleaned markdown next to it
        import shutil
        shutil.copy(S.COVERAGE_MD + "/%s.md" % slug, S.OUT + "/coverage/%s.md" % slug)


# ============================================================ SITEMAP + ROBOTS ====
def build_sitemap():
    today = os.environ.get("BUILD_DATE", "2026-06-23")
    urls = []
    for rel, pri in S.PAGES:
        # extensionless canonical URLs: index -> /, everything else drops the .html suffix
        if rel == "index.html":
            loc = S.BASE + "/"
        else:
            loc = S.BASE + "/" + (rel[:-5] if rel.endswith(".html") else rel)
        urls.append("  <url><loc>%s</loc><lastmod>%s</lastmod><priority>%s</priority></url>" % (e(loc), today, pri))
    sm = ("<?xml version='1.0' encoding='UTF-8'?>\n"
          "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n" + "\n".join(urls) + "\n</urlset>\n")
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
