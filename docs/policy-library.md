# The policy library

How a carrier's policy wordings get into a vector index, and how a notice finds the
ones that answer it.

Related: [policy-identification.md](policy-identification.md) for matching a notice
against the *book*, which is a different question this module does not replace, and
[document-intelligence.md](document-intelligence.md) for the chunking, embedding and
retrieval primitives this reuses.

---

## Two matchers, one screen

There are now two answers to "which policy is this loss under", and the reason to have
both is that they fail in different places.

| | Policy identification | The policy library |
|---|---|---|
| Matches against | `policies` — structured rows | `policy_documents` — uploaded PDFs |
| Compares | field against field | the notice's words against the wording's clauses |
| Can answer | *which contract* | *which wording speaks to this loss*, and quotes it |
| Cannot answer | whether the wording mentions the peril | which contract, on its own |
| Authority | **authoritative** — writes `fnol_policy_matches`, confirming binds | **advisory** — writes nothing |

The book answers "which contract" and cannot answer "does this wording cover the
peril", because a row carries a `perils_covered` array and a policy carries fourteen
pages of definitions, exceptions and endorsements. Retrieval answers "which wording
talks about this loss" and on its own cannot answer "which contract", because a
well-drafted property policy discusses a frozen sprinkler exactly as well as every
other property policy does.

So they are complementary and their union is the point. Where they agree an officer has
corroboration from two independent methods; where they disagree, that disagreement is
the most useful thing on the screen, and neither is allowed to silently win.

**Why the library is advisory.** A wording is a document an administrator uploaded.
Letting a retrieval score bind a contract would mean a badly-read declarations page
could attach a claim to the wrong policy with no book row ever consulted. The review
panel therefore offers no confirm control at all: an officer who reads a clause and
decides it settles the question goes and confirms the *policy*, in the panel that owns
that decision.

---

## The chain

```
an administrator uploads a PDF          POST /policies/documents      → 202
            │
   validate · store · read the text     services/policies/library.py
     · PDF only, checked on magic bytes as well as the name
     · the same bytes twice is one entry, recognised on its checksum
     · text read here, not in the worker — so a re-ingest never fetches from S3
            │
     row written at `pending`, committed, *then* the task enqueued
            │
            ▼
   PolicyIngestionService.ingest()       services/policies/ingestion.py
     · read the declarations             domain/policy_extraction.py   ← pure
     · link to the book by policy number  (never creates a policy)
     · chunk, page-aware                 intelligence/chunking.py     ← reused
     · embed                             intelligence/embedding.py    ← reused
     · upsert into the policy collection  services/policies/vectors.py
            │
            ▼
   a notice arrives, or an officer opens the review screen
            │
   NoticeQuery built from `fnol_cases` columns   services/policies/matching.py
            │
   four facet queries, concurrently              services/policies/retrieval.py
     · identity · site · peril · cover
     · hybrid RRF over Qdrant and Postgres full text
            │
            ▼
   policy_matching.match()                       domain/policy_matching.py  ← pure
     · seven corroborating signals, one function each
     · retrieval and corroboration scored *separately*
     · a ladder to a confidence band, then the excerpts that explain it
            │
            ▼
   GET /policies/matches/{ref}   → the card: confidence, reasons, clauses, warnings
```

---

## What is reused, and the one thing that is not

Reused verbatim, because the primitives were already right:

* `app/services/documents/` — validation, storage and the PDF reader. A wording is a
  PDF and the reader that reads a loss notice reads it correctly.
* `app/services/intelligence/chunking.py` — pure, page-aware, and holds the
  `chunk.content == text[start:end]` invariant an excerpt's page number depends on.
* `app/services/intelligence/embedding.py` — **one** embedding provider for the whole
  deployment. Two would be two vector spaces, and a library embedded by a different
  model than the query searching it matches nothing.

Not reused: the **vector store**. `VectorStore` takes `case_id` as a required argument
on every read and write, because every claim passage belongs to a case. A policy belongs
to none, so satisfying that Protocol would mean inventing a case id and filtering on a
lie. `PolicyVectorStore` is a parallel Protocol over its own collection.

