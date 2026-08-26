# Extraction review, end to end

How a document becomes a checked value on the intake screen, and how to run the
whole thing on demo data.

This is the joining document. The two layers underneath have their own
references and neither knows about this screen:

* [document-intelligence.md](document-intelligence.md) — a stored file becomes
  searchable, citable passages.
* [extraction-datasets.md](extraction-datasets.md) — a configured set of
  questions is answered from those passages.

---

## The chain

```
email or upload
      │
      ├─ attachments ──────────┐
      └─ the message body ─────┤   (written out as notification-body.txt,
                               │    so a value quoted from the email is a
                               │    citation like any other)
                               ▼
                    FNOLDocument, stored
                               │
                    read per page → page_offsets
                               │
                    chunked into passages, each carrying its page
                               │
                    embedded → Qdrant          (optional; degrades to keyword)
                               │
        per dataset field: retrieve with the field's description as the query
                               │
              one model call per batch, passages labelled [C1], [C2]…
                               │
     value + confidence + quote + label  →  extracted_values
                               │
              label → passage → source_chunk_id, source_document_id
                               │
        ┌──────────────────────┴───────────────────────┐
        ▼                                              ▼
  extraction review                          fnol_extracted_fields
  (the dataset's own values)                 (mirrored by the adapter, so the
                                              rest of the pipeline and the
                                              claim record see the same values)
```

The single most important property: **a value knows which passage it was read
from**, and a passage knows its document and its page. Everything the review
screen does with evidence follows from that and nothing else. It is why the
screen can show page 3 of a survey report for a figure that also appears in the
covering email — the citation is a passage id recorded at extraction time, not a
search performed afterwards.

---

## What the screen shows

`/intake/:reference` runs in two stages. The stage switch is local state: it is
where an officer has got to in this sitting, not what the link points at.

### Stage one — Extraction review

