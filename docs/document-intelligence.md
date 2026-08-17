# Document intelligence

How a claim document becomes fields on a notice, and how a field points back at the
page it was read from.

This is the phase [mail-intake.md](mail-intake.md) stops before, and the phase
[extraction-datasets.md](extraction-datasets.md) builds on: that layer decides
*which fields* to read, and reads them out of the passages this one produces. Mailbox intake collects
a message, stores its attachments and leaves the notice at
`processing_state = queued`. Everything below consumes that.

---

## What one run does

```
attachment stored  (mail intake, or a browser upload)
        │
        ▼
read       per-page text.  PDF: pypdfium2 text layer + pdfplumber tables.
           .docx: python-docx in body order.  .xlsx: openpyxl, one page per sheet.
           .eml / .msg: envelope, body, and attachments one level deep.
        │
        ▼
chunk      passages of ~320 tokens, cut inside a page or section, never across one.
           Each carries its page, its section label, and its exact character offsets.
        │
        ▼
embed      one vector per passage  →  Qdrant          (skipped when unconfigured)
        │
        ▼
retrieve   per field group: "date and time of loss", "policy number", …
           hybrid — Qdrant + Postgres full text, fused by reciprocal rank
        │
        ▼
extract    one LLM call over the retrieved passages, each labelled `[C1]`, `[C2]`…
           The model returns each value with the label it read it from.
        │
        ▼
cite       label → passage → `fnol_extracted_fields.source_chunk_id`
        │
        ▼
highlight  offsets → page → rectangles, drawn over the page in the case file
```

Stages 2 to 4 are `POST /fnol/{reference}/index`. Stages 5 to 7 are the existing
pipeline. The worker does both, in that order, from one task.

## The one invariant

    chunk.content == document.extracted_text[chunk.char_start : chunk.char_end]

Everything a claims officer sees rests on it. The evidence endpoint takes a passage's
offsets, finds which page they fall in, and locates the text on that page — so if a
passage's content were ever assembled rather than sliced, the officer would be shown
the wrong part of the wrong page, confidently and with no error anywhere.

It is why passage overlap is implemented by moving a passage's *start* backwards rather
than by prepending the previous passage's tail, and it is asserted in most of
`tests/unit/test_document_chunking.py`.

---

## Nothing here is required

Three capabilities, each independently optional, each degrading rather than failing.
This is the same posture `app/services/ai/factory.py` takes toward the LLM, and it is
what keeps a demo environment, an air-gapped deployment and a provider outage working.

| Unconfigured or unavailable | What happens |
|---|---|
| No embedding key | Passages are still written and still keyword-searchable. `index_status = skipped`. |
| No `CWB_DOCINT_QDRANT_URL` | Same. Retrieval runs on Postgres full text alone (`strategy: keyword`). |
| Qdrant up at boot, down at search | Keyword results still return; `degraded: true` is reported on the response and recorded in the analysis. |
| Qdrant down at index time | That document alone lands `index_status = failed`; it is retried, then counted as unreadable. |
| Fewer than `retrieval_min_chunks` passages | Retrieval is skipped and the whole corpus is sent — selecting six passages out of eight is overhead with a downside and no upside. |
| No `CWB_DOCINT_OCR_URL` | A scan stays `unsupported` with a sentence naming OCR, and raises the existing `document_unreadable` exception. |
| No `CWB_AI_API_KEY` | The deterministic reader in `app/domain/heuristics.py` runs, exactly as before. |
| `CWB_DOCINT_ENABLED=false` | No chunking, no indexing, no retrieval. The pipeline behaves as it did before this module existed. |

Three rules hold throughout:

- **The pipeline never gains a failure mode.** Indexing returns an outcome; it does not
  raise. The contract in `app/services/fnol/pipeline.py` — *"an unreachable model
  provider, an unreadable PDF or a policy repository returning nothing are all normal
  states of a claims desk"* — now covers an unreachable vector store too.
- **No new exception code.** "Qdrant is down" is an infrastructure fact, not a claims
  fact, and putting it on an officer's exception list teaches them to ignore the list.
  An unreadable document *is* a claims fact and already has `document_unreadable`.
- **Retrieval narrows the prompt; it never gates it.** The notification body is always
  sent first and always whole.

---

## Configuration

Every setting is `CWB_DOCINT_*`. The defaults below are what runs with nothing set.

