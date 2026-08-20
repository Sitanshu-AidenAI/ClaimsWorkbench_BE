# What Is To Be Built

**A sequenced plan across all three source documents**

*195 requirements, 131 of them not fully met. This document puts them in an order. Phases are sequenced by dependency and by what removes an objection soonest — not by size. Phase 0 ships no features and is still the most valuable phase here.*

*The line-item detail for every reference below (`R1.1.03`, `B.24`, `S.04` …) is in `02_Requirements_Matrix.xlsx`, sheet "Build plan".*

---

## Before anything: one decision and one request

**The decision.** Our own BRD (§4) excludes "fully autonomous claims processing without human validation" and "post-claim lifecycle management" from the MVP. RFP Section 2 and Attachment 2 require precisely that. **33 of the 195 requirements sit outside the BRD's own scope boundary**, and they are almost exactly Section 2. So:

| Reading | Requirements | Coverage today | Implication |
| --- | --- | --- | --- |
| The BRD's MVP is the plan | 124 | **58%** | Phases 0–3 only. Credible, deliverable, more than half built. |
| The RFP in full is the plan | 195 | **44%** | Phases 0–5. Two unbuilt programmes, one unstaffed. |

Phases 4 and 5 below are roughly six months of work that no current planning document asks for. Until somebody writes down which reading wins, they are speculative. **This costs a meeting and it gates everything after Phase 3.**

**The request.** Send four asks to Allianz in week one. None is a build; all four gate work below.

| Ask | Gates |
| --- | --- |
| **Attachment 1** — `01042025_BMP_CLAWS_EFNOL.xlsx`, the ~180 CLAWS attributes | Phase 1.3, Phase 3.1, Phase 3.2 — three of the largest packages here |
| Allianz internal IT security standards + approved-technology list | Phase 0.5, Phase 0.6, and whether our stack is acceptable at all |
| Which uptime figure binds — 96%/year or 99.9% API | Deployment architecture. A 40× difference in error budget |
| Where attachment passwords come from | Phase 2.1 — nothing can be built until the business chooses |

Plus two of our own: the AQS ruleset (Attachment 2 names it, nothing defines it), and Attachment 3, the rollout plan, without which none of the durations below can be held against real dates.

---

## Phase 0 — Evidence and credibility · 2–3 weeks

**Ships no user-facing features. Still the highest-value phase in the plan.** Everything here either removes an objection that currently ends conversations, or captures something that can only be captured *before* deployment.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 0.1 | Field-level accuracy harness over the `case_data` packs, scored with the RFP's own rule that a wrongly-formatted value counts as a **miss**, not partial credit | Criterion 1's 95% is the headline acceptance criterion and nothing measures it. Policy *matching* accuracy is already measured (18/20 rank-1); extraction accuracy is not | M |
| 0.2 | AHT capture — time to first touch, time on the review screen, time to submit, fields corrected per case | Criterion 6 asks for a **30% reduction against a baseline**, and a baseline can only be captured before deployment. Every week of delay is unrecoverable | S |
| 0.3 | The five data-quality KPIs as queries, plus the seven NLP text-quality measures criterion 7 names | All five are null-rate queries over existing tables. The seven NLP measures are self-contained and visibly answer a named criterion | M |
| 0.4 | Wire the existing Analytics screen to the new KPI service | Criterion 6 requires the application to "provide a visible representation of its performance against a defined set of KPIs". A finished, designed screen already exists — on fixture data | M |
| 0.5 | Unset the public `base_url` default and **fail loudly**; add an Azure OpenAI provider with managed identity; add a `CWB_AI_ALLOW_PUBLIC_ENDPOINT` guard defaulting to false | One line of default configuration currently breaches "no storage outside Allianz system landscape allowed". This is an objection that ends a conversation | L |
| 0.6 | The evidence pack: data-flow document, EU AI Act technical file and risk classification, model card, human-oversight design record, DPIA outline, BCP and DR skeletons, security architecture | The hard engineering already exists — citations, inference notes, per-signal explanations, deterministic scoring, overrides, provider stamping. **This is assembly, not construction**, and it is what the two named executives actually read | M |
| 0.7 | Audit hardening — read/access events, payload and log redaction, `REVOKE UPDATE/DELETE` on `audit_events` | Closes the "all **access** and changes" half of criterion 9 and the BRD's §5.3 password-exposure rule. Immutability is already met by construction; make it enforced as well as intended | M |
| 0.8 | Real-versus-fixture map and an agreed demo script | Eight finished screens have no backend. Demoing them as capability is a misrepresentation diligence would find | S |

**Exit test.** You can state an extraction accuracy figure, show a baseline AHT, point at a live KPI screen, and hand a security reviewer a data-flow diagram. None of that is true today.

---