**Left: the configured dataset's values.** Rendered from `GET
/fnol/{ref}/extraction`, which carries the dataset *with* the values so panels,
order and labels come from configuration rather than from the frontend. An
administrator adding a field to `fnol_notice` changes this screen without anybody
editing it.

Above the values sits the case's documents — what was attached, how far each got
(`Read` / `Reading` / `Not read`), and the control to attach another. A document
that could not be read is called out by name: the run still completed, so nothing
else on the screen would mention it, and an unread file and an empty field look
identical on a column of values.

**Right: the source document, and only that.** Nothing from the notification's
envelope appears on this stage. The submission column that used to sit here would
put the covering email beside a figure taken from an adjuster's schedule, which is
precisely the mistake the citation exists to prevent.

Clicking **"Where this was read from"** on a value fetches `GET
…/extraction/values/{key}/evidence` and opens the pane. What it draws depends on
what the server could resolve, reported in `strategy`:

| `strategy` | What the pane shows |
|---|---|
| `chunk-grounded` | The PDF page, rendered by pdf.js, with the quote boxed. The accurate answer — this is the occurrence the model actually read. |
| `document-search` | The same, but the passage was replaced by a re-index so the value's own text was searched for. May be a different occurrence, and the pane says so. |
| `text-only` | No page geometry — a Word file, a spreadsheet, the notification body. The quoted text *is* the highlight. |
| `none` | Nothing to point at, e.g. a value an officer typed. Said plainly; never papered over with nearby text. |

`rects` are PDF points from the top-left, and each carries its `page_width` /
`page_height` so the viewer can scale to whatever size it renders at without a
second request. `<Page scale={zoom}>` renders at exactly `zoom` CSS pixels per
point, so `zoom` *is* the point→pixel factor. One rectangle per line of the quote:
a single box round a two-line quote would cover the text between the lines, which
the quote does not contain.

**"Find every occurrence"** is a deliberate second step, calling `POST
…/documents/{id}/locate`. The first answer is the *cited* one; searching the whole
file first would quietly replace it with whichever match comes first.

Correcting a value `PATCH`es it, and the server drops its citation — keeping it
would point at the sentence the officer disagreed with. The correction is written
back onto `fnol_cases` in the same transaction, so the header, the blockers and
stage two follow immediately.

### A date of loss is two things at once

`loss.date_of_loss` is the one field on this screen where what the document says
and what the claim record needs are not the same shape. A notice states the date
however the broker felt like stating it — `13 September 2025, overnight,
discovered 14 September 06:20`, `overnight on Friday`, `26 February 2026 14:52
EST` — and `fnol_cases.date_of_loss` is a timestamp that a claim cannot be created
without.

So the value carries both, and the screen should show both:

| Field | Holds | Example |
|---|---|---|
| `value` | The wording, exactly as the source writes it. Never reformatted. | `overnight on Friday` |
| `typed_value` | The instant it resolves to, ISO-8601. What lands on `fnol_cases.date_of_loss`. | `2026-05-01T22:00:00+00:00` |
| `inference_note` | What was inferred to get from one to the other, in a sentence. Null when nothing was. | `“Friday” is read as 1 May 2026 22:00, relative to the notification of 5 May 2026.` |

The resolution is `app/domain/temporal.py`, it is deterministic, and it is
anchored on `received_at` — the notice's own arrival — because that is what the
person writing "Friday" meant by it. Three of its rules are worth knowing at the
screen:

* **A discovery date is never the date of loss.** `overnight, discovered 14
  September 06:20` is one loss with two dates in it, and the clause after the
  discovery word is set aside before anything is read. Where a notice states
  *only* a discovery date it is used, and the note says so.
* **The stated wall clock is kept as stated.** `21:04 EDT` is stored as 21:04, not
  shifted to 01:04 the following day. The calendar day is what the policy-period
  check, the catastrophe window and the claim all read, and a true-UTC conversion
  moves evening losses onto the wrong one.
* **Nothing is stored that cannot be justified.** A phrase that reads as no date,
  or as a date after the notification arrived, leaves `typed_value` null with the
  reason in `validation_error` — and the value itself still on the screen, for the
  officer to correct.

The extracting model is also asked for its own ISO reading of the same wording
(`normalised` in its answer). It is used only where the resolver cannot read the
words at all, and where the two land on different *days* the value is flagged
`needs_review` with both readings in the note: two defensible readings of one
notice is exactly what review is for.

An officer's own correction goes through the same resolution, against the same
notice date, so a typed "Friday" and an extracted "Friday" cannot disagree.

### Stage two — Intelligence review

Unchanged by this work. The fixed extracted record on the left, and the
pipeline's conclusions on the right: completeness, severity, fraud signals,
coverage, policy candidates, duplicates, the catastrophe attribution and the
exception list. Switching stages does not disturb the case or lose a selection.

### Why "document-extracted fields only" is structural here

Every value on stage one came out of a document, because the dataset is run
*against documents* and the notification body is one of them. There is no set of
"original submission" fields to filter out — those live on stage two, on
`fnol_extracted_fields`, which is a different surface with a different job.

A dataset value with no citation is still shown (a required field the reader could
not find is exactly what an officer needs to see), and clicking it returns
`strategy: none` with a plain explanation rather than a wrong source.

---

## Running the demo

### Services

```bash
make up          # postgres, redis, qdrant, minio, keycloak, wiremock, mailpit
make migrate
```

Postgres is required. Everything else degrades:

| Not configured | What happens |
|---|---|
| Qdrant / embeddings | Passages are still written and still keyword-searchable through the generated `content_tsv` column. Retrieval reports `strategy: keyword`, `degraded: true`. Every value is still cited. |
| **Model provider** | **The dataset path stands aside entirely** and the deterministic reader in `app/domain/heuristics.py` runs. It answers the fixed FNOL schema and cannot cite a passage, because it never read one — so extraction review will show no sources. The demo reports this and exits non-zero rather than pretending. |

### Environment

| Variable | For | Default |
|---|---|---|
| `CWB_AI_API_KEY` | The model. **Required for citations.** | unset |
| `CWB_AI_MODEL` | | `gpt-5.6-luna` |
| `CWB_AI_TEMPERATURE` | Omitted by default — a GPT-5-family model accepts only its own default | unset |
| `CWB_AI_REASONING_EFFORT` | `none`/`low`/`medium`/`high`/`xhigh` | unset → provider default |
| `CWB_AI_BASE_URL` | Any OpenAI-compatible endpoint | `https://api.openai.com/v1` |
| `CWB_DOCINT_EMBEDDING_API_KEY` | Vectorisation | unset → keyword only |
| `CWB_DOCINT_QDRANT_URL` | Vector store | unset → keyword only |
| `CWB_EXTRACT_SCHEMA_DRIVEN` | Off runs the fixed schema | `true` |
| `CWB_EXTRACT_FIELDS_PER_CALL` | Batch size | `10` |

