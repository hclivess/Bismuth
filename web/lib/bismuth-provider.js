// bismuth-provider.js — the injected window.bismuth provider shim (doc/33 §2-3).
//
// TWO-LAYER MODEL: this file is the KEYLESS in-page provider. It exposes an EIP-1193-flavoured API
// (request({method, params}), events, isBismuth, networkId) to a dApp page, and forwards every call to
// the origin-isolated WALLET APP (web/wallet/, a sandboxed iframe or a second-origin window) over
// window.postMessage with STRICT origin checks. No private key, seed, or signing logic lives here — the
// wallet app is the only code that touches key material (doc/33 §1, §8.1 origin isolation).
//
// Usage in a dApp page:
//   import { installBismuthProvider } from "../lib/bismuth-provider.js";
//   const provider = installBismuthProvider({ walletOrigin: "http://127.0.0.1:8097", walletUrl: "..." });
//   const [addr] = await window.bismuth.request({ method: "bismuth_requestAccounts" });
//   const { txid } = await window.bismuth.request({ method: "bismuth_sendTransaction",
//          params: [{ recipient, amount, operation, openfield }] });
//
// The wallet app must answer postMessage envelopes of shape:
//   { __bismuth: 1, id, method, params }  ->  { __bismuth: 1, id, result }  or  { __bismuth: 1, id, error }
// and may push event envelopes  { __bismuth: 1, event, data }.

const PROTO = "__bismuth";

// EIP-1193-style provider error codes (so dApps can branch like MetaMask).
export const ProviderErrors = {
  USER_REJECTED: 4001,
  UNAUTHORIZED: 4100,
  UNSUPPORTED_METHOD: 4200,
  DISCONNECTED: 4900,
  CHAIN_DISCONNECTED: 4901,
};

const KNOWN_METHODS = new Set([
  "bismuth_requestAccounts",
  "bismuth_accounts",
  "bismuth_getBalance",
  "bismuth_signTransaction",
  "bismuth_sendTransaction",
  "bismuth_signMessage",
  "bismuth_networkId",
]);

export class BismuthProvider {
  constructor(opts) {
    opts = opts || {};
    this.isBismuth = true;
    this.networkId = opts.networkId || null;      // populated after connect (ledger fingerprint)
    this._walletOrigin = opts.walletOrigin || null; // EXACT origin of the wallet app (required for security)
    this._walletWindow = opts.walletWindow || null; // the wallet iframe contentWindow or popup
    this._walletUrl = opts.walletUrl || null;       // for auto-creating a sandboxed iframe if no window given
    this._timeoutMs = opts.timeoutMs || 120000;     // approval prompts can take a while
    this._pending = new Map();                      // id -> {resolve, reject, timer}
    this._listeners = new Map();                    // event -> Set<fn>
    this._seq = 1;
    this._accounts = [];
    this._connected = false;
    this._onMessage = this._onMessage.bind(this);

    if (typeof window !== "undefined") {
      window.addEventListener("message", this._onMessage);
    }
    if (!this._walletWindow && this._walletUrl && typeof document !== "undefined") {
      this._createIframe();
    }
  }

  // ---- iframe bootstrap (single-machine demo): a sandboxed iframe on the wallet origin ----
  _createIframe() {
    const iframe = document.createElement("iframe");
    iframe.src = this._walletUrl;
    iframe.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;";
    // sandbox lets the wallet run scripts + use its own origin storage, but the dApp realm cannot reach in.
    iframe.setAttribute("sandbox", "allow-scripts allow-same-origin allow-popups allow-modals");
    document.body.appendChild(iframe);
    this._iframe = iframe;
    this._walletWindow = iframe.contentWindow;
    if (!this._walletOrigin) {
      try { this._walletOrigin = new URL(this._walletUrl, location.href).origin; } catch (e) { /* */ }
    }
  }

  // ---- the EIP-1193-style entry point ----
  request(args) {
    args = args || {};
    const method = args.method;
    const params = args.params || [];
    if (!KNOWN_METHODS.has(method)) {
      return Promise.reject(this._err(ProviderErrors.UNSUPPORTED_METHOD, "unsupported method: " + method));
    }
    if (!this._walletWindow) {
      return Promise.reject(this._err(ProviderErrors.DISCONNECTED, "wallet app not available"));
    }
    return this._rpc(method, params).then((result) => {
      // bookkeeping for connect/accounts/network
      if (method === "bismuth_requestAccounts") {
        this._accounts = result || [];
        if (!this._connected) { this._connected = true; this._emit("connect", { networkId: this.networkId }); }
        this._emit("accountsChanged", this._accounts);
      } else if (method === "bismuth_accounts") {
        this._accounts = result || [];
      } else if (method === "bismuth_networkId") {
        this.networkId = result;
      }
      return result;
    });
  }

