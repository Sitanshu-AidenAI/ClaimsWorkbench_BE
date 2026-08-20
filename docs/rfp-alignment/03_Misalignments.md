# What Does Not Align

**Where Claims Workbench and its three source documents actively pull in different directions**

*This is deliberately not a gap list. Document 2 has the gap list — 131 requirements not fully met, most of them ordinary work. This document holds only the smaller, nastier set: places where something has been built in a way that **conflicts** with a requirement, where a missing thing is a **blocker** rather than a backlog item, or where the product would **look aligned and not be**. Twenty items, in four groups, worst first.*

*Assessed against all three documents: the RFP, Attachment 2 (`01042025_Claim Segmentations_LTP.pdf`) and our own BRD (`Claims Workbench_BRD.docx`). Group D holds the five items that only became visible when the latter two arrived.*

---

## The one-paragraph summary

The product reads claim documents extremely well and, in two respects — citation with a bounding box back to the source page, and business-editable field definitions — better than any of the three documents asks for. But it was built on the assumption that **it is the claims system**, whereas all three documents want **a component that feeds Allianz's claims system**. Almost every serious misalignment below descends from that one assumption. On top of it sit two hard blockers — a data-residency breach in the default configuration, and the fact that nothing in the product measures anything, which makes four of the thirteen acceptance criteria unprovable regardless of how well the system works.

And now a third: **our own BRD excludes from its MVP most of what RFP Section 2 and Attachment 2 require.** That is not an engineering problem. It is an unmade decision, and until it is made, roughly a sixth of the requirement matrix is speculative work. See M16.

| Group | Items | Character |
| --- | --- | --- |
| **A — Blockers** | M1–M5 | Would sink or badly damage a bid. Fix before any Allianz conversation. |
| **B — Wrong direction** | M6–M10 | Real work exists, pointed at a different customer than AGCS. |
| **C — Credibility risks** | M11–M15 | Would not fail a checklist, would fail a demo or a security review. |
| **D — Surfaced by Attachment 2 and the BRD** | M16–M20 | Invisible until those two documents arrived. One of them is now the single most consequential open decision. |

---

# Group A — Blockers

## M1 · The product is a system of record; the RFP is buying a pre-processor

**Severity: critical. This is the parent of most of the rest.**

**What the RFP wants.** Read the acceptance criteria together — criterion 4 ("the extracted information needs to be automatically transferred into CLAWS… copy/paste of all attributes… document upload into FileNet"), criterion 10 ("compatibility with CLAWS, FileNet, Global Genius"), criterion 9 ("no storage outside Allianz system landscape allowed") — and the intended shape is unambiguous. The vendor's product sits **in front of** CLAWS: it reads email, structures data, shows a human a review screen, and then **pushes into CLAWS and FileNet, which remain the systems of record.** The RFP's own architecture slide even offers that the review surface "as an option can be performed within Excel file." They are not asking for a claims application.

**What we built.** A claims application. `POST /api/v1/fnol/{reference}/create-claim` is documented as the front door of the claims book — *"every claim in this system is created by this endpoint, and nothing else creates one."* There is a `claims` table, a claims queue, a claim workbench, a claim lifecycle state machine, claim assignment, claim triage, claim audit. Documents live in this product's own object storage. Policies live in this product's own `policies` table. All of that is well-built, and all of it duplicates something AGCS already owns.

**Why it is a conflict, not a gap.** Adding a CLAWS adapter to a pre-processor is a project. Retrofitting one onto a system of record raises questions the RFP has already answered against us: which system holds the truth, what happens when they diverge, why is claim data being persisted here at all when criterion 9 forbids storage outside the Allianz landscape, and who reconciles the two claim references.

**What to do.**
- Reposition, in the proposal and in the product, as **an intake and assessment layer**. The claim record here becomes a *working copy* with an explicit CLAWS claim number, not a parallel book.
- `app/services/fnol/adapter.py` is already described in the codebase as *"the one module that knows both a dataset and a claim."* That is precisely the right seam. Add a second target behind it — a CLAWS field dictionary and a submission path — and the architecture survives the repositioning.
- Make persistence-of-claim-data a **configuration choice**, so a deployment can run in "pass-through" mode where the working copy is purged after successful CLAWS submission. This turns criterion 9 from an objection into a feature.

---

## M2 · The default configuration sends claim documents to a public AI API

**Severity: critical. Single hardest compliance blocker.**

**What the RFP says.** Criterion 9, Data Security, one line, no qualification: **"Data Deletion / Retention: no storage outside Allianz system landscape allowed."** And criterion 11 requires GDPR and EU AI Act compliance, signed off by AGCS-COO-IT *and* AGCS-CEO-Legal.

**What we built.** `app/core/config.py`:

```
class AISettings(BaseSettings):
    api_key: str | None = None
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-5.6-luna"
```

The default target is a public third-party API, and what gets sent is up to 60,000 characters of claim document text per call — loss narratives, named injured persons, addresses, financial figures. There is exactly one provider implementation (`OpenAIProvider`).