**Its own collection, not a shared one with a `kind` field.** Mixing policy wordings
and claim passages and filtering on a payload key would work, and is the wrong trade: a
carrier's policy book and its claim documents have different retention periods, access
stories and lifecycles — a policy stays indexed for the length of its term, a claim's
passages are destroyed with the claim. A filter is a promise the application makes; a
collection is one the datastore makes.

---

## The matching engine

`app/domain/policy_matching.py`. Pure functions over two dataclasses, so the whole
thing is testable with no database and an auditor recomputing a 0.87 in two years gets
0.87.

The score is **two things, reported separately**:

* **Retrieval** — how well the wording's clauses answer the notice's words.
* **Corroboration** — a weighted mean over seven signals compared here.

Reported separately because they mean different things to the officer reading them, and
because collapsing them is how a system produces a confident-looking 0.9 that rests
entirely on shared vocabulary.

### The corroborating signals

| Signal | Weight | Axis | Compared how |
|---|---|---|---|
| `policy_number` | 4.0 | policy | exact → containment → near-agreement (a transposed digit) |
| `insured_name` | 2.2 | insured | `entity_similarity` against **every** party the wording insures |
| `risk_location` | 1.6 | risk | postcode against the premises schedule first, then address overlap |
| `project_reference` | 1.3 | project | contract or project name against the heading and schedule |
| `policy_period` | 1.2 | cover | in force / before inception / after expiry |
| `line_of_business` | 0.8 | cover | equality, with construction ≡ engineering and friends |
| `broker_name` | 0.7 | broker | `entity_similarity` against the producer |

Weighted lower than the identification engine's equivalents on purpose: there the
policy number is compared against a *book* whose numbers are authoritative, here
against a number read out of a PDF — one more reading step, one more place to be wrong.

### The two rules that keep weak matches off the screen

**Retrieval alone cannot recommend a policy.** A wording that matched on nothing but
its prose is capped at `RETRIEVAL_ONLY_CEILING`, below the possible band, whatever its
similarity. Top-k over a library of twelve always returns twelve scores, and
normalising them hands the first one a 1.0.

**Descriptive agreement is not identification.** This was a real defect and it is worth
recording. A notice for a loss the library holds no policy for still agrees with half
the book on the *date of loss* and the *line of business*, because every in-force
property policy is in force on the date and is a property policy. Two agreements, an
axis each, and a normalised retrieval score computed to a confident-looking 0.56 for
four notices whose right answer is "no policy here". So `_classify` requires at least
one **identifying** signal — something that names the party, the place, the contract or
the reference — before a wording can be a candidate at all.

### Confidence is a ladder, not a threshold

```
REJECTED   nothing identifying agreed; or a quoted policy number disagreed and
           nothing about the client or the site agreed
EXACT      policy number exact + inside the period + no identity conflict
STRONG     a primary identifier agreed, or two identity axes agree, and the score
           clears the strong threshold
POSSIBLE   above the possible threshold — or capped here by an identity conflict
WEAK       above the weak floor — shown below the line, never recommended
```

Two candidates within `match_ambiguity_margin` means the engine recommends **neither**.
Ambiguity is an outcome to surface, not one to tie-break: two companion policies of one
insured is precisely when the officer has a choice, and pre-selecting one hides that it
existed.

### Warnings are not scores

`outside_policy_period`, `prior_term`, `line_of_business_mismatch`, `identity_conflict`
and `not_linked_to_book` are reported beside the reasons and change no rank. A wording
can be certainly the right policy and a poor answer to this loss. Every sentence says
"may" where it is about cover — this module never states a coverage decision.

---

## Reading a wording's declarations

`app/domain/policy_extraction.py`. Pure, deterministic, no model call — and that is a
deliberate departure from how everything else in this product reads a document.

A loss notice is prose written by whoever was at the site, and no pattern survives it;
that is what `SchemaExtractionEngine` is for. A policy's declarations page is the
opposite kind of document: generated by a policy administration system from a form, so
`POLICY NUMBER:` is on a line by itself in a fixed place. A regex reads it with
certainty where a model reads it with a confidence score, and spending a model call —
and accepting a hallucination risk — on the one machine-generated document in the
product would be the wrong trade twice over.

