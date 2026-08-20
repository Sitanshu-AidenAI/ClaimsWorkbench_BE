# The Requirements, Explained From Scratch

**A plain-English reading of all three source documents**

| # | Document | What it is |
| --- | --- | --- |
| 1 | `Claims_RP_RFP_Scope_Description_Final.docx` | The Allianz AGCS request for proposal. The commercial ask. |
| 2 | `01042025_Claim Segmentations_LTP.pdf` | Attachment 2 to the RFP. One slide, and it turns Section 2 from vague into buildable. |
| 3 | `Claims Workbench_BRD.docx` | Our own Business Requirements Document. Adds requirements the RFP never mentions, and excludes some the RFP insists on. |

*Written for someone who has never worked in insurance. No jargon is used before it is explained. If you read only Part 0, Part 5 and Part 8, you will still know what matters.*

---

## Part 0 — The sixty-second version

A very large corporate insurer called **AGCS** (Allianz Global Corporate & Specialty) handles insurance claims from big companies — factories, shipping fleets, construction sites, film productions, oil terminals. Two parts of that work are painfully manual, and they are asking vendors to automate them with AI.

**Problem 1 — the front door.** When something bad happens to a customer, the news arrives as an **email** from an insurance broker, with attachments. A human reads that email, understands it, and then re-types roughly **180 pieces of information** into Allianz's claims system by hand. It takes **20–40 minutes per claim**, and the data quality is poor. They want AI to read the email and the attachments, structure the information, show it to a human for a quick check, and then push it into the system automatically.

**Problem 2 — the middle of the claim.** Once a claim is open, experts called **loss adjusters** send in long reports (5–15 pages, sometimes far more). A human reads each one, works out what changed since the last report, decides how much money to set aside, writes notes, replies to the adjuster, and updates the system. This takes anywhere from **30 minutes to over 1,000 minutes** per claim. They want AI to read those reports, compare them against previous versions, recommend the money figure, draft the notes and the emails, and let the human simply approve or edit.

**The prize.** Faster claims, happier staff, happier customers, and — the part they care about most strategically — **clean data** they can use to price insurance better in future.

**The ask.** One vendor for both problems, or several vendors who co-operate. Two-week release cycles. Must work in **8 regions**, across **8 lines of business**, in **5 languages**. Must plug into Allianz's existing (old) systems. Must never store their data outside Allianz's own infrastructure.

**What Attachment 2 adds.** Problem 2 is not one process — it is **six**. Claims are sorted by whether Allianz *leads* the policy or *follows* another insurer, and by size (over £1m, over £50k, under £50k). Each of the six cells gets a different amount of automation, from "the machine helps and the person decides everything" up to "the machine does it and somebody checks if they feel like it." One thing is automated for *all six*: the routine paperwork.

**What our own BRD adds.** Four capability areas the RFP never mentions: classifying which **country business** a claim belongs to, opening **password-protected** emails, recognising when **five separate emails are all one claim**, and assigning work by **taking turns** rather than by best fit. It also draws an MVP line that excludes most of Problem 2 — which is a decision somebody needs to make on purpose rather than by accident.

---

## Part 1 — Who is asking, and what do they actually do all day?

### Who is AGCS?

Allianz is one of the world's largest insurance groups. **AGCS** is the arm that insures *large corporations* rather than individuals — no car insurance for you and me, but insurance for a container ship, a chemical plant, a stadium, a Hollywood production, an offshore rig.

This matters for one reason: **their claims are big, rare, complicated, and heavily documented.** A household insurer processes millions of small, near-identical claims. AGCS processes far fewer claims, each one a bespoke package of surveyor reports, legal letters, engineering assessments and photographs. Automation therefore cannot be "match this claim to a template" — it has to *read*.

### Who are the people in the story?

| Person | What they do | Why you should care |
| --- | --- | --- |
| **The insured** | The company that bought the insurance and suffered the loss. | Their money is at stake. They rarely contact the insurer directly. |
| **The broker** | A middleman firm that arranged the insurance and reports the claim on the customer's behalf. | **This is who emails Allianz.** Almost every claim arrives as a broker's email, in the broker's own words and format. |
| **The FNOL team / claims handler** | The Allianz staff who receive that email and open the claim on the system. | These are the people whose 20–40 minutes you are trying to save. |
| **The loss adjuster** | An independent expert Allianz sends to inspect the damage and write a report. | Their reports are the input to Problem 2. |
| **The underwriter** | The person who decided to sell the insurance and at what price. | They want the clean data at the end. |
| **The reinsurer / coinsurer** | Other insurers who share the risk with Allianz. | They must be told when a big claim happens. This is a legal and financial obligation. |

### The one-paragraph version of how insurance claims work

A company pays a **premium** for a **policy** — a contract that promises to pay out if certain bad things happen. When a bad thing happens, that is a **loss**, and telling the insurer about it is the **First Notice of Loss** (**FNOL**). The insurer must then answer four questions in order: *(1) Which policy is this?* *(2) Is this thing actually covered?* *(3) How much will it cost us?* *(4) Who pays what?* Everything in this RFP is machinery for answering those four questions faster and more consistently.

---

## Part 2 — The decoder ring

Every term in the RFP that a newcomer would trip over.

### Insurance vocabulary