**The mitigating facts, stated fairly.** `base_url` is a setting, so the *code* can already point at an Allianz-hosted OpenAI-compatible endpoint. Every other stateful dependency — Postgres, Redis, MinIO/S3, Qdrant — is self-hostable and self-hosted in the compose file. The product runs end-to-end with no AI provider at all, on the deterministic readers in `app/domain/heuristics.py`. So this is a configuration and evidence problem more than a rewrite.

**Why it is still a blocker.** A configurable base URL is not an answer to a security review. What is missing is everything around it: no Azure AD / managed-identity authentication path (an Allianz-hosted Azure OpenAI endpoint will not take a bearer API key), no private-endpoint or VNet configuration, no provider abstraction for an internal AI gateway, no data-flow diagram, no statement of what is sent, no zero-retention attestation, no prompt/response logging policy. A COO-IT reviewer will ask for those documents, not for a `.env` line.

**What to do.**
1. Change the default `base_url` to unset, and **fail loudly** rather than silently defaulting to a public endpoint.
2. Add an Azure OpenAI provider with `DefaultAzureCredential` / managed identity, and a generic "internal gateway" provider.
3. Write the data-flow document: what leaves the process, to where, under what auth, retained for how long, and what the fallback is. Two pages. It is worth more in this bid than most features.
4. Add a hard configuration guard — `CWB_AI_ALLOW_PUBLIC_ENDPOINT=false` as the default — so a misconfiguration cannot become an incident.

---

## M3 · RFP Section 2 is effectively unbuilt — but it is now specified, which changes what to do about it

**Severity: critical. Character changed with Attachment 2.**

**The numbers.** Of the 43 requirements that touch Section 2 or come from Attachment 2, **32 are Not built**, and average coverage across the requirements the BRD places outside its MVP is **11%**. Of the nine capabilities the RFP names explicitly under "Reasoning Capabilities", **eight do not exist**:

| Section 2 capability | Status |
| --- | --- |
| Identify the claim segment and apply the matching automation lane | Not built |
| Provide reserve estimates | Not built |
| Synthesise the sources of uncertainty | Not built |
| Synthesise changes vs. previous report versions | Not built |
| Identify reinsurance / coinsurance | Not built |
| Recommend next steps / action plan | Not built |
| Draft claims notes | Not built |
| Draft email responses to stakeholders | Not built |
| Draft emails to reinsurers about changes | Not built |
| Archive documents to a filing and naming convention | Partial |
| Identify fraud risk | **Built** |
| Provide expert rationale behind decisions | **Built** |
| Knowledge graph | **Built** |

**What Attachment 2 changed.** Previously the largest problem here was that Section 2 had *no spine* — nothing in the product had a concept of "how much automation does this claim get", and the RFP's four lane names were too abstract to build against. That is no longer true. Attachment 2 specifies the model completely:

- Two axes, six cells: **Lead/Follow × >£1m / >£50k / <£50k**.
- **Admin lane automated for all six**, with the seven activities named in its footnote.
- Four loss-assessment lanes mapped to specific cells, each with its required bot behaviour spelled out.
- Thresholds explicitly **configurable** as business confidence grows.

So this is no longer an open question. It is a specified, bounded, sequenceable piece of work — which is exactly why it now belongs in a plan rather than in a list of unknowns. Document 4 sequences it as Phases 4 and 5.

**Three things that remain genuinely wrong.**

1. **Neither segmentation axis exists.** Lead/Follow is a policy attribute nobody models — and note it also appears in the RFP's own coverage-note fields as "Lead / Follow / Fronted", alongside "AGCS Share". The size band needs a claim value at intake, which the `estimated_loss` extraction field supplies and nothing bands. Both are small. Everything else in Section 2 waits behind them.
2. **The economics are still inverted.** Section 1 saves 20–40 minutes per claim. Section 2 saves 30 to over 1,000 minutes. Section 2 is where the money is and where we are weakest. Attachment 2 sharpens this: the *most* valuable segment (Lead >£1m) needs the *least* autonomy — the machine only synthesises and compares — so the highest-value work is also the safest to build first.
3. **There is still no entry point.** Mail intake turns *every* collected message into a *new* FNOL notice. There is no path to attach a loss adjuster's report on an open claim to that claim. Section 2 cannot begin. This is the same gap as the BRD's fragmented-FNOL requirement — see M18.

**The one under-read finding in Attachment 2.** The Admin lane is automated for **every** segment, and its footnote defines Admin as *FNOL, triage, saving a document in CLAWS, searching for relevant documents, payments, AQS and sanctions*. Three of those seven are built. Because this lane applies regardless of claim size or lead status, **it pays back on every claim rather than only on small ones** — which makes it a better first target than any single loss-assessment lane, and a much easier business case. Nothing in the RFP body makes this visible; only the slide does.

**The honest bright spot, restated.** The **reasoning engine** that does exist is deterministic by deliberate choice — completeness, severity, fraud, coverage, duplicates, catastrophe, triage, two policy matchers, all pure functions with per-signal explanations. `app/domain/matching.py` says why: *"it is arithmetic, it has to be reproducible, and an auditor has to be able to follow it."* That is a defensible position and the right foundation for an EU AI Act conversation. But it is a **different capability** from what Section 2 asks for, which is generative reasoning: estimate, compare, plan, draft. Do not conflate the two in a proposal.