The rule it holds to: **a value it is not sure about is `None`.** A wrong insured name
is worse than a missing one, because a missing one drops out of the mean and a wrong one
scores a mismatch against the right policy.

Six shapes it gets right that a first pass did not, each of them a real page in the
corpus:

* **`Renewal of Policy No. CP-4471-88209`** sits two lines above the real number and is
  one digit from it. Read naively it binds every claim to last year's term.
* **A numbered `NAMED INSUREDS` block** is a list of insureds, not one — and the party
  reporting a construction loss is very often the contractor rather than the employer.
  Reading only the first entry scores a *mismatch* against the right policy.
* **A wrapped qualifying clause** inside that block ("…but only for their interest in
  materials, supplies,") is not a party. Joint names must carry a corporate form.
* **`2600 North Central Avenue`** starts with digits, and so does a list marker. A
  marker pattern that did not require punctuation deleted the street number — the most
  discriminating token on the line.
* **`NAIC No. 39152`** is five digits and is not a postcode. Nor is a producer code, a
  NAICS code, or half a policy number. A US ZIP counts only behind a state code.
* **A property policy's builder's-risk exclusion** contains "course of construction".
  Read as a whole page, that exclusion decides the line of business — and a
  manufacturing property policy filed as construction is filtered out of every property
  notice's candidate set. The heading decides; position settles ties.

---

## Storage

| What | Where | Why there |
|---|---|---|
| The uploaded PDF | object storage, under `policy-library/` | its own namespace: a bucket where the policy book sits under `fnol/` says these belong to a notice |
| The wording's metadata and ingestion state | `policy_documents` | the row is written before any reading happens, which is what makes the upload fast and the ingestion asynchronous |
| The passages | `policy_document_chunks` | Postgres owns the text so an excerpt is readable when Qdrant is unreachable, and the index is rebuildable from Postgres alone |
| The vectors | Qdrant, `policy_document_chunks` collection | see above on why it is not the claim collection |
| The match | nowhere | it is a read. Computed live on every request, because a stale answer on a screen an officer is binding a policy from is worse than a fast one |

`policy_documents.policy_id` is **nullable**, and it is the interesting column. A
wording is uploaded before anybody says which book row it is the wording *for*, and on
a real desk it may never be linked — the policy administration system owns that record.
So the link is an outcome of ingestion rather than a precondition for it, and an
unlinked wording is still ingested, still searchable and still matchable.

---

## The API

| Route | What it is for |
|---|---|
| `GET /policies/documents` | the board: a page of the library plus the counts the status strip and the poller read |
| `POST /policies/documents` | upload one PDF. `202` — the entry exists, the passages do not yet |
| `GET /policies/documents/{id}` | one wording's ingestion status. Cheap enough to poll |
| `GET /policies/documents/{id}/content` | the PDF, `inline`, so a quoted clause can be checked on its page |
| `POST /policies/documents/{id}/reingest` | read it again. Inline, so an administrator pressing retry sees the outcome |
| `DELETE /policies/documents/{id}` | remove it from Postgres, Qdrant and object storage |
| `GET /policies/matches/{reference}` | the wordings that answer one notification |
| `POST /policies/match` | the same, against a reference or against values typed directly |

**`202`, not `201`.** Returning `201` would tell the client the resource it asked for —
a matchable wording — is ready, and it is not.

**The commit happens before the enqueue.** A worker that picked the id up before the
transaction landed would read no row and do nothing, leaving an upload that never
ingests and nothing to say why.

**An unreachable broker is not a failed upload.** `_enqueue` never raises: the document
sits at `queued` and the beat sweep picks it up within the minute. The response's
`queued: false` is what lets the screen say "waiting for a worker" instead of "ready
shortly".

Authorisation splits along a line worth stating. **Reading** the library and its matches
is open to everyone who reads intake, because the review screen shows an officer which
wordings a notice retrieved. **Writing** is a configuration act — a wording changes what
every future notice is matched against — so it sits with `POLICY_LIBRARY_WRITE_ROLES`
rather than with the officers who work the queue.

---

## The screens

**`FE: src/features/policies/PoliciesPage.tsx`** — two tabs, because the two halves are
not the same thing. *Wordings* is the live integration: upload, per-file results,
ingestion status, search, re-ingest, remove, and a panel that matches an ad-hoc loss
against the library. *Policy book* is the pre-existing board over `policiesService`,
still a fixture until that endpoint lands.

The upload panel says three things a plain file input does not: PDF only *before* the
drop, "accepted is not ready", and every file's own outcome by name — a twelve-file drop
with one refusal has to be legible without re-dropping the folder.

**`FE: src/features/intake/components/review/PolicyWordingsPanel.tsx`** — a fourth panel
on the identification stage, after the candidates and before the fallback. That order is
the order an officer works: what the notice gave us, which book rows it could be, what
the wordings themselves say, then the way out when none of them is right.

**`FE: src/features/policies/components/PolicyMatchCard.tsx`** — shared by both. Draws
the two scores separately, every signal including the silent ones, the warnings verbatim,
and the excerpts as quotations on their pages with a control that opens the PDF there.

---

## Verifying

* **Unit** — `tests/unit/test_policy_extraction.py` (the declarations reader, including
  the whole twelve-wording corpus), `test_policy_ingestion.py` (chunking, vector
  insertion, idempotency, failure isolation), `test_policy_document_matching.py` (the
  engine — asserted on the *reason list and the band*, not only the score),
  `test_policy_retrieval.py` (facet queries, fusion, and the guard that the database is
  never touched concurrently), `test_policy_upload_validation.py`, `test_policy_api.py`.
* **Frontend** — `FE: tests/unit/policies.test.tsx` and the
  `describe('the policy wordings the notice retrieved')` block in `fnolReview.test.tsx`.
* **By hand** — load the twelve synthetic wordings in `policy/` and match the FNOL packs
  in `case_data/` against them. A dev token works outside staging and production:
  `CWB_DEV_AUTH=true`, then `Authorization: Bearer dev.<base64url({"sub":"…"})>`.

### Measured accuracy

Against the twelve synthetic wordings in `policy/` and the twenty-four FNOL packs in
`case_data/`, with a deterministic bag-of-words embedding double standing in for a real
model:

| | |
|---|---|
| Correct wording ranked #1 | **18 / 20** matchable cases |
| Correct wording in the candidate list | **20 / 20** matchable cases |
| `NO_MATCH` cases with no candidate returned | **4 / 4** |

The two that are not ranked first are the two the dataset itself labels as needing
coverage reasoning rather than identification, and in both the engine declines to
recommend anything:

* *cypress-landing-copper-theft* quotes a real policy number for the **wrong policy of
  the same group**. The engine ranks the policy whose number was quoted and puts the
  right one second — which is the honest identity answer; resolving it needs the
  equipment floater's exclusion to be read, which is a coverage opinion.
* *aurora-jobsite-theft* is one insured with two companion policies. They tie, so the
  result is `ambiguous` and neither is recommended.

Real embeddings should improve the peril facet on both. Neither is a case where a wrong
policy is put forward as correct, which is the failure that matters.

---

## Deliberately not built

**A cross-encoder reranker** is the right next step for accuracy and changes no
interface: `match()` takes hits that are already scored, so it slots in above.

**A model asked "does this policy cover this loss"** is a coverage opinion, not a match.
It belongs beside the coverage assessment and must never move an identity rank.

**Scoring the absence of a peril as a mismatch** conflates two things. An exclusion an
officer has to read is not the same as a policy that does not answer the loss, and one
number cannot say both — so it is a warning.

**Writing the match to the case.** Deliberate, and the safety property this module rests
on. See "Why the library is advisory" above.

**Per-page OCR for a scanned wording.** A wording with no text layer fails ingestion
with a sentence naming OCR as the remedy. `CWB_DOCINT_OCR_URL` already exists for the
claim side and wiring it here is a small change; it is not done because a carrier's own
wordings arrive as generated PDFs, and building for the scan first would be building for
the rare case.