| Term | Plain English |
| --- | --- |
| **FNOL** | *First Notice of Loss.* The very first message telling the insurer that something happened. "e-FNOL" just means an electronic, automated version of it. |
| **LoB** (Line of Business) | The category of insurance. AGCS names eight: Property, Natural Resources, Construction, Liability, Financial Lines, Marine, Entertainment, Aviation. Each has different questions to ask and different data to capture. |
| **SUBLOB** | A finer sub-category inside a line of business. |
| **Cause of loss / peril** | *What went wrong* — fire, flood, theft, collapse, a defective weld. The single most important field for analysis, and the one most often recorded badly today. |
| **Date of loss** | *When it went wrong.* Not the date somebody noticed, not the date somebody emailed — the date the damage actually happened. Subtly hard to extract, because emails often only state one of the other two. |
| **Loss location** | *Where it went wrong.* Which can differ from the insured's head office, and from the "risk address" on the policy. |
| **Reserve** | Money the insurer sets aside on its books as its best estimate of what this claim will eventually cost. Setting reserves is a regulated, consequential act — set them too low and the insurer's accounts are wrong. |
| **Movement type** | The accounting label for a reserve change: indemnity (money to the customer), expense (money to lawyers/adjusters), recoveries (money coming back). |
| **Deductible** | The first slice of a loss the customer pays themselves. |
| **Limit / sublimit** | The maximum the policy will pay, overall or for a specific thing. |
| **Coverage** | A specific promise inside a policy. One policy contains many. A claim must be attached to the right ones. |
| **Endorsement** | An amendment bolted onto a policy after it was written. |
| **Suffix** | Allianz's internal word for a sub-record on a claim. Ignore the word; treat "suffix details" as "the claim's detail fields". |
| **Triage** | Deciding how urgent and how complex a claim is, and therefore who should handle it. |
| **Segmentation** | Sorting claims into pre-defined buckets (by line of business, size, complexity) so each bucket can be processed differently. |
| **Subrogation** | Chasing a third party who actually caused the loss to get the money back. Spotting the opportunity early is worth real money. |
| **Reinsurance / coinsurance** | Other insurers sharing the risk. Reinsurance = Allianz bought cover for itself. Coinsurance = several insurers wrote the policy together. Both must be identified and notified. |
| **Lead / Follow** | Whether Allianz is the insurer *in charge* of a shared policy or one of the followers. Changes who does the work and who makes decisions. |
| **Sanctions screening** | A legal requirement to check every party named on a claim against government watchlists before paying anyone. Getting this wrong is a criminal matter, not a service failure. |
| **AHT** | *Average Handling Time.* Minutes of human effort per claim. The headline number this whole RFP is trying to reduce. |
| **NPS** | *Net Promoter Score.* A customer-satisfaction metric. |
| **Bordereau** | A spreadsheet listing *many* claims at once, sent by a broker or a lead insurer. Attachment 2 requires reading these on Follow business — which is a different problem from reading one claim's documents, because one file becomes many claims. |
| **Line size** | How big a share of the risk Allianz took. A 5% share of a £20m loss and a 100% share of a £1m loss cost the same, but need very different levels of attention. Attachment 2 uses it to decide review depth. |
| **Attachment point / layer** | Large risks are insured in stacked slices. The "attachment point" is the loss level at which a particular slice starts paying. Our BRD requires inferring this; the RFP does not mention it. |
| **AQS** | An Allianz internal quality-standards check. Named in Attachment 2's footnote as something to automate. The ruleset itself is an Allianz document we do not have. |
| **Market / OE** | *Operating Entity.* Which Allianz country business owns the claim. Our BRD makes this a first-class thing to classify; the RFP calls the same idea a "region" and names eight of them. |
| **Fronted** | Allianz's name is on the policy but another insurer carries the risk. Appears alongside Lead/Follow in the RFP's coverage-note fields. |

### Allianz's own systems (all of them named in the RFP)

| System | What it is | Why it appears |
| --- | --- | --- |
| **CLAWS** | **Cla**ims **W**orkflow **S**ystem — Allianz's core claims application. The system of record. **This is where the extracted data must end up.** Roughly 180 fields across 9 entry screens. |
| **FileNet** | Allianz's document archive (IBM FileNet). Every attachment must be filed here. |
| **Global Genius** | Allianz's policy administration system — the master list of which policies exist. Used to look up "which policy is this claim against?" |
| **LIRMA / CLASS / ECF2** | Shared London insurance-market systems for claims that involve Lloyd's and the London market. |
| **CARA** | The sanctions-screening tool. |
| **Harmon.ie** | An Outlook add-in. Referenced only as a *shape* — "we want something that lives beside Outlook like this does". |
| **RPA / SCI** | *Robotic Process Automation* — software that drives an old system's screens by simulating a user, used when no API exists. SCI is an Allianz-internal integration flavour. The RFP repeatedly says "APIs/RPA/SCI", which is a polite admission that **CLAWS may have no usable API and you may have to type into it like a robot.** |

---

## Part 3 — Problem 1 in detail: the front door (Section 1, e-FNOL)

### A day in the life, today

Picture Sarah, an Allianz claims handler in London. At 09:12 an email lands from a broker:

> *Subject: New claim notification — Harborline Cold Storage*
> *Please find attached notification of an ammonia release at our client's Delaware facility on the night of the 3rd. Preliminary damage estimate USD 1.2m. Reports to follow. Regards, ...*

Attached: a scanned PDF claim form, three photographs, a spreadsheet of damaged stock, and the whole email chain between the broker and the customer.

Sarah now does the following **by hand**:

1. **Reads everything** — the email body, the attachments, and the email chain, to work out what actually happened.
2. **Finds the policy.** The broker didn't quote a policy number, which is normal. She searches Global Genius by company name and guesses which of the customer's several policies applies.
3. **Opens CLAWS and types.** Nine screens, in order:
   - Select the policy
   - Basic details — date of loss, claim type, cause of loss, loss location
   - Additional details — date reported to Allianz, trade-sanctions applicability, whether subrogation is being pursued, the broker's own claim number
   - Select which coverages apply
   - Add every named party (so they can be sanctions-screened)
   - Link each party to the coverages that concern them
   - Set the deductibles
   - Review the whole thing
   - Triage and segmentation — set the reserve, choose the movement type, leave comments

   Depending on the line of business that is **50 to 180 individual field entries.**