**What to do.** The scope decision in M16 comes first — it determines whether any of this is in the bid at all. If Section 2 is in scope, build in autonomy order: assist-only (Lead >£1m), then recommend-and-approve (Lead <£50k), then write-with-check (Follow). Each step adds a safety requirement the previous one did not need, and the last two are unsafe without the material-wrong-decision detector in M16's wake.

---

## M4 · Nothing measures anything, and four acceptance criteria are therefore unprovable

**Severity: critical, and by far the cheapest to fix.**

Four of the thirteen acceptance criteria are *measurement* criteria. Not one can be answered today:

| Criterion | What must be proven | What exists |
| --- | --- | --- |
| 1 · Extraction accuracy | **95%** across ~180 CLAWS attributes | No field-level accuracy harness. Ground truth exists for *policy matching* (`case_data/_ground_truth/`, measured 18/20 rank-1) and for five fields; nothing for the rest. |
| 6 · Processing time | **AHT down ≥30%** within four months, and an **in-product KPI display** | No AHT concept, no human-time capture, no baseline. The analytics screen exists and is fixture data. |
| 7 · Data quality | The five null-rate KPIs' correct ratio **up 90%**, plus seven named NLP text-quality measures | No baseline, no KPI store. None of the seven NLP measures exists. |
| 8 / 5 · Reliability | **96% uptime/yr**, **99.9% API uptime**, **100 rps at 95% success**, **2s p95** | Prometheus HTTP instrumentation only. No SLO, no load test, no published figures. |

**Why this is worse than it sounds.** Every one of these criteria is phrased as an *improvement over a baseline*. A baseline can only be captured **before** deployment. Every week that passes without instrumentation is a week of baseline that cannot be recovered. And criterion 6 asks for the improvement to be *visible in the application* — the RFP's words are "the application should provide a visible representation of its performance against a defined set of KPIs."

**Why it is cheap.** Most of the raw material is already there. `fnol_cases` carries `received_at`, `processing_started_at`, `processing_completed_at`. `audit_events` timestamps all 28 write event types with an actor. `extracted_values` records `human_modified`, `original_value` and `modified_by` on every correction — which is per-field accuracy data, already accumulating, unread. And the Analytics screen is a finished, designed UI waiting for a service.

**What to do, in order.**
1. Build the **accuracy harness** first: a ground-truth set over the `case_data` packs at field level, scored per field per run, with the RFP's own scoring rule (a wrongly-formatted value counts as a miss, not partial credit).
2. Capture **AHT**: time-to-first-touch, time-on-review-screen, time-to-submit, and count of fields corrected. This is a handful of events.
3. Derive the **five data-quality KPIs** from the existing tables — they are all null-rate queries — and put them behind the Analytics screen.
4. Add the seven **NLP text-quality measures**. Self-contained, well-specified, and visibly answers a named criterion.

---

## M5 · English only, against five required languages

**Severity: high.**

The RFP requires English, German, Spanish, Italian and French. The product is monolingual, and not in a shallow way:

- The extraction system prompt is written in English and instructs in English.
- `app/domain/heuristics.py` — 483 lines of the deterministic fallback path — matches English labels (`"policy number"`, `"date of loss"`, `"insured"`).
- `app/domain/temporal.py` — 788 lines of date reasoning, the best-engineered module in the domain layer — knows English month names, English weekday names, English day-parts ("overnight", "morning"), English relative phrases ("last Friday", "three days ago").
- `app/domain/rules.py` `LOB_KEYWORDS` and `LOSS_TYPES` are English keyword sets.
- `ocr_language` defaults to `"en"`.
- `app/domain/policy_extraction.py` reads English declarations-page labels.

**Why it is a conflict.** A large language model would partly cope with a German email by accident. Everything *around* it would not: the fallback reader would return nothing, the date parser would misread `3. Mai 2026`, the classifier would fail to assign a line of business, and the loss-type vocabulary would reject valid values. The result is not graceful degradation — it is a system that appears to work and is quietly wrong on four of five required languages. Combined with M4 (no measurement), nobody would notice.

**What to do.** Treat language as a first-class dimension alongside region (M7). The deterministic layers need per-language vocabulary tables — which is the same shape as the existing per-LoB tables, so the pattern is established. Start with German: it is the largest non-English AGCS region and the hardest date format.

---

# Group B — Wrong direction

## M6 · The line-of-business portfolio is a retail carrier's, not AGCS's

**Severity: high. The clearest single piece of evidence that the product was designed for a different customer.**

| AGCS's eight lines (per the RFP) | In `LineOfBusiness`? |
| --- | --- |
| Property | ✅ `property` |
| Construction | ✅ `construction` |
| Liability | ✅ `liability` |
| Marine | ✅ `marine` |
| **Natural Resources** | ❌ |
| **Financial Lines** | ❌ |
| **Entertainment** | ❌ |
| **Aviation** | ❌ |

