# Extraction datasets

What the system reads out of an intake is **configuration**. A dataset is a named
set of fields; a field carries a description written the way a *document* phrases
the thing; and that description is used verbatim as the retrieval query. Adding a
field is a row in a table, not a deployment.

This is the layer above [document-intelligence.md](document-intelligence.md),
which turns a stored file into searchable, citable passages. That layer knows
documents and passages. This one knows questions and answers. Neither knows what
a claim is — that translation is one module, and it is named below.

---

## What one run does

```
notification body ─┐
email attachment   ├─→ FNOLDocument  →  read → chunk → embed → Qdrant
manual upload     ─┘        (all three through one path)
                                        │
       per field: retrieve with the field's own description as the query
                  (query vector cached on the field row)
                                        │
       fields batched — a batch's passages are the union of its fields', deduped
                                        │
              one model call per batch, passages labelled [C1], [C2]…
                                        │
       value + confidence + quote + label  →  extracted_values, one row per field
                                        │
              label → passage → document, page, offsets, rectangles
```

The notification body is a document like any other. That is the single change
that makes a value read out of the broker's own email citable: before it, the
body was a text column pasted into the prompt, and no value from it could ever
point anywhere.

---

## The three shapes

### A dataset

`extraction_schemas` — `key`, `name`, `version`, `status`, `review_threshold`,
`is_default`. Exactly one dataset is the default, enforced by a partial unique
index rather than by application code.

`FNOL notice` ships with the product, is the default, and is seeded on boot from
`app/data/extraction_schemas/fnol_notice.json`. Seeding is **additive**: it
creates what is missing and adds fields a release introduces, and never
overwrites a description an administrator has rewritten.

### A field

`extraction_schema_fields` — and the column that matters is `description`.

> **The description is the retrieval query.** It is the highest-leverage column
> in the module. `policy_number` retrieves nothing; *"The policy, certificate or
> contract number the risk is written under, usually printed near the top of a
> schedule, slip or certificate of insurance"* retrieves the block that contains
> it. A dataset author's time is better spent here than anywhere else.

| Column | |
|---|---|
| `key` | The dotted address the value is stored and served under. Never renamed — a run points at it. |
| `label` | What a reviewer sees. Leads the retrieval query, because a document often uses the schema's own word. |
| `description` | The retrieval query. Prose, in a document's vocabulary. |
| `data_type` | `string · text · integer · number · money · date · datetime · boolean · json` |
| `group_label` | The panel the field is drawn in. Free text; a new dataset invents its own. |
| `aliases` | Other names a document might use. Appended to the query and shown to the model. |
| `extraction_hint` | A rule for the model about this field only. Kept out of `description`, because a rule makes a poor search query. |
| `required` | Missing ⇒ flagged for review. |
| `enabled` | Off ⇒ the question is not asked at all. The supported way to stop asking one. |

### A value

`extracted_values` — one row per `(case, dataset, field)`, updated in place, with
everything a viewer needs to draw a highlight: the document, the passage, the
page, the offsets, the quote, and the rectangles once they have been resolved.

`value_text` is always what the document said. `value_json` is the coerced form
for a type that has one. **A value that will not coerce is stored and flagged,
never discarded** — a reviewer who can see `13/45/2026` can correct it, and one
shown an empty box cannot.

---

## Configuring one

```bash
# What this desk can read a notice against
curl .../api/v1/extraction/schemas

# A new dataset
curl -X POST .../api/v1/extraction/schemas -d '{
  "key": "marine_slip",
  "name": "Marine slip",
  "fields": [{
    "key": "vessel.imo_number",
    "label": "IMO number",
    "description": "The seven-digit IMO identification number of the vessel, printed on the slip beside the vessel name and unchanged for the life of the hull.",
    "data_type": "string",
    "group_label": "Vessel",
    "aliases": ["IMO", "hull number"]
  }]
}'

# Save the whole field list, in the order it should be asked
curl -X PUT .../api/v1/extraction/schemas/marine_slip/fields -d '{"fields": [...]}'

# Make it the one the pipeline runs
curl -X PATCH .../api/v1/extraction/schemas/marine_slip -d '{"is_default": true}'
```

Saving fields **bumps the dataset's version**, which moves every open notice's
extraction fingerprint and re-reads them on their next run. That is the intended
cost of changing the questions: a value extracted against a question nobody asks
any more is a value nobody can defend.

---

## Running one

Ordinarily nothing: an upload or a collected email leaves the notice `queued`,
and the worker runs the default dataset as part of the pipeline.