4. **Uploads the documents** to FileNet.
5. **Checks whether this is a duplicate** of a claim someone already opened.
6. **Notifies the underwriters and any reinsurers.**

**Elapsed time: 20 to 40 minutes.** Multiply by every claim, in eight regions.

### Why Allianz genuinely hates this

The RFP is unusually candid about it. Three costs, and only one is about speed:

1. **Wasted skill.** Sarah is a claims professional spending her morning as a typist. The RFP explicitly says handlers would be "more motivated and engaged" reviewing data rather than entering it.
2. **Bad data.** Because typing 180 fields is miserable, people take shortcuts. The RFP says cause-of-loss and loss-location data "cannot be used or evaluated meaningfully." That means Allianz **cannot answer questions like "which of our customers keep suffering the same kind of loss?"** — and that question is how insurers price and design products. This is the strategic prize hiding behind the efficiency story.
3. **Slow service.** Brokers and customers wait. Big claims wait behind small ones.

### What they want instead — the five-step target process

Taken directly from the RFP's own architecture slide:

| Step | What happens | Who does it |
| --- | --- | --- |
| **1. Case creation** | The system recognises an incoming email as an FNOL and opens a case automatically. | Machine |
| **2. Policy matching** | It reads the email and attachments, pulls out the identifying details, cross-references Global Genius / CLAWS, and finds the policy number. It also checks nothing identical already exists. | Machine |
| **3. Data extraction** | It structures the ~180 CLAWS attributes and **presents them in a review screen**. Corrections the handler makes feed back into the model. | Machine, then human review |
| **4. CLAWS update** | It fills CLAWS in, runs the duplication check, works out whether reinsurance applies by scanning the policy, and uploads documents to FileNet. | Machine |
| **5. Inform stakeholders** | It emails a confirmation to the sender and an automated notice to the underwriters. | Machine |

The human's *only* remaining jobs are: **type the policy number if the machine couldn't find it**, and **confirm the data is right, filling any gaps**. That is the whole design intent, and it is worth remembering when you evaluate any product against this RFP: *the human is a reviewer, not an operator.*

---

## Part 4 — Problem 2 in detail: the middle of the claim (Section 2, Light-touch & Augmented Processing)

### First, what "light touch" and "augmented" mean

The RFP body names four processing *lanes* and wants the system to decide which lane each claim belongs in — from "the machine does it and a human barely looks" through to "a human does the work with the machine helping."

**Attachment 2 is where this stops being vague.** It is a single slide, and it is the most useful page in the whole document set, because it replaces four abstract lane names with a six-cell grid and tells you exactly what the machine must do in each cell.

### The segmentation grid

A claim's **segment** is decided by two things only:

- **Lead or Follow** — did Allianz write and run this policy, or is Allianz one of several insurers following someone else's lead?
- **Size** — over £1m, over £50k, or under £50k.

Two axes, six cells. And then:

| Segment | Admin work | Loss assessment | In plain English |
| --- | --- | --- | --- |
| **Lead, > £1m** | **Automated** | Manual w/ AI assist | The machine prepares and compares. The person decides everything. |
| **Lead, > £50k** | **Automated** | Manual w/ AI assist | Same as above. |
| **Lead, < £50k** | **Automated** | Augmented with human oversight | The machine drafts *everything* — the money figure, the notes, the emails, the next steps. The person edits or approves, and then the machine carries out what they approved. |
| **Follow, > £1m** | **Automated** | Augmented light touch, w/ human check for any materially wrong decisions | The machine actually **does** it — sets the reserve, writes the notes, emails the broker. A human checks afterwards for anything badly wrong. |
| **Follow, > £50k** | **Automated** | Augmented light touch, w/ human check | Same as above. |
| **Follow, < £50k** | **Automated** | Largely automated, human check as needed | The machine handles it end to end. A person looks only if something flags. |

Three things follow from that grid, and each one changes what has to be built.

**1. The paperwork lane is automated for every single claim.** On the slide it is one arrow spanning all six cells, and the footnote says what "Admin" means: *FNOL, triage, saving a document in CLAWS, searching for relevant documents, payments, AQS and sanctions.* That is seven activities, on every claim, big or small, lead or follow. It is the most under-read line in the document set — because it means the cheapest automation to justify is not the clever reasoning on small claims, it is the boring filing on *all* claims.

**2. The autonomy runs backwards to the risk.** The biggest claims get the *least* automation — on a £5m lead claim the machine never decides anything. The smallest follow claims get the most. That is sensible risk management, and it tells you the build order: start where the machine only assists, and earn the right to let it write.

**3. The thresholds are a dial, not a constant.** In red at the bottom of the slide: *"Financial thresholds could be increased/changed once the business teams have confidence in the accuracy of the AI solution."* Allianz expects to move the £50k and £1m lines as trust grows. So those numbers are configuration a business user turns, not constants in code — and building them any other way guarantees a rewrite.

### What the machine must actually do, per cell

Reading the grid's right-hand column, the required capabilities are:

| Capability | Which cells need it |
| --- | --- |
| Synthesise what changed vs the previous report or email, and highlight the key changes | Lead > £1m, Lead > £50k, Lead < £50k |
| Flag potential subrogation or fraud risk | **All four lanes** — the only universal capability |
| Perform all administrative tasks (save documents, structure and save notes) | All six cells |
| Recommend a reserve figure, and synthesise what is uncertain about it | Lead < £50k |
| Show the history of the claim (to manage deductibles) | Lead < £50k |
| Recommend notes, emails to the loss adjuster and to reinsurers, and next steps | Lead < £50k |
| Carry out the actions the handler approved | Lead < £50k |
| Extract from a **bordereau** and *input* the reserve and the notes directly | Follow, all bands |
| Contact the broker or the lead insurer where information is missing | Follow, all bands |
| Highlight risks that need human intervention — e.g. **risk of claims escalation** | Follow, all bands |
| Review depth driven by how big Allianz's **line size** is | Follow > £1m, Follow > £50k |

That last group is worth pausing on. In the Follow lanes the machine **writes before a human looks** — it sets a reserve and sends an email, and the human check happens afterwards, and only for "materially wrong decisions." That is a genuinely higher bar than anything in the Lead lanes, and it needs a specific new capability that neither the RFP nor the BRD names: something that can tell the difference between *low confidence* and *confidently wrong about something that matters*. A hesitant guess on a £3k claim is fine. A confident, wrong £400k reserve is not.

### A day in the life, today

The same Sarah, three weeks later. A loss adjuster emails a 12-page report on the Harborline claim.

1. She searches Global Genius and CLAWS for the claim number, using whatever the adjuster mentioned — the policy number, the client name, a keyword like "ammonia release".
2. She opens **FileNet and CLAWS** to find the *previous* reports and her own earlier notes, and re-reads them to remember where the claim stood.
3. She reads the new 12-page report, checking the nature of the damage, the recommended settlement figure, and the executive summary — and **manually compares it against the previous report to work out what changed.**
4. She replies to the adjuster: confirms she's read it, comments on whether she agrees, approves a settlement figure for Allianz's share.
5. She drags the report into CLAWS, adds the party name, the party role, labels it with a category, records the date received, clicks attach.
6. She writes a note in a **free-text box**, manually typing a category, a title, and — for mandatory coverage notes — nine separate things: policy form, policy section, applicable limits, deductible or captive, applicable endorsements, lead/follow/fronted, AGCS share, reinsurance, coverage analysis.
7. She submits a new reserve: movement type, sub-movement type, amount, original currency, accounting currency.

**Elapsed time: 30 minutes to over 1,000 minutes.**

### What they want instead — the nine things the AI must do

This is the concrete checklist from the RFP's "Reasoning Capabilities" section. Every one is a distinct feature:

1. **Identify the claim segment** and apply the matching automation lane.
2. **Provide a reserve estimate**, plus a written synthesis of *what is uncertain about it*.
3. **Synthesise what changed** between the current report and its previous versions.
4. **Identify reinsurance and coinsurance** where relevant.
5. **Recommend next steps** — an action plan.
6. **Draft the claims notes.**
7. **Draft email replies** to any party on the claim.
8. **Draft emails to reinsurers/coinsurers** telling them about changes.
9. **Archive the documents** — extract them from emails and file them under a defined naming and filing convention.

Plus, from the front-end slide: **flag potential subrogation or fraud risk**, and show the **history of the claim** to support deductible management.

And the reading job is bigger than in Section 1: the system must extract not just from emails, but from **CLAWS, Global Genius, FileNet, and the London market systems (LIRMA/CLASS/ECF2)** — including the full history of the claim and the claimant.

---

## Part 5 — The scorecard: exactly how they will grade you

This is the most important part of the RFP and the part most easily skimmed past. There are **thirteen acceptance areas**, and the RFP applies all of them to *both* sections. Each one below is stated as the plain question an evaluator will ask.

| # | Area | The question they will actually ask | The number to hit |
| --- | --- | --- | --- |
| **1** | **Accurate & consistent extraction** | *Did you read the fields correctly?* | **95% accuracy** on all CLAWS attributes; **100% format consistency** — anything not matching the format standard must return "no value"/"unknown", and **those count as errors**. Must handle PDF, DOCX, XLSX, JPEG, PNG, MSG. Must handle **English, German, Spanish, Italian, French**. Must flag typos. Must adapt to regional date formats, currencies and time zones. Must work **zero-shot** (no per-use-case training) but **improve over time** toward few-shot. |
| **2** | **Proper structuring** | *Did it land in the right CLAWS boxes?* | Correct mapping with **no manual intervention**; validation for completeness/correctness/consistency; anything wrong is **flagged for review**; the review screen **feeds corrections back to the model**. |
| **3** | **Human-in-the-loop interface** | *Can a handler review and fix this comfortably?* | Review and confirm/change every value; **highlight where the model was unsure**; **show a confidence level per case** so the handler knows how much attention it needs. For Section 2, additionally: **summarise the data to help the handler decide.** |
| **4** | **Transfer into CLAWS** | *Did it actually get into the system of record?* | Data complete and accurate, formats correct, **duplicate check performed**, all fields completed, **reinsurance attached**, documents uploaded to **FileNet**. |
| **5** | **API for future integration** | *Can we build on this later?* | REST, versioned, documented (endpoints, params, sample payloads, error codes, migration guides), **HTTPS + OAuth2/API keys**, **100 requests/second at 95% success**, **99.9% uptime**, duplicate checking, **rollback on failure**, standard error messages, logging, health-check endpoint. |
| **6** | **Reduction in processing time** | *Did it get faster, provably?* | **AHT down at least 30% within four months.** Response within **2 seconds for 95% of requests**. No degradation under concurrent load; horizontal scaling. **And the application must display its own performance against defined KPIs.** |
| **7** | **Data quality** | *Is the data now usable for analysis?* | Correct-ratio of five KPIs improved by **90%**: claim status code not null, currency not null, LOB not null, SUBLOB not null, type-of-loss code acceptable. Plus NLP-based quality measures on the free text: word count, complexity, **named entity recognition**, semantic similarity, sentiment analysis, topic modelling, information density. |
| **8** | **Reliability** | *Will it stay up, and can we survive a disaster?* | **96% uptime over a year**; graceful error handling with meaningful messages; data integrity; a **Business Continuity Plan**; a **Disaster Recovery plan**. |
| **9** | **Data security** | *Is our data safe and provably ours?* | Encryption **in transit and at rest**; **role-based access control**; **all access and changes to claims data logged and auditable**; **"no storage outside Allianz system landscape allowed"**; signed off by **AGCS-COO-IT and AGCS-CEO-Legal**. |
| **10** | **Interoperability** | *Does it talk to our estate?* | Compatible with **CLAWS, FileNet, Global Genius** and third-party tools. |
| **11** | **Compliance** | *Is it legal?* | **GDPR**, Allianz IT security standards, and the **EU AI Act**. Data-retention policies to match. Signed off by COO-IT and CEO-Legal. |
| **12** | **Logging & monitoring** | *Can we run it in production?* | Real-time monitoring of performance and health; **automated alerts** for outages and bottlenecks; secure, accessible log management. |
| **13** | **Handler satisfaction** | *Do the people using it like it?* | A **survey** of claims handlers scoring at least **4 out of 5**, plus an implemented feedback loop. |

