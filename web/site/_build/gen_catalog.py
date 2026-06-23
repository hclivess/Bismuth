# -*- coding: utf-8 -*-
"""Generate Bismuth_Interview_Catalog.{md,html,bbcode.txt} from one structured source."""
import html as _h, os

TITLE = "Bismuth — Interview & Coverage Source Catalog"
SUBTITLE = "A working bibliography of published interviews, profiles and reviews of the Bismuth blockchain (BIS)"
BYLINE = "Compiled June 2026 · assembled for press outreach and source assessment"

INTRO = [
 "This is a reference index, not a reproduction. Each entry lists where the piece lives, who made it, and a "
 "one-line note on what it covers. Full texts remain at the linked URLs; where a live page may have changed "
 "or vanished, retrieve the snapshot via the Wayback Machine by prefixing the URL with "
 "https://web.archive.org/web/*/ (paste the original link into archive.org's search box). The bismuth.cz and "
 "bismuthplatform.com domains are both heavily archived there.",
 "Source-type key: PRIMARY = subject speaking about itself (interviews, the foundation's own posts) — useful "
 "for facts, not for establishing notability. SPS = self-published platform (Medium, Steemit, publish0x) — "
 "generally not treated as a reliable independent source. PEER-REVIEW = academic, but co-authored by the "
 "project lead (not independent). None of the items below are independent secondary coverage in the Wikipedia "
 "sense — that is the gap. They are, however, exactly the body of material a journalist would want to see to "
 "know the story is real.",
]

FOOTER = (
 "How to recover anything dead: open web.archive.org, paste the original URL, pick a snapshot. For a "
 "whole-domain sweep, browse web.archive.org/web/*/bismuth.cz/* and /bismuthplatform.com/*. The Bismuth "
 "Foundation's own ‘Awesome-Bismuth’ repo (github.com/bismuthfoundation/Awesome-Bismuth, Articles.md) is the "
 "most complete living list and is worth checking for anything added after this catalog. This document "
 "indexes what was findable as of June 2026 and is not guaranteed exhaustive.")

