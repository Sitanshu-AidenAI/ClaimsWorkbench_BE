# Claims Workbench

What a handler does to a claim after intake has finished with it. Twenty-three writes
and one read: a note, a movement on the reserve, a decision, a coverage standpoint,
a party, two ends of a party-to-coverage link, an excess, seven against the field
inspection, four against the recovery register and four against the SIU case — and
the read that assembles the six tabs the workbench renders. Plus the manager's own
two reads, which have their own router.

This is the other side of a boundary. Everything under `app.services.fnol` works
a **notification**: reading it, scoring it, matching it to a policy, turning it
into a claim. Everything under `app.services.claims` works the **claim**.
`ClaimCreationService` deliberately stays on the intake side of that line — it is
the last act of intake rather than the first act of casework, and it belongs with
the notice it consumes.

It is also the **one place the boundary is crossed**, and it is worth knowing
where: creation calls `ClaimCoverageService.materialise` to give the new claim its
coverage sections, its parties and its excess. That dependency is optional and
defaulted to `None`, because it reaches forward into the claims module rather than
sideways within intake — and because the unit tests that predate coverage construct
the service without one. A claim created without it simply has no coverage rows.

---

## The honest part first

**All six sections are built.** For most of this module's life that sentence read
"two of the six are not", and the `available` flag is still on every section because
the distinction it draws — *empty* against *unmodelled* — is one this product will
need again. What has changed is that nothing answers false:

| Section | State | What is real |
| --- | --- | --- |
| **Activity log** | Real | The audit trail, grouped by which part of the claim each event belongs to. Nothing new is stored to produce it. |
| **Financials** | Real | The reserve ledger, the claim's own excesses with their erosion, the assigned handler's authority. Every figure traceable to a row. |
| **Field inspection** | Real | The visit commissioned, the adjuster instructed, when they attended, what they found priced element by element, and what is still owed. |
| **Assessment** | Real | Coverage sections with their standpoints and overrides, the parties on them, the exposure across them. **No priced damage breakdown** and no record of what the handler is waiting on. |
| **Fraud & SIU** | Real | The pipeline's indicators with a durable verdict against each, the SIU case with its own six-state machine, and other claims on the same policy. `inconsistencies` is the one list still empty. |
| **Recoveries** | Real | The register: what is expected back, what has arrived, who it is being pursued from, and when it is barred. |

Three sections moved out of the bottom group, and each made the same change. The
field inspection reports `available: true` on a claim nobody has sent an adjuster to;
the recovery register does on a claim with nothing to recover; the SIU case does on a
claim nobody has referred. In every case the empty answer is a **state this system
holds** rather than a gap in it — which is the difference between a tab explaining
its own absence and a tab offering *Commission a visit*, *Identify a recovery* or
*Refer to SIU*.

The assessment is the only section that still carries an `unavailable_reason` while
available, naming the priced damage breakdown it lacks rather than disowning the
whole tab.

Each section carries `available` and `unavailable_reason`. **`available` is not
"did the query succeed".** It says whether the *product* models this section yet,
which is a different question from whether the claim has any records — and the
screen needs both answers. "Nothing to recover" and "recoveries are not tracked
here" are different statements, and a tab that cannot tell them apart teaches a
handler to distrust the sections that are real.

The reason text is the server's, not the component's, so it stops being shown the
day the section stops being unbuilt rather than the day somebody remembers to
delete a string from a React file. The sentences live in one block at the top of
`app/services/claims/sections.py`.

**What the sections never do is invent a figure.** No default adjuster, no zero
excess standing in for an unknown one, no recovery opportunity nobody identified.
The temptation is to fill the gaps so the screen looks finished; the cost is that
a demo becomes a misrepresentation and the sections that *are* real lose their
credibility along with the ones that were faked.

---

## The reserve ledger

Two tables were added: `claim_notes` and `claim_reserve_movements`. The second is
where the load-bearing decisions are.

### A movement is a signed delta, not a new total

A reserve raised from £40,000 to £60,000 is **one row of `+2000000`**, and what
the claim currently holds is the *sum* of its rows.

```
claim_reserve_movements          held (indemnity)
  +4 000 000  "Adjuster's initial schedule"      4 000 000
  +2 000 000  "Revised after the survey"         6 000 000
    -500 000  "Strip-out scope reduced"          5 500 000
```

That is what makes the ledger the *explanation* of the figure rather than a log
sitting beside it. Storing new totals instead would make "what changed, and who
changed it" a diff between adjacent rows — which works right up until two
movements land in the same second.

`claims.reserve_minor` is a **cache** of that sum.
`app.domain.claim_lifecycle.held_by_movement_type` is its definition, and if the
two ever disagree the ledger is right. Only `ClaimCaseworkService.post_movement`
writes the column, and it does so in the same transaction as the row that
explains it.

A check constraint refuses `amount_minor = 0`: a movement that changes nothing
explains nothing, and a ledger that accepts them teaches its reader to skim.

### The currency pair is two columns

CLAWS requires every reserve in both the currency it was incurred in and the
currency the book is kept in, so `accounting_amount_minor` and
`accounting_currency` sit beside `amount_minor` and `currency`. A database
constraint requires them to be present or absent **together** — a converted figure
without its currency is not a figure, and a currency without an amount is not a
conversion.

Both are nullable, because the rate that relates them is a fact this system does
not hold. An absent accounting figure is honest; one converted at 1.0 would be a
fabrication that reconciles.