**Read criteria 1, 4, 9 and 10 together and a hard truth appears:** this is not a request for a standalone claims application. It is a request for **an AI pre-processor that feeds Allianz's existing systems and stores nothing of its own outside Allianz's walls.** Any product that positions itself as *the* system of record is answering a different question.

---

## Part 6 — The technology shopping list, in their words

Allianz put their own architecture on two slides. It is a short list, and it is worth quoting because it tells you what they expect to be sold.

**For e-FNOL — three components:**

1. **Document Extraction (Gen AI)** — solution: *Retrieval Augmented Generation*; components: LLM + ML. (RAG means: find the relevant passages first, then ask the language model about those passages, rather than dumping everything at it.)
2. **Human-in-the-loop interface** — solution: web development. Notably, they add *"as an option can be performed within Excel file"* — a signal that they expect a lightweight review surface, not a whole new application.
3. **Integration with CLAWS** — solution: APIs / RPA / SCI.

**For light-touch — the same three, plus one:**

4. **Reasoning & Contextual Understanding** — solution: *Retrieval Augmented Reasoning*; components: **Knowledge Graph + Reasoning Engine**. Described as "understanding and reasoning with complex relationships between data". They also want *"expert rationale provided behind decision"* — the machine must show its working.

The footnote on both slides is quietly important: *"Simple RPA bots will be required to send automatic notifications, upload LA reports to CLAWS."* They have accepted that part of the integration will be screen-driving robots rather than clean APIs.

---

## Part 7 — The size of the problem

Numbers that determine whether a solution is real or a demo:

| Dimension | Requirement |
| --- | --- |
| **Regions** | 8 — UK & Ireland, Germany & Switzerland, Central & Eastern Europe, North America, Benelux & Nordics, France & South Africa, Iberia, Latin America |
| **Lines of business** | 8 — Property, Natural Resources, Construction, Liability, Financial Lines, Marine, Entertainment, Aviation |
| **Languages** | 5 — English, German, Spanish, Italian, French |
| **File formats** | PDF, DOCX, XLSX, JPEG, PNG, MSG |
| **Fields per claim** | ~180 CLAWS attributes across 9 entry categories (50–180 depending on LoB) |
| **Currencies** | Multiple, including **original currency and accounting currency separately** on every reserve |
| **Throughput** | 100 requests/second sustained |
| **Onward scale** | "Must be scalable to all LoBs and regions of AGCS **as well as to other Allianz OEs**" — i.e. the rest of the Allianz group |

Note the awkward pair: **8 regions × 8 lines of business = 64 combinations**, each with its own field set, vocabulary, regulator and language. The RFP's answer to this is criterion 1's suggestion that there be *"a user interface where business users can provide (e.g. LoB-specific) business descriptions for the information that the model needs to extract."* In other words: **the field definitions must be editable by business users, not hard-coded by developers.** That is the single most load-bearing architectural hint in the document.

---

## Part 8 — Our own BRD: what it adds, and where the three documents disagree

The third document is not from Allianz. `Claims Workbench_BRD.docx` is AidenAI's own Business Requirements Document, and it matters for two reasons: it names requirements the RFP never mentions, and it draws a scope line the RFP does not accept.

### The four things the BRD adds that Allianz never asked for

Each of these is a genuine capability area, and three of the four do not exist in the product today.

**1. Classify the Market / OE, not just the product.** BRD §5.1 requires the AI to determine **both** the Market/Operating Entity *and* the Product/Line of Business. The RFP names eight regions but never asks for them to be *detected* — it assumes you know where a claim came from. The BRD is right and the RFP is loose: an email arriving in a shared mailbox does not say which Allianz country business owns it. And the BRD then leans on that answer twice more — §5.4 says extract "only the data elements defined for the applicable Market/Product", and §5.6b says route the work using it. So one missing concept blocks three requirements.

