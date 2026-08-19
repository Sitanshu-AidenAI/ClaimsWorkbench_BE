# Policy identification

How a broker's email becomes a bound policy, and how an officer sees why each
candidate was ranked where it was.

The first decision on a commercial claim, and the one everything after it depends
on: coverage, the limit check and the claim record all reason against a policy.
This document is the reference for the module — the engine, the seam a new signal
goes through, the API, and the review stage.

Related: [architecture.md](architecture.md) for the layering, [mail-intake.md](mail-intake.md)
for how the notice arrives, and [extraction-review.md](extraction-review.md) for how
its fields are read and cited. The citations that document describes are what make
a reason on a candidate card clickable, so the two screens share one viewer.

---

## The chain

```
notice + attachments read      (extraction; see extraction-review.md)
            │
   values promoted to fnol_cases columns        ← the seam. §"Adding a signal"
            │
            ▼
   PolicyIdentificationService.notice_signals()  services/fnol/identification.py
     · one NoticeSignals, each value carrying the document it was read from
     · reporter ≠ insured ≠ broker, held apart
            │
            ▼
   PolicyRepository.find_identification_candidates()   repositories/policy.py
     · every reference on the notice against every reference on a policy
     · postcode narrowed against the schedule, not the head office
     · nothing identifying → policies in force on the loss date, not the book
            │
            ▼
   policy_identification.identify()                    domain/policy_identification.py
     · 12 signals compared, one function each          ← pure, no IO
     · weighted mean over what could be compared
     · deterministic confidence ladder                 ← not thresholds on the mean
     · reasons, missing signals and warnings, kept apart
            │
            ├─ ranked candidates      → fnol_policy_matches   (an officer can act on these)
            └─ whole answer + near misses → fnol_ai_analyses  ("why is my policy not listed")
            │
            ▼
   the officer decides                             GET/POST …/policy-identification
     confirm a candidate · search the book · refer with no policy
            │
            ▼
   the policy becomes authoritative                POST …/policy-selection
     policy_confirmed = true, policy-bounded fields copied onto the case
```

---

## The engine

`app/domain/policy_identification.py`. Pure functions over two plain objects, so
the whole thing is testable with two dictionaries and no database, and an auditor
recomputing a 0.91 in two years' time gets 0.91.

Five parts, separable on purpose:

| Part | What it is |
|---|---|
| **Signals** | A declared table: what can be compared, what it is worth, and which *axis of identity* it speaks to |
| **Normalisation** | The notice becomes a `NoticeSignals`; a policy becomes a `PolicyFacts`. Built once; nothing below reaches into an ORM row |
| **Comparison** | One function per signal, each returning a typed `SignalResult` — never a bare float |
| **Scoring** | A weighted mean over what could be compared, then a ladder to a confidence band |
| **Explanation** | Reasons, missing signals and warnings, kept apart |

### The signals

Heaviest first, which is also the order the panel lists them in.

| Signal | Weight | Axis | Compared how |
|---|---|---|---|
| `policy_number` | 5.0 | policy | exact → OCR-folded → containment → edit distance ≤ 2 |
| `broker_reference` | 2.5 | broker | normalised equality, and the value quoted *as* the policy number |
| `contract_number` | 2.2 | project | normalised equality; construction risks only |
| `insured_name` | 2.0 | insured | `entity_similarity` against every party the policy insures |
| `project_name` | 1.6 | project | name similarity; construction risks only |
| `policy_period` | 1.5 | cover | five outcomes — see below |
| `risk_location` | 1.3 | risk | best across the whole schedule; postcode first |
| `insured_organisation` | 1.2 | insured | `entity_similarity`, joint names included |
| `insured_domain` | 1.0 | insured | the *insured's* address, never the sender's |
| `broker_domain` | 1.0 | broker | the envelope's sender, against the broker on the policy |
| `broker_name` | 0.9 | broker | name similarity |
| `line_of_business` | 0.6 | cover | equality, construction ≡ engineering |

The axis matters as much as the weight. Two signals agreeing across *different*
axes is worth far more than two agreeing on the same one, and the confidence ladder
reads the axes rather than only the total.

### Six things the engine gets right that a naive matcher does not