### `sub_movement_type` is a free column

CLAWS's second level of classification. Deliberately not an enum: the code list
lives in Attachment 1 (`01042025_BMP_CLAWS_EFNOL.xlsx`), which is not in hand, and
guessing at it would produce values that have to be migrated when it arrives.

### No status on a movement

A reserve movement is a book entry and takes effect when it is written. It is a
*payment* that waits for approval, and payments are not this table. A pending
reserve would be a figure that is neither held nor not held.

### Recoveries are tracked, not netted

A `recovery` movement is appended and audited but does **not** move
`reserve_minor`, and `claim_lifecycle.incurred_minor` excludes it. The reserve is
what the claim is expected to cost; money coming back is tracked against it. This
matters most at the authority check: netting an identified-but-unbanked recovery
off the incurred figure is how a claim slips under a limit it is genuinely over.

---

## Coverage, parties and the excess

CLAWS entry categories 4, 5, 6 and 7. Four tables and one rule module, and the
distinction that makes all of it coherent:

**`assess_coverage` answers "does this policy respond". `claim_coverages` answers
"which sections are we paying under".** Those are different questions and the
second had no home. The pipeline's coverage read produces one verdict with its
per-check working; category 4 asks a handler to *select* sections, and there was
nothing to select from. That is why this was the next slice rather than a nicer
version of the last one.

### Sections are proposed, never decided

At claim creation, `app.domain.coverage.propose_sections` turns the policy's named
perils into one row each, at a standpoint that reflects what the pipeline already
concluded. Everything about it is deliberately conservative:

- Every section lands **`in_question`** unless the policy is *bound* and its checks
  passed. A candidate the matcher ranked highly is not a contract.
- A **failed peril check never excludes a section.** A rule flagging a problem is a
  reason for a handler to look, not a refusal to pay — and a module that
  pre-declined sections would be a coverage decision engine, which this is not.
- A policy **out of force** puts every section in question, because a contract that
  was not running does not respond under any of its sections.
- The peril check is **one check about the cause of loss**, not one per section. So
  it can confirm the section matching the cause and has nothing to say about the
  others, which land in question with a note saying exactly that.

The point of proposing at all is that a handler who has to create eight rows by
hand creates none — and a claim with no coverage rows cannot answer category 4.

### `section_key` is derived, and that is what makes overrides survive

The key comes from the peril's own name, so re-proposing against the same policy
lands on the same rows. A generated id would make every re-proposal a fresh set of
sections and silently discard every position anybody had taken, which is the
failure mode that makes a "refresh from policy" button unusable.

`proposed_standpoint` sits beside `standpoint` for the same reason
`claim_triage.original_route` does: it is what makes an override legible as an
override rather than becoming the truth. **A reason is required only when the two
differ.** Agreeing with a proposal needs no justification; overruling one does.

### Exposure is not a total

`exposure_minor` sums only the **responding** sections — confirmed and
applies-to-limit — each capped at its own limit. Three rules worth stating because
each is expensive to get wrong:

- An **excluded** section contributes nothing. It will not pay.
- An **in-question** section contributes nothing *yet*. It is undecided, not a
  maximum, and summing it would report a ceiling as an expectation.
- A section with a limit and **nothing claimed contributes nothing**. A limit is a
  ceiling, not an estimate — treating it as one reserves every claim at policy
  maximum.

Sections claimed above their limit are **named** rather than silently capped, in
`over_limit_sections`: the arithmetic hides the difference and the difference is a
conversation with the insured.

### Parties are a second table

`fnol_parties` holds who a broker's email named. `claim_parties` holds who is on
the claim — a superset that grows after the notification closes and carries three
roles no email mentions: the **underwriter**, the **internal handler** and the
**loss adjuster**. All three are screened by CLAWS category 5, and
`parse_party_role` currently maps "loss_adjuster" to `other`, which loses the one
role Attachment 2's entire loss-assessment lane is about.

The copy happens once, at claim creation, and each row keeps `fnol_party_id` as
provenance. That column is **not a foreign key**: a notice can be deleted, and the
claim's record of who was involved has to survive it — the same guarantee
`audit_events` gets by carrying no key to the case it describes.

Widening the notice's role vocabulary instead was considered and rejected. It
would change what `parse_party_role` returns for an input it already handles, so
existing rows would read `other` where new ones read `loss_adjuster` — a silent
split in the data with no backfill to close it, to save one enum.

### The excess, and what erodes it

`claim_deductibles` carries the CLAWS type, the amount, the cap
(`maximum_applied_minor` — a percentage excess capped at a figure needs both
numbers) and `applied_minor`, which is **what has actually been taken off a
payment** rather than what is due. It stays zero until a payment carries it, which
is why the financials tab reports the excess as outstanding: there are no payments.

Two rules live in `app.domain.coverage` and not in a column:

**Only an aggregate erodes across the book.** For every other type the other
claims' figures are *ignored rather than summed*, because a per-claim excess starts
whole every time — and quietly netting other losses off it would understate what
the insured carries on this one. `applied_elsewhere` is reported separately so a
handler seeing a fraction of a large aggregate left can see that other losses ate
the rest, rather than being handed a figure they cannot account for.

**A franchise does not deduct.** It is a *threshold*: a loss below it pays nothing,
a loss above it pays in full with nothing taken off. Treating it as an ordinary
excess understates every settlement that clears it by the franchise amount, which
is the single most expensive misreading available in this module.