# section: (title, note, [ (title, where, url, date, type, what) ])
SECTIONS = [
 ("1. Interviews with the founder / core team", "", [
  ("Cryptointerview with the lead developer of Bismuth", "Panama_TJ / xamanap — Medium",
   "https://xamanap.medium.com/cryptointerview-with-tezosevangelist-b45cbb72e414", "29 Sep 2017", "PRIMARY / SPS",
   "The original Q&A; source of the “blockchain Lego” framing. Earliest interview. (Author renamed; old @Panama_TJ link may 404.)"),
  ("Bismuth Developer Interview — HCLivess (The First Python Crypto Currency)", "aetsen — Steemit",
   "https://steemit.com/cryptocurrency/@aetsen/bismuth-developer-interview-hclivess", "7 Mar 2018", "PRIMARY / SPS",
   "Technical Q&A: mempool design, throughput, sidechains, dev background."),
  ("Bismuth — the First Python Blockchain (An Interview with Jan)", "cblackbeard — Medium",
   "https://medium.com/@cblackbeard/bismuth-the-first-python-blockchain-6e2fb0b53a4f", "16 Jan 2019", "PRIMARY / SPS",
   "Team, why-Python, use cases, fund/treasury disclosure."),
  ("teamtalks #32 — bismuth", "Jens Ibsen (flashygordy) — Medium",
   "https://medium.com/@flashygordy/teamtalks-32-bismuth-82d56f9b37c", "Aug 2019", "PRIMARY / SPS",
   "Long Q&A on emission schedule, hypernodes, supply, philosophy."),
  ("Bismuth Core Developer Interview", "aetsen — PeakD (backup: Steemit)",
   "https://peakd.com/@aetsen/bismuth-developer-interview-hclivess", "7 Mar 2018", "PRIMARY / SPS",
   "On external indexing for open-field data, token/alias handling. (Same interview as the aetsen Steemit post.)"),
  ("The Bismuth Plugin System", "bitsignal — Steemit / PeakD",
   "https://steemit.com/cryptocurrency/@bitsignal/the-bismuth-plugin-system", "16 Jul 2018", "PRIMARY / SPS",
   "Dev-oriented talk on Bismuth's plugin (Crystal) system and its applications."),
  ("Dragginator.com NFT Interview", "bitsignal (EggdraSyl) — Steemit / PeakD",
   "https://steemit.com/cryptocurrency/@bitsignal/developer-interview-draggon-s-eggs-soccer-cup", "11 Jul 2018",
   "PRIMARY / SPS", "Interview with the dev of Dragginator, an NFT-style game on Bismuth (early non-ETH NFT)."),
 ]),
 ("2. BisBabble — the community interview series",
  "Bismuth's own interview series (originally on hypernodes.bismuth.live, now mirrored on bismuthcoin.org/blog). "
  "Each profiles a community contributor. Primary source — good colour for a feature, not independent.", [
  ("BisBabble — Aravind", "bismuthcoin.org / hypernodes.bismuth.live",
   "https://bismuthcoin.org/blog/2019-12-18-interview", "18 Dec 2019", "PRIMARY",
   "Issue 1. New contributor — background and dev work."),
  ("BisBabble — Endogen (Telegram bot ‘Bauer’)", "hypernodes.bismuth.live",
   "https://hypernodes.bismuth.live/?p=1086", "18 Jan 2020", "PRIMARY",
   "Issue 2. German engineer behind the Bauer Telegram bot."),
  ("BisBabble — ShadowCrypto (Pawer bot)", "hypernodes.bismuth.live", "https://hypernodes.bismuth.live/?p=1110",
   "7 Feb 2020", "PRIMARY", "Contributor to the Pawer Discord bot."),
  ("BisBabble — Nyzblossom (wBismuth dev)", "bismuthcoin.org/blog", "https://bismuthcoin.org/blog/2020-11-04-interview",
   "4 Nov 2020", "PRIMARY", "Q&A with the wBismuth wallet developer."),
 ]),
 ("3. Profiles, reviews & spotlights", "", [
  ("Bismuth — No ICO, No Premine and First Python Coin!", "kingscrown — Steemit",
   "https://steemit.com/bitcoin/@kingscrown/bismuth-no-ico-no-premine-and-first-python-coin", "5 Oct 2017", "SPS",
   "Early write-up + embedded interview; fair-launch framing."),
  ("Crypto Spotlight: Bismuth (BIS)", "coinstats — Steemit",
   "https://steemit.com/cryptocurrency/@coinstats/crypto-spotlight-bismuth-bis", "12 Apr 2018", "SPS",
   "Profile contrasting Bismuth with ERC-20 hype (CryptoKitties example)."),
  ("Bismuth [$BIS] — Valuable project. Hidden gem.", "Joseph L. / bitcointrading — Medium",
   "https://medium.com/@bitcointrading/bismuth-bis-cryptocurrency-valuable-project-hidden-gem-576c90152cb0", "May 2018", "SPS",
   "Investor-oriented profile; quotes the cryptointerview."),
  ("Bismuth — Project Review", "Thant11 — Medium", "https://medium.com/@thant11/bismuth-project-review-fdaa4976bd33",
   "2018", "SPS", "Independent-ish community review of the platform."),
  ("3 Promising Altcoins to Invest in for 2018 (A Thoreau Analysis)", "altcointhoreau — Medium",
   "https://medium.com/@altcointhoreau/3-promising-altcoins-to-invest-in-for-2018-a-thoreau-analysis-f6cc95cd83", "2018", "SPS",
   "Bismuth featured as one of three picks."),
  ("Bismuth: A Dapp Building Platform that doesn’t Bite", "CryptoShib",
   "https://cryptoshib.com/bismuth-dapp-building-platform/", "12 Apr 2020", "SPS", "On Python dApp accessibility."),
  ("Coin Report — Bismuth", "Altcoin Trader’s Handbook", "https://altcointradershandbook.com/coin-report-bismuth/",
   "7 Dec 2018", "SPS", "Structured ‘coin report’ on the first Python blockchain."),
  ("Bismuth (BIS) — A Sleeping Legend that Should be Remembered", "publish0x — crypto-bits",
   "https://publish0x.com/crypto-bits/bismuthbis-a-sleeping-legend-that-should-be-remembered-xxvlvol", "5 Mar 2024", "SPS",
   "Retrospective appreciation piece, ~7 years in."),
 ]),
 ("4. Academic & patent record (not interviews — included for the press kit)", "", [
  ("Nonlinear Feedback Control and Stability Analysis of a Proof-of-Work Blockchain",
   "G. Hovland & J. Kučera — Modeling, Identification and Control (MIC) 38(4)",
   "https://mic-journal.no/ABS/MIC-2017-4-1.asp", "2017", "PEER-REVIEW",
   "Difficulty controller as a feedback-control problem. doi:10.4173/mic.2017.4.1"),
  ("Tail Removal Block Validation: Implementation and Analysis", "G. Hovland & J. Kučera — MIC",
   "https://mic-journal.no/ABS/MIC-2018-3-1.asp", "2018", "PEER-REVIEW",
   "Block-validation method, simulation and analysis."),
  ("Optimizing Performance of a Blockchain (US 10,880,073)", "Hwang et al. — IBM (USPTO)",
   "https://patents.google.com/patent/US10880073B2/en", "granted 2020", "PATENT",
   "IBM patent; Bismuth/Hovland work reportedly listed in prior-art citations (verify the ‘Non-patent citations’ section yourself)."),
 ]),
]