```bash
# Read this notice now, and re-run the pipeline behind it
curl -X POST .../api/v1/fnol/FNOL-2026-000123/extraction -d '{"force": true}'

# The dataset, the last run and the values — what the review screen reads
curl .../api/v1/fnol/FNOL-2026-000123/extraction

# Where did this value come from
curl .../api/v1/fnol/FNOL-2026-000123/extraction/values/policy.policy_number/evidence

# Every other place that text appears, for stepping through occurrences
curl -X POST .../api/v1/fnol/FNOL-2026-000123/documents/{id}/locate -d '{"text": "GBP 128,000"}'

# An officer's correction
curl -X PATCH .../api/v1/fnol/FNOL-2026-000123/extraction/values/policy.policy_number \
  -d '{"value": "CP-2026-4471", "reason": "The slip says otherwise."}'
```

---

## Cost, and how it is bounded

Retrieval per field is where the accuracy is. Extraction per field is where the
cost would be, and it is avoidable — so fields are **batched**, and a batch's
prompt carries the deduplicated union of its fields' passages.

A thirty-field dataset therefore costs thirty cheap searches and three model
calls, not thirty of each. Two other economies matter at volume:

* **Query vectors are cached on the field row.** A field's query is fixed until
  somebody edits it, so it is embedded once rather than on every run of every
  notice. The cache key includes the query text and the embedding model, so an
  edit or a model change invalidates it with nobody having to remember.
* **An unchanged notice costs one query.** The run fingerprint covers the notice
  body, the document set, and the dataset's *questions* — not just its version
  number.

---

## Configuration

Every setting is `CWB_EXTRACT_*`. The defaults are what runs with nothing set.

| Variable | Default | |
|---|---|---|
| `CWB_EXTRACT_SCHEMA_DRIVEN` | `true` | Off runs the fixed FNOL schema in `app/domain/extraction.py`, exactly as before this module existed |
| `CWB_EXTRACT_SEED_ON_STARTUP` | `true` | Create the bundled datasets if missing. Additive; never overwrites an edit |
| `CWB_EXTRACT_FIELDS_PER_CALL` | `10` | The setting to raise first for a much larger dataset |
| `CWB_EXTRACT_PASSAGES_PER_FIELD` | `4` | Retrieved per field, before deduplication across a batch |
| `CWB_EXTRACT_MAX_PASSAGES_PER_CALL` | `30` | Ceiling per call, after deduplication |
| `CWB_EXTRACT_BODY_CHUNKS_ALWAYS_INCLUDED` | `8` | Passages of the notification body in *every* call, whatever retrieval thought |
| `CWB_EXTRACT_CALL_CONCURRENCY` | `3` | Concurrent model calls |
| `CWB_EXTRACT_MAX_PASSAGE_CHARACTERS` | `60000` | How much of one prompt may be passages |
| `CWB_EXTRACT_REVIEW_THRESHOLD` | `0.6` | Fallback for a dataset that does not set its own |
| `CWB_EXTRACT_LLM_CONFIDENCE_WEIGHT` | `0.7` | The rest goes to the retrieval score |
| `CWB_EXTRACT_CACHE_HIGHLIGHT_RECTS` | `true` | Cache resolved rectangles on the value row |

---

## Degradation

| Unconfigured or unavailable | What happens |
|---|---|
| **No `CWB_AI_API_KEY`** | The dataset path stands aside entirely and the deterministic reader in `app/domain/heuristics.py` runs, exactly as before. A dataset is an arbitrary set of questions and needs a model; a regex reader cannot stand in for one, and returning an empty form would be honest and useless. |
| No embedding provider | Retrieval runs on Postgres full text alone. Passages are still written, still searchable, still citable. |
| No Qdrant | The same. `strategy: keyword` is reported on the run. |
| No default dataset | The fixed FNOL schema runs. A desk that has not configured one is not broken. |
| One batch's model call fails | Its fields have no answer; every other field is extracted; the run is `partial` and says so. |
| A document cannot be read | It is one failed document, counted as unreadable, feeding the exception an officer already sees. |
| `CWB_EXTRACT_SCHEMA_DRIVEN=false` | The pre-existing path, unchanged and still tested. |

---

## The frontend contract

> What consumes it: **extraction review**, the first stage of `/intake/:reference`.
> See [extraction-review.md](extraction-review.md) for the screen, the demo that
> exercises this end to end, and what happens when a provider is missing.

### `GET /fnol/{reference}/extraction`

Carries **the dataset as well as the values**, deliberately: a client rendering a
configurable field set needs the order, the grouping and the labels, and fetching
those separately is how a panel of values ends up under the wrong headings for
the half-second between two responses.