And the enum contains five lines AGCS does not name: `motor`, `casualty`, `workers_compensation`, `cyber`, `engineering`. Those are retail and mid-market lines.

**Why it matters beyond a missing enum value.** The line of business determines the required-field set (`LOB_REQUIRED_FIELDS`), the loss-type vocabulary (`LOSS_TYPES`), the classification keywords (`LOB_KEYWORDS`), the triage route and the specialist-line escalation. Four of AGCS's eight lines therefore have **no field set, no vocabulary and no routing**. Aviation and Marine also carry line-specific CLAWS fields the RFP names directly — "type of vessel", "shipment date" — which do not exist here.

**The upside.** Because these are configuration tables rather than code branches, adding four lines is genuine, bounded work rather than a redesign. But it must be done with AGCS's own vocabularies, which means Attachment 1.

## M7 · There is no concept of a region

**Severity: high. The parent of four other findings.**

The RFP names eight regions and requires the solution to "adapt to regional settings such as date formats, currencies, and time zones." The application has no region:

- `policies.country` and `policies.region` are columns on the *policy book*. Nothing else has a region — not a notice, not a claim, not a user, not a configuration.
- Date-format resolution has no authority to consult, so `03/04/2026` cannot be resolved regionally (M11).
- `SUPPORTED_CURRENCIES` is `{GBP, USD, EUR, SGD}` — four currencies for eight regions including Latin America.
- `timezone` is one global setting, `"UTC"`.
- `currency` defaults to `"GBP"` (M14).

**Why it is a direction problem.** Regionalisation is not a feature you add at the end; it is a dimension that field sets, vocabularies, formats and routing all hang off. Introducing it late means touching every one of them. It should be introduced now, alongside language (M5), as a single "locale" concept.

## M8 · Eight finished screens have no backend, and they look finished

**Severity: high — a demo and credibility risk more than a functional one.**

Thirteen of the frontend's twenty-one services run on `src/services/mockTransport.ts` — a shared fake transport with 220ms of deliberate latency "enough for a skeleton to be worth rendering". The screens behind them are complete, designed and typed against the real domain model. They are indistinguishable from working software:

| Screen | Backed by | Note |
| --- | --- | --- |
| Approval Queue | Fixture | Closest existing screen to Section 2's "edits and/or approves bot actions". |
| Field Inspection | Fixture | Not requested by this RFP at all. |
| Analytics | Fixture | The exact screen criterion 6 asks for. |
| Connectors | Fixture | And it names the wrong systems (M9). |
| Assistant (agent chat) | Fixture | A plausible home for Section 2 drafting. |
| Team / Handoff | Fixture | — |
| Admin: User Directory | Fixture | Authority limits are real on the backend; this screen does not read them. |
| Admin: Claims Rules | Fixture | — |
| Settings | Fixture | — |
| Claims Workbench | **Mixed** | Queue and detail are real; workbench sections and case documents are fixture. |
| Policies | **Mixed** | Wordings library is real; the structured policy book table is fixture. |

**Why this is a misalignment, not merely incomplete work.** Two of these — Approval Queue and Analytics — are *exactly* what two RFP criteria describe, and demoing them as capability would be a misrepresentation an evaluator's technical due diligence would find. The two **Mixed** screens are worse: half of one screen is real and half is fixture, so an honest demo requires knowing which panel is which. And Field Inspection is effort spent on something this RFP does not ask for.

**What to do.** Before any Allianz demo, produce an internal one-page "real / fixture" map — the table above — and agree a script that only touches real paths. Then decide which prototypes to back (Approval Queue and Analytics earn it; Field Inspection does not, for this bid).

## M9 · The connector catalogue targets the wrong systems

**Severity: medium, but symbolically bad.**

`src/mocks/connectors.ts` catalogues Guidewire ClaimCenter, Guidewire PolicyCenter, Duck Creek Claims, Sapiens IDIT, Majesco Core Suite, Outlook and Teams. Two are marked "live", the rest "available".

The RFP names **CLAWS, FileNet, Global Genius, LIRMA, CLASS, ECF2 and CARA**. None of them appears anywhere in either repository — the search returns zero hits across all of `app/` and `docs/`.

This is the M6 problem in a second place: the product was designed for a carrier running a modern packaged core system, and AGCS is not that carrier. It also has a presentational cost — an Allianz evaluator opening the Connectors page sees a competitor's ecosystem and none of their own systems, and two of them falsely marked live.

## M10 · Field scope is 38 attributes against roughly 180, and the missing ones are the hard ones

**Severity: high.**

`app/data/extraction_schemas/fnol_notice.json` configures **38 fields**. The RFP's process slide describes "~50-180 data field entries depending on LoB" across nine CLAWS categories, and criterion 1's 95% is measured against all of them.

The count is not the real problem — the engine is dataset-driven, so adding fields is rows in a table, which is a genuine strength. The real problem is that **four of the nine CLAWS categories are missing as concepts, not just as fields**:

| CLAWS category | Coverage | Why it is structural |
| --- | --- | --- |
| 4 · Select coverages | 10% | There is no `Coverage` entity. Coverage *assessment* produces a verdict; it does not select coverages. |
| 6 · Link parties to coverages | 0% | Depends on category 4. |
| 7 · Manage deductibles | 15% | No deductible type, flag, application or erosion tracking. |
| 9 · Movement type on reserve | 35% | No movement/sub-movement type, no original-vs-accounting currency pair. |

Those four categories are where CLAWS spends most of its 180 fields. Adding them is data modelling, not configuration.

**And a hard dependency:** none of this can be scoped without **Attachment 1 (`01042025_BMP_CLAWS_EFNOL.xlsx`)**, which is the definitive attribute list and the thing accuracy is measured against. Obtaining it should precede any further engineering against criterion 1.

---

# Group C — Credibility risks

## M11 · Images are accepted and silently unreadable

**Severity: medium-high, because the failure is silent.**

`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`, `.tif` and `.tiff` are all in `ALLOWED_EXTENSIONS`. They are validated by magic bytes, stored, and yield **no text**, because OCR is an outbound HTTP hook (`CWB_DOCINT_OCR_URL`) with no engine behind it and no default.

The scaffolding is genuinely good — image-only-page detection in the PDF reader, cost ceilings (`ocr_max_pages: 50`), a confidence floor, and `ocr_applied` / `ocr_confidence` / `ocr_reason` recorded per document so the *provenance* of an OCR'd value is honest. The engine is simply not chosen.

**Why it is a credibility risk.** The RFP names JPEG and PNG as required formats. A site manager photographing a completed claim form is a normal FNOL. Today that upload succeeds, extracts nothing, and the case reports low completeness — the user sees a system that "didn't find anything" rather than "couldn't read this". Combined with M4 (no measurement), the failure rate is invisible.

**And it is a residency decision, not just a build decision:** a cloud OCR API would breach criterion 9 exactly as M2 does. The engine has to be self-hostable.

## M12 · Sanctions screening does not exist, and it is a legal control

**Severity: high.**

CLAWS category 5 exists for one purpose: *"Add parties (for sanctions screening) — select the names of companies/parties from CARA sanctions check that match for: insured, broker, underwriter, internal claims handler, and add other third parties."*

The product captures the parties well — `fnol_parties` with normalised roles, and a `parties.people` extraction field covering claimants, third parties, witnesses, drivers, injured persons, authorities, surveyors and adjusters. It does not screen them, and there is no CARA client.

**Why it belongs in this document.** A missing feature is a backlog item. A missing sanctions check is a **regulatory control failure** — paying a sanctioned party is a criminal matter, not a service-level breach. An automated claims intake that fills CLAWS but skips screening is worse than manual entry, because it removes the human who would otherwise have looked at the names. Any proposal must state explicitly that screening remains a gated step.

## M13 · The EU AI Act evidence base is unusually strong; the EU AI Act artefacts do not exist

**Severity: high, and this is a *presentation* failure of real assets.**

The engineering that an AI Act assessment asks for is, unusually, already here:

- **Traceability** — every extracted value cites the document, the page, the passage, the quoted phrase and its character offsets; rectangles are resolved from the stored file's own word geometry, so a citation cannot be fabricated in a browser.
- **Explainability of derivation** — `inference_note` records *how* a value was reached: *"read 'overnight on Friday' as 1 May 2026 22:00, relative to the notification of 5 May 2026."*
- **Explainability of decisions** — policy identification returns a per-signal result with an explanation, warnings and a recommendation reason; triage, severity and completeness each return their weighted factors.
- **Determinism where it matters** — the scoring is arithmetic, explicitly so an auditor can follow it.
- **Human oversight** — every AI conclusion has a human override, and each override is audited with actor and reason.
- **Provider transparency** — a heuristic reading is stamped `provider="heuristic"` so it cannot be mistaken for a model's.
- **Graceful degradation** — the pipeline never fails; it raises typed exceptions an officer works from.

None of it is assembled into anything. There is no risk classification, no technical documentation file, no model card, no accuracy-and-robustness statement, no human-oversight design record, no post-market monitoring plan, no logging-for-traceability statement.

**Why it is a misalignment.** Criterion 11 requires EU AI Act compliance and criteria 9 and 11 both require sign-off by the COO-IT *and* the CEO-Legal. Those reviewers read documents. We have built the hard part and skipped the cheap part, which means we currently score zero on a criterion we could nearly top. This is the highest return-on-effort item in this entire document.

## M14 · Two defaults turn missing data into wrong data

**Severity: medium.**

Criterion 7 requires the currency attribute never to be null. The product satisfies this by defaulting: `claims.currency` defaults to `"GBP"`, and `normalisation.parse_currency(fallback="GBP")`.

That passes the KPI and defeats its purpose. For a German or Brazilian claim, a silent GBP is worse than a null — a null is visibly missing and gets fixed; a wrong currency propagates into a reserve, a reinsurance calculation and a management report. Criterion 1's own rule points the other way: *"Outputs deviating from predefined format standards must return 'no value' or 'unknown'."*

The same shape appears in `LineOfBusiness.UNKNOWN`, which is handled correctly — an explicit, honest, visible "we don't know". Currency should follow that pattern, with a per-region default once regions exist (M7).

