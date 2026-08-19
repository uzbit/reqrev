#!/usr/bin/env python3
"""reqrev — a tiny local requirement-review annotator.

Serves requirement HTML docs with an injected annotation layer. Reviews are
saved as self-contained HTML files (human-readable report + embedded JSON)
under the reviews directory.

Usage:
    python3 server.py [--docs DIR] [--reviews DIR] [--port N]

No dependencies beyond the Python standard library.
"""
import argparse
import html
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

DATA_RE = re.compile(
    r'<script type="application/json" id="reqrev-data">(.*?)</script>', re.S
)
TYPE_LABELS = {"comment": "Comment", "edit": "Suggested edit", "question": "Question"}


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(name):
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def safe_doc(name):
    """Doc names may include subdirectories — docs are discovered recursively."""
    return (bool(name) and "\\" not in name and ".." not in name
            and not name.startswith("/"))


def review_path(reviews_dir, doc_name):
    return reviews_dir / (Path(doc_name).stem + ".review.html")


def git_doc_state(docs_dir, name):
    """Return (rev, dirty, subject, date, error) for the last commit that touched
    the doc (not repo HEAD — unrelated commits must not invalidate a review)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(docs_dir), "log", "-1",
             "--format=%H%x09%ad%x09%s", "--date=short", "--", name],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, False, "", "", "git is not available"
    if r.returncode != 0:
        return None, False, "", "", "docs directory is not in a git repository"
    line = r.stdout.strip()
    if not line:
        return None, False, "", "", f"{name} is not tracked in git (commit it first)"
    rev, date, subject = (line.split("\t", 2) + ["", ""])[:3]
    s = subprocess.run(
        ["git", "-C", str(docs_dir), "status", "--porcelain", "--", name],
        capture_output=True, text=True, timeout=10,
    )
    return rev, bool(s.stdout.strip()), subject, date, None


class TextExtractor(HTMLParser):
    """Flatten a doc to the text a browser DOM walk over <body> would yield
    (entities decoded, script/style/title skipped), so occurrence numbers
    computed here line up with the client-side text index in search.js."""

    SKIP = {"script", "style", "noscript", "template", "title"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.pos = 0
        self.skip = 0
        self.headings = []  # (start_pos, title) for h1/h2, in document order
        self._h_start = None
        self._h_parts = None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in ("h1", "h2") and not self.skip:
            self._h_start = self.pos
            self._h_parts = []
        elif self._h_parts is not None:
            self._h_parts.append(" ")  # tag boundary inside a heading, e.g. </span>

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2") and self._h_parts is not None:
            self.headings.append(
                (self._h_start, " ".join("".join(self._h_parts).split()))
            )
            self._h_start = self._h_parts = None
        elif self._h_parts is not None:
            self._h_parts.append(" ")

    def handle_data(self, data):
        if self.skip:
            return
        self.parts.append(data)
        self.pos += len(data)
        if self._h_parts is not None:
            self._h_parts.append(data)


def extract_doc_text(raw):
    p = TextExtractor()
    try:
        p.feed(raw)
        p.close()
    except Exception:
        pass
    return "".join(p.parts), p.headings


def load_review(reviews_dir, doc_name):
    path = review_path(reviews_dir, doc_name)
    if not path.exists():
        return {"version": 1, "doc": doc_name, "annotations": []}
    matches = DATA_RE.findall(path.read_text(encoding="utf-8"))
    for candidate in reversed(matches):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return {"version": 1, "doc": doc_name, "annotations": []}


def render_review_html(doc_name, data):
    annotations = data.get("annotations", [])
    updated = data.get("updated", "")
    stem = Path(doc_name).stem
    open_n = sum(1 for a in annotations if not a.get("resolved"))
    resolved_n = len(annotations) - open_n

    status = data.get("status", "in-progress")
    doc_rev = data.get("docRev") or ""
    revs = sorted({a.get("docRev") for a in annotations if a.get("docRev")})
    if status == "complete":
        status_html = '<span class="done">✓ review complete</span> ' + html.escape(
            data.get("completedAt") or ""
        )
        if doc_rev:
            status_html += f" · document revision <code>{html.escape(doc_rev)}</code>"
        if len(revs) > 1:
            status_html += f" · reviewed across {len(revs)} document revisions"
    else:
        status_html = "review in progress"
        if len(revs) == 1:
            status_html += f" · document revision <code>{html.escape(revs[0])}</code>"
        elif len(revs) > 1:
            status_html += (
                ' · <span class="warn">⚠ annotations span multiple document revisions: '
                + html.escape(", ".join(r[:12] for r in revs))
                + "</span>"
            )

    articles = []
    for a in sorted(annotations, key=lambda a: a.get("start") or 0):
        typ = a.get("type", "comment")
        label = TYPE_LABELS.get(typ, typ)
        status = "resolved" if a.get("resolved") else "open"
        sec = a.get("sectionTitle") or ""
        sec_html = f'<span class="sec">§ {html.escape(sec)}</span>' if sec else ""
        repl = ""
        if typ == "edit" and a.get("replacement"):
            repl = (
                '<p class="repl"><strong>Proposed replacement:</strong> '
                + html.escape(a["replacement"])
                + "</p>"
            )
        resp = ""
        if a.get("response"):
            disp = html.escape(a.get("disposition") or "addressed")
            resp = (
                f'<p class="repl"><strong>Response — {disp}:</strong> '
                + html.escape(a["response"])
            )
            if a.get("changes"):
                resp += "<br>\n<em>Changes:</em> " + html.escape(a["changes"])
            resp += "</p>"
        comment = html.escape(a.get("comment", "")) or "<em>(no comment)</em>"
        articles.append(
            f"""<article class="ann {html.escape(typ)} {status}">