**2. Open password-protected emails and attachments.** BRD §5.3, in full: identify protected content, obtain the password through a configured mechanism, process it once authorised, keep the original email-to-attachment relationship, handle wrong or missing passwords through an exception workflow, and never persist the password in the audit log. The RFP does not mention this at all. It is a real operational fact — brokers routinely send encrypted PDFs containing personal injury detail — and today an encrypted file is indistinguishable from a corrupt one. Note that §5.3 needs a **business decision before any code**: where do the passwords come from? Standing per-broker passwords, a secrets vault, or an automated request back to the sender?

**3. Recognise that five emails are one claim.** BRD §5.5, "Fragmented FNOL Processing." A notification often arrives split across several emails — the initial note, then the photographs, then the survey report, then a correction. The BRD requires identifying the relationship (using policy number, insured, claimant, broker, subject, date of loss, claim reference, sender, document content and existing claim data), grouping them into one claim context, avoiding a duplicate claim where the email relates to an existing one, presenting the proposed relationship when confidence is low, and letting the user **merge, reject or correct** it.

  This is *not* the same as duplicate detection, and the difference is worth being precise about. Duplicate detection asks *"is this the same claim arriving twice?"* and its correct answer is to suppress or flag. Fragmentation asks *"are these several parts of one claim?"* and its correct answer is to **join them up**. The product does the first well and the second not at all.

**4. Assign work by taking turns.** BRD §5.6 specifies a configurable **Round Robin**: maintain the eligible handler pool, find the next eligible person, allocate sequentially, honouring Market/OE, Product/LoB, eligibility, availability, the assignment queue, **working hours and time zone**, and temporary exclusions. The RFP says nothing about the algorithm. The product implements *best-fit* scoring instead — skills, lines, countries, severity ceiling, authority limit, current caseload — which is arguably a better algorithm and is definitely not the one specified.

### The one thing the BRD requires that the RFP only implies — and we already built

BRD's Human-in-the-Loop section asks, for every AI-extracted value, that the user can: view the value; view the source document or email; **navigate to the relevant location** in it; **see a bounding box or highlight** around the supporting text; know whether it came from the email body or an attachment; modify or override it; and see AI-extracted values **clearly distinguished** from manually entered ones.

All seven exist. The RFP only asks that ambiguity be "highlighted"; the BRD asks for the specific mechanism, and it is built. Worth saying loudly in any proposal, because it is unusual.

### Where the three documents disagree

Four conflicts. None is fatal, all four need a decision.

**Conflict 1 — scope. This is the big one.** The BRD's §4 lists what is *out of scope for the initial MVP*: automated adjudication, automated claim settlement, post-claim lifecycle management, and **"fully autonomous claims processing without human validation."**

Now read Attachment 2's Follow < £50k cell again: *"Largely automated with human check as needed."* And its Follow > £50k cell: the bot inputs the reserve, inputs the notes and contacts the broker, with a human check only for materially wrong decisions.

RFP Section 2 and Attachment 2 are asking for precisely the thing the BRD excludes. **33 of the 195 requirements in the matrix sit outside the BRD's own declared MVP scope**, and they are almost exactly RFP Section 2 plus Attachment 2. Somebody has to choose, explicitly:

- *Bid Section 1 only* — the RFP permits it, since Section 1 is "also a prerequisite for Section 2." The BRD's MVP then stands as written and is 58% built. This is the honest, deliverable position.
- *Bid both* — then the BRD's §4 needs rewriting, because it currently contradicts what is being promised.

What must not happen is proposing Section 2 while an internal BRD says Section 2 is out of scope. That inconsistency surfaces in diligence.

**Conflict 2 — what defines a segment.** The RFP body says segments are defined on *"Line of Business, Lead/Follow, direct/indirect, claim size."* Attachment 2 uses only **Lead/Follow and claim size**. Attachment 2 is the later, more specific artefact — build its two axes, and leave the model open for the other two.

**Conflict 3 — what the lanes are called.** The RFP body names four lanes: *Light touch/Augmented, Light Touch + Augmented, Augmented with human oversight, Manual with AI Assist.* Attachment 2 names four different ones and maps each to specific cells. Use Attachment 2's names — they are the ones tied to actual behaviour.

**Conflict 4 — the assignment algorithm.** Best-fit (built) versus Round Robin (specified). Ship Round Robin as one selectable strategy alongside best-fit rather than replacing it. The product already has an `AssignmentStrategy` enum, which suggests somebody anticipated exactly this.

### What the BRD gets right that the RFP does not

Two things, in fairness to it.

- **Multi-channel from the start.** BRD §5.2 requires email, API, portal, file upload and future channels, each normalising into one common structure with the source channel and metadata retained, and explicitly requires that a new channel be onboardable "without redesigning the core FNOL processing capability." The RFP assumes email forever. The product satisfies the architectural requirement properly — there is one normalisation type every channel converts into — which is a better answer than the RFP asked for.
- **Immutable audit.** BRD §5.7 requires immutability, and distinguishing AI decisions from user overrides. Both are met, and the immutability is met in the strongest possible way: the audit repository has no update method, by design and by comment.

### What the BRD misses

The BRD's blind spot is everything non-functional. **35 requirements in the matrix are real RFP requirements the BRD does not cover at all**, averaging 22% coverage: the 95% accuracy target and how to measure it, the 30% AHT reduction, the data-quality KPIs, the five languages, uptime and load targets, business continuity and disaster recovery, the EU AI Act, data retention, and the two executive sign-offs.

That is not a criticism of a capability document — but it means the BRD cannot be the only plan of record. Half of what Allianz will grade the solution on is not in it.

*(Minor, but worth fixing before the BRD goes anywhere client-facing: it has two sections numbered 5.5, two numbered 5.6, and a stray editing instruction — "2. Enhance the Human-in-the-Loop (HITL) section" — left inline at §5.4.)*

---