**The sender is not the insured.** 60–85% of commercial notices arrive from a
broking house, so the envelope's domain is evidence about the *broker*.
`broker_domain` and `insured_domain` are separate signals against separate policy
columns, and the insured's address comes from a party recorded as the insured or it
does not exist. One combined "email" signal compares the broking house's domain to
the client's and scores the mismatch as a fact about the client.

**A reference in the wrong field is one fact, not two.** Brokers put their own
scheme reference in the field a form labels "policy number", because that is what
their system prints; construction desks put the contract number there. The engine
recognises it, scores it on the signal it belongs to, and reports the policy-number
row as `not_compared` *with the reason* — rather than counting one fact as a match
and a mismatch at once and punishing the right policy for identifying itself.

**The schedule is the location, not the head office.** `policy_locations` is a
table so matching can search it and then *name* the entry that matched. A loss at
warehouse seven of twelve scores nothing against a primary location. The candidate
card then shows that location's own sum insured and excess, because that is where
they attach — a card printing the policy's headline excess beside a loss at a
location with its own is printing the wrong number.

**A group company is not the same company.** `entity_similarity` is stricter than
the shared `name_similarity`, which is built for finding a name inside a body of
text and scores "Northline Logistics" against "Northline Logistics (Scotland)
Limited" as perfect. The extra tokens are exactly what distinguishes one company in
a group from another, so a strict subset is capped below the match threshold:
similar enough to rank, not similar enough to bind.

**Joint names are insureds.** A CAR policy insures the employer, the main
contractor and every subcontractor for their interest, so the party reporting a
loss is very often not the *named* insured. Both identity signals compare against
`joint_names()` and the reason says which party matched — "the insured named on the
notice matches the principal".

**Missing and not-compared are different.** "The broker was not stated on the
notice" is a gap an officer can go and fill. "Neither the notice nor this policy
carries a contract number" is a signal that does not apply to this risk. Both are
dropped from the mean — a sparse notice is not punished for being sparse — and both
are *listed*, because silence tells the officer nothing.

### The date of loss: five outcomes, not two

| Outcome | Score | Why it exists |
|---|---|---|
| `in_force` | 1.0 | the ordinary case |
| `in_maintenance_period` | 1.0 | a construction defect discovered after practical completion is in cover |
| `prior_term` | 0.3 | losses are discovered late; the useful answer is last year's policy, not "uninsured" |
| `outside_period` | 0.0 | shown, never hidden — "this is your policy but the loss is outside it" is a coverage conversation |
| `unknown` | — | not compared, and said so |

### Confidence is a ladder, not a threshold

`_classify` reads *which* signals agreed, not only the total. The mean is a
summary; the band is a judgement, and the two disagree in exactly the case that
matters: an exact policy number alongside a mismatched insured name computes to
roughly 0.66 and would demote by arithmetic. Here it is demoted by rule — an
identity conflict caps the band whatever the number says — and the officer is told
why in as many words.

```
REJECTED   nothing identifying agreed, or a unique reference disagreed with
           nothing about the client agreeing
EXACT      policy number exact + not outside the period + no identity conflict
STRONG     a primary identifier matched, or two identity axes agree, and the
           score clears the strong threshold
POSSIBLE   above the candidate threshold
WEAK       above the weak floor — shown below the line, never acted on
```

Two candidates that both reach `EXACT` or `STRONG` means the engine recommends
**neither**. Ambiguity is an outcome to surface, not one to tie-break: two sister
companies matching on name, broker and line of business is precisely when the
officer has a choice, and pre-selecting one hides that it existed.

### Warnings are not scores

A candidate can be certainly the right policy and a poor answer to this loss.
`policy_not_active`, `outside_policy_period`, `prior_term`,
`line_of_business_mismatch`, `identity_conflict`, `peril_not_listed`,
`peril_excluded` and `exceeds_limit` are reported beside the reasons and change no
rank. Collapsing coverage plausibility into identity confidence hides the thing the
officer most needs to see.

---

## Adding a signal

Four edits, in this order. The engine reads **`fnol_cases` columns and nothing
else**, which is what makes "which extracted fields matter for identification" a
structural question rather than a conventional one: a value is a signal once it has
a column, and until then it is extracted, stored, cited and shown but not matched
on.

1. **Ask for it.** A field in `app/data/extraction_schemas/fnol_notice.json`. The
   `description` is used verbatim as the retrieval query, so write it in the
   vocabulary a document uses, not the schema's.