Claim creation types the copied policy excess as `per_claim` and **says so in the
row's comment**. The policy book records an amount and no type, so the alternative
is a null field CLAWS rejects — a stated default a handler can correct beats an
absent one nobody notices.

### What is still missing here

- **Sublimits cannot be proposed.** The policy book carries one overall limit and
  no schedule, so every section gets the policy limit and a sublimit is a handler's
  edit until Global Genius supplies a real one (`Phase 3.4`). The note on every
  proposed row says so.
- **Nothing screens a party.** Category 5 captures them and CARA does not exist
  (`Phase 3.5`). There is deliberately no `screened` field on the payload — a field
  reporting a check nobody runs would be the one lie on it.

---

## The field inspection

Three tables — `claim_inspections`, `claim_inspection_observations`,
`claim_inspection_actions` — and seven endpoints for one tab, which is more than
any other section needs. The reason is that an inspection is a **process** rather
than a record: it is instructed, booked, attended, reported on, argued with and
closed, by three different people over several weeks. Collapsing that into one
`PUT` would mean the trail could not say who booked the visit or when the report
came back, which is most of what the trail is for.

### It is the answer to a question the product could not answer

The tab used to render a sentence explaining that field inspections were not
recorded here. That was honest and it was also the wrong shape: on anything but the
smallest loss the reserve is a figure an adjuster measured on site, so a claims
workbench that cannot say who was instructed is missing the input to its own
financials. This is misalignment **D.7** in `docs/rfp-alignment/`, and it is now
closed.

### The status machine lives in one module and is *sent* to the client

`app.domain.inspection` holds the transitions, and `next_statuses` on the payload
is that machine, serialised. The tab's buttons are read off it rather than derived
in TypeScript, because a second copy of those rules in the frontend would drift
from this one within a release — and the failure mode is a screen offering a move
the API refuses.

Two edges are worth reading twice:

* `visit_booked → to_schedule` is a **rebooking**, and it has to exist because
  adjusters and insureds cancel. Without it a desk would either mark a missed visit
  attended or leave it booked for a date that has gone by.
* `more_needed → visit_booked` is the **second visit**, which is the normal outcome
  on a large loss rather than an exception.

### `scheduled_at` and `attended_at` are separate columns

A booked date that has passed is not attendance. Attendance is only legal from
`visit_booked`, so a missed visit is rebooked rather than inferred to have happened
— and every figure the tab derives from a visit is therefore derived from somebody
having said it took place.

Recording attendance again is a **correction**, not a second arrival. The evidence
counts land there and they arrive late: the adjuster says forty-one photographs on
the telephone and the report shows forty-four. An omitted count is left alone
rather than zeroed, so correcting the photographs does not silently wipe the
statements.

### An unpriced observation is not a zero

`quantified_minor` is nullable and the null case is the common one: an adjuster
prices what they can price on site and leaves the rest to a contractor's quote.
`app.domain.inspection.quantified_minor` sums only the rows that carry a figure and
`priced_count` reports how many did, so a handler sees that £180,000 is the sum of
two priced rows out of eleven rather than the assessed cost of the whole loss.

A check constraint requires the amount and its currency to be present or absent
**together**, for the reason the reserve's accounting pair does: a figure with no
currency is not an amount.

Elements recorded as `unaffected` are excluded from the sum even if they carry a
figure — which they should not, but a free-text-fed field eventually will.

### `completed` is not the same as finished

An accepted report and a finished job are different facts, and outstanding actions
survive the first. `is_settled` insists on both, and the tab counts open actions
separately from the status. `claim_inspection_actions.owner` is a **name** rather
than a key to `handlers`, because the owner is as often the adjuster, the insured or
a contractor as somebody on the desk.

### One inspection per claim, and the constraint says so

A unique index on `claim_id`. A large loss can carry two instructions — an adjuster
and then a forensic accountant — and this models the first as one record whose
status can go round the `more_needed → visit_booked` loop. Two concurrent experts
are out of scope, and the constraint is what makes that visible at the point
somebody tries rather than after they have made a mess of one row.

### The site defaults to the loss location and to nothing else

An adjuster needs an address to attend and the claim already holds where the loss
happened; making a desk retype it invites a typo on the one field the visit cannot
proceed without. The default is applied **server-side**, not pre-filled by the
client — a form that copied the address in would send the same string back as
though somebody had chosen it, which is how a stale address outlives a corrected
claim. Everything more specific (the unit, the gate code, who has the keys) is null
until somebody supplies it.

### Filing and accepting are two people's acts

`status = completed` is a **handler** accepting a report. `filed_at` is an
**adjuster** submitting one. Migration `0014` exists because 0013 modelled only the
first, and between them the report sits with the handler, unread — which is the one
distinction the adjuster's own queue is built to draw.

`filed_at` is cleared when a report is sent back, because it is then owed again.
`returned_at` is **never** cleared: "has this been sent back" has to outlive the
adjuster picking the work up, so a report returned last week and now back
`in_progress` still shows under the *Sent back* chip. Deriving that from
`status = more_needed` would empty the chip the moment work resumed, which is
exactly when a supervisor looks for it.

### The schedule is kept in one currency, and the total says which

`add_observation` refuses a currency the claim is not booked in, and
`app.domain.inspection.quantified_total` returns the sum **with the currency the
rows are in** — or nothing, when they disagree.