<header><span class="badge b-{html.escape(typ)}">{label}</span> {sec_html}
<span class="status">{status}</span></header>
<blockquote>{html.escape(a.get("exact", ""))}</blockquote>
<p class="comment">{comment}</p>
{repl}{resp}
<p class="meta">created {html.escape(a.get("created", "?"))} · updated {html.escape(a.get("updated", "?"))}</p>
</article>"""
        )

    payload = json.dumps(data, indent=2, ensure_ascii=False).replace("<", "\\u003c")

    body = "\n".join(articles) if articles else "<p><em>No annotations.</em></p>"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review — {html.escape(stem)}</title>
<style>
body{{margin:0 auto;max-width:760px;padding:40px 24px 80px;background:#fafaf6;color:#20241f;
  font:400 15.5px/1.65 system-ui,sans-serif}}
h1{{font-size:26px;margin:0 0 4px}}
.summary{{color:#5c6058;font-size:13.5px;border-bottom:1px solid #e2e1d7;padding-bottom:14px;margin-bottom:28px}}
article.ann{{border:1px solid #e2e1d7;background:#fff;padding:16px 18px;margin:16px 0;border-radius:6px}}
article.ann.resolved{{opacity:.62}}
article header{{display:flex;gap:10px;align-items:baseline;font-size:12.5px;margin-bottom:8px}}
.badge{{font-weight:600;text-transform:uppercase;letter-spacing:.06em;font-size:10.5px;
  padding:2px 7px;border-radius:3px;background:#eee}}
.b-comment{{background:#fdf0b8}}.b-edit{{background:#ffd9cf}}.b-question{{background:#d4e5fb}}
.sec{{color:#5c6058}}
.status{{margin-left:auto;color:#5c6058;font-size:11px;text-transform:uppercase;letter-spacing:.08em}}
blockquote{{margin:8px 0;padding:8px 14px;border-left:3px solid #c9c8bb;background:#f4f4ec;
  color:#3c3c34;font-size:14px}}
.comment{{margin:10px 0 4px;white-space:pre-wrap}}
.repl{{margin:8px 0 4px;padding:8px 12px;background:#e9f1ec;border-radius:4px;white-space:pre-wrap;font-size:14px}}
.meta{{color:#8a8a7e;font-size:11.5px;margin:10px 0 0}}
.done{{color:#2e5e4e;font-weight:600}}
.warn{{color:#a03123}}
code{{font:500 12px ui-monospace,monospace;background:#f0f0e9;padding:1px 5px;border-radius:3px}}
</style>
</head>
<body>
<!--
  reqrev review file — generated by reqrev, editable via the reqrev app.
  Human-readable report below. The authoritative machine-readable data is the
  JSON inside the script element with id "reqrev-data" at the bottom.
  Each annotation: {{ exact, prefix, suffix, start, section, sectionTitle,
  type: comment|edit|question, comment, replacement, resolved, created,
  updated, docRev }} — `exact` quotes the reviewed passage in the source
  document, `prefix`/`suffix` give surrounding context for locating it, and
  for type "edit" the `replacement` field holds the proposed new text.
  Annotations that have been addressed may additionally carry `disposition`
  (applied | answered | orphaned), `response` (the author's answer or
  rationale), and `changes` (what was edited as a result).
  `docRev` is the git revision of the source document the annotation was made
  against ("-dirty" suffix = uncommitted changes were present). Top-level
  `status` is "in-progress" or "complete"; when complete, `docRev` and
  `completedAt` record the document's revision at completion time. Each
  annotation keeps the revision it was made against — reviews addressed in
  rounds may span several.
-->
<h1>Review: {html.escape(stem)}</h1>
<p class="summary">source: {html.escape(doc_name)} · updated {html.escape(updated)} ·
{len(annotations)} annotation(s) — {open_n} open, {resolved_n} resolved<br>
{status_html}</p>
{body}
<script type="application/json" id="reqrev-data">
{payload}
</script>
</body>
</html>
"""