  _rpc(method, params) {
    const id = this._seq++;
    const envelope = {}; envelope[PROTO] = 1; envelope.id = id; envelope.method = method; envelope.params = params;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(this._err(ProviderErrors.DISCONNECTED, "wallet did not respond (timeout)"));
      }, this._timeoutMs);
      this._pending.set(id, { resolve, reject, timer });
      // targetOrigin MUST be the exact wallet origin (never "*") so a malicious frame cannot receive params.
      const target = this._walletOrigin;
      if (!target) {
        clearTimeout(timer); this._pending.delete(id);
        return reject(this._err(ProviderErrors.DISCONNECTED, "wallet origin not configured; refusing to postMessage to '*'"));
      }
      try {
        this._walletWindow.postMessage(envelope, target);
      } catch (e) {
        clearTimeout(timer); this._pending.delete(id);
        reject(this._err(ProviderErrors.DISCONNECTED, "postMessage failed: " + e));
      }
    });
  }

  _onMessage(ev) {
    // STRICT origin check: require a configured wallet origin and an exact match (never wildcard).
    if (!this._walletOrigin || ev.origin !== this._walletOrigin) return;
    const data = ev.data;
    if (!data || data[PROTO] !== 1) return;
    // also bind the source window when known (defence in depth)
    if (this._walletWindow && ev.source && ev.source !== this._walletWindow) return;

    if (data.event) {
      this._handleEvent(data.event, data.data);
      return;
    }
    if (data.id == null) return;
    const p = this._pending.get(data.id);
    if (!p) return;
    this._pending.delete(data.id);
    clearTimeout(p.timer);
    if (Object.prototype.hasOwnProperty.call(data, "error") && data.error) {
      const e = data.error;
      p.reject(this._err(e.code || ProviderErrors.UNAUTHORIZED, e.message || String(e), e.data));
    } else {
      p.resolve(data.result);
    }
  }

  _handleEvent(event, payload) {
    if (event === "accountsChanged") this._accounts = payload || [];
    if (event === "chainChanged") this.networkId = payload;
    if (event === "disconnect") { this._connected = false; this._accounts = []; }
    this._emit(event, payload);
  }

  // ---- event subscription (MetaMask-style on/removeListener) ----
  on(event, fn) {
    if (!this._listeners.has(event)) this._listeners.set(event, new Set());
    this._listeners.get(event).add(fn);
    return this;
  }

  removeListener(event, fn) {
    const s = this._listeners.get(event);
    if (s) s.delete(fn);
    return this;
  }

  _emit(event, payload) {
    const s = this._listeners.get(event);
    if (s) for (const fn of Array.from(s)) { try { fn(payload); } catch (e) { /* swallow */ } }
  }

  _err(code, message, data) {
    const e = new Error(message);
    e.code = code;
    if (data !== undefined) e.data = data;
    return e;
  }

  // ---- convenience helpers mirroring the doc/33 §3 table ----
  requestAccounts() { return this.request({ method: "bismuth_requestAccounts" }); }
  accounts() { return this.request({ method: "bismuth_accounts" }); }
  getBalance(address) { return this.request({ method: "bismuth_getBalance", params: [{ address }] }); }
  signTransaction(tx) { return this.request({ method: "bismuth_signTransaction", params: [tx] }); }
  sendTransaction(tx) { return this.request({ method: "bismuth_sendTransaction", params: [tx] }); }
  signMessage(message) { return this.request({ method: "bismuth_signMessage", params: [{ message }] }); }
}

// Inject as window.bismuth (idempotent). Returns the provider instance.
export function installBismuthProvider(opts) {
  const provider = new BismuthProvider(opts);
  if (typeof window !== "undefined") {
    try {
      Object.defineProperty(window, "bismuth", { value: provider, configurable: true });
    } catch (e) {
      window.bismuth = provider;
    }
    // EIP-1193-style announce so dApps can detect the provider lazily.
    try { window.dispatchEvent(new Event("bismuth#initialized")); } catch (e) { /* */ }
  }
  return provider;
}

export default BismuthProvider;
