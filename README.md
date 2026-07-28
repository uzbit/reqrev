# reqrev

A tiny local tool for reviewing requirement documents (HTML). Open a doc in the
browser, select any passage, and attach a **comment**, **suggested edit**, or
**question**. Reviews autosave to self-contained `.review.html` files that are
readable by humans, by Claude, and by this app.

No dependencies — Python 3 standard library only.

## Run

```sh
python3 server.py
# then open http://localhost:8765/
```

Defaults: serves docs from `../srts/requirements` (scanned recursively — doc
names include their subpath, e.g. `modules/0002-identity-access.html`), writes
reviews to a `reviews/` directory next to the docs directory (i.e.
`../srts/reviews`). Review files stay flat, named by document stem, so
document stems must be unique across the tree.
Override with:

```sh
python3 server.py --docs /path/to/docs --reviews /path/to/reviews --port 8765
```

## Workflow

1. Pick a document on the index page.
2. Select text in the document → click the **✎ Comment** button that appears.
3. Choose a type (Comment / Suggested edit / Question), write your note —
   suggested edits also take proposed replacement text — and Save.
4. Everything autosaves to `reviews/<doc>.review.html`. Highlights and the
   sidebar restore when you reopen the doc. Resolve/reopen/delete from the
   sidebar cards; click a card to jump to its passage and vice versa.
5. When done, click **✓ Review complete** — this records the document's git
   revision at completion time in the review file. Completion is refused if
   the document currently has uncommitted changes, or if any *unresolved*
   annotation was made while the document had uncommitted changes (its stamp
   carries a `-dirty` suffix). Resolved annotations may span document
   revisions — normal for reviews addressed in rounds; each keeps the stamp
   of what the reviewer saw when it was made. Editing the review afterwards
   flips it back to in-progress.
6. Hand the completed review to whoever owns the document — human or Claude.
   They address each item (apply suggested edits, answer questions), record
   `disposition`, `response`, and `changes` on the annotation, and set
   `resolved: true`. The rendered report shows these as response blocks
   beneath each reviewer note.

> **Tip:** commit the requirement docs *before* starting a review. Annotations
> made while a doc has uncommitted changes are stamped `-dirty` and will block
> **✓ Review complete** until they are deleted and re-added against a
> committed revision.

## Review file format

`reviews/<doc>.review.html` is a single HTML file containing:

- a rendered, human-readable review report (open it directly in a browser), and
- the authoritative machine-readable data in
  `<script type="application/json" id="reqrev-data">…</script>`.

Annotation fields:

| field | meaning |
|---|---|
| `exact` | the passage of the source document being reviewed (verbatim quote) |
| `prefix` / `suffix` | ~32 chars of surrounding context used to relocate the quote |
| `start` | character offset of the quote in the document's flattened text |
| `section` / `sectionTitle` | id and title of the enclosing section heading |
| `type` | `comment`, `edit` (suggested edit), or `question` |
| `comment` | the reviewer's note |
| `replacement` | for `type: "edit"`, the proposed replacement text |
| `resolved` | whether the item has been addressed |
| `disposition` | optional — how it was addressed: `applied`, `answered`, or `orphaned` |
| `response` | optional — the author's answer or rationale |
| `changes` | optional — what was edited in the document as a result |
| `created` / `updated` | ISO-8601 UTC timestamps |
| `docRev` | git revision of the source doc the annotation was made against (`-dirty` suffix = uncommitted changes were present) |

Top-level fields: `status` (`in-progress` or `complete`), `docRev` (the
document's committed revision at completion), and `completedAt`. Each
annotation's own `docRev` records what the reviewer saw when that annotation
was made — check out that revision to reproduce it exactly.

Note that `docRev` is the last commit that **touched the document**, not the
repo HEAD at review time. Commits elsewhere in the repo (other docs, review
files, tooling) don't change a document's revision, so they can't split an
in-progress review across "revisions" or block completion. The sidebar shows
the commit's subject line next to the hash so you can recognize it, and warns
if the document gains uncommitted changes or new commits mid-review.

**For Claude:** to act on a review, parse the JSON block from the
`.review.html` file. Each annotation's `exact` text locates the passage in the
corresponding source document under the docs directory; apply `replacement`
text for suggested edits, and answer or address `comment`/`question` items.
When addressing items, record `disposition`, `response`, and `changes` on each
annotation and set `resolved: true` — the rendered report shows them as
response blocks beneath the reviewer's note.
Annotations are anchored by text content, so they survive edits to the source
document; if a quoted passage disappears entirely, the app flags it as
orphaned rather than deleting it.
