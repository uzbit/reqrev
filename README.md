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

Defaults: serves docs from `../srts/requirements`, writes reviews to a
`reviews/` directory next to the docs directory (i.e. `../srts/reviews`).
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
| `created` / `updated` | ISO-8601 UTC timestamps |

**For Claude:** to act on a review, parse the JSON block from the
`.review.html` file. Each annotation's `exact` text locates the passage in the
corresponding source document under the docs directory; apply `replacement`
text for suggested edits, and answer or address `comment`/`question` items.
Annotations are anchored by text content, so they survive edits to the source
document; if a quoted passage disappears entirely, the app flags it as
orphaned rather than deleting it.
