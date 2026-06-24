// poker-nav.js — shared cross-page navigation for the Bismuth poker app, so the table, tournament lobby,
// spectator-betting panel and wallet feel like ONE app. Include on every page (the data-active picks the
// highlighted tab):
//     <script src="../lib/poker-nav.js" data-active="table"></script>      (from web/poker/*)
//     <script src="../lib/poker-nav.js" data-active="wallet"></script>     (from web/wallet/*)
// It injects a slim sticky top bar and resolves links relative to whichever directory the page lives in
// (web/poker/ vs web/wallet/). The wallet connection persists per origin, so moving between tabs keeps the
// same connected account (no re-prompt) — clicking Connect on any page reconnects silently.
(function () {
  var script = document.currentScript;
  var active = (script && script.getAttribute("data-active")) || "";
  // Resolve hrefs relative to the including page's directory.
  var inWallet = /\/wallet\//.test(location.pathname);
  var pokerBase = inWallet ? "../poker/" : "";
  var walletBase = inWallet ? "" : "../wallet/";
  var links = [
    { id: "table",   label: "Table",       href: pokerBase + "index.html",   glyph: "♠" }, // ♠
    { id: "lobby",   label: "Tournaments", href: pokerBase + "lobby.html",   glyph: "♣" }, // ♣
    { id: "betting", label: "Betting",     href: pokerBase + "betting.html", glyph: "♦" }, // ♦
    { id: "wallet",  label: "Wallet",      href: walletBase + "index.html",  glyph: "⛃" }, // ⛃
  ];

  var css =
    ".pk-nav{position:sticky;top:0;z-index:9999;display:flex;align-items:center;gap:2px;" +
    "padding:6px 12px;background:#10151c;border-bottom:1px solid #2a3340;" +
    "font:600 14px/1 system-ui,Segoe UI,Roboto,Arial,sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.35)}" +
    ".pk-nav .pk-brand{color:#7fd1b9;letter-spacing:.5px;margin-right:14px;white-space:nowrap}" +
    ".pk-nav .pk-brand b{color:#e8eef5}" +
    ".pk-nav a{color:#9fb0c3;text-decoration:none;padding:7px 12px;border-radius:7px;white-space:nowrap;" +
    "transition:background .12s,color .12s}" +
    ".pk-nav a:hover{background:#1b2530;color:#e8eef5}" +
    ".pk-nav a.on{background:#1f6f5c;color:#fff}" +
    ".pk-nav a .g{opacity:.85;margin-right:6px}" +
    ".pk-nav .pk-spacer{flex:1}" +
    ".pk-nav .pk-status{color:#6f8197;font-weight:500;font-size:12px;white-space:nowrap}";
  var style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  var nav = document.createElement("nav");
  nav.className = "pk-nav";
  var html = '<span class="pk-brand"><b>♣ Bismuth Poker</b></span>';
  for (var i = 0; i < links.length; i++) {
    var l = links[i];
    html += '<a href="' + l.href + '"' + (l.id === active ? ' class="on"' : "") +
      '><span class="g">' + l.glyph + "</span>" + l.label + "</a>";
  }
  html += '<span class="pk-spacer"></span><span class="pk-status" id="pkNavStatus"></span>';
  nav.innerHTML = html;
  document.body.insertBefore(nav, document.body.firstChild);

  // Best-effort shared wallet-status pill: if window.bismuth is present and already authorized for this
  // origin, show the short address. Never prompts (uses bismuth_accounts, not requestAccounts).
  function shorten(a) { return a && a.length > 12 ? a.slice(0, 8) + "…" + a.slice(-4) : a; }
  function render(accts) {
    var el = document.getElementById("pkNavStatus");
    if (!el) return;
    el.textContent = (accts && accts.length) ? ("● " + shorten(accts[0])) : "wallet not connected";
    el.style.color = (accts && accts.length) ? "#7fd1b9" : "#6f8197";
  }
  function poll() {
    try {
      if (window.bismuth && window.bismuth.request) {
        window.bismuth.request({ method: "bismuth_accounts" }).then(render).catch(function () {});
        if (window.bismuth.on) window.bismuth.on("accountsChanged", render);
      } else { render(null); }
    } catch (e) { render(null); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", poll);
  else poll();
})();