Both exist because the first version returned a bare integer and the caller stamped
the claim's booking currency on it. A GBP schedule on a claim booked in dollars was
reported as dollars: the same number, different money, and nothing on screen to say
so. Two currencies in one schedule make its total either a fabrication or an FX
conversion, and the rate that would do the second is a fact this system does not
hold — the same reason the reserve ledger's accounting pair is nullable.

---

## The loss adjuster's board

`GET /inspections` and the two routes beside it. The same `claim_inspections` rows,
read from the other side: a handler asks *what is happening on this claim*, an
adjuster asks *what have I been asked to go and look at*.

**This replaced a screen made of fixtures.** `src/features/inspection/` read
`MOCK_INSPECTIONS`, so a visit commissioned from the workbench appeared nowhere on
it — the two screens described unrelated worlds. That is misalignment **M8**, for
this board.

### Two of the six chips are not statuses

`sent_back` and `filed` are facts about the *report*; the other four are states of
the *visit*. An inspection returned last week and being worked again today is
`in_progress` **and** sent back, and a fifth status could not express both at once.
`app.domain.inspection.matches_chip` owns which rows fall under which chip, and the
frontend reads the answer rather than recomputing it.

### The counts come from the same read as the list

Six facet counts, four tiles and the footer's warning are all computed over the one
query the rows came from. Six `COUNT(*)` statements would each be true of a slightly
different instant, and a chip reading 3 above a list of 4 is the kind of defect
nobody reports and everybody stops trusting.

The two aggregates per row — what was priced, and how many actions are open — are
correlated scalar subqueries rather than joins. A join to the observations would
multiply the inspection row by its schedule and then need a `GROUP BY` over every
selected column of two tables.

### The board cannot yet be narrowed to one adjuster, and says so

`claim_inspections.adjuster_name` is a free-text **name**: a firm is instructed
before a person is named, and neither has an account here. So "commissioned to me"
can only be resolved by matching that string against the reader's own name, which
works for an internal adjuster and cannot work for Crawford & Co.

`mine` is therefore off by default and the payload reports `whole_desk`, which the
page states in the server's own words. A board that showed everybody's work under
the heading *commissioned to you* would misrepresent whose job each row is.

### What a full report carries and this one does not

`not_recorded` is a list of sentences, sent to the screen and drawn as its own tab.
Two tabs on that pane used to be *Coverage & risk* and *Recommendation*, and both
rendered fixtures — a liability finding nobody had made and a settlement figure
nobody had recommended, in the same type as the real fields beside them. What is
real is what the adjuster recorded on site.

### `INSPECTION_WORK_ROLES` is the first write a loss adjuster has

Every other claim write excludes them on the grounds that they read the file. That
was right while there was no inspection record and is wrong for this one surface:
the report is the adjuster's own work product, and a system where the handler types
it up on their behalf has an audit trail that attributes the adjuster's findings to
somebody else.

---

## The handler directory

Who *Assign a handler* is allowed to offer. `handlers` was seeded with eight
fictional colleagues at `@carrier.com`, which was fine while assignment was a
demonstration and became a fault the moment it had a consequence: nobody can sign in
as Amara Bello, so a claim assigned to her leaves the queue and arrives nowhere.

### Identity comes from the caller's own token

`HandlerDirectoryService.register` runs on the two paths that establish a session —
the password grant and the SSO callback — and writes the caller's `subject`, name and
address to the directory if they hold a role that can own a claim.

From the token rather than from the realm's user list, because the token is what the
identity provider has just asserted about them **and** because reading
`/admin/realms/{realm}/users` needs a `realm-management` grant this service account
does not hold and should not need. The trade is stated plainly: a handler who has
never signed in is not yet in the directory.

Not on `/auth/token`. A browser renews its token every few minutes, and a lookup
plus a commit per user per few minutes to record something that changes when a
person is renamed is not a trade worth making.

### What the token supplies and what it must never overwrite

Keycloak owns who a person *is*: subject, display name, address. The desk owns what
they may *do*: team, skills, lines, territories, severity ceiling, settlement
authority, capacity. A sign-in updates the first three and touches none of the rest,
or every login would quietly reset a manager's decision about somebody's authority.

**A new account arrives with no settlement authority at all.** A number would be a
figure nobody approved on the field that decides whether this person can release
money; null means *a manager has not said yet*, and `approval_blocks` already
refuses a settlement against an absent limit. So a new handler can be assigned work
immediately and cannot approve a settlement until somebody decides what they are
worth trusting with — which is the right way round.

### Adoption, not insertion

A seeded row may already carry a real colleague's address — the case of somebody
whose demo record predates their account — and `handlers.email` is unique, so
inserting would fail anyway. Claiming the row keeps their team, skills and
authority, which is the whole reason the seed had a row for them.

### `has_account` is on the wire because the consequence is invisible otherwise

A directory row without a `subject` is somebody with no way to sign in. Assigning to
one is still allowed — a desk does record work against a joiner — but the dialog
groups the roster in two and says which is which, because a handover to nobody looks
exactly like a handover.

One seeded row used to carry a `subject`: a UUID typed into `app/db/seed.py` rather
than one any realm had issued, which made `has_account` report that a fictional
colleague could be shown a claim. It is gone.

### "Assigned to me" resolves by account, not by name

`GET /claims?scope=assigned` filters through the claim's assignment to the directory
row carrying the reader's `sub`. It used to compare `Claim.handler_name` against
whatever the token called the reader — two strings nothing keeps in step — so a claim
assigned to a real person left the manager's queue and appeared on nobody's. That was
the second half of the same bug as the fictional roster, and neither half was visible
without the other being fixed.