### Chunking — always on, free, deterministic

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_ENABLED` | `true` | Master switch for the whole module |
| `CWB_DOCINT_CHUNK_TOKENS` | `320` | Large enough that a form's label and value stay together |
| `CWB_DOCINT_CHUNK_OVERLAP_TOKENS` | `48` | So a value split across a boundary is whole in one of the two |
| `CWB_DOCINT_MAX_CHUNKS_PER_DOCUMENT` | `2000` | A 400-page schedule truncates with a log line |

Token counts are **estimates** (≈4 characters each), not a tokeniser's output. `tiktoken`
was considered and rejected: it fetches its vocabulary from a CDN on first use, so an
air-gapped worker would fail on its first document at whatever hour that happened — a
real operational risk taken on to compute a number that only feeds a size heuristic.
The prompt budget in `CWB_AI_MAX_INPUT_CHARACTERS` is already denominated in characters.

### Embeddings — optional

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_EMBEDDING_MODEL` | `text-embedding-3-small` | |
| `CWB_DOCINT_EMBEDDING_DIMENSION` | `1536` | Verified against the provider's first response; a mismatch is an error, not a silently wrong index |
| `CWB_DOCINT_EMBEDDING_API_KEY` | falls back to `CWB_AI_API_KEY` | One key configures both; separable when a deployment routes them differently |
| `CWB_DOCINT_EMBEDDING_BASE_URL` | falls back to `CWB_AI_BASE_URL` | Any OpenAI-compatible endpoint |
| `CWB_DOCINT_EMBEDDING_BATCH_SIZE` | `64` | |
| `CWB_DOCINT_EMBEDDING_TIMEOUT_SECONDS` | `30.0` | |
| `CWB_DOCINT_EMBEDDING_MAX_ATTEMPTS` | `3` | Applied by the shared `HttpClient` retry policy |

### Vector store — optional

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_QDRANT_URL` | *unset* | Unset means keyword-only retrieval |
| `CWB_DOCINT_QDRANT_API_KEY` | *unset* | |
| `CWB_DOCINT_QDRANT_COLLECTION` | `fnol_document_chunks` | |
| `CWB_DOCINT_QDRANT_TIMEOUT_SECONDS` | `15.0` | |

### Retrieval

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_RETRIEVAL_ENABLED` | `true` | |
| `CWB_DOCINT_RETRIEVAL_TOP_K` | `6` | Passages per field group |
| `CWB_DOCINT_RETRIEVAL_MIN_SCORE` | `0.25` | A floor on the normalised fused score |
| `CWB_DOCINT_RETRIEVAL_SEMANTIC_WEIGHT` | `0.7` | |
| `CWB_DOCINT_RETRIEVAL_KEYWORD_WEIGHT` | `0.3` | |
| `CWB_DOCINT_RETRIEVAL_MIN_CHUNKS` | `12` | Below this the whole corpus is sent instead |
| `CWB_DOCINT_MAX_EVIDENCE_CHUNKS` | `24` | Ceiling across all field groups in one prompt |
| `CWB_DOCINT_RETRIEVAL_CONCURRENCY` | `4` | Field-group searches run concurrently, bounded |

### OCR — optional, auto-detected

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_OCR_URL` | *unset* | Unset means a scan is reported, not read |
| `CWB_DOCINT_OCR_TIMEOUT_SECONDS` | `180.0` | |
| `CWB_DOCINT_OCR_LANGUAGE` | `en` | |
| `CWB_DOCINT_OCR_DPI` | `300` | |
| `CWB_DOCINT_OCR_MAX_PAGES` | `50` | Cost ceiling per document |
| `CWB_DOCINT_OCR_MIN_CONFIDENCE` | `0.40` | |
| `CWB_DOCINT_OCR_MIN_CHARS_PER_PAGE` | `50` | Detector signal 1 |
| `CWB_DOCINT_OCR_EMPTY_PAGE_RATIO` | `0.5` | Detector signal 2 |
| `CWB_DOCINT_OCR_IMAGE_ONLY_RATIO` | `0.5` | Detector signal 3 |

### Orchestration

| Variable | Default | |
|---|---|---|
| `CWB_DOCINT_INDEX_MAX_ATTEMPTS` | `3` | Per document, before it is left for a human |
| `CWB_DOCINT_STALE_INDEX_MINUTES` | `30` | After which a document stuck mid-index is requeued |
| `CWB_DOCINT_QUEUE_POLL_ENABLED` | `true` | Registers the beat entry that consumes queued notices |
| `CWB_DOCINT_QUEUE_POLL_INTERVAL_SECONDS` | `60` | |
| `CWB_DOCINT_QUEUE_BATCH_SIZE` | `20` | Notices claimed per tick |

---

## Requirements

**Postgres** — migration `0004_document_intelligence` adds `fnol_document_chunks`, the
indexing columns on `fnol_documents`, and `source_chunk_id` on `fnol_extracted_fields`.
`content_tsv` is a `GENERATED ALWAYS` tsvector with a GIN index over it; that is the
keyword half of retrieval, and it is why the feature degrades to something real rather
than to nothing.

> **One-time cost on deploy.** `extraction_signature` starts NULL, so every existing
> document is re-read once and every case's stored extraction fingerprint is invalidated
> once — one free re-extraction per open case. That is correct rather than wasteful: the
> readers introduced alongside this migration genuinely produce different text than the
> ones they replaced. It should not be a surprise on the bill.

**Qdrant** — a service in `docker-compose.yml`, pinned by digest and **kept in step with
`qdrant-client` in `pyproject.toml`**. The client refuses a server whose major version
differs or whose minor differs by more than one, and it says so as a *warning* rather
than an error, so a drifting pair degrades quietly. Update both together.

Qdrant's on-disk storage is also not forward-compatible across large version jumps. That
is survivable here by design: Postgres owns the passages, so the recovery is to discard
the volume and re-index rather than to migrate anything.

```bash
docker compose up -d postgres redis minio qdrant
make migrate
```

---

## Running it

Indexing happens on its own. An upload or a collected email leaves the notice
`queued`, and the beat task picks it up within `queue_poll_interval_seconds`.

```bash
make worker      # index_document, index_case_documents, process_case
make beat        # process_queued_cases, reap_stale_indexing
```

To drive it by hand:

```bash
# collect real mail, then watch the notice move: queued → processing → completed
python -m app.services.mail --limit 5
curl .../api/v1/fnol/FNOL-2026-000123