STORIES = "stories/"


def make_handler(docs_dir, stories_dir, reviews_dir):
    def resolve(name):
        """Map a display name to (root_dir, path relative to that root).
        Names under the stories/ prefix live in the stories directory."""
        if stories_dir and name.startswith(STORIES):
            return stories_dir, name[len(STORIES):]
        return docs_dir, name

    def list_docs():
        req = sorted(
            p.relative_to(docs_dir).as_posix() for p in docs_dir.rglob("*.html")
        )
        stories = []
        if stories_dir:
            stories = sorted(
                STORIES + p.relative_to(stories_dir).as_posix()
                for p in stories_dir.rglob("*.html")
                if p.name != "STORY-TEMPLATE.html"
            )
        return req, stories

    class Handler(BaseHTTPRequestHandler):
        server_version = "reqrev/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("[reqrev] %s\n" % (fmt % args))

        # ---- helpers ----
        def send_body(self, body, ctype="text/html; charset=utf-8", code=200):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, obj, code=200):
            self.send_body(json.dumps(obj), "application/json; charset=utf-8", code)

        # ---- routes ----
        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            if path == "/":
                return self.page_index()
            if path.startswith("/doc/"):
                return self.page_doc(path[len("/doc/"):])
            if path.startswith("/static/"):
                return self.file_static(path[len("/static/"):])
            if path.startswith("/api/review/"):
                return self.api_get_review(path[len("/api/review/"):])
            if path == "/api/search":
                return self.api_search()
            if path.startswith("/reviews/"):
                return self.file_review(path[len("/reviews/"):])
            self.send_error(404)

        def do_POST(self):
            path = unquote(self.path.split("?")[0])
            if path.startswith("/api/review/"):
                return self.api_save_review(path[len("/api/review/"):])
            self.send_error(404)

        def doc_row(self, name, label, badge=""):
            """Returns (row html, {"complete": 0|1, "open": n})."""
            data = load_review(reviews_dir, name)
            anns = data.get("annotations", [])
            open_n = sum(1 for a in anns if not a.get("resolved"))
            complete = data.get("status") == "complete"
            counts = "—"
            review_link = ""
            if anns or complete:
                counts = f"{open_n} open / {len(anns) - open_n} resolved"
                if complete:
                    counts = (f'<span class="done">✓ complete @ '
                              f'{html.escape((data.get("docRev") or "")[:8])}</span> · {counts}')
                rfile = review_path(reviews_dir, name).name
                review_link = f'<a class="rev" href="/reviews/{html.escape(rfile)}">review file</a>'
            row = (
                f"""<div class="row"><a class="doc" href="/doc/{html.escape(name)}">{html.escape(label)}</a>{badge}
<span class="counts">{counts}</span>{review_link}</div>"""
            )
            return row, {"complete": int(complete), "open": open_n}

        def doc_tree(self, names, strip=""):
            """Nest display names into {segment: subtree}; files live under ""."""
            tree = {}
            for n in names:
                parts = (n[len(strip):] if strip else n).split("/")
                node = tree
                for seg in parts[:-1]:
                    node = node.setdefault(seg, {})
                node.setdefault("", []).append((n, parts[-1]))
            return tree

        def render_tree(self, node, badge_fn=None, path=""):
            """Files first, then a collapsible <details> per subdirectory.
            Returns (html, total docs, complete, open annotations)."""
            out, total, done, opens = [], 0, 0, 0
            for name, label in node.get("", []):
                row, st = self.doc_row(name, label,
                                       badge_fn(name) if badge_fn else "")
                out.append(row)
                total += 1
                done += st["complete"]
                opens += st["open"]
            for seg in sorted(k for k in node if k != ""):
                key = path + seg
                inner, t, d, o = self.render_tree(node[seg], badge_fn, key + "/")
                total, done, opens = total + t, done + d, opens + o
                meta = f"{t} doc{'' if t == 1 else 's'}"
                if d:
                    meta += f' · <span class="done">{d} complete</span>'
                if o:
                    meta += f" · {o} open"
                out.append(
                    f'<details class="grp" open data-k="{html.escape(key)}">'
                    f'<summary><span class="dir">{html.escape(seg)}/</span>'
                    f'<span class="gmeta">{meta}</span></summary>'
                    f'<div class="kids">{inner}</div></details>'
                )
            return "\n".join(out), total, done, opens

        def story_badge(self, name):
            root, rel = resolve(name)
            try:
                raw = (root / rel).read_text(encoding="utf-8")
            except OSError:
                return ""
            m = re.search(r"Status:\s*([A-Za-z]+)", raw)
            if not m:
                return ""
            st = m.group(1)
            return f' <span class="st st-{st.lower()}">{html.escape(st)}</span>'

        def page_index(self):
            req_names, story_names = list_docs()
            listing = self.render_tree(self.doc_tree(req_names))[0] \
                or "<p><em>No .html files found in docs directory.</em></p>"
            story_section = ""
            if story_names:
                srows = self.render_tree(
                    self.doc_tree(story_names, STORIES),
                    self.story_badge, STORIES,
                )[0]
                story_section = f'<h2>Stories</h2>\n<div class="list">{srows}</div>'
            sub = (f"requirement review — docs from <code>{html.escape(str(docs_dir))}</code>"
                   + (f", stories from <code>{html.escape(str(stories_dir))}</code>"
                      if stories_dir else "")
                   + f",\nreviews saved to <code>{html.escape(str(reviews_dir))}</code>")
            req_heading = "<h2>Requirements</h2>"
            self.send_body(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>reqrev</title>
<link rel="stylesheet" href="/static/search.css">
<script src="/static/search.js" defer></script>
<style>
body{{margin:0 auto;max-width:720px;padding:72px 24px 48px;background:#fafaf6;color:#20241f;
  font:400 15.5px/1.7 system-ui,sans-serif}}
h1{{font-size:24px;margin:0}}
h2{{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:#5c6058;
  margin:36px 0 2px;display:flex;align-items:baseline;gap:12px}}
h2 button{{font:600 10.5px system-ui,sans-serif;letter-spacing:.06em;
  text-transform:uppercase;color:#35566b;background:none;border:none;
  padding:0;cursor:pointer}}
h2 button:hover{{text-decoration:underline}}
p.sub{{color:#5c6058;margin:4px 0 30px;font-size:13.5px}}
.list{{width:fit-content;min-width:100%;max-width:calc(100vw - 48px)}}
.row{{display:flex;gap:14px;align-items:baseline;padding:11px 4px;
  border-bottom:1px solid #e2e1d7;white-space:nowrap}}
details.grp{{border-bottom:1px solid #e2e1d7}}
details.grp>summary{{display:flex;gap:10px;align-items:baseline;padding:10px 4px;
  cursor:pointer;list-style:none;white-space:nowrap}}
summary::-webkit-details-marker{{display:none}}
summary::before{{content:"";width:0;height:0;border:5px solid transparent;
  border-left-color:#9a9a8c;align-self:center;margin-right:2px;
  transition:transform .12s ease}}
summary:hover::before{{border-left-color:#2e5e4e}}
details[open]>summary::before{{transform:rotate(90deg) translateX(-1px)}}
summary:hover .dir{{color:#2e5e4e}}
.dir{{font-weight:600;color:#3c3c34}}
.gmeta{{margin-left:auto;color:#8a8a7e;font-size:13px}}
.gmeta .done{{color:#2e5e4e;font-weight:600}}
.kids{{margin-left:5px;padding-left:19px;border-left:2px solid #eceadf}}
.kids>.row:last-child,.kids>details.grp:last-child{{border-bottom:none}}
a.doc{{color:#2e5e4e;font-weight:600;text-decoration:none}}
a.doc:hover{{text-decoration:underline}}
.counts{{color:#5c6058;font-size:13px;margin-left:auto}}
.counts .done{{color:#2e5e4e;font-weight:600}}
a.rev{{color:#35566b;font-size:13px}}
.st{{font:700 10px/1 system-ui,sans-serif;letter-spacing:.06em;text-transform:uppercase;
  padding:3px 7px;border-radius:3px;background:#f0f0e9;color:#5c6058}}
.st-done,.st-approved{{background:#e9f1ec;color:#2e5e4e}}
.st-inprogress{{background:#e9eff4;color:#35566b}}
.st-review{{background:#f7eeda;color:#6e5316}}
.st-blocked{{background:#fbe0dc;color:#a03123}}
</style></head><body>
<h1>reqrev</h1>
<p class="sub">{sub}</p>
{req_heading}<div class="list">{listing}</div>
{story_section}
<script>
/* remember which directory groups are collapsed across reloads */
(() => {{
  const KEY = "reqrev-collapsed";
  /* Freeze each list at its fully-expanded width — measured now, while the
     server-rendered markup still has every group open — so toggling a group
     never reflows the section sideways. Lists start at the text column's left
     edge and grow right, so that edge is fixed and the width can be clamped
     to whatever room is left. */
  const lists = [...document.querySelectorAll(".list")];
  const natural = lists.map((l) => l.getBoundingClientRect().width);
  const fit = () => lists.forEach((l, i) => {{
    const left = l.getBoundingClientRect().left;
    l.style.width = Math.min(natural[i], window.innerWidth - left - 24) + "px";
  }});
  fit();
  window.addEventListener("resize", fit);

  const shut = new Set(JSON.parse(localStorage.getItem(KEY) || "[]"));
  const groups = [...document.querySelectorAll("details.grp")];
  for (const d of groups) if (shut.has(d.dataset.k)) d.open = false;
  const save = () => localStorage.setItem(KEY, JSON.stringify(
    groups.filter((d) => !d.open).map((d) => d.dataset.k)));
  for (const d of groups) d.addEventListener("toggle", save);
  for (const h of document.querySelectorAll("h2")) {{
    const list = h.nextElementSibling;
    const inner = [...list.querySelectorAll("details.grp")];
    if (!inner.length) continue;
    const btn = document.createElement("button");
    const sync = () => {{ btn.textContent = inner.some((d) => d.open) ? "collapse all" : "expand all"; }};
    btn.addEventListener("click", () => {{
      const opening = !inner.some((d) => d.open);
      inner.forEach((d) => {{ d.open = opening; }});
      sync();
    }});
    inner.forEach((d) => d.addEventListener("toggle", sync));
    sync();
    h.appendChild(btn);
  }}
}})();
</script>
</body></html>""")

        def page_doc(self, name):
            if not safe_doc(name):
                return self.send_error(400)
            root, rel = resolve(name)
            path = root / rel
            if not path.is_file():
                return self.send_error(404)
            raw = path.read_text(encoding="utf-8")
            inject = (
                '<link rel="stylesheet" href="/static/annotator.css">'
                '<link rel="stylesheet" href="/static/search.css">'
                f"<script>window.REQREV_DOC={json.dumps(name)}</script>"
                '<script src="/static/annotator.js" defer></script>'
                '<script src="/static/search.js" defer></script>'
            )
            m = re.search(r"</body>", raw, re.I)
            if m:
                raw = raw[: m.start()] + inject + raw[m.start():]
            else:
                raw += inject
            self.send_body(raw)

        def file_static(self, name):
            if not safe_name(name):
                return self.send_error(400)
            path = STATIC_DIR / name
            if not path.is_file():
                return self.send_error(404)
            ctype = {
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(path.suffix, "application/octet-stream")
            self.send_body(path.read_bytes(), ctype)

        def file_review(self, name):
            if not safe_name(name):
                return self.send_error(400)
            path = reviews_dir / name
            if not path.is_file():
                return self.send_error(404)
            raw = path.read_text(encoding="utf-8")
            inject = ('<link rel="stylesheet" href="/static/search.css">'
                      '<script src="/static/search.js" defer></script>')
            m = re.search(r"</body>", raw, re.I)
            if m:
                raw = raw[: m.start()] + inject + raw[m.start():]
            else:
                raw += inject
            self.send_body(raw)

        def api_search(self):
            MAX_PER_DOC, MAX_TOTAL, CTX = 6, 40, 60
            qs = parse_qs(urlparse(self.path).query)
            raw_q = (qs.get("q") or [""])[0]
            # leading/trailing whitespace on the query demands a token boundary
            # (non-letter/digit) on that side — "D-1 " matches D-1 but not D-10
            want_pre = bool(raw_q) and raw_q[0].isspace()
            want_post = bool(raw_q) and raw_q[-1].isspace()
            q = raw_q.strip()
            results = []
            if len(q) < 2:
                return self.send_json({"q": raw_q, "results": results})
            ql = q.lower()
            req_names, story_names = list_docs()
            for name in req_names + story_names:
                if len(results) >= MAX_TOTAL:
                    break
                root, rel = resolve(name)
                try:
                    raw = (root / rel).read_text(encoding="utf-8")
                except OSError:
                    continue
                text, headings = extract_doc_text(raw)
                low = text.lower()
                shown = 0
                n = 0  # occurrence number within the doc — the client's locator
                i = low.find(ql)
                while i != -1 and shown < MAX_PER_DOC and len(results) < MAX_TOTAL:
                    j = i + len(ql)
                    if (want_pre and i > 0 and text[i - 1].isalnum()) or (
                        want_post and j < len(text) and text[j].isalnum()
                    ):
                        i = low.find(ql, i + 1)
                        continue
                    section = ""
                    for start, title in headings:
                        if start <= i:
                            section = title
                        else:
                            break
                    results.append({
                        "doc": name,
                        "n": n,
                        "section": section,
                        "before": re.sub(r"\s+", " ", text[max(0, i - CTX):i]).lstrip(),
                        "match": re.sub(r"\s+", " ", text[i:i + len(q)]),
                        "after": re.sub(r"\s+", " ", text[i + len(q):i + len(q) + CTX]).rstrip(),
                    })
                    shown += 1
                    n += 1
                    i = low.find(ql, i + 1)
            self.send_json({"q": q, "results": results})

        def api_get_review(self, name):
            if not safe_doc(name):
                return self.send_error(400)
            data = load_review(reviews_dir, name)
            rev, dirty, subject, date, err = git_doc_state(*resolve(name))
            data["docNow"] = {"rev": rev, "dirty": dirty, "subject": subject,
                              "date": date, "error": err}
            self.send_json(data)

        def api_save_review(self, name):
            if not safe_doc(name):
                return self.send_error(400)
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
                annotations = data["annotations"]
                assert isinstance(annotations, list)
                complete = bool(data.get("complete"))
            except (ValueError, KeyError, AssertionError):
                return self.send_json({"ok": False, "error": "bad payload"}, 400)

            rev, dirty, subject, date, git_err = git_doc_state(*resolve(name))
            stamp = (rev + ("-dirty" if dirty else "")) if rev else None
            stamps = {}
            for a in annotations:
                if isinstance(a, dict) and stamp and not a.get("docRev"):
                    a["docRev"] = stamp
                    stamps[a.get("id")] = stamp
            revs = sorted({a.get("docRev") for a in annotations
                           if isinstance(a, dict) and a.get("docRev")})

            path = review_path(reviews_dir, name)
            if complete:
                # Resolved annotations may carry -dirty or older stamps: reviews
                # addressed in rounds legitimately accrete annotations against
                # successive revisions. Completion certifies the document's
                # revision now; each annotation keeps the stamp of what the
                # reviewer saw when it was made.
                err = None
                open_dirty = [a for a in annotations
                              if isinstance(a, dict) and not a.get("resolved")
                              and (a.get("docRev") or "").endswith("-dirty")]
                if git_err:
                    err = git_err
                elif dirty:
                    err = (f"{name} has uncommitted changes — commit the document "
                           "before completing the review")
                elif open_dirty:
                    err = ("some unresolved annotations were made while the "
                           "document had uncommitted changes (revision marked "
                           "-dirty); resolve them, or delete and re-add them "
                           "against a committed document")
                if err:
                    return self.send_json({"ok": False, "error": err}, 409)
                status, completed_at = "complete", now_iso()
                doc_rev = rev
            else:
                status, completed_at = "in-progress", None
                doc_rev = revs[0] if len(revs) == 1 else None
                if not annotations:
                    path.unlink(missing_ok=True)
                    return self.send_json({"ok": True, "deleted": True,
                                           "status": status})

            updated = now_iso()
            out = {"version": 1, "doc": name, "updated": updated, "status": status,
                   "docRev": doc_rev, "completedAt": completed_at,
                   "annotations": annotations}
            reviews_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(render_review_html(name, out), encoding="utf-8")
            self.send_json({"ok": True, "file": str(path), "updated": updated,
                            "status": status, "docRev": doc_rev, "stamps": stamps,
                            "docNow": {"rev": rev, "dirty": dirty,
                                       "subject": subject, "date": date}})

    return Handler


def main():
    ap = argparse.ArgumentParser(description="reqrev — local requirement review annotator")
    ap.add_argument("--docs", default=str((APP_DIR / "../srts/requirements").resolve()),
                    help="directory containing requirement .html files")
    ap.add_argument("--reviews", default=None,
                    help="directory where .review.html files are written "
                         "(default: <docs parent>/reviews)")
    ap.add_argument("--stories", default=None,
                    help="directory containing story .html files, listed in their "
                         "own index section (default: <docs parent>/stories; "
                         "skipped if absent)")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    docs_dir = Path(args.docs).resolve()
    if not docs_dir.is_dir():
        sys.exit(f"docs directory not found: {docs_dir}")
    reviews_dir = (
        Path(args.reviews).resolve() if args.reviews else docs_dir.parent / "reviews"
    )
    stories_dir = (
        Path(args.stories).resolve() if args.stories else docs_dir.parent / "stories"
    )
    if not stories_dir.is_dir():
        stories_dir = None

    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(docs_dir, stories_dir, reviews_dir)
    )
    print(f"reqrev serving {docs_dir}")
    if stories_dir:
        print(f"stories  -> {stories_dir}")
    print(f"reviews  -> {reviews_dir}")
    print(f"open     http://localhost:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