2. **Land it.** A `WriteBack` entry in `app/services/fnol/adapter.py` naming the
   column and the parser.
3. **Hold it.** A column on `FNOLCase` plus an Alembic migration.
4. **Compare it.** A `SignalDefinition` in `SIGNALS`, a comparator in
   `_COMPARATORS`, and its "not stated" wording in `_NOT_STATED`. Add the
   `fnol_cases` column and the dataset field key to `SIGNAL_SOURCES` in
   `app/services/fnol/identification.py` so the value arrives with its citation.

Step 4's `evidence_field_key` is what makes the new reason clickable. A signal with
no document behind it — the envelope's own sender domain — holds `None`, and the
panel offers no link rather than a dead one.

---

## Storage

| What | Where | Why there |
|---|---|---|
| Ranked candidates | `fnol_policy_matches` | rows an officer can *act* on, which is what makes the selection rule enforceable |
| Per-signal working, warnings, the card's face | `fnol_policy_matches.signals` / `.warnings` / `.display` | stored rather than recomputed, so what an officer saw when they bound a policy is recoverable |
| The whole answer, including near misses | `fnol_ai_analyses`, kind `policy_identification` | it *is* a computed reading fingerprinted by its inputs, which is what that table holds |
| Decision state | `fnol_cases.policy_identification_status`, `policy_referral_reason`, `policy_confirmed_by` | whether the *question* is settled is a different axis from whether a policy is bound |
| The schedule of locations | `policy_locations` | matching has to search it and name what matched |

Near misses live in the analysis and not in the candidate table on purpose: they
were compared and rejected, so they are an *explanation*, not an option. Confirming
one goes through the same path as confirming a searched policy, which persists it
as a candidate first.

---

## The API

| Route | What it is for |
|---|---|
| `GET /fnol/{ref}/policy-identification` | the whole first-stage answer in one call |
| `POST /fnol/{ref}/policy-identification` | identify again — one query, no model call |
| `POST /fnol/{ref}/policy-selection` | the officer's decision, made authoritative |
| `POST /fnol/{ref}/policy-referral` | record that no policy could be identified |
| `GET /fnol/{ref}/policy-search?q=` | the policy book by hand, scored against this notice |

The `GET` reads from three places on purpose. The **signals** are rebuilt from the
case every time, so an officer who has just corrected an insured name sees the
corrected one. The **candidates** come from the rows an officer can act on. The
**near misses** come from the stored analysis.

### The safety rule

> An AI never introduces a policy. Candidates come *from* the repository, so a
> hallucinated policy number cannot become a policy match — it can only fail to
> match one.

`confirm()` refuses any `policy_id` that does not resolve to a row in the policy
book. It does **not** require the policy to have been ranked: an officer who found
the right one after the engine ranked the wrong ones must be able to use it, so it
is scored and persisted as a candidate with `origin = "officer_search"` *before* it
is bound. That keeps "the bound policy is always one of this case's candidates"
true while stopping the manual search from being a dead end — which it was, because
the old selection endpoint refused exactly the policies the search returned.

---

## The review stage

`FE: src/features/intake/components/review/PolicyIdentificationColumn.tsx`, first
of three stages in `ReviewStageSwitcher`.

Why first: the policy is the authority on who is insured, what is covered and up to
what limit. An extraction review presented before a policy is bound shows the
broker's spelling of the insured's name as though it were settled, and a coverage
assessment before it is an assessment against nothing. Extraction is the *reading*;
identification is the first *decision*, and it comes first.

Three panels, in the order an officer works:

1. **What we matched on** — the notice's own side, grouped by axis, one line per
   signal, with what it did *not* state behind a disclosure. Shown whether or not
   anything matched: an officer who can see the search ran on a policy number and an
   insured name knows the *book* is the problem rather than the reading.
2. **Policy candidates** — the recommendation drawn large with its working open,
   the rest a line each. Below a rule, the policies compared and rejected.
3. **Not the right policy?** — the policy book, and the referral. A first-class
   panel because on a commercial desk an unresolvable notice is routine, and an
   officer who has to leave the screen to search the book will paste a policy number
   into a spreadsheet instead.