## Part 9 — The unwritten rules (what will actually kill a deal)

Things the RFP states once, quietly, that are absolute:

1. **"No storage outside Allianz system landscape allowed."** This rules out sending claim documents to a public AI API. Any language model must run inside Allianz's own cloud tenancy or on-premise. It is one line in the Data Security section and it constrains the entire technical architecture.
2. **Two named human sign-offs** — AGCS-COO-IT and AGCS-CEO-Legal — appear twice. These are not procurement formalities; they are the gates. The evidence pack (security architecture, data-flow diagrams, DPIA, AI Act classification) is a deliverable in its own right.
3. **The EU AI Act.** An AI system making recommendations that affect an insurance payout attracts documentation obligations: risk classification, human-oversight design, technical documentation, logging, accuracy/robustness statements. A product with no AI-governance artefacts fails criterion 11 regardless of how well it extracts.
4. **"Must fit into the existing architectural model of Allianz."** They are not re-platforming for you.
5. **Zero-shot, with continuous learning.** They explicitly refuse to fund a per-use-case training project, *and* they explicitly require accuracy to improve over time from real corrections. Both at once. That combination points at retrieval-plus-prompting with a correction store that becomes few-shot examples — not at fine-tuning.
6. **"100% consistency in extracted output formats… Outputs deviating from predefined format standards must return 'no value' or 'unknown'. These cases will count as errors in the 95% accuracy calculation."** This is stricter than it looks. A model that returns `03/04/2026` where the standard is ISO does not score partial credit — it scores as a *miss*, and it eats the 5% error budget. Format normalisation is not polish; it is scoring.
7. **"The correct ratio of the following KPIs should be increased by 90%."** To prove any improvement you must first **measure the baseline**. A solution with no measurement harness cannot pass criteria 6 or 7 even if it works perfectly.

---

## Part 10 — How they want to work with you

| Aspect | What the RFP says |
| --- | --- |
| **Vendor structure** | One vendor for everything, or several who "collaborate seamlessly to deliver the services as outlined, ensuring full integration and cohesion". |
| **Pricing** | "A detailed breakdown of costs for **each individual requirement**." Line-item, not a single number. |
| **Delivery rhythm** | "A structured sprint rhythm with rapid release & test cycles (e.g. every 2 weeks) and ensure full realization of quick wins." |
| **Relationship** | "Close collaboration with Allianz IT." |
| **Sequencing** | Section 1 (e-FNOL) is "**also a prerequisite for Section 2**." You cannot win Section 2 alone. |
| **Timeline** | Deferred to attachment `01042025_Claims_RP_Rollout_Plan.pdf` (not supplied with the document we hold). |

### Attachments: what we have, and what is still missing

**Now in hand.** `01042025_Claim Segmentations_LTP.pdf` (Attachment 2) — the segmentation model, covered in Part 4. This was previously the second-largest missing artefact and it substantially de-risks Section 2: what was an open question is now a specified six-cell grid.

**Still missing, and it matters:**

1. **`01042025_BMP_CLAWS_EFNOL.xlsx`** (Attachment 1) — *the definitive list of ~180 CLAWS attributes.* Criterion 1's 95% is measured against this file. It gates three of the largest work packages: adding the four missing lines of business with their vocabularies, the CLAWS field dictionary, and the CLAWS data model for coverages and deductibles. **Without it, no accuracy claim can be made honestly.** Ask for it first.
2. **`01042025_Claims_RP_Rollout_Plan.pdf`** (Attachment 3) — the timeline for both sections. Needed to sequence anything against real dates.
3. **The AQS ruleset** — named in Attachment 2's footnote as something to automate, and defined nowhere.
4. **Allianz's internal IT security standards and approved-technology list** — criterion 11 requires compliance with them and they are not in hand, so conformance cannot be claimed. Likely to constrain the identity provider, the data stores and the cloud tenancy.

And two questions that are not documents but block work all the same:

5. **Which uptime figure binds?** Criterion 5 says 99.9% for the API; criterion 8 says 96% over a year. That is a 40× difference in error budget and it changes the deployment architecture.
6. **Where do attachment passwords come from?** BRD §5.3 requires opening protected content but not how the password is obtained. Only the business can answer this, and nothing can be built until they do.

## Appendix A — The RFP's own structure, mapped

| RFP reference | Title | What it covers |
| --- | --- | --- |
| Introduction | — | Two focus areas, vendor model, pricing, scalability, sprint rhythm |
| **Section 1** | **e-FNOL** | |
| 1.1 | Problem statement | 20–40 min AHT, poor data quality |
| 1.2 | Scope & service description | Global rollout, all LoBs |
| 1.3.1 | Accurate and consistent extraction | 95% accuracy, formats, languages, zero-shot |
| 1.3.2 | Proper structuring | CLAWS field mapping, validation, feedback loop |
| 1.3.3 | Human in the loop interface | Review, ambiguity highlighting, confidence per case |
| 1.3.4 | Transfer into CLAWS | Duplicate check, reinsurance, FileNet upload |
| 1.3.5 | API interface | REST, OAuth2, 100 rps, 99.9% |
| 1.3.6 | Reduction in processing time | −30% AHT, 2s p95, KPI display |
| 1.3.7 | Data quality | Five null-rate KPIs +90%, NLP measures |
| 1.3.8 | Reliability | 96% uptime, BCP, DR |
| 1.3.9 | Data security | Encryption, RBAC, audit, data residency, sign-off |
| 1.3.10 | Interoperability | CLAWS, FileNet, Global Genius |
| 1.3.11 | Compliance | GDPR, EU AI Act, retention |
| 1.3.12 | Logging and monitoring | Real-time monitoring, alerting, log management |
| 1.3.13 | Positive feedback | Survey ≥ 4/5, feedback loop |
| **Section 2** | **Light-touch & augmented processing** | |
| 2.1 | Problem statement | 30–1,000+ min per assessment |
| 2.2 | Scope & service description | Segments, lanes, drafting, filing |
| 2.3.1 | Document reading and extraction | 95% on segmentation attributes; reads CLAWS/GG/FileNet/LIRMA/CLASS/ECF2 + claim history |
| 2.3.2 | Reasoning capabilities | Segment, reserve, diff, reinsurance, next steps, notes, emails, archive |
| 2.3.3 | Human in the loop interface | As 1.3.3, plus decision-support summary |
| 2.3.4–2.3.12 | — | Inherits Section 1's criteria 4–12 verbatim |