OUT = "/tmp/catalog_out"
os.makedirs(OUT, exist_ok=True)

# ---------------- Markdown ----------------
def md():
    L = ["# %s" % TITLE, "", "*%s*" % SUBTITLE, "", "*%s*" % BYLINE, "", "---", ""]
    for p in INTRO:
        L += [p, ""]
    for stitle, note, entries in SECTIONS:
        L += ["## %s" % stitle, ""]
        if note:
            L += ["*%s*" % note, ""]
        L += ["| Title | By / Where | Date | Type | What it is |", "|---|---|---|---|---|"]
        for t, where, url, date, typ, what in entries:
            w = "[%s](%s)" % (where, url) if url else where
            cell = lambda s: s.replace("|", "\\|")
            L.append("| %s | %s | %s | %s | %s |" % (cell(t), cell(w), date, typ, cell(what)))
        L += [""]
    L += ["---", "", FOOTER, ""]
    return "\n".join(L)

# ---------------- HTML (INTEGRATED site page: dark Bootstrap theme, nav + footer like index.html) ----------------
def html():
    e = _h.escape
    head = (
"<!doctype html>\n<html lang='en' data-bs-theme='dark'>\n<head>\n"
"  <meta charset='utf-8'>\n  <meta name='viewport' content='width=device-width, initial-scale=1'>\n"
"  <title>%s</title>\n  <link href='/assets/bootstrap.min.css' rel='stylesheet'>\n"
"  <style>\n    body { background:#0d1117; }\n"
"    .hero { background:linear-gradient(180deg,#16263a 0%%, transparent 100%%); }\n"
"    main table a { word-break:break-all; }\n"
"    .badge-type { font-size:.7rem; }\n  </style>\n</head>\n<body>\n" % e(TITLE))
    nav = (
"  <nav class='navbar border-bottom border-secondary-subtle'>\n    <div class='container'>\n"
"      <a class='navbar-brand fw-semibold text-decoration-none' href='/'>⛏️ Bismuth</a>\n      <div>\n"
"        <a class='navbar-text text-decoration-none me-3' href='/10-years-of-bismuth.html'>10 Years</a>\n"
"        <a class='navbar-text text-decoration-none me-3' href='/Bismuth_Interview_Catalog.html'>Interviews</a>\n"
"        <a class='navbar-text text-decoration-none me-3' href='https://explorer.bismuth.cz/'>Explorer</a>\n"
"        <a class='navbar-text text-decoration-none' href='https://github.com/hclivess/Bismuth'>GitHub</a>\n"
"      </div>\n    </div>\n  </nav>\n")
    hero = (
"  <header class='hero py-5'>\n    <div class='container py-3'>\n"
"      <h1 class='fw-bold'>Interview &amp; Coverage Catalog</h1>\n"
"      <p class='lead text-secondary col-lg-9'>%s</p>\n"
"      <p class='text-secondary small mb-3'>%s</p>\n"
"      <a href='/Bismuth_Interview_Catalog.pdf' class='btn btn-info btn-sm'>⬇&nbsp; PDF</a>\n"
"      <a href='/Bismuth_Interview_Catalog.md' class='btn btn-outline-secondary btn-sm ms-1'>Markdown</a>\n"
"      <a href='/Bismuth_Interview_Catalog.bbcode.txt' class='btn btn-outline-secondary btn-sm ms-1'>BitcoinTalk</a>\n"
"    </div>\n  </header>\n" % (e(SUBTITLE), e(BYLINE)))
    body = ["  <main class='container pb-5'>\n"]
    for p in INTRO:
        body.append("    <p class='text-secondary small col-lg-10'>%s</p>\n" % e(p))
    for stitle, note, entries in SECTIONS:
        body.append("    <h2 class='h4 mt-4 mb-2 border-bottom border-secondary-subtle pb-2'>%s</h2>\n" % e(stitle))
        if note:
            body.append("    <p class='text-secondary small'>%s</p>\n" % e(note))
        body.append("    <div class='table-responsive'>\n      <table class='table table-dark table-striped table-hover table-sm align-middle'>\n"
                    "        <thead><tr><th>Title</th><th>By / Where</th><th>Date</th><th>Type</th><th>What it is</th></tr></thead>\n        <tbody>\n")
        for t, where, url, date, typ, what in entries:
            w = "<a href='%s' target='_blank' rel='noopener'>%s</a>" % (e(url), e(where)) if url else e(where)
            body.append("          <tr><td class='fw-semibold'>%s</td><td>%s</td><td class='text-nowrap'>%s</td>"
                        "<td><span class='badge text-bg-secondary badge-type'>%s</span></td>"
                        "<td class='text-secondary small'>%s</td></tr>\n" % (e(t), w, e(date), e(typ), e(what)))
        body.append("        </tbody>\n      </table>\n    </div>\n")
    body.append("    <p class='text-secondary small mt-4'>%s</p>\n  </main>\n" % e(FOOTER))
    footer = ("  <footer class='container text-center text-secondary small py-4 border-top border-secondary-subtle'>\n"
              "    Served from bismuth.cz · <a class='link-secondary' href='https://github.com/hclivess/Bismuth'>github.com/hclivess/Bismuth</a>\n"
              "  </footer>\n</body>\n</html>\n")
    return head + nav + hero + "".join(body) + footer