The right-hand column is the **same** `EvidenceViewer` the extraction stage uses.
A signal names the dataset field its value came from; `useExtraction.showEvidence`
already knows how to resolve a field's citation, cache it and hand it to the viewer.
So a reason on a candidate card and a value on the extraction stage take exactly the
same path to exactly the same highlighted passage — including the `HighlightRect`
overlays on a PDF — and there is one viewer in this product rather than two.

Product rules the stage holds to, all of them pre-existing:

- **The machine assists, the officer decides.** Nothing is pre-selected; the
  recommendation is drawn as a recommendation.
- **Never state a coverage decision.** Warnings say "may not cover", never "not
  covered".
- **A human-entered value has an author, not a confidence.** A confirmed policy
  carries the officer's name, not a percentage.
- **Show the mismatches.** A candidate list that prints only agreements is a sales
  pitch, not an audit trail.
- **A score is not a probability.** It is a weighted agreement across the signals
  that could be compared, said once in the panel's footer and never contradicted.

---

## Sample data

`app/db/seed.py`. Twelve policies and twelve notices, and the notices are written as
**raw broker emails and call notes** run through the real pipeline — never as
derived values, because a fixture that writes `policy_id` directly decorates the
screen instead of testing the system behind it.

The book is built to be got wrong: a nine-location schedule, a corporate group with
three near-identical names, a renewal chain linked by `prior_policy_id`, a lapsed
policy, two policy numbers one digit apart, and two CAR policies with contracts,
principals and defects liability periods. Fields are deliberately left null on some
rows — a book where everything is populated tests nothing, because
`missing`/`not_compared` is the branch most likely to be wrong.

Scenarios A–G exercise reading, citing and assessing a notice. H–L exercise
*identifying* it, one signal each:

| # | What it is | What it should do |
|---|---|---|
| H | broker quotes their own reference, no policy number | `STRONG` on `broker_reference` alone; last year's policy shown with an out-of-period warning |
| I | policy number OCR-damaged (`P0L-2O26-OO41`) | matched on the folded tier, confirmed by the schedule |
| J | names the group loosely, no reference | two `STRONG` candidates, **neither** recommended |
| K | construction, reported by the employer not the named insured | matched on contract, project and joint names, inside the defects liability period |
| L | loss discovered after renewal | `prior_term` on the current policy, and the prior policy as its own candidate |

Run it:

```bash
uv run alembic upgrade head
uv run python -m app.db.seed --reset      # rebuilds reference data too, not only the notices
```

---

## Verifying

- **Unit** — `tests/unit/test_policy_identification.py`. Assert on the *reason
  list*, not only the score: a test that pins "the loss location does not match any
  location insured under this policy" is a test of a promise made to an officer,
  where one that pins 0.61 is a test of arithmetic.
- **Integration** — `tests/integration/test_fnol_flow.py` walks a notice through the
  pipeline and checks the `policy_identification` analysis was recorded.
- **Frontend** — `FE: tests/unit/fnolReview.test.tsx`, `describe('identifying the
  policy')`. Covers the working, the evidence link, confirmation, manual search,
  referral and the rejected-candidate disclosure.
- **By hand** — seed, then `GET /fnol/{ref}/policy-identification` for H–L and check
  the expectation table above. A dev token works outside staging and production:
  `CWB_DEV_AUTH=true`, then
  `Authorization: Bearer dev.<base64url({"sub":"…"})>`.

---

## Deliberately not built

| Candidate | Why not yet |
|---|---|
| `Insurer` entity | single-carrier deployment; `Policy.insurer_name` is a string until a second carrier's book arrives |
| `Broker` entity | `broker_name` + `broker_reference` + `broker_domain` covers matching. Promote when broker-level reporting needs it |
| `Insured` entity | right eventually — one insured, many policies, group hierarchy — but a migration of every row for no matching gain today. The next entity to extract |
| `PolicySection` / `Coverage` | construction sections are fields, not rows. Revisit when the coverage check reasons per section |
| Additional insureds, loss payees | `joint_names()` covers the matching case from the principal and contractor columns. A real table when payments need one |
| Trigram indexes on the book | `ILIKE '%token%'` is free at a dozen policies and a sequence scan at fifty thousand. `_normalised()` is written as the expression a functional index would be built over, so the fix is an index and not a rewrite |
| A two-pass match → classify → re-score | classification currently feeds the `line_of_business` signal unaided on a first pass. The circularity is real and the weight is 0.6, so it is not worth the complexity yet |