## M15 · Read access is not audited

**Severity: medium.**

Criterion 9: *"All access **and** changes to claims data must be logged and auditable."*

The change half is done thoroughly — 28 typed event types covering every write, and audit rows deliberately carry no foreign key to the case so that deleting a notice cannot remove its trail. That last detail is genuinely thoughtful design.

The access half is absent. There are no read events. For claim data that contains named injured persons, medical detail and financial exposure, read auditing is normally a hard requirement in an insurer's own security standard, and "who looked at this claim" is a question a DPO will ask.

---

# Group D — Surfaced by Attachment 2 and the BRD

*Five items that were invisible until the two new documents arrived. The first is now the most consequential open decision in the programme.*

## M16 · Our own BRD excludes from its MVP most of what the RFP and Attachment 2 require

**Severity: critical. Not an engineering problem — an unmade decision, and it blocks a sixth of the matrix.**

**What the BRD says.** §4, "Out of Scope for Initial MVP", four items:

- Automated adjudication
- Automated claim settlement
- Post-claim lifecycle management
- **Fully autonomous claims processing without human validation**

**What the other two documents ask for.** Attachment 2's Follow < £50k cell: *"Largely automated with human check as needed."* Its Follow > £50k cell: the bot inputs the reserve, inputs the notes, and reaches out to the broker or lead — with a human check only *"for any material wrong decisions."* And RFP Section 2 in its entirety is post-claim lifecycle work: loss assessment happens after the claim exists.

These are not adjacent positions. Attachment 2's two Follow lanes are a precise description of the thing BRD §4 excludes.

**The measurement.** **33 of the 195 requirements sit outside the BRD's own declared MVP scope**, averaging **11%** coverage. Inside the MVP, 124 requirements average **58%** coverage. So the two readings of the same product give wildly different answers:

| Reading | Requirements | Coverage | What it implies |
| --- | --- | --- | --- |
| The BRD's MVP is the plan | 124 | **58%** | More than half built. A credible, deliverable Section 1 bid. |
| The RFP in full is the plan | 195 | **44%** | Two large unbuilt programmes, one of which nobody has staffed. |

**Why it is a misalignment rather than a gap.** A gap is work you have not done. This is two documents promising different things to different audiences. Three concrete risks:

1. **Diligence.** Proposing Section 2 to Allianz while an internal BRD says Section 2 is out of MVP scope is the kind of inconsistency a technical evaluation surfaces, and it costs more credibility than the missing feature would.
2. **Planning.** Phases 4 and 5 of the build plan are roughly six months of work that nobody has decided to fund, because the document that governs the roadmap excludes them.
3. **Safety.** BRD §4's exclusion is not arbitrary — "no fully autonomous processing without human validation" is a *sound* engineering position for a first release. Attachment 2's Follow lanes need a specific safety capability (M16's sibling, S.10 in the matrix: a material-wrong-decision detector) that nothing in any document except Attachment 2 even names. If Section 2 is taken on, that detector is not optional.

**What to do.** Make the choice explicitly, and write it down in one paragraph:

- **Option A — bid Section 1 only.** The RFP permits it: Section 1 is *"also a prerequisite for Section 2"*, so Section 1 alone is viable and Section 2 alone is not. The BRD stands as written. The story is coherent: "we do intake, to a very high standard, and we integrate." This is the honest position given the 58%/11% split.
- **Option B — bid both.** Then BRD §4 must be rewritten, Phases 4 and 5 must be funded, and the material-wrong-decision detector becomes a P0.

What must not happen is Option B in the proposal and Option A in the BRD.

## M17 · Password-protected attachments fail in a way nobody can diagnose

**Severity: high. BRD-only requirement, and its current failure mode is the worst kind.**

BRD §5.3 requires the product to identify password-protected emails and attachments, obtain the password through a configured mechanism, process the content, preserve the original email-to-attachment relationship, handle wrong or missing passwords through an exception workflow, and never persist the password in an audit record. The RFP does not mention any of this.

**What exists.** Nothing. There is no occurrence of password, encrypted or protected-content handling anywhere in `app/services/documents/`.

**Why it belongs here rather than in the gap list.** An encrypted PDF today is accepted by validation (the magic bytes are a valid PDF), stored, and then fails text extraction — and it is reported as *unreadable*, with no indication that a password is the cause. So the officer sees "we couldn't read this file", indistinguishable from a corrupt attachment, and the low completeness score that follows looks like a bad notification rather than a locked one. Combined with M4 (nothing measures anything), the rate at which this happens is invisible.

This is the same failure shape as M11 (images accepted and silently unreadable), and brokers send encrypted PDFs routinely — usually the ones containing personal-injury detail, which is to say the most sensitive and most important content in the file.

**And it needs a business decision before any code.** BRD §5.3 says "obtain the password through the appropriate configured mechanism" and does not say what that is. Standing per-broker passwords in a secrets store? An automated reply asking the sender? A shared convention? Nothing can be built until somebody chooses, and the choice has security implications either way. Ask now.

## M18 · Duplicate detection is built; fragmented-FNOL consolidation is not, and they have been conflated

**Severity: high. One work package serving two documents, and it is the largest BRD-only gap.**

**The distinction that matters.** Two questions look similar and have opposite correct answers:

| Question | Correct response | Status |
| --- | --- | --- |
| *"Is this the same claim arriving twice?"* — duplicate detection | Flag it, let a human dismiss or confirm | **Built, and well** |
| *"Are these several parts of one claim?"* — fragmentation | **Join them up** into one claim context | **Not built** |

**What is genuinely strong.** `app/services/fnol/matching.py` compares an incoming notice against both other notices (`kind="fnol"`) **and existing claims** (`kind="claim"`), scoring on policy number, insured, claimant, date of loss and free text with date decay. That satisfies BRD §5.6's duplicate requirement outright, including the "against existing claims" clause, which is the part most implementations miss.

**What is missing, and the detail that makes it sting.** `fnol_cases.thread_id` already captures the Microsoft Graph conversation id, and it is **indexed** — and **nothing reads it**. No query in either repository groups by `thread_id`. The key to the single most-requested BRD capability is sitting in the database, indexed, unused.

Beyond that: there is no parent/child or grouping relationship between notices, and no merge operation. `fnol_duplicate_candidates` is documented as *"never merged and never deleted automatically"*, which is correct for duplicates and exactly wrong for fragments.

**Why it is worth prioritising above its apparent size.** This one work package closes:

- BRD §5.5 fragmented-FNOL processing (five requirements, B.24–B.28)
- RFP Section 2's entry point — attaching a loss adjuster's report to an open claim (R2.1.01), without which nothing in Section 2 can start
- Part of the BRD's traceability requirement (§6.3)

**The hard part is the merge**, not the detection: it has to move documents, extracted values, passages, vectors and audit references between notices without losing provenance. The encouraging precedent is `app/services/fnol/deletion.py`, which already reasons correctly about every store a notice touches and answers with a receipt of what it removed and from where. A merge needs the same map, walked in the other direction.

## M19 · The BRD specifies Round Robin; the product implements best-fit

**Severity: medium. A specification mismatch rather than a quality problem.**

BRD §5.6 is specific: *"automatically assign claims to Claims Handlers using a configurable Round Robin approach"* — maintain the eligible pool, determine the next eligible handler, **assign sequentially using Round Robin logic**, honouring Market/OE, Product/LoB, eligibility, availability/status, the assignment queue, **working hours/time zone**, and temporary unavailability.

What exists is *best-fit* scoring: `app/domain/triage.py recommend_assignment` weights handlers on skills, lines of business, countries, severity ceiling, authority limit and open-claim capacity, returns weighted factors and a reasoning string, and treats `RECOMMENDED` as a real resting state so the engine proposes and a manager disposes.

**Best-fit is arguably the better algorithm** — it puts a marine claim in front of someone who knows marine — and it is not the one specified. Three of the BRD's parameters are also absent entirely: **working hours, time zone and dated exclusions**. That third gap is not cosmetic given the RFP's eight regions: an overnight European loss must not route to a sleeping desk, and `handlers.active` as a single boolean cannot express "on leave until Tuesday".

**What to do.** Do not replace best-fit. Ship Round Robin as a *selectable strategy* beside it — the codebase already carries an `AssignmentStrategy` enum, which suggests somebody anticipated exactly this — and add the availability model, which both algorithms need.

## M20 · The BRD is the plan of record and it omits half of what the solution will be graded on

**Severity: high, and it is a planning failure rather than a code failure.**

The BRD is an excellent capability document. It is not a complete requirements document, and if it is being used as the roadmap then a large, well-defined body of work has no owner.

**35 of the 195 requirements are real RFP requirements the BRD does not cover at all.** They average **22%** coverage — the lowest of any group in the matrix, which is what happens to work nobody is tracking. What is in there:

| Missing from the BRD | RFP criterion | Consequence of leaving it out |
| --- | --- | --- |
| The 95% extraction-accuracy target, and any way to measure it | 1 | The headline acceptance criterion has no owner |
| Five languages — English, German, Spanish, Italian, French | 1 | Four of five unaddressed, failing silently (M5) |
| Regional date formats, currencies, time zones | 1 | Wrong data rather than missing data (M14) |
| The 30% AHT reduction and its baseline | 6 | Unprovable, and the baseline is perishable |
| The in-product KPI display | 6 | A finished screen sits on fixture data |
| The five data-quality KPIs and the seven NLP text measures | 7 | Named, numbered, and unowned |
| 96%/99.9% uptime, 100 rps, 2s p95 | 5, 8 | No SLO, no load test |
| Business Continuity and Disaster Recovery plans | 8 | Required for the COO-IT sign-off |
| EU AI Act compliance | 11 | Required for the CEO-Legal sign-off |
| Data retention policies | 9, 11 | Erasure exists; scheduled expiry does not |
| The two named executive sign-offs | 9, 11 | These are the gates, not paperwork |
| Handler satisfaction survey at ≥4/5 | 13 | Scored, and there is no instrument |

**Why this is the same class of problem as M4.** M4 says nothing measures anything. M20 explains why: measurement is not in the document that drives the work. Fixing M4 without fixing M20 means it happens once and then rots.

**What to do.** Add a section to the BRD — "Non-functional and compliance requirements" — that names these twelve, with an owner each. It is a day's work and it is the difference between a capability document and a plan.

---

# Consolidated view

| # | Misalignment | Group | Severity | Fix shape | Effort |
| --- | --- | --- | --- | --- | --- |
| **M16** | Our BRD's MVP excludes what the RFP and Attachment 2 require | D | **Critical** | A written scope decision, not code | S |
| **M1** | System of record vs. pre-processor | A | Critical | Reposition + CLAWS adapter behind the existing seam | XL |
| **M2** | Public AI endpoint by default | A | Critical | Config guard + Azure/managed-identity provider + data-flow doc | L |
| **M3** | Section 2 unbuilt — now specified by Attachment 2 | A | Critical | Segment and lane model, then build in autonomy order | XL |
| **M4** | Nothing measures anything | A | Critical | Accuracy harness + AHT events + KPI service | L |
| **M5** | English only | A | High | Per-language vocabulary tables | XL |
| **M6** | Wrong line-of-business portfolio | B | High | Four AGCS lines + their vocabularies (needs Attachment 1) | L |
| **M7** | No region concept — the BRD calls it Market/OE | B | High | Introduce locale as a first-class dimension | L |
| **M8** | Eight fixture screens that look finished | B | High | Real/fixture map + demo script; back two, park the rest | M |
| **M9** | Connector catalogue names the wrong systems | B | Medium | Re-target to the Allianz estate | M |
| **M10** | 38 fields vs ~180; four CLAWS categories missing as concepts | B | High | Coverage/deductible/movement data model (needs Attachment 1) | XL |
| **M11** | Images accepted, silently unreadable | C | Medium-high | Choose and self-host an OCR engine | L |
| **M12** | No sanctions screening — and Attachment 2 puts it in the automated admin lane | C | High | CARA client + a gated screening step | L |
| **M13** | AI Act evidence exists, artefacts do not | C | High | Assemble the technical file from what is already built | M |
| **M14** | Defaults turn missing into wrong | C | Medium | Follow the `UNKNOWN` pattern; per-market defaults | S |
| **M15** | Read access not audited | C | Medium | Read events on the audit trail | M |
| **M17** | Password-protected attachments fail undiagnosably | D | High | Detection + password mechanism + decryption + exception | L |
| **M18** | Fragmentation conflated with duplication; `thread_id` indexed and unread | D | High | Group, present, merge — one package, two documents | L |
| **M19** | Round Robin specified, best-fit built | D | Medium | Round Robin as a selectable strategy + availability model | M |
| **M20** | The BRD omits half the grading criteria | D | High | Add a non-functional section with owners | S |

## If only six things get done

1. **M16 — make the scope decision.** One paragraph, and it determines whether six months of Phase 4 and 5 work is real or speculative. Nothing else on this list can be sequenced honestly until it is written down. Cost: a meeting.
2. **M4 — build the measurement layer.** Cheapest of the engineering items, unblocks four acceptance criteria, and every day without it destroys baseline that cannot be recovered.
3. **M13 — assemble the AI Act and security evidence pack.** The hard engineering is already done; this is writing. It converts a zero score into a near-top one, and it is what the two named executives will actually read.
4. **M2 — close the data-residency hole.** One line of default configuration is currently an objection that ends a conversation.
5. **M20 — give the BRD a non-functional section.** Otherwise M4 gets fixed once and then rots, because nothing in the plan of record asks for it.
6. **Get Attachment 1.** M6, M10, criterion 1 and three phases of the build plan cannot be scoped honestly without the CLAWS attribute list. This is a request, not a build. Send it first.

Note what moved: Attachment 2 is now in hand, so "get the attachments" has gone from two items to one — and Section 2 has moved from *unknown* to *specified but unfunded*, which is a much better problem.

## What to keep saying loudly

Four things the product does that are worth more than several things it does not — now including two the BRD asked for by name:

- **Per-value citation to the source page**, with rectangles resolved from the document's own word geometry. The RFP only asks that ambiguity be "highlighted". **The BRD's HITL section asks for exactly this mechanism — value, source, navigation to the location, a bounding box, body-vs-attachment, override, and AI-vs-manual distinction — and all seven exist.** This is the demo, and it is the AI Act answer.
- **Business-editable field definitions.** A new extraction field is a row in a table, with the description written in the vocabulary a document uses. RFP criterion 1 *advises* this; BRD §5.4 requires extraction to be scoped per Market/Product; the product is built on it. The only credible way to serve 64 market × product combinations.
- **Duplicate detection against existing claims, not just other notices.** BRD §5.6 asks for this specifically and it is the clause most implementations miss. Built, scored, presented rather than silently suppressed, and audited.
- **It works with no AI provider at all.** Deterministic readers take over and stamp everything `provider="heuristic"` so nothing can be mistaken for a model's reading. The answer to criterion 8's reliability and to a procurement officer's question about vendor lock-in.