## Phase 1 — The Market × Product spine · 3–4 weeks

The cheapest way to make the product recognisably AGCS's, and the prerequisite for everything regional or per-product. Our BRD calls this Market/OE; the RFP calls it a region and names eight. Same concept, and it does not exist.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 1.1 | **Market / OE as a first-class concept** — on the notice, on the claim, in configuration, and as a classification output | Parent of six other requirements. BRD §5.1 classifies on it, §5.4 scopes extraction by it, §5.6b routes on it | M |
| 1.2 | Bind extraction datasets to a **Market × Product** pair, so classification selects the field set | The join between classification and extraction. Highest-leverage single row in the matrix for serving 64 combinations. BRD §5.4: extract "only the data elements defined for the applicable Market/Product" | M |
| 1.3 | Add the four missing AGCS lines — **Natural Resources, Financial Lines, Entertainment, Aviation** — with their required-field sets, loss-type vocabularies and classification keywords | Four of AGCS's eight lines have no field set, no vocabulary and no routing. Needs Attachment 1 for their own vocabularies. Configuration tables, not code branches | L |
| 1.4 | Locale per market — date-format authority (DMY vs MDY), currency default, time zone, and the **original-vs-accounting currency pair** CLAWS requires on every reserve | `temporal.py` already parses every format competently; what it lacks is an authority to resolve ambiguity against. Also fixes the GBP default that turns missing data into wrong data | M |
| 1.5 | Assignment on Market/OE, and **Round Robin as a selectable strategy** beside best-fit | BRD §5.6b specifies Round Robin; the product implements best-fit. Keep best-fit — it is better — and ship what is specified. An `AssignmentStrategy` enum already exists | M |
| 1.6 | **Case-level confidence score**, and a named review queue as a routing destination | Per-field confidence exists; one number per case does not. It is what criterion 3 asks for, what BRD §6.2 routes on, and later what selects a segmentation lane | M |

**Exit test.** A German property notice classifies to a German market and a property product, selects the German property field set, resolves `03/04/2026` as 3 April, defaults to EUR, and routes to a handler who is awake and handles property.

---

## Phase 2 — Intake completeness · 4–6 weeks

The BRD MVP's remaining intake gaps. Two of these fail *silently* today, which is the worst kind of gap: the failure is invisible, and with no measurement layer (fixed in Phase 0) nobody would know the rate.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 2.1 | **Password-protected email and attachments** — detection, a configured password mechanism, decryption, an exception path, and no password ever persisted | BRD §5.3, and the RFP does not mention it at all. Today an encrypted PDF is indistinguishable from a corrupt one. Blocked on the business deciding where passwords come from | L |
| 2.2 | **OCR** — choose and self-host an engine behind the existing hook | JPEG and PNG are required formats. Detection, cost ceilings, a confidence floor and provenance fields are all built; the engine is not chosen. **Must be self-hostable** — a cloud OCR API breaches criterion 9 exactly as the AI provider does | L |
| 2.3 | **Fragmented FNOL** — read the `thread_id` nobody reads, score relatedness, group into one claim context, present below threshold, and merge / reject / correct | One package, two documents: BRD §5.5 *and* RFP Section 2's entry point. The merge is the hard part — it must move documents, values, passages, vectors and audit references without losing provenance. `deletion.py` already proves the codebase can map every store a notice touches | L |
| 2.4 | **Machine-to-machine credentials** — client-credentials grant or API keys | Only a signed-in user's token is accepted today. Required before CLAWS, an RPA bot or a batch job can call in. **Blocks Phase 3** | M |
| 2.5 | Portal ingestion channel | BRD §5.2. The channel is already named and reserved in the enum; the normalisation seam makes it cheap | M |
| 2.6 | Load and concurrency testing to the RFP's numbers — 100 rps at 95% success, 2s p95 | Criteria 5 and 6. Architecturally plausible, entirely unmeasured | M |

**Exit test.** A broker sends four emails over three days — one encrypted, one a photographed form — and they arrive as one claim with everything readable.

---

## Phase 3 — The CLAWS seam · 6–10 weeks