---

## The recovery register

`claim_recoveries`, `claim_recovery_events`, `claim_recovery_tasks`. Migration
`0015`.

### Expected and recovered are two columns and never one

`expected_minor` is a judgement about the future; `recovered_minor` is a bank
statement. Storing their sum, or netting one into the other, would report a forecast
as an asset — which is the specific way a recovery ledger flatters a loss ratio.

`app.domain.recovery.expected_minor` reports what is **still outstanding**, so the
two figures cannot be added into a number that means nothing: a recovery expected at
40,000 with 30,000 banked has 10,000 outstanding, not 40,000.

### A recovery does not net off the reserve

`claim_lifecycle.incurred_minor` already excludes recovery movements and this service
adds none. The reserve is what the claim is expected to cost; money coming back is
tracked against it. Netting an identified-but-unbanked recovery in would shrink the
figure a handler is measured against on the strength of a solicitor's opinion — and
would do it at the authority check.

### `prospects` is nullable and the null is not nought

A recovery nobody has assessed and one judged hopeless are opposite facts. Storing
0.0 for both would put *Poor* against every subrogation the day it was opened.

### Limitation is a date, not a status

A subrogation barred by limitation is still `pursuing` until somebody writes it off:
the legal position and the desk's own view are different facts, and only one of them
is a decision anybody took. `limitation_warnings` on the section is the one thing on
this tab that costs real money by being missed.

### `recovered_minor` is a running total

A lower figure than the one recorded is refused. That is a correction, and a
correction to a money column belongs in a ledger rather than an overwrite — so until
recovery payments are their own rows, refusing is the safe half of the trade.

### `claim_recovery_tasks.recovery_id` is nullable

"Obtain the police report" is recovery work before anybody knows which recovery it
will support, and a task table that demanded a parent would push that into a note.

---

## The SIU case

`claim_siu_cases` and `claim_fraud_dispositions`. Migration `0015`.

### A referral is a person's act, and the flag is a machine's

`siu_status` used to be **derived from `claims.fraud_flag`**, which gave the screen
two of its six states and made *screening* mean nothing more than "the model was
suspicious". A case exists because a handler decided to refer, and only a person
moves it. The referral reason is required: a referral is read outside the claims desk
and is an accusation of sorts against the insured.

`screening` is now reachable honestly, and it is the state that matters most for
being reachable — it is the difference between the model having scored a claim and a
person examining it. `SCREENING` back to `NOT_REFERRED` exists so that screening a
suspicion and finding nothing in it does not force an accusation.

### A closed case can be reopened

New information arrives after a case closes more often than anybody would like, and a
second case for the same suspicion splits one story in half. `closed_no_action` and
`closed_confirmed` stay distinct, because they are opposite outcomes and collapsing
them makes the one statistic anybody asks about unanswerable.

### A disposition is per indicator, and its absence is a state

`claim_fraud_dispositions` is keyed on the indicator's **code**, not a row id: the
indicators are derived from the stored fraud analysis and have no rows of their own,
so a verdict has to attach to something stable. Unique per claim — one verdict per
indicator, replaced when somebody changes their mind, with the previous one kept in
the audit trail.

**This is what made verdicts survive a reload.** They were held in a working copy on
the client, so a handler who worked through eight indicators lost all eight by
refreshing — and the tab said so, which was honest and useless.

`FraudDisposition` has two values and no `undecided`: an indicator with no row has
not been looked at, and that absence is the third state. `undecided_indicators`
counts it, which is the honest replacement for a count of unresolved conflicts that
was always nought.

**`discounted` requires a note and `accepted` does not.** Accepting an indicator
agrees with the assessment; dismissing one is a person overruling a fraud signal, and
that is the decision somebody may be asked to justify.

---

## The manager's approval queue

`GET /approvals`, and it closed a loop that had been open since the workbench was
built. A handler could refer a claim, the claim moved to `escalated`, and the queue a
manager works read `MOCK_APPROVALS` — so the referral arrived **nowhere**. Both
halves of that exchange were real; only the screen joining them was not, which is the
most expensive kind of fixture: the one that makes a working feature look finished.

### It carries no decision endpoint

`POST /claims/{ref}/decision` already owns every transition and every block. A second
decision path would be a second set of rules to keep in step, which is the failure
`approval_blocks` exists to prevent. The sheet reads from `/approvals` and writes
there.

`RETURN_TO_HANDLER` was added for this board and is its own verb rather than a reuse
of `REQUEST_INFORMATION`: that one asks the broker for something, this one tells a
colleague to do more work, and a trail sharing one verb could not tell them apart.

### The conditions include the ones that passed

A list of only problems would leave a manager unable to tell a checked claim from an
unchecked one. "The fraud review is clear" is worth reading before signing a large
settlement, so `cleared` conditions are listed beside the `blocking` ones.

### `scope=my_team` needs the reader in the directory

Narrowing to a manager's own reporting line needs them in `handlers` with a team
recorded. A manager who is not gets the whole book and the payload says
`whole_book: true` — a queue that widened silently while calling itself *your
decision* would misstate whose work it is.

**Unassigned escalations appear under every scope.** They belong to nobody's team, and
hiding them would hide the claims that most need a decision: the ones with no handler
to make it.

### Each tile is summed within one currency