```jsonc
{
  "reference": "FNOL-2026-000123",
  "dataset": {
    "key": "fnol_notice", "name": "FNOL notice", "version": 3,
    "groups": ["Notification", "Policy", "Loss", "Financial", "Parties", "Additional"],
    "fields": [ { "key": "policy.policy_number", "label": "Policy number",
                  "data_type": "string", "group_label": "Policy", "required": true, … } ]
  },
  "run": {
    "status": "completed",            // completed | partial | failed | skipped
    "fields_total": 30, "fields_extracted": 22, "fields_needing_review": 4,
    "retrieval_strategy": "hybrid-rrf", "degraded": false, "llm_calls": 3
  },
  "values": [{
    "field_key": "policy.policy_number",
    "label": "Policy number", "group_label": "Policy", "data_type": "string",
    "value": "CP-2026-4471",
    "typed_value": null,              // the coerced form, for a type that has one
    "confidence": 0.93,
    "needs_review": false,
    "validation_error": null,         // set ⇒ show the value *and* the problem
    "required": true,
    "human_modified": false,
    "source_chunk_id": "…",           // non-null ⇒ offer "show me where"
    "source_document_filename": "survey-report.pdf",
    "page_number": 4,
    "quote": "Policy number: CP-2026-4471"
  }]
}
```

### `GET …/extraction/values/{field_key}/evidence`

```jsonc
{
  "field_key": "policy.policy_number",
  "value": "CP-2026-4471",
  "filename": "survey-report.pdf",
  "content_type": "application/pdf",
  "document_source": "email_attachment",   // or email_body | upload
  "page_number": 4,
  "page_count": 12,
  "text": "Policy number: CP-2026-4471",
  "char_start": 1284, "char_end": 1311,
  "rects": [{ "page_number": 4, "x0": 60.0, "top": 32.4, "x1": 220.7, "bottom": 44.4,
              "page_width": 595.0, "page_height": 842.0 }],
  "strategy": "chunk-grounded",
  "note": null
}
```

`strategy` is the field a viewer should branch on:

| | |
|---|---|
| `chunk-grounded` | Located inside the passage the model actually read. The accurate answer, and the one to prefer. |
| `document-search` | The passage was replaced by a re-index, so the value's own text was searched for instead. Useful; may be a different occurrence. |
| `text-only` | No page geometry — a Word file, a spreadsheet, the notification body. Highlight the quoted text in your own render. |
| `none` | Nothing to point at: a value an officer typed. `note` says which. |

`rects` are **PDF points from the top-left**, and `page_width`/`page_height`
travel with every rectangle so a viewer can scale to whatever size it renders at
without a second request. One rectangle **per line** of the quote: one box round
a two-line quote would cover the text between the lines, which the quote does not
contain.

### `POST …/documents/{id}/locate`

Every occurrence of a piece of text, each with its page, offsets, snippet and
rectangles. What a reviewer needs *after* the evidence endpoint has answered —
stepping between matches to check that the cited one is the one that matters —
and the only answer available for a value with no citation.

---

## Where the seams are

| Module | Knows about |
|---|---|
| `app/services/intelligence/` | Documents, passages, vectors. No claims, no datasets. |
| `app/services/extraction/` | Datasets, fields, confidence, evidence. **No claims.** |
| `app/services/fnol/adapter.py` | **Both.** The one module that maps a dataset's values onto `fnol_cases` and `fnol_extracted_fields`. |
| `app/services/fnol/body.py` | Writing the notification body out as a document. |

The adapter's mapping is a **table**, not a function. A field the dataset carries
and the table does not is simply not mirrored onto the claim record — it is still
extracted, stored, cited and shown, which is the correct outcome for a question
somebody added last Tuesday.

---

## What is deliberately not built

Named because each is a real improvement, and because none changes an interface.

- **Table datasets.** A marine SOV is hundreds of location *rows*, and
  retrieval-per-field cannot extract them — that needs sheet-aware direct table
  parsing rather than RAG. The `json` data type covers a small list today (it is
  how `parties.people` works); a schedule of five hundred locations is separate
  work.
- **Reranking, MMR, query rewriting.** `RetrievalService.search` returns scored
  ordered hits, so a reranker is a decorator over it.
- **Multiple datasets per run.** The engine takes one dataset; running two is a
  loop in the pipeline and a second row in `extraction_runs`.
- **An evaluation set.** The gap that matters most, inherited from the layer
  below. Per-field retrieval should improve recall over shared section queries —
  but "should" is not a measurement. Five to ten real notices with their expected
  field values, run as an integration test, is the highest-value next piece of
  work in this area.