**This is what all three documents are actually buying.** Dependency-heavy: every item needs Allianz access, so start that conversation in Phase 0 and expect it to be the critical path rather than the code.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 3.1 | **CLAWS field dictionary and mapping layer** behind `app/services/fnol/adapter.py` | `adapter.py` is already documented as "the one module that knows both a dataset and a claim" — the correct seam. Needs Attachment 1 | XL |
| 3.2 | **The four missing CLAWS categories as data model** — a Coverage entity, party-to-coverage links, deductible management, movement/sub-movement type with dual currency | Categories 4, 6, 7 and 9 of nine, and where CLAWS spends most of its 180 fields. Data modelling, not configuration | XL |
| 3.3 | **FileNet adapter**, plus a configurable filing and naming convention | Criterion 4 requires document upload to FileNet; Attachment 2 requires archiving "based on a given filing and naming logic". A clean storage abstraction already exists to put this behind | L |
| 3.4 | **Global Genius policy-book synchronisation** | The landing shape is already designed — a 40-column flat projection — and the loader script says outright that an empty book makes every notice fail to match for reasons that have nothing to do with the notice | L |
| 3.5 | **CARA sanctions screening**, as a gated step that cannot be skipped | A legal control, not a feature. Attachment 2 places sanctions inside the Admin lane, which is automated for **all six** segments — so it is not optional in the target operating model | L |
| 3.6 | Policy-document retrieval from **SharePoint / FileNet** rather than manual upload | BRD §5.5b requires "approved repositories". The retrieval half is real and good; the source half does not exist | L |
| 3.7 | **Reinsurance and coinsurance** — model it, detect it, attach it | Zero occurrences of "reinsur" in the codebase. Required at CLAWS update by the RFP and for drafted RI emails by Attachment 2. A missed notification is a recoverable-loss problem | L |
| 3.8 | RPA / SCI hand-off for whatever CLAWS exposes no API for | The RFP's own slides assume this for notes, reserve submission and FileNet upload | L |
| 3.9 | **Pass-through mode** — purge the working copy after successful CLAWS submission | Turns criterion 9's residency rule from an objection into a feature, and directly answers the "system of record vs pre-processor" misalignment. `deletion.py` already proves every store can be swept and reported on | M |

**Exit test.** A notification arrives, is read, reviewed, and lands complete in CLAWS with its documents in FileNet, its parties sanctions-cleared, its reinsurance attached — and nothing personal left behind in our own stores.

**If only Phases 0–3 are delivered, that is a coherent, honest and complete answer to RFP Section 1 and to the BRD's entire MVP.** It is also the option the 58%/11% coverage split points at.

---

## Phase 4 — Segmentation and the universal admin lane · 4–6 weeks

*Gated on the M16 scope decision.* Attachment 2's model, and the one lane that pays back on every claim rather than only on small ones.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 4.1 | **Lead / Follow and AGCS share** on the policy book, plus attachment point / layer | One modelling exercise serves three requirements (segmentation, review depth by line size, BRD §5.5b's attachment/layer inference). Also fills the RFP's own "Lead / Follow / Fronted" and "AGCS Share" coverage-note fields | M |
| 4.2 | **The segment model** — six cells on Lead/Follow × size band, with business-configurable thresholds and an admin surface | Attachment 2 says thresholds "could be increased/changed once the business teams have confidence in the accuracy of the AI solution". They are a dial the business turns, so build them as configuration from the start. Copy the extraction-dataset pattern | M |
| 4.3 | **The lane model** — four named lanes mapped to cells, driving what runs and what a human must approve | Use Attachment 2's lane names, not the RFP body's four different ones | M |
| 4.4 | **Universal admin-lane automation** — save documents to CLAWS, document search, sanctions clearance | Attachment 2's Admin arrow spans all six segments. Its footnote names seven activities; three are built. **This is the best business case in Section 2** because it applies to every claim | L |
| 4.5 | **Material-wrong-decision and claims-escalation risk detector** | The safety interlock for both Follow lanes, where the bot writes before a human looks. Not the same as low confidence — a *confidently wrong* £400k reserve is the case this must catch. Attachment 2 names it twice and neither other document names it at all | L |
| 4.6 | Back the **Approval Queue** prototype with a real action model — propose, review, edit, approve, execute | The screen is already designed and matches Attachment 2's "handler edits / approves recommendations" and "bot actions claims handler requests" closely | L |

---

## Phase 5 — Loss-assessment reasoning · 10–16 weeks

*Gated on the M16 scope decision.* **Build in autonomy order.** Each step adds a safety requirement the previous one did not need, and steps 5.7's last two lanes are unsafe without 4.5.