# ---------------- BBCode (BitcoinTalk) ----------------
def bbcode():
    L = ["[center][size=14pt][b]%s[/b][/size][/center]" % TITLE, "",
         "[center][i]%s[/i][/center]" % SUBTITLE, "", "[i]%s[/i]" % BYLINE, "", "[hr]", ""]
    for p in INTRO:
        L += [p, ""]
    for stitle, note, entries in SECTIONS:
        L += ["[size=12pt][b]%s[/b][/size]" % stitle, ""]
        if note:
            L += ["[i]%s[/i]" % note, ""]
        L.append("[table]")
        L.append("[tr][td][b]Title[/b][/td][td][b]By / Where[/b][/td][td][b]Date[/b][/td][td][b]Type[/b][/td][td][b]What it is[/b][/td][/tr]")
        for t, where, url, date, typ, what in entries:
            w = "[url=%s]%s[/url]" % (url, where) if url else where
            L.append("[tr][td][b]%s[/b][/td][td]%s[/td][td]%s[/td][td]%s[/td][td]%s[/td][/tr]" % (t, w, date, typ, what))
        L.append("[/table]")
        L.append("")
    L += ["[hr]", "", FOOTER, ""]
    return "\n".join(L)

open(OUT + "/Bismuth_Interview_Catalog.md", "w").write(md())
open(OUT + "/Bismuth_Interview_Catalog.html", "w").write(html())
open(OUT + "/Bismuth_Interview_Catalog.bbcode.txt", "w").write(bbcode())
for f in ("Bismuth_Interview_Catalog.md", "Bismuth_Interview_Catalog.html", "Bismuth_Interview_Catalog.bbcode.txt"):
    print(f, os.path.getsize(OUT + "/" + f), "bytes")