The queue holds claims booked in whatever the loss was in. The first version took the
currency of the first row and stamped it on the sum, so a month whose approvals were
dollars was reported in pounds. A tile that cannot be labelled truthfully reports how
many instead of how much — the third time this pattern has been needed, after the
inspection's schedule and the authority check.

---

## The decision

Five verbs, in `ClaimDecisionAction`, mapping onto `ClaimStatus`:

| Verb | Lands on | Closes the claim |
| --- | --- | --- |
| `approve_settlement` | `approved` | Yes |
| `decline` | `rejected` | Yes |
| `send_to_approval` | `escalated` | No |
| `refer_to_manager` | `escalated` | No |
| `request_information` | `in_review` | No |

Two of them share a status and are still two verbs — the first says "I have
finished and somebody senior must sign it", the second says "I cannot finish
this". They are told apart in the audit event's `context`, not by the status they
produce, which is why there is one `claim.decided` event type rather than five.

`request_information` is why `IN_REVIEW` lists **itself** as a legal transition,
which no other state does. Asking a broker for a missing schedule is a real
decision with a real audit event and it leaves the claim exactly where it was.
Modelling it as a no-op transition rather than as "a decision that sometimes does
not transition" is what keeps `status_for_decision` total.

### What blocks a settlement

`claim_lifecycle.approval_blocks` — and it is the *same function* the workbench
renders above the decision bar and the decision endpoint enforces. That is the
point of it existing at all: a screen listing three blockers and an endpoint
checking two produces the worst outcome available, which is a handler pressing a
button the screen said was safe and getting a 422 naming a reason they were never
shown.

Three blocks, phrased as imperatives because the list is read as a to-do:

1. `clear the fraud review flag`
2. `refer the settlement — it is above your authority`
3. `assign a handler`

Only the two **committing** decisions are gated. Asking for information or
referring upward is always available — gating those would trap a claim nobody can
move, which is exactly the claim that most needs referring.

Two properties worth stating because they are easy to lose:

**A limit only applies to somebody actually on the file.** `authority_limit_minor`
is ignored unless the assignment status is `ASSIGNED`. Passing the limit of
somebody who is not assigned cannot clear a block, and the unassigned check
catches the case where nobody is. `RECOMMENDED` is a resting state, not a handler.

**The ledger beats the cache.** The authority check re-reads the movements and
uses `incurred_minor`, not `claims.reserve_minor`. A claim reserved at £40,000
that has incurred £55,000 is over a £50,000 authority, and this is the one place
where trusting a stale cache lets a claim through a limit it is above.

A **blocked attempt is audited**, as `claim.decision_blocked`, rather than
silently refused. Somebody trying to approve a claim £300,000 over their authority
is a fact worth keeping.

### Reason

Required for `approve_settlement` and `decline`, enforced in
`ClaimDecisionRequest` rather than in the route so the rule appears in the API
documentation. A declined claim with no recorded reason is the one write on this
API a complaint would be built out of.

