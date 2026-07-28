/* reqrev annotator — injected into requirement docs by server.py */
(() => {
  "use strict";
  const DOC = window.REQREV_DOC;
  if (!DOC) return;

  const TYPES = { comment: "Comment", edit: "Suggested edit", question: "Question" };
  const CONTEXT = 32;

  let annotations = [];
  let meta = { status: "in-progress", docRev: null };
  let editingId = null;
  let editingIsNew = false;
  let pendingSel = null;
  let saveTimer = null;
  let ui = {};

  const uid = () =>
    "a" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  const brief = (s) => (s.length > 140 ? s.slice(0, 137) + "…" : s);
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])
    );

  /* ---------- text index: flat string over content text nodes ---------- */

  function buildIndex() {
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(n) {
        if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
        const p = n.parentElement;
        if (p && p.closest("#rr-sidebar,#rr-fab,script,style,noscript"))
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

  function globalOffset(idx, container, offset) {
    if (container.nodeType === Node.TEXT_NODE) {
      for (const s of idx.spans) if (s.node === container) return s.start + offset;
      return -1;
    }
    const ref =
      offset < container.childNodes.length ? container.childNodes[offset] : null;
    for (const s of idx.spans) {
      if (ref) {
        if (
          s.node === ref ||
          ref.compareDocumentPosition(s.node) & Node.DOCUMENT_POSITION_FOLLOWING
        )
          return s.start;
      } else {
        if (container.contains(s.node)) continue;
        if (container.compareDocumentPosition(s.node) & Node.DOCUMENT_POSITION_FOLLOWING)
          return s.start;
      }
    }
    return idx.text.length;
  }

  /* Locate an annotation's quoted text; disambiguate with prefix/suffix/start. */
  function findMatch(idx, ann) {
    const exact = ann.exact;
    if (!exact) return null;
    const candidates = [];
    let i = idx.text.indexOf(exact);
    while (i !== -1) {
      candidates.push(i);
      i = idx.text.indexOf(exact, i + 1);
    }
    if (!candidates.length) return null;
    let best = candidates[0];
    let bestScore = -1;
    for (const c of candidates) {
      let score = 0;
      const pre = ann.prefix || "";
      const suf = ann.suffix || "";
      if (pre && idx.text.slice(Math.max(0, c - pre.length), c) === pre) score += 2;
      if (suf && idx.text.slice(c + exact.length, c + exact.length + suf.length) === suf)
        score += 2;
      if (typeof ann.start === "number") score += 1 / (1 + Math.abs(c - ann.start));
      if (score > bestScore) {
        bestScore = score;
        best = c;
      }
    }
    return { start: best, end: best + exact.length };
  }

  /* ---------- highlights ---------- */

  function clearMarks() {
    document.querySelectorAll("mark.rr-hl").forEach((m) => {
      const parent = m.parentNode;
      while (m.firstChild) parent.insertBefore(m.firstChild, m);
      parent.removeChild(m);
      parent.normalize();
    });
  }

  function wrapRange(idx, start, end, ann) {
    for (const s of idx.spans) {
      const a = Math.max(start, s.start);
      const b = Math.min(end, s.end);
      if (a >= b) continue;
      let target = s.node;
      if (a - s.start > 0) target = target.splitText(a - s.start);
      if (b - a < target.nodeValue.length) target.splitText(b - a);
      const mark = document.createElement("mark");
      mark.className =
        "rr-hl rr-" + (ann.type || "comment") + (ann.resolved ? " rr-resolved" : "");
      mark.dataset.rrId = ann.id;
      target.parentNode.replaceChild(mark, target);
      mark.appendChild(target);
    }
  }

  function sectionFor(node) {
    let best = null;
    for (const h of document.querySelectorAll("h2[id], h2, h1")) {
      if (h.closest("#rr-sidebar")) continue;
      if (h.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_FOLLOWING) best = h;
      else if (h.contains(node)) best = h;
    }
    if (!best) return { section: "", sectionTitle: "" };
    const no = best.querySelector(".no");
    const title = [...best.childNodes]
      .filter((n) => !(n.nodeType === 1 && n.classList.contains("no")))
      .map((n) => n.textContent)
      .join("")
      .replace(/\s+/g, " ")
      .trim();
    return {
      section: best.id || "",
      sectionTitle: (no ? no.textContent.trim() + " " : "") + title,
    };
  }

  /* ---------- render ---------- */

  function renderAll() {
    clearMarks();
    for (const ann of annotations) {
      const idx = buildIndex();
      const m = findMatch(idx, ann);
      if (m) {
        ann._start = m.start;
        ann.start = m.start; // keep stored offset in sync with current doc
        wrapRange(idx, m.start, m.end, ann);
      } else {
        ann._start = null;
      }
    }
    annotations.sort(
      (x, y) =>
        (x._start ?? Number.MAX_SAFE_INTEGER) - (y._start ?? Number.MAX_SAFE_INTEGER)
    );
    renderSidebar();
  }

  function renderSidebar() {
    const open = annotations.filter((a) => !a.resolved).length;
    ui.count.textContent = annotations.length
      ? `${open} open · ${annotations.length - open} resolved`
      : "";
    ui.hint.style.display = annotations.length ? "none" : "";
    ui.cards.innerHTML = "";
    for (const ann of annotations) ui.cards.appendChild(cardFor(ann));
    renderMeta();
  }

  function cardFor(ann) {
    const card = document.createElement("div");
    card.className =
      "rr-card rr-" + (ann.type || "comment") + (ann.resolved ? " rr-resolved" : "");
    card.dataset.rrId = ann.id;

    if (editingId === ann.id) {
      card.appendChild(editorFor(ann));
      return card;
    }

    card.innerHTML = `
      <div class="rr-top">
        <span class="rr-badge">${TYPES[ann.type] || esc(ann.type)}</span>
        ${ann.resolved ? '<span class="rr-badge rr-badge-res">Resolved</span>' : ""}
      </div>
      ${ann.sectionTitle ? `<div class="rr-sec">§ ${esc(ann.sectionTitle)}</div>` : ""}
      <blockquote>${esc(brief(ann.exact))}</blockquote>
      ${ann._start == null ? '<div class="rr-orphan">⚠ quoted text not found in current document</div>' : ""}
      <div class="rr-text">${esc(ann.comment) || "<em>(no comment)</em>"}</div>
      ${
        ann.type === "edit" && ann.replacement
          ? `<div class="rr-repl"><b>Proposed:</b> ${esc(ann.replacement)}</div>`
          : ""
      }
      ${
        ann.response
          ? `<div class="rr-resp"><b>Response — ${esc(ann.disposition || "addressed")}:</b> ${esc(ann.response)}${
              ann.changes ? `<br><em>Changes:</em> ${esc(ann.changes)}` : ""
            }</div>`
          : ""
      }
      <div class="rr-actions">
        <button data-act="edit">Edit</button>
        <button data-act="resolve">${ann.resolved ? "Reopen" : "Resolve"}</button>
        <button data-act="delete">Delete</button>
      </div>`;

    card.addEventListener("click", (e) => {
      const act = e.target instanceof HTMLElement ? e.target.dataset.act : null;
      if (act === "edit") {
        openEditor(ann.id, false);
      } else if (act === "resolve") {
        ann.resolved = !ann.resolved;
        touch(ann);
        renderAll();
        scheduleSave();
      } else if (act === "delete") {
        if (confirm("Delete this annotation?")) {
          annotations = annotations.filter((a) => a.id !== ann.id);
          renderAll();
          scheduleSave();
        }
      } else {
        scrollToMark(ann.id);
      }
    });
    return card;
  }

  function editorFor(ann) {
    const wrap = document.createElement("div");
    wrap.className = "rr-editor";
    wrap.innerHTML = `
      <blockquote>${esc(brief(ann.exact))}</blockquote>
      <select class="rr-ed-type">
        ${Object.entries(TYPES)
          .map(([v, l]) => `<option value="${v}">${l}</option>`)
          .join("")}
      </select>
      <textarea class="rr-ed-comment" rows="4" placeholder="Your comment…"></textarea>
      <textarea class="rr-ed-repl" rows="3" placeholder="Proposed replacement text…"></textarea>
      <div class="rr-actions">
        <button class="rr-primary" data-ed="save">Save</button>
        <button data-ed="cancel">Cancel</button>
      </div>`;

    const typeSel = wrap.querySelector(".rr-ed-type");
    const commentTa = wrap.querySelector(".rr-ed-comment");
    const replTa = wrap.querySelector(".rr-ed-repl");
    typeSel.value = ann.type || "comment";
    commentTa.value = ann.comment || "";
    replTa.value = ann.replacement || "";
    const syncRepl = () => {
      replTa.style.display = typeSel.value === "edit" ? "" : "none";
    };
    typeSel.addEventListener("change", syncRepl);
    syncRepl();

    wrap.addEventListener("click", (e) => {
      const act = e.target instanceof HTMLElement ? e.target.dataset.ed : null;
      if (act === "save") {
        ann.type = typeSel.value;
        ann.comment = commentTa.value.trim();
        ann.replacement = replTa.value.trim();
        touch(ann);
        editingId = null;
        editingIsNew = false;
        renderAll();
        scheduleSave();
      } else if (act === "cancel") {
        if (editingIsNew) annotations = annotations.filter((a) => a.id !== ann.id);
        editingId = null;
        editingIsNew = false;
        renderAll();
      }
    });
    setTimeout(() => commentTa.focus(), 0);
    return wrap;
  }

  function openEditor(id, isNew) {
    editingId = id;
    editingIsNew = isNew;
    renderSidebar();
    const card = ui.cards.querySelector(`[data-rr-id="${id}"]`);
    if (card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function scrollToMark(id) {
    const mark = document.querySelector(`mark.rr-hl[data-rr-id="${id}"]`);
    if (!mark) return;
    mark.scrollIntoView({ block: "center", behavior: "smooth" });
    document
      .querySelectorAll(`mark.rr-hl[data-rr-id="${id}"]`)
      .forEach((m) => {
        m.classList.remove("rr-flash");
        void m.offsetWidth;
        m.classList.add("rr-flash");
      });
  }

  function focusCard(id) {
    const card = ui.cards.querySelector(`[data-rr-id="${id}"]`);
    if (!card) return;
    card.scrollIntoView({ block: "nearest", behavior: "smooth" });
    card.classList.remove("rr-flash");
    void card.offsetWidth;
    card.classList.add("rr-flash");
  }

  /* ---------- selection → new annotation ---------- */

  function captureSelector(range) {
    const idx = buildIndex();
    const start = globalOffset(idx, range.startContainer, range.startOffset);
    const end = globalOffset(idx, range.endContainer, range.endOffset);
    if (start < 0 || end <= start) return null;
    const node =
      range.startContainer.nodeType === Node.TEXT_NODE
        ? range.startContainer.parentElement
        : range.startContainer;
    return {
      exact: idx.text.slice(start, end),
      prefix: idx.text.slice(Math.max(0, start - CONTEXT), start),
      suffix: idx.text.slice(end, end + CONTEXT),
      start,
      ...sectionFor(node),
    };
  }

  function showFab(rect) {
    ui.fab.style.display = "block";
    ui.fab.style.top = Math.max(8, rect.top - 42) + "px";
    ui.fab.style.left =
      Math.max(8, Math.min(window.innerWidth - 150, rect.left + rect.width / 2 - 55)) +
      "px";
  }

  function hideFab() {
    ui.fab.style.display = "none";
    pendingSel = null;
  }

  function onMouseUp(e) {
    if (e.target.closest && e.target.closest("#rr-sidebar,#rr-fab")) return;
    setTimeout(() => {
      const sel = window.getSelection();
      if (!sel || sel.isCollapsed || !sel.toString().trim()) {
        hideFab();
        return;
      }
      const anchorEl =
        sel.anchorNode &&
        (sel.anchorNode.nodeType === Node.TEXT_NODE
          ? sel.anchorNode.parentElement
          : sel.anchorNode);
      if (anchorEl && anchorEl.closest && anchorEl.closest("#rr-sidebar")) return;
      const range = sel.getRangeAt(0);
      const captured = captureSelector(range);
      if (!captured) {
        hideFab();
        return;
      }
      pendingSel = captured;
      showFab(range.getBoundingClientRect());
    }, 0);
  }

  function onFabClick() {
    if (!pendingSel) return;
    const now = new Date().toISOString().replace(/\.\d+Z$/, "Z");
    const ann = {
      id: uid(),
      ...pendingSel,
      type: "comment",
      comment: "",
      replacement: "",
      resolved: false,
      created: now,
      updated: now,
    };
    annotations.push(ann);
    hideFab();
    window.getSelection().removeAllRanges();
    renderAll();
    openEditor(ann.id, true);
  }

  function touch(ann) {
    ann.updated = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  }

  /* ---------- persistence ---------- */

  function setStatus(msg, cls) {
    ui.status.textContent = msg;
    ui.status.className = cls || "";
  }

  function scheduleSave() {
    setStatus("saving…");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveNow, 400);
  }

  async function postReview(complete) {
    const clean = annotations.map((a) => {
      const { _start, ...rest } = a;
      return rest;
    });
    const r = await fetch("/api/review/" + encodeURIComponent(DOC), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ doc: DOC, annotations: clean, complete: !!complete }),
    });
    const data = await r.json().catch(() => ({ ok: false, error: "bad server response" }));
    if (!r.ok || !data.ok) throw new Error(data.error || "HTTP " + r.status);
    if (data.stamps)
      for (const a of annotations) if (data.stamps[a.id]) a.docRev = data.stamps[a.id];
    meta.status = data.status || "in-progress";
    meta.docRev = data.docRev || null;
    renderMeta();
    return data;
  }

  async function saveNow() {
    try {
      await postReview(false);
      setStatus("saved ✓", "rr-ok");
      setTimeout(() => setStatus(""), 2000);
    } catch (err) {
      setStatus("save failed ✗", "rr-err");
    }
  }

  async function completeReview() {
    if (
      !confirm(
        "Mark this review complete?\n\nThis records the document's git revision " +
          "in the review file. It fails if annotations span multiple revisions " +
          "or the document has uncommitted changes."
      )
    )
      return;
    clearTimeout(saveTimer);
    try {
      await postReview(true);
      setStatus("review complete ✓", "rr-ok");
    } catch (err) {
      alert("Cannot complete review:\n\n" + err.message);
      setStatus("not completed ✗", "rr-err");
    }
  }

  function renderMeta() {
    const rev = meta.docRev ? meta.docRev.slice(0, 12) : null;
    if (meta.status === "complete") {
      ui.rev.innerHTML = "✓ complete @ <code>" + esc(rev || "?") + "</code>";
      ui.rev.className = "rr-rev-done";
      ui.completeBtn.style.display = "none";
    } else {
      ui.rev.innerHTML = rev
        ? "in progress @ <code>" + esc(rev) + "</code>"
        : "in progress";
      ui.rev.className = "";
      ui.completeBtn.style.display = annotations.length ? "" : "none";
    }
  }

  /* ---------- boot ---------- */

  function buildUI() {
    const sb = document.createElement("aside");
    sb.id = "rr-sidebar";
    sb.innerHTML = `
      <header>
        <div class="rr-h1">
          <span class="rr-title">Review</span>
          <span id="rr-count"></span>
          <span id="rr-status"></span>
          <a href="/" title="back to document list">index</a>
        </div>
        <div class="rr-h2">
          <span id="rr-rev"></span>
          <button id="rr-complete" title="record the document's git revision and mark this review complete">✓ Review complete</button>
        </div>
      </header>
      <p id="rr-hint">Select text in the document to add a comment,
        suggested edit, or question. Everything autosaves to the review file.</p>
      <div id="rr-cards"></div>`;
    document.body.appendChild(sb);

    const fab = document.createElement("button");
    fab.id = "rr-fab";
    fab.textContent = "✎ Comment";
    fab.style.display = "none";
    document.body.appendChild(fab);
    fab.addEventListener("mousedown", (e) => e.preventDefault());
    fab.addEventListener("click", onFabClick);

    document.documentElement.classList.add("rr-active");
    ui = {
      sb,
      fab,
      cards: sb.querySelector("#rr-cards"),
      status: sb.querySelector("#rr-status"),
      count: sb.querySelector("#rr-count"),
      hint: sb.querySelector("#rr-hint"),
      rev: sb.querySelector("#rr-rev"),
      completeBtn: sb.querySelector("#rr-complete"),
    };
    ui.completeBtn.addEventListener("click", completeReview);

    document.addEventListener("mouseup", onMouseUp);
    window.addEventListener("scroll", hideFab, { passive: true });
    document.addEventListener("click", (e) => {
      const m = e.target instanceof HTMLElement ? e.target.closest("mark.rr-hl") : null;
      if (m) focusCard(m.dataset.rrId);
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        hideFab();
        if (editingId) {
          if (editingIsNew)
            annotations = annotations.filter((a) => a.id !== editingId);
          editingId = null;
          editingIsNew = false;
          renderAll();
        }
      }
    });
  }

  async function init() {
    buildUI();
    try {
      const r = await fetch("/api/review/" + encodeURIComponent(DOC));
      const data = await r.json();
      annotations = Array.isArray(data.annotations) ? data.annotations : [];
      meta.status = data.status || "in-progress";
      meta.docRev = data.docRev || null;
    } catch (err) {
      annotations = [];
    }
    renderAll();
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else init();
})();