Full tables are in the two layer documents.

### The run

```bash
make demo              # ingest, index, extract, report
make demo-reset        # delete the demo case and start again
make demo-documents    # rebuild the PDFs from their generator
```

The demo drives the same `build_pipeline` the API route and the Celery worker
use — not a parallel copy of it — so a change that breaks production breaks the
demo. It exits non-zero when the pipeline did not produce what the screen needs,
which makes it a check rather than a report.

### Expected output

Real output from a run against `gpt-4o-mini` — the default at the time it was
recorded, since replaced by `gpt-5.6-luna` — and `text-embedding-3-small`, with
Qdrant configured. Field counts move with the model, so treat the count as a
neighbourhood rather than a target: the transcript below is 22/30, and the six
packs on `gpt-5.6-luna` land between 27/30 and 30/30. What should not move is
that **every value carrying a value carries a citation**, and that the page is
the page the value is printed on.

```
Providers
  model      : configured
  embeddings : configured
  vectors    : configured

ingested FNOL-2026-000650 with 4 attachments
pipeline: state=completed retrieval_used=True

Documents
  harbourline-loss-notice.pdf            extracted   index=indexed  pages=  2 passages=  3
  harbourline-policy-schedule.pdf        extracted   index=indexed  pages=  1 passages=  1
  harbourline-survey-report.pdf          extracted   index=indexed  pages=  3 passages=  3
  harbourline-damage-schedule.csv        extracted   index=indexed  pages=  - passages=  1
  notification-body.txt                  extracted   index=indexed  pages=  1 passages=  2

Extraction run — dataset fnol_notice v1
  status=completed extracted=22/30 needing review=0 strategy=hybrid-rrf degraded=False llm_calls=3

Values with a citation — 22 of 22 extracted
  * policy.policy_number             MAR-2026-77413                harbourline-loss-notice.pdf   p.1
  * policy.insured_name              Ravensgate Marine Logistics…  harbourline-loss-notice.pdf   p.1
  * loss.date_of_loss                29 July 2026                  harbourline-loss-notice.pdf   p.2
  * financial.estimated_loss         GBP 486,500                   harbourline-loss-notice.pdf   p.2
  * loss.loss_location               Berth 8, Port of Felixstowe…  notification-body.txt         p.1
  …

Evidence — what the review screen gets when a field is clicked
  policy.policy_number      chunk-grounded   harbourline-loss-notice.pdf   page=1 rects=2
      “Policy number: MAR-2026-77413”
  financial.estimated_loss  chunk-grounded   harbourline-loss-notice.pdf   page=2 rects=1
      “Estimated loss: GBP 486,500”
  loss.cause_of_loss        text-only        notification-body.txt         page=1 rects=0
      “Cause: Heavy weather - container stow collapse”
      note: This document has no page layout; highlight the quoted text instead.

FNOL-2026-000650: 5/5 documents read, 10 passages (10 embedded), 22/30 fields,
22 cited, 4 drawable highlights
```

Two things in that output are the whole point. **The policy fields cite page 1 and
the loss and financial fields cite page 2** of the same file — a whole-document
answer could not tell them apart. And `loss.cause_of_loss` came from the
notification body, which resolves `text-only` with no rectangles because a plain
text file has no page geometry — the viewer marks the quote instead. Both are
normal answers, and the pane says which it is.

### What changes when something is not configured

| Run | Result |
|---|---|
| Model + embeddings + Qdrant | `index=indexed`, `strategy=hybrid-rrf`, 22/30 fields, all cited |
| Model + embeddings, **no Qdrant** | `index=skipped`, `strategy=keyword`, 21/30 fields, all cited. Passages are still written and still keyword-searchable through the generated `content_tsv` column; nothing is lost but recall. |
| **No `CWB_AI_API_KEY`** | Documents are still read, chunked and indexed — the Documents section above is real — but there is no run, no values and no citations. The demo says so and exits 1. |