| Ref | What | Why | Effort |
| --- | --- | --- | --- |
| 5.1 | **Document version lineage, and the report-versus-previous-report change synthesis** | The defining capability of Attachment 2's highest-value segment (Lead > £1m), and the task the slide shows handlers doing entirely by hand. Passages are already page-aware and embedded — the substrate a diff runs over | XL |
| 5.2 | **Subrogation potential detection** | Required in **all four** loss-assessment lanes — the only capability common to every one. One occurrence of the word in the codebase today, as a litigation keyword. Directly revenue-relevant | M |
| 5.3 | **Claim and claimant history**, and deductible erosion | Attachment 2 requires "view of history of claim to support deductible management" | L |
| 5.4 | **Reserve recommendation with an explicit uncertainty synthesis** | Required for Lead < £50k as a recommendation, and for both Follow lanes as a direct *input*. Regulated output — wrap it in the existing evidence, explanation and override machinery from day one | XL |
| 5.5 | **Drafting** — claims notes with the nine mandatory coverage-note sub-fields, emails to the loss adjuster, emails to reinsurers, next-step recommendations | Graph is already authenticated for reading; sending is a scope change, not a new integration. Every draft needs an approval gate | L |
| 5.6 | Automated outreach to the broker or lead where information is missing | Attachment 2, both Follow lanes. The trigger — a completeness gap with named missing fields — is the best-built part of the product | M |
| 5.7 | **Ship the lanes in autonomy order:** Lead >£1m/>£50k (assist only) → Lead <£50k (recommend, human approves) → Follow >£1m/>£50k (bot writes, human checks) → Follow <£50k (largely automated) | Do not invert this. The first lane lets the machine only synthesise and compare, which is both the highest value per claim and the lowest risk. The last two let the bot write before a human looks | XL |

---

## Parallel track — Multilingual · one quarter, alongside any phase

Four of five required languages are unaddressed and the failure is **silent**: a large model would partly cope with a German email by accident, while the deterministic fallback reader, the date parser, the classifier and the loss-type vocabulary would all fail quietly.

| Ref | What | Effort |
| --- | --- | --- |
| L.1 | Per-language vocabulary tables for the deterministic layers — labels, month and weekday names, day-parts, relative phrases, loss-type keywords, LoB keywords, declarations-page labels. **Start with German**: largest non-English AGCS region and the hardest date format | XL |
| L.2 | Multilingual OCR and multilingual extraction prompting (`ocr_language` is currently one global `"en"`) | M |
| L.3 | Typo and spelling-error flagging — named by criterion 1 and unbuilt | M |

Same shape as the existing per-LoB tables, so the pattern is established. Sequence this by which region goes live first: if Germany & Switzerland is in the first wave, it is not parallel, it is Phase 1.

---

## Deferred — real requirements, deliberately not sequenced above

| Ref | What | Why deferred |
| --- | --- | --- |
| D.1 | **Bordereau fan-out** ingestion — one file, N notices, row-level provenance | Both Follow lanes need it. New ingestion shape: every path today assumes one document set produces one notice |
| D.2 | **Payments automation** | In Attachment 2's admin-lane footnote, and the highest-risk automation in any of the three documents — it moves money. Needs authority limits (exist), sanctions clearance (does not) and four-eyes approval (does not) |
| D.3 | **AQS automation** | The ruleset is an Allianz artefact we do not hold |
| D.4 | **LIRMA / CLASS / ECF2** | UK & Ireland region and Marine/Aviation lines specifically |
| D.5 | **Multi-tenancy** for other Allianz operating entities | The RFP asks for it as onward scale, not as a launch requirement |
| D.6 | In-product **satisfaction survey** and feedback channel | Criterion 13 is scored at ≥4/5 and there is no instrument. Cheap — pull it forward if a survey date is set |
| D.7 | **Field Inspection** screen | Not requested by any of the three documents. Park it for this bid |
| D.8 | Re-target the **Connectors** catalogue from Guidewire / Duck Creek / Sapiens to the Allianz estate | Presentationally damaging as it stands: an Allianz evaluator sees a competitor's ecosystem, none of their own systems, and two of them falsely marked "live" |

---

## The shape of it

| | Duration | Cumulative | What you can claim at the end |
| --- | --- | --- | --- |
| **Phase 0** | 2–3 weeks | ~3 weeks | Measured accuracy, a real baseline, a live KPI screen, a compliant AI path, and a security evidence pack |
| **Phase 1** | 3–4 weeks | ~7 weeks | Works per market and per product. Recognisably built for AGCS rather than for a generic carrier |
| **Phase 2** | 4–6 weeks | ~13 weeks | Nothing arrives that we cannot read. Fragmented notifications become one claim |
| **Phase 3** | 6–10 weeks | ~23 weeks | **A complete answer to RFP Section 1 and the entire BRD MVP.** Data lands in CLAWS, documents in FileNet, parties screened |
| **Phase 4** | 4–6 weeks | ~29 weeks | Claims are segmented and the admin lane is automated for every one of them |
| **Phase 5** | 10–16 weeks | ~45 weeks | RFP Section 2: reserves recommended, reports compared, notes and emails drafted |

Multilingual runs alongside and adds a quarter of effort wherever it lands.

**Three things about these durations.** They assume the scope decision is made before Phase 4 and Attachment 1 arrives before Phase 1.3. They assume Allianz system access is granted before Phase 3, which is the most likely thing to slip and the least under our control. And they are engineering durations only — the two executive sign-offs in criteria 9 and 11 are a separate track that Phase 0.6 exists to feed, and that track should start in week one because it is the one nobody can accelerate later.
