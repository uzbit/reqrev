/* reqrev search — cross-page search bar, injected into every page by server.py */
(() => {
  "use strict";
  const MIN = 2;
  let box, input, drop;
  let timer = null;
  let ctrl = null;
  let items = [];
  let active = -1;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );

  /* ---------- search bar + dropdown ---------- */

  function buildUI() {
    const mac = /Mac|iP(hone|ad|od)/.test(navigator.platform);
    box = document.createElement("div");
    box.id = "rr-search";
    box.innerHTML = `
      <input id="rr-search-input" type="search" placeholder="Search docs…  ${mac ? "⌘K" : "Ctrl+K"}"
        autocomplete="off" spellcheck="false">
      <div id="rr-search-drop" hidden></div>`;
    document.body.appendChild(box);
    input = box.querySelector("#rr-search-input");
    drop = box.querySelector("#rr-search-drop");

    input.addEventListener("input", () => {
      clearTimeout(timer);
      const q = input.value; // keep whitespace: a leading/trailing space means
      if (q.trim().length < MIN) return hide(); // "token boundary here"
      timer = setTimeout(() => search(q), 200);
    });

    input.addEventListener("keydown", (e) => {
      if (e.key === "ArrowDown") {
        move(1);
        e.preventDefault();
      } else if (e.key === "ArrowUp") {
        move(-1);
        e.preventDefault();
      } else if (e.key === "Enter") {
        const a = items[active] || items[0];
        if (a) a.click();
        e.preventDefault();
      } else if (e.key === "Escape") {
        hide();
        input.blur();
      }
    });

    document.addEventListener("click", (e) => {
      if (!(e.target instanceof Element) || !e.target.closest("#rr-search")) hide();
    });

    document.addEventListener("keydown", (e) => {
      const t = e.target;
      const typing =
        t instanceof Element && t.closest("input,textarea,select,[contenteditable]");
      if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        input.focus();
        input.select();
      } else if (e.key === "/" && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        input.focus();
        input.select();
      }
    });
  }

  function hide() {
    drop.hidden = true;
    drop.innerHTML = "";
    items = [];
    active = -1;
  }

  function move(d) {
    if (!items.length) return;
    active = (active + d + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle("rr-active", i === active));
    items[active].scrollIntoView({ block: "nearest" });
  }

  async function search(q) {
    if (ctrl) ctrl.abort();
    ctrl = new AbortController();
    let data;
    try {
      const r = await fetch("/api/search?q=" + encodeURIComponent(q), {
        signal: ctrl.signal,
      });
      data = await r.json();
    } catch (err) {
      return; // aborted, or server gone
    }
    if (input.value !== q) return;
    render(data.results || [], q);
  }

  function render(results, q) {
    drop.innerHTML = "";
    items = [];
    active = -1;
    if (!results.length) {
      drop.innerHTML = '<div class="rr-s-empty">No matches</div>';
      drop.hidden = false;
      return;
    }
    let lastDoc = null;
    for (const r of results) {
      if (r.doc !== lastDoc) {
        const h = document.createElement("div");
        h.className = "rr-s-doc";
        h.textContent = r.doc;
        drop.appendChild(h);
        lastDoc = r.doc;
      }
      const a = document.createElement("a");
      a.className = "rr-s-item";
      a.href =
        "/doc/" + encodeURI(r.doc) + "#rrfind=" +
        encodeURIComponent(JSON.stringify({ q, n: r.n }));
      a.target = "_blank";
      a.rel = "noopener";
      a.innerHTML =
        (r.section ? `<span class="rr-s-sec">§ ${esc(r.section)}</span>` : "") +
        `<span class="rr-s-snip">${esc(r.before)}<b>${esc(r.match)}</b>${esc(r.after)}</span>`;
      a.addEventListener("click", () => hide());
      drop.appendChild(a);
      items.push(a);
    }
    drop.hidden = false;
  }

  /* ---------- reveal a search hit on the target page ---------- */
  /* Doc links carry #rrfind={q,n}: n is the case-insensitive occurrence number
     of q, counted by the server over the same flat text a DOM walk yields. */

  function flatText() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (p && p.closest("#rr-sidebar,#rr-fab,#rr-search,#rr-rail,script,style,noscript"))
          return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let text = "";
    const spans = [];
    let n;
    while ((n = walker.nextNode())) {
      spans.push({ node: n, start: text.length, end: text.length + n.nodeValue.length });
      text += n.nodeValue;
    }
    return { text, spans };
  }

  function markRange(idx, start, end) {
    for (const s of idx.spans) {
      const a = Math.max(start, s.start);
      const b = Math.min(end, s.end);
      if (a >= b) continue;
      const parent = s.node.parentElement;
      if (parent && parent.closest("svg")) {
        // HTML <mark> is a foreign element inside SVG — tint instead of wrapping
        parent.classList.add("rr-find-svg");
        continue;
      }
      let target = s.node;
      if (a - s.start > 0) target = target.splitText(a - s.start);
      if (b - a < target.nodeValue.length) target.splitText(b - a);
      const mark = document.createElement("mark");
      mark.className = "rr-find";
      target.parentNode.replaceChild(mark, target);
      mark.appendChild(target);
    }
  }

  /* Mirrors the server's matcher: leading/trailing whitespace on the query
     demands a token boundary (non-letter/digit) on that side of the match. */
  function findHits(text, rawQ) {
    const q = rawQ.trim().toLowerCase();
    const wantPre = /^\s/.test(rawQ);
    const wantPost = /\s$/.test(rawQ);
    const isWord = (ch) => /[\p{L}\p{N}]/u.test(ch);
    const low = text.toLowerCase();
    const hits = [];
    let i = low.indexOf(q);
    while (i !== -1) {
      let ok = true;
      if (wantPre && i > 0 && isWord(low[i - 1])) ok = false;
      if (ok && wantPost) {
        const j = i + q.length;
        if (j < low.length && isWord(low[j])) ok = false;
      }
      if (ok) hits.push(i);
      i = low.indexOf(q, i + 1);
    }
    return { q, hits };
  }

  function reveal(spec) {
    const idx = flatText();
    const { q, hits } = findHits(idx.text, spec.q);
    if (!hits.length)
      return toast(`“${spec.q.trim()}” not found in the current document`);
    const start = hits[Math.min(spec.n || 0, hits.length - 1)];
    markRange(idx, start, start + q.length);
    const first = document.querySelector("mark.rr-find, .rr-find-svg");
    if (first) first.scrollIntoView({ block: "center" });
  }

  function locateFromHash() {
    const m = (location.hash || "").match(/rrfind=([^&]*)/);
    if (!m) return;
    let spec;
    try {
      spec = JSON.parse(decodeURIComponent(m[1]));
    } catch (err) {
      return;
    }
    if (!spec || typeof spec.q !== "string" || !spec.q.trim()) return;
    const run = () => setTimeout(() => reveal(spec), 60);
    if (document.readyState === "complete") run();
    else window.addEventListener("load", run);
  }

  function toast(msg) {
    const t = document.createElement("div");
    t.id = "rr-search-toast";
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.remove(), 3500);
  }

  function boot() {
    buildUI();
    locateFromHash();
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