`CWB_DOCINT_EMBEDDING_API_KEY` falls back to `CWB_AI_API_KEY`, so one key turns on
both. Qdrant needs its URL set explicitly even when the container is running:

```bash
CWB_DOCINT_QDRANT_URL=http://localhost:6343     # the host port make up publishes
```

Then open `/intake/FNOL-2026-000NNN` in the frontend (`cd ../ClaimsWorkbench_FE
&& npm run dev`), sign in with any email, password and 6-digit code, and click a
value's "Where this was read from".

---

## Verifying it

```bash
uv run pytest tests/unit/test_demo_documents.py       # the files carry the facts,
                                                       # and a quote resolves to a rectangle
uv run pytest tests/integration/test_demo_pipeline_flow.py -m integration
                                                       # the chain, against real Postgres
uv run pytest -m "not integration"                     # everything else
uv run alembic check                                   # no model drift
```

The integration test's stub model is a *citing* one: it reads the prompt it was
given, finds the labelled passage that actually contains the quote, and cites
that. A stub told which label to return would let a broken label→chunk resolution
pass, because the label would be correct by construction. Only the part that would
need an API key is substituted.

On the frontend:

```bash
cd ../ClaimsWorkbench_FE
npx tsc -b && npx vitest run && npm run build
```

---

## Known limitations

* **No OCR.** A scanned PDF lands `extraction_status = unsupported` with a
  sentence naming OCR. Nothing else depends on it: adding it later is a
  registration in `documents/text.py` plus a client module, with no change to
  chunking, retrieval, citation or the API.
* **Field-level accuracy is still not measured.** Retrieval narrowing the prompt is
  the point, but it can lose a value stated once and oddly worded that no field
  query happens to match. Four things mitigate it by design — the notification body
  is always sent whole, below `CWB_EXTRACT_RETRIEVAL_MIN_CHUNKS` the whole corpus is
  sent instead, queries are per-field, and there is a score floor — but no field's
  *value* is compared against a known answer anywhere. What now is measured is the
  layer below it: `case_data/_ground_truth/MATCHING_GROUND_TRUTH.json` states the
  expected policy for all 24 packs, and `make eval-matching` scores the matcher
  against it with `tests/unit/test_matching_ground_truth.py` as the regression
  guard. That harness feeds the answer key's field values straight in, so it holds
  extraction constant at perfect on purpose — a matching failure on correct inputs
  is a matching bug, and one on extracted inputs could be either. Extending the key
  to per-field expected values, driven through a real run, is the next piece.
* **The corpus a real pack produces is small, and the engine now knows it.** Four
  documents produce ~10 passages, below the floor of 12, so both paths send the
  whole corpus rather than narrowing it. That used to be true of the legacy evidence
  path only: `SchemaExtractionEngine._retrieve` called search directly, so on nearly
  every real fixture a field whose best passage scored under
  `CWB_DOCINT_RETRIEVAL_MIN_SCORE` was dropped with no recourse. It now checks
  `CWB_EXTRACT_RETRIEVAL_MIN_CHUNKS` first and records `retrieval_strategy =
  whole-corpus` on the run when it fires. Raise the setting or add documents to
  exercise narrowing specifically.
* **PDF highlight rendering is not covered by a unit test.** Rectangle
  *resolution* is tested against real PDFs on the backend, and the viewer's
  non-PDF paths are tested on the frontend, but drawing boxes over a pdf.js render
  needs a browser. It is exercised by the demo run and would suit a Playwright
  test.
* **Table datasets.** A schedule of five hundred locations cannot be extracted by
  retrieval-per-field; that needs sheet-aware table parsing. The `json` data type
  covers a small list today.
* **Superseded surfaces are retained, not withdrawn.** `DocumentFieldsColumn`,
  `SourceDocumentColumn`, `useFieldEvidence` (the fixed-schema evidence path) and
  the mock case-file band (`CaseFilePanel`, `DocumentPreviewModal`,
  `useEvidenceTrace`) are still in the frontend and still tested, but nothing
  mounts them. They are one deletion away whenever the dataset path has proved
  itself in front of users.