# index now, synchronously
curl -X POST .../api/v1/fnol/FNOL-2026-000123/index -d '{"force": false}'

# what the reader actually got out of a file
curl '.../api/v1/fnol/FNOL-2026-000123/documents/{document_id}/chunks?page=1'

# search this notice's documents
curl '.../api/v1/fnol/FNOL-2026-000123/search?q=policy+number'

# where did this value come from
curl '.../api/v1/fnol/FNOL-2026-000123/fields/policy.policy_number/evidence'
```

### Retrying a failure

Failures are recorded on the row, never only in a log.

| Symptom | Where to look | Fix |
|---|---|---|
| A document produced no fields | `index_status`, `index_error`, `extraction_status`, `extraction_error` on the document | |
| `index_status = failed` | `index_error` names the stage | `POST /{reference}/index {"force": true}` |
| `index_status = skipped` | `index_error` says why — usually no embedding provider, or no text | Configure a key, or accept keyword-only |
| `index_status = indexing`, stuck | The worker died | `reap_stale_indexing` requeues it hourly |
| `chunk_count > embedded_chunk_count` | The vector index is behind the passages | `force: true` re-embeds from Postgres; no document is re-read |
| `processing_state = queued`, not moving | Beat is not running, or `queue_poll_enabled` is off | `make beat` |
| A field has a value but no evidence | `source_chunk_id` is null | The value came from the email body, was typed by an officer, or its passage was replaced by a re-index |

Re-running is always safe. An unchanged document costs one `SELECT`: no re-read, no
embedding call, no vector write, no row written.

---

## How a citation survives a re-index

Re-indexing replaces a document's passages — a re-chunked passage 7 is different text at
a different offset, so updating in place would leave a citation pointing at prose the
model never read.

`source_chunk_id` is therefore `ON DELETE SET NULL`, not `CASCADE`. A re-index costs the
citation and keeps the **value**: an officer would rather read "CP-2026-4471, source no
longer available" than find the field empty. In practice the citation comes straight
back, because the index fingerprint feeds the extraction fingerprint, so a re-index
always causes a re-extraction.

---

## The frontend contract

> The screen that consumes this is **extraction review**, described in
> [extraction-review.md](extraction-review.md). It reads the dataset layer's
> endpoints rather than these directly — the citations are the same, and the
> dataset carries the grouping, the confidence and the location strategy as well.
> The endpoints below remain the fixed-schema route to the same data.

### `GET /fnol/{reference}` — the case file

`documents[]` gains, per file:

```jsonc
{
  "filename": "survey-report.pdf",
  "extraction_status": "extracted",     // could the text be read
  "text_extractor": "pdf_text_layer",   // how it was read
  "ocr_applied": false,
  "ocr_confidence": null,
  "ocr_reason": null,                   // "avg chars/page 12.4 below threshold 50"
  "index_status": "indexed",            // pending|indexing|indexed|skipped|failed
  "chunk_count": 34,
  "embedded_chunk_count": 34,           // below chunk_count means the index is behind
  "index_error": null,
  "indexed_at": "2026-08-12T18:04:11Z"
}
```

`fields[]` gains, per value:

```jsonc
{
  "path": "policy.policy_number",
  "value": "CP-2026-4471",
  "confidence": 0.95,
  "source_chunk_id": "…",               // non-null ⇒ offer "show me where"
  "source_document_filename": "survey-report.pdf",
  "source_page_number": 4
}
```

`source_chunk_id` being non-null is the whole signal: it means the evidence endpoint has
something to show for this field.

### `GET /fnol/{reference}/fields/{field_path}/evidence` — the click-through

```jsonc
{
  "path": "policy.policy_number",
  "value": "CP-2026-4471",
  "filename": "survey-report.pdf",
  "content_type": "application/pdf",
  "page_number": 4,
  "section_label": null,                // a sheet name or heading, when there is no page
  "text": "Policy number: CP-2026-4471",
  "char_start": 1284,                   // into the document's stored text
  "char_end": 1311,
  "rects": [
    { "page_number": 4, "x0": 60.0, "top": 32.4, "x1": 220.7, "bottom": 44.4,
      "page_width": 595.0, "page_height": 842.0 }
  ],
  "note": null
}
```

Three levels of precision, and the response says which it reached:

1. **Document and page** — always, when a passage is cited.
2. **Exact text** — always. The model's quoted evidence when it can be located inside
   the passage, otherwise the passage itself, capped at 600 characters. Preferring the
   quote matters: highlighting a whole 320-token passage to show where one policy number
   came from is a wall of colour that answers nothing.
3. **Rectangles** — for PDFs whose words could be located.

`rects` are in **PDF points from the top-left**, and `page_width`/`page_height` travel
with each one so the viewer can scale to whatever size it renders at without a second
request. One rectangle **per line** of the quote: one box round a two-line quote would
cover the text between the lines, which the quote does not contain.

`rects: []` with a `note` is a normal answer, not an error — a Word document has no page
geometry, and a quote may not be locatable. The page and the text are still useful, and
a viewer can search its own text layer.

**Why matching is by word sequence rather than by character offset:** the page text comes
from PDFium and the word geometry from `pdfplumber`/`pdfminer.six`. Two engines reading
one page agree on the words and their order but *not* on where spaces and line breaks
belong, so an offset cannot be handed from one to the other. Words can.

### `GET /fnol/{reference}/search`

```jsonc
{
  "items": [
    { "chunk_ref": "…:00007", "chunk_id": "…", "document_id": "…",
      "filename": "survey-report.pdf", "page_number": 4, "section_label": null,
      "snippet": "Policy number: CP-2026-4471 …",
      "score": 0.81, "semantic_score": 0.79, "keyword_score": 0.44 }
  ],
  "strategy": "hybrid-rrf",             // or semantic | keyword | none
  "degraded": false,
  "chunks_searched": 214
}
```

`strategy` is worth surfacing: `keyword` means the vector index was unavailable or
unconfigured, which explains a thin result set without anyone reading a log.

### Changed behaviour: uploads are now asynchronous

`POST /fnol/{reference}/documents` **no longer runs the pipeline inline.** It stores the
file, leaves the notice `queued`, enqueues the work and returns. Reading a document now
means opening it, possibly sending it to OCR, chunking it and embedding it — seconds to
minutes for a large scan, inside an HTTP request a proxy will give up on first.

The frontend should poll `GET /fnol/{reference}` and watch `processing_state`:
`queued` → `processing` → `completed`.

---

## What is deliberately not built

Named because each is a real improvement, and because none of them changes an interface
— so each is an addition rather than a rewrite.

- **Reranking, MMR, query rewriting, multi-query.** `RetrievalService.search` returns
  scored ordered hits, so a reranker is a decorator over it.
- **Multi-passage citation per field.** An FNOL field is a scalar and the honest citation
  is one passage. The additive shape is `fnol_field_evidence(field_id, chunk_id, rank,
  score, snippet)` with `source_chunk_id` becoming the rank-0 denormalisation.
- **Nested attachments as documents of their own.** An `.eml`'s attachments are read one
  level deep and inlined; materialising them needs its own de-duplication, per-case count
  limit and cycle story.
- **A Postgres/Qdrant reconciliation task.** `chunk_count` versus `embedded_chunk_count`
  makes the drift visible, and `force: true` repairs it. Automating the repair is the
  follow-up.
- **Legacy `.doc` / `.xls`.** The pre-2007 binary formats need a record parser.

### The gap that matters most

**There is no evaluation set.** Retrieval narrowing the prompt is the point, but it can
also *lose* a value that used to sit inside the truncated corpus — a figure stated once,
oddly worded, that no field query happens to match. Four things mitigate it by design:
the notification body is always sent whole, `retrieval_min_chunks` means small cases
never use retrieval, queries are unioned per field section so recall is per-group rather
than global, and there is a score floor.

None of that is a measurement. The highest-value next piece of work is five to ten real
notices with their expected field values, run as an integration test.
`ProcessResult.retrieval_used` and the `retrieval` block in the analysis record exist to
make a regression *visible* after the fact; they do not prevent one.