The frontend collects it in a dialog before sending — `useClaimDecision` plus
`DecisionReasonModal` — so a handler is asked rather than shown a 422 for a field
no control existed for. The confirm button restates the decision ("Decline the
claim") rather than saying "Confirm", because a dialog agreed to without
re-reading is the failure the whole flow exists to prevent.

---

## Notes

`claim_notes` is a **second** notes table. `fnol_notes` exists and stays.

An intake officer's note about a broker's email and a handler's note about an
adjuster's visit are records about different things at different stages, and one
table for both would put the first into a settlement audit.

`section` says which tab the note was written on — one table with a discriminator
rather than five, because the desk reads them as one chronology on the activity
log and as five conversations on the tabs, and the tab is a property of the note
rather than a different kind of thing.

The note's **body is not copied into the audit event**. The note is itself a
durable, attributed record, so duplicating the text would store the same sentence
twice and leave two places to redact it from if it ever has to be.

---

## The activity log

Nothing is stored to produce it. It is `audit_events` for the claim *and* its
notification — `AuditService.history` takes `related_ids` for exactly this — mapped
through two pure functions:

* `activity_category(event_type)` → which of nine chips the event sits under,
  grouped by the part of the claim rather than by who acted, because that is how a
  handler reads a log back ("what happened to the money").
* `is_material(event_type)` → whether it is highlighted. Deliberately a minority:
  a log where every line is material is a log nobody scans.

The category table is exhaustive over `AuditEventType` and a test keeps it so, but
`activity_category` still **falls back on the event type's prefix** rather than
raising. A new `fnol.*` event should appear in the log under a reasonable heading,
not break the tab that renders it.

`detail` is the audit event's own `before`/`after` diff rendered as a sentence —
`reserve minor 4000000 → 6000000` — rather than raw JSON. Those columns hold only
the fields that moved, so the diff *is* the whole story and the screen should not
have to parse a payload to tell it.

---

## Endpoints

| Method | Path | Access | Notes |
| --- | --- | --- | --- |
| `GET` | `/claims/{reference}/sections` | `FNOL_READ_ROLES` | The six tabs, one read |
| `POST` | `/claims/{reference}/notes` | `CLAIM_WORK_ROLES` | 201, returns the note |
| `POST` | `/claims/{reference}/reserve` | `CLAIM_WORK_ROLES` | Returns the **recomputed financials section**, not the movement |
| `POST` | `/claims/{reference}/decision` | `CLAIM_WORK_ROLES` | Returns the updated `ClaimDetail` |
| `POST` | `/claims/{reference}/coverages/{section_key}` | `CLAIM_WORK_ROLES` | Category 4. Returns the **assessment section** |
| `POST` | `/claims/{reference}/parties` | `CLAIM_WORK_ROLES` | Category 5. 201, returns the party |
| `POST` | `/claims/{reference}/coverages/{section_key}/parties` | `CLAIM_WORK_ROLES` | Category 6. Returns the assessment |
| `DELETE` | `/claims/{reference}/coverages/{section_key}/parties/{party_id}` | `CLAIM_WORK_ROLES` | Returns the assessment |
| `POST` | `/claims/{reference}/deductible` | `CLAIM_WORK_ROLES` | Category 7. Returns the **financials section** |
| `POST` | `/claims/{reference}/inspection` | `CLAIM_WORK_ROLES` | Commission a visit. 201. Returns the **inspection section** |
| `PATCH` | `/claims/{reference}/inspection/schedule` | `CLAIM_WORK_ROLES` | Book, move, or arrange a second visit |
| `POST` | `/claims/{reference}/inspection/attendance` | `CLAIM_WORK_ROLES` | The adjuster went. A repeat is a correction |
| `PATCH` | `/claims/{reference}/inspection/status` | `CLAIM_WORK_ROLES` | Accept the report, or send it back with a reason |
| `POST` | `/claims/{reference}/inspection/observations` | `CLAIM_WORK_ROLES` | One element and how it came off. 201 |
| `POST` | `/claims/{reference}/inspection/actions` | `CLAIM_WORK_ROLES` | Raise a follow-up. 201 |
| `PATCH` | `/claims/{reference}/inspection/actions/{action_id}` | `CLAIM_WORK_ROLES` | Tick or untick one follow-up |
| `POST` | `/claims/{reference}/recoveries` | `CLAIM_WORK_ROLES` | Identify one. 201. Returns the **register** |
| `PATCH` | `/claims/{reference}/recoveries/{recovery_id}` | `CLAIM_WORK_ROLES` | Move it on, or bank what came back |
| `POST` | `/claims/{reference}/recoveries/tasks` | `CLAIM_WORK_ROLES` | Raise recovery work. 201 |
| `PATCH` | `/claims/{reference}/recoveries/tasks/{task_id}` | `CLAIM_WORK_ROLES` | Tick or untick one |
| `POST` | `/claims/{reference}/siu/referral` | `CLAIM_WORK_ROLES` | Refer to SIU. Reason required. 201 |
| `POST` | `/claims/{reference}/siu/screening` | `CLAIM_WORK_ROLES` | Somebody is looking, nobody accused. 201 |
| `PATCH` | `/claims/{reference}/siu` | `CLAIM_WORK_ROLES` | Move the investigation, and close it |
| `POST` | `/claims/{reference}/fraud/dispositions` | `CLAIM_WORK_ROLES` | Accept or discount one indicator |
| `GET` | `/approvals` | `CLAIM_ASSIGN_ROLES` | The manager's queue. `scope`, `chip` |
| `GET` | `/approvals/{reference}` | `CLAIM_ASSIGN_ROLES` | One approval sheet |
| `GET` | `/inspections` | `FNOL_READ_ROLES` | The adjuster's board. `chip`, `mine` |
| `GET` | `/inspections/{reference}` | `FNOL_READ_ROLES` | One report, by the **claim's** reference |
| `POST` | `/inspections/{reference}/filing` | `INSPECTION_WORK_ROLES` | The adjuster files it. Refused with the blockers named |

`CLAIM_WORK_ROLES` is `claims-handler`, `claims-manager`, `claims-admin`. Intake
officers are not in it — an officer's job ends when the notice becomes a claim —
and neither is `loss-adjuster`, who reads the file rather than writing to it.

**A settlement is not gated by role.** It is gated by the assigned handler's
authority limit, which is how a claims desk actually works: seniority is a number
on a person, not a realm role, and a manager with no authority on the file has no
more business approving it than the handler does.

`POST /reserve` returns the whole financials section rather than the row it wrote
because the caller wants to redraw the tab, and the tab needs the recomputed held
figures, the reordered transaction list and the re-derived authority position.
Returning the single row would make the client compute all three and get one of
them wrong.

Every inspection write returns the whole rebuilt section for the same reason: a
booking moves the status, the tiles **and** `next_statuses`, and a client deriving
any of the three would be the second implementation of a rule that has one correct
answer.

---

## Where the code is

| Concern | Module |
| --- | --- |
| Transitions, decisions, blocks, activity mapping, ledger arithmetic | `app/domain/claim_lifecycle.py` — pure, no session, no clock |
| Proposing sections, exposure, erosion, the franchise rule | `app/domain/coverage.py` — pure, no session, no clock |
| The visit's transitions and the arithmetic over what it found | `app/domain/inspection.py` — pure, no session, no clock |
| Notes, reserve movements, decisions | `app/services/claims/casework.py` |
| Sections, parties, links, the excess | `app/services/claims/coverage.py` |
| Commissioning, booking, attendance, findings, follow-ups | `app/services/claims/inspection.py` |
| The adjuster's queue, one report, and filing it | `app/services/claims/inspection_queue.py` |
| The recovery register's arithmetic and machine | `app/domain/recovery.py` — pure, no session, no clock |
| The SIU case's machine and per-indicator verdicts | `app/domain/siu.py` — pure, no session, no clock |
| Opening, progressing and tasking recoveries | `app/services/claims/recoveries.py` |
| Referring, investigating, and disposing of indicators | `app/services/claims/siu.py` |
| The manager's queue and one approval sheet | `app/services/claims/approvals.py` |
| Keeping the handler directory made of real people | `app/services/handlers/directory.py` |
| The six-section assembly | `app/services/claims/sections.py` |
| Tables | `app/models/claim.py`, migrations `0011_claim_casework` through `0015_recoveries_and_siu` |
| Request/response shapes | `app/schemas/claims.py`, `app/schemas/claim_sections.py` |
| Routes | `app/api/v1/routes/claims.py` |

`claim_lifecycle.py` is deliberately the mirror of `lifecycle.py`, which is the
notification's equivalent: the same jobs, stated the same way, so a reader who
knows one knows the other. It is pure for the reason `matching.py` gives — it is
arithmetic and rules, it has to be reproducible, and an auditor has to be able to
follow it.

Nothing in `casework.py` commits. The route owns the transaction, for the reason
`FNOLContext.commit` states: a decision that transitioned the claim but failed to
audit is worse than a decision that did not happen.

---

## What this does and does not answer in the RFP

Honest scoring, because the alignment pack in `docs/rfp-alignment/` is specific
about this module and not flattering to it.

**Advanced by this work:**

* `R1.9.04` — "all access **and** changes to claims data logged and auditable".
  The changes half gains fourteen claim event types; the access half is still
  absent.
* `B.48` — end-to-end traceability. The activity log is the surface that makes the
  existing trail readable as one narrative.
* CLAWS category 9 (triage and segmentation, previously 35%) — movement type,
  sub-movement type and the dual-currency pair now exist as data model.

* CLAWS category 4 (select coverages, was 10%) — a `Coverage` entity exists, is
  proposed from the policy, and carries a standpoint a handler owns.
* CLAWS category 6 (link parties to coverages, was 0%) — the link exists in both
  directions, audited.
* CLAWS category 7 (manage deductibles, was 15%) — type, cap, application and
  erosion all exist. What is missing is a payment to apply one to.
* `B.31` (infer applicable coverage) and `B.33` (infer applicable limits) — the
  inference is the pipeline's and unchanged; what is new is that its conclusion
  lands on records a person can correct, which is what those requirements are
  about.

* Misalignment **D.7** (the field inspection screen was parked) — closed. The
  visit, the adjuster, the priced schedule of damage and the outstanding follow-ups
  are records with a state machine and an audit trail behind them, and the screen
  leads with commissioning one rather than with a sentence about its own absence.
* Misalignment **M8** (fixture screens), for the field-inspection board, the
  assignment dialog and the **manager's approval queue**. All three read the database
  now. The approval queue was the worst of them: a handler could refer a claim and it
  appeared on no manager's screen. The adjuster's queue was
  `MOCK_INSPECTIONS` and the roster was eight fictional colleagues, and in both
  cases the fixture was not merely cosmetic: a visit commissioned from the workbench
  appeared on no board, and a claim assigned to somebody reached no queue.
* `R1.9.04` again — the trail gains `claim.inspection_filed`, which is the one event
  on this record written by somebody outside the desk.

**Not advanced, and named so nobody assumes otherwise:**

* Attachment 2's subrogation requirement — **partly**. The register a handler works
  is real; *detecting* an opportunity automatically is not, so `no_recovery_reason`
  stays null rather than asserting that a claim has nothing worth pursuing.
* `inconsistencies` on the fraud tab is still empty. Nothing cross-checks two records
  against each other server-side, though the document review tab does compare
  extracted values against the claim — so the ingredients exist and the stored
  comparison does not.
* CLAWS category 5 (parties for sanctions screening, 30%) — parties are captured
  and **nothing is screened**. CARA does not exist (`Phase 3.5`), and this slice
  deliberately adds no field that would imply otherwise.
* `B.32` (infer the attachment point / layer) — needs Lead/Follow and AGCS share
  on the policy book, which is `Phase 4.1`.
* Sublimits per section — needs a real schedule from Global Genius (`Phase 3.4`).
* `R2.1.06` / `R2.2.18` — claim and claimant history, deductible erosion. The
  fraud tab's prior-claims list is a first step and is not the history view
  Attachment 2 asks for.
* The adjuster's board **cannot be narrowed to one adjuster**, for the same reason:
  the adjuster is a name and not an account. `mine` exists and only resolves for an
  internal adjuster whose name matches their account's exactly.
* The handler directory holds only people who have **signed in at least once**.
  Reading the realm's user list would fix that and needs a `realm-management` grant
  on the Keycloak service account that nobody has made.
* The adjuster is a **name on a row**, not a party on the claim and not a record in
  a panel of firms. Instructing Crawford & Co here does not tell Crawford & Co
  anything: there is no outbound instruction, no acknowledgement, and no portal.
  What exists is the desk's own record of what it asked for and what came back.
* The photograph, measurement and statement counts are **counts**. The files
  themselves are on the notice or in FileNet, and this holds how much evidence
  exists rather than the evidence.
* Nothing here writes to CLAWS. This is a working copy, which is misalignment
  **M1** and the reason `Phase 3.1`'s field dictionary belongs behind
  `app/services/fnol/adapter.py` rather than here.

**Built to a CLAWS shape on purpose.** The reserve ledger uses movement /
sub-movement type and the dual currency pair rather than the frontend fixture's
bespoke `TransactionKind`, so that when Attachment 1 arrives this is a mapping
exercise and not a rewrite. That was the one design choice in this slice with a
material cost attached to getting it wrong.