---

## Appendix B — The BRD's own structure, mapped

| BRD ref | Title | What it covers | Requirement IDs in the matrix |
| --- | --- | --- | --- |
| §1 | Business context | Fragmented, multi-channel, protected-content intake | — |
| §2 | Product vision | Seven bullets: multi-channel, Market/Product classification, protected email, fragmented-FNOL consolidation, policy documentation, Round Robin, audit trail | — |
| §3 | Objectives | Reduce triage effort, improve policy accuracy, handle complex intake, reduce assignment effort, AI transparency, scale across markets, one workbench | — |
| §4 | High-level scope | 13 items in scope; **4 items out of scope for MVP** — automated adjudication, automated settlement, post-claim lifecycle, fully autonomous processing | B.50 |
| §5.1 | FNOL classification — Market & Product | Classify Market/OE and Product/LoB, capture confidence, route low confidence to review, allow override, audit it | B.01–B.05 |
| §5.2 | Multiple ingestion channels | Email, API, portal, file upload, future; normalise to a common structure; retain source metadata; exception on failure; onboard new channels without redesign | B.06–B.12 |
| §5.3 | Password-protected email & attachments | Identify, obtain the password, process, preserve relationships, exception on failure, never persist the password | B.13–B.17 |
| §5.4 | AI data extraction — predetermined loss data | Extract only the elements defined for the Market/Product; body and attachments; capture the source; identify missing/ambiguous; allow modification | B.18–B.19 |
| §5.4 (HITL) | Extraction review & validation | View value, view source, navigate to location, **bounding box/highlight**, body-vs-attachment, override, distinguish AI from manual | B.20–B.23 |
| §5.5 (a) | Fragmented FNOL processing | Identify related emails, group into one claim, avoid duplicate claims, present below threshold, merge/reject/correct | B.24–B.28 |
| §5.5 (b) | Policy document access & AI inference | Retrieve from approved repositories, associate with the policy, infer coverage / attachment-layer / limits / provisions, provide evidence, allow override, distinguish system-of-record from inferred | B.29–B.36 |
| §5.6 (a) | Duplicate check | Against existing claims, on policy number / insured / claimant / date, present matches, allow proceed, audit the outcome | R1.4.03, B.26 |
| §5.6 (b) | Claims handler assignment — Round Robin | Configurable on Market, Product, eligibility, availability, queue, working hours/time zone, exclusions; sequential allocation; manual reassignment; audit history | B.37–B.42 |
| §5.7 | Audit log | 14 named event types; **immutable**; AI vs override distinguishable; supports investigation; no passwords | B.43–B.48 |
| §6.1 | Human-in-the-loop | Review, correct, override classification, override policy/coverage, correct associations, reassign, resolve exceptions | R1.3.02 |
| §6.2 | Confidence & exception management | Confidence indicator or threshold; below threshold route to a human review queue rather than progressing | B.04, B.49 |
| §6.3 | Traceability | FNOL → extraction → classification → policy → coverage/limit → assignment → intervention → submission | B.48 |
| §7 | High-level end-to-end process | Thirteen steps; matches the pipeline's existing stage order closely, with fragmentation and password handling inserted early | — |
| §8 | Product backlog | References `Claims Workbench_ProductBacklog.xlsx` — **not supplied** | — |
| §9 | Key dependencies | Ingestion infrastructure, policy systems, SharePoint/FileNet, **CLAWS integration**, handler master data, classification rules, coverage master data, AI/LLM platform, security, **OCR** | R1.10.x, R1.1.11–12 |

### The BRD's end-to-end process against the product's actual pipeline

The BRD's §7 flow and the pipeline in `app/services/fnol/pipeline.py` line up more closely than either document admits. Mapping them:

| BRD §7 step | In the pipeline today? |
| --- | --- |
| FNOL received | Yes |
| Identify source channel | Yes — one of seven channels, stored with source metadata |
| Process email / attachments | Yes — the body is written out as a document and read by the same code as attachments |
| Handle password-protected content | **No** |
| Identify related / fragmented FNOLs | **No** — the thread id is captured and indexed, and nothing reads it |
| AI classification — Market + Product | Product only |
| Policy identification | Yes — and strong |
| Duplicate check | Yes — against both notices and existing claims |
| Retrieve policy documents | Partly — from an internal library, not an approved repository |
| AI coverage / attachment / limit inference | Coverage verdict yes; attachment/layer no; limits partly |
| Human review & validation | Yes — and it exceeds what is asked |
| Claims handler assignment — Round Robin | Best-fit instead |
| Claim creation / downstream submission | Claim creation yes; **downstream submission no** |
| Audit log updated throughout | Yes — writes only, not reads |

Nine and a half of fourteen steps. The four that are missing are the four that matter most commercially: protected content, fragmentation, Market/OE, and downstream submission.
