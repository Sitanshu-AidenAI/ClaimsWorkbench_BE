# Matching ground truth

Generated from `scripts/fnol_scenarios.py` by `scripts/build_fnol_ground_truth.py`. Do not edit by hand — edit the scenario and rebuild, so the packs and the answer key cannot drift apart.

24 packs against the 12 policies in `../../policy/`.

| Pack | Number on notice | Broker ref | Expected | Confidence |
|---|---|---|---|---|
| `harborline-ammonia-release` | `CP-4471-88210` | `TR/PROP/2025/4471` | `CP-4471-88210` | Exact |
| `windrow-grove-hail` | `CP-7735-19042` | `KRP/HAB/2025/7735` | `CP-7735-19042` | Exact |
| `fairmount-dye-house-fire` | `CP-2210-55870` | `RK/MFG/2024/2210` | `CP-2210-55870` | Exact |
| `kestrel-ridge-copper-theft` | — | `HV/RET/2025/9083` | `CP-9083-64115` | Strong |
| `rivergate-dropped-load` | `BR-3358-20471` | `AV/BR/2025/3358` | `BR-3358-20471` | Exact |
| `northfield-water-intrusion` | — | `KRP/CAR/2025/6612` | `BR-6612-77309` | Strong |
| `cypress-landing-copper-theft` | `IM-2298-66401` | `SRR/BR/2025/1147` | `BR-1147-30926` | Possible |
| `sable-creek-falling-brick` | `GL-5529-41836` | `AV/GL/2025/5529` | `GL-5529-41836` | Exact |
| `cherry-creek-water-damage` | — | `FRC/GL/2025/8804` | `GL-8804-27153` | Possible |
| `driver-middle-school-open-roof` | `GL-3376-90284` | `CB/ROOF/2024/3376` | `GL-3376-90284` | Exact |
| `ironbark-excavator-fire` | `IM-2298-66401` | `SRR/IM/2025/2298` | `IM-2298-66401` | Exact |
| `aurora-jobsite-theft` | — | `FRC/IM/2025/7741` | `IM-7741-15530` | Strong |
| `harborline-delaware-freeze` | — | `TR/PROP/2025/4471` | `CP-4471-88210` | Strong |
| `windrow-grove-building-k-fire` | — | `KRP/HAB/2025/7735` | `CP-7735-19042` | Strong |
| `fairmount-mezzanine-collapse` | `CP-2210-55870` | `RK/MFG/2024/2210` | `CP-2210-55870` | Exact |
| `kestrel-ridge-vehicle-impact` | `CP-9038-64115` | `HV/RET/2025/9083` | `CP-9083-64115` | Possible |
| `rivergate-copper-theft` | — | `AV/BR/2025/3358` | `BR-3358-20471` | Possible |
| `northfield-site-vandalism` | — | `KRP/CAR/2025/6612` | `BR-6612-77309` | Strong |
| `cypress-landing-haboob` | — | `SRR/BR/2025/1147` | `BR-1147-30926` | Strong |
| `tidewater-rooftop-equipment` | — | `CB/ROOF/2024/3376` | `GL-3376-90284` | Strong |
| `larkspur-fleet-collision` | — | `GRS/AUTO/2026/0114` | **NO_MATCH** | No Match |
| `meadowcrest-frozen-sprinkler` | `CP-6650-11223` | `PWA/PROP/2025/0022` | **NO_MATCH** | No Match |
| `bayview-terrace-riser` | — | `HCM/BT/2026/0502` | **NO_MATCH** | No Match |
| `cobalt-ridge-trench-collapse` | — | `OMI/CAS/2026/0033` | **NO_MATCH** | No Match |

## Detail

### harborline-ammonia-release

**Harborline Cold Storage — ammonia release and product loss** — Commercial property

- **Policy number on the notice:** `CP-4471-88210`
- **Broker reference:** `TR/PROP/2025/4471`
- **Insured:** Harborline Cold Storage & Logistics, LLC
- **Claimant:** Harborline Cold Storage & Logistics, LLC
- **Date of loss:** 10 January 2026
- **Loss location:** 2870 Patapsco Industrial Parkway, Baltimore, MD 21226
- **Cause:** Ammonia release — weld failure on a liquid ammonia header
- **Estimated loss:** USD 3,700,000
- **Expected match:** `CP-4471-88210`
- **Confidence:** Exact

The policy number is stated and every corroborating axis agrees: insured name, the loss address is scheduled Location 001, the broker reference is the one on the policy, and the sender domain is the policy's broker domain. This is the control case — if this does not match, nothing will.

What it exercises:

- **Every signal firing at once.** Policy number, broker reference, insured name, risk location, broker domain and policy period all agree. The ladder should reach `exact` on the number alone and be corroborated on five further axes.
- **Bailee exposure.** Most of the product is customer-owned, so the value at risk sits under the personal-property-of-others limit rather than the insured's own stock.
- **A figure stated twice.** USD 3,700,000 appears in the covering email and on the loss notice; the product schedule totals separately. A citation naming the right document is doing real work.

### windrow-grove-hail

**Windrow Grove Apartments — hail and straight-line wind** — Commercial property

- **Policy number on the notice:** `CP-7735-19042`
- **Broker reference:** `KRP/HAB/2025/7735`
- **Insured:** Sundale Property Group, LLC
- **Claimant:** Windrow Grove Apartments, LP
- **Date of loss:** 19 April 2026
- **Loss location:** 6120 East 91st Street, Tulsa, OK 74137
- **Cause:** Hail and straight-line wind
- **Estimated loss:** USD 2,400,000
- **Expected match:** `CP-7735-19042`
- **Confidence:** Exact

Policy number stated and corroborated by the insured, the additional named insured as claimant, the scheduled Location 001 address, the broker reference and the broker's sender domain.

What it exercises:

- **Claimant is not the named insured.** The notice names Sundale Property Group as insured and Windrow Grove Apartments, LP as claimant. Both are on the policy — the second as insured organisation — so `insured_name` must compare against joint names.
- **A coverage argument that is not an identity signal.** The cosmetic damage dispute belongs in warnings, not in the score.
- **Per-location excess.** The candidate card should show Location 001's deductible, not the policy headline.

### fairmount-dye-house-fire

**Fairmount Textile Mills — dye house fire** — Commercial property

- **Policy number on the notice:** `CP-2210-55870`
- **Broker reference:** `RK/MFG/2024/2210`
- **Insured:** Fairmount Textile Mills Holdings, Inc.
- **Claimant:** Piedmont Dye & Finish, LLC
- **Date of loss:** 13 August 2025
- **Loss location:** 1180 Woodruff Industrial Road, Greenville, SC 29607
- **Cause:** Fire — hydraulic fluid release onto a hot thermal oil line
- **Estimated loss:** USD 30,500,000
- **Expected match:** `CP-2210-55870`
- **Confidence:** Exact

Policy number stated, with the insured, the additional named insured as claimant, scheduled Location 001, the broker reference and the wholesale broker's sender domain all agreeing.

What it exercises:

- **A subsidiary as claimant.** Piedmont Dye & Finish, LLC is an additional named insured, not the first named insured — `insured_name` must match against joint names.
- **A warranty breach that is not a matching signal.** The lint log gap is a coverage defence recorded in the engineer's report; it must not move the rank.
- **Quantum split across documents.** The stock schedule totals separately from the engineer's reinstatement figures, and the email states a third, higher number.

### kestrel-ridge-copper-theft

**Kestrel Ridge Retail Center — copper theft and consequent water damage** — Commercial property

- **Policy number on the notice:** *not stated*
- **Broker reference:** `HV/RET/2025/9083`
- **Insured:** Kestrel Ridge Holdings, LP
- **Claimant:** Kestrel Ridge Holdings, LP
- **Date of loss:** 2 March 2026
- **Loss location:** 4820 - 4890 North Eagle Ridge Boulevard, Boise, ID 83713
- **Cause:** Theft of copper, and water damage consequent on a cut live water line
- **Estimated loss:** USD 425,000
- **Expected match:** `CP-9083-64115`
- **Confidence:** Strong

No policy number is stated. The broker reference `HV/RET/2025/9083`, the property address matching scheduled Location 001, the named insured, the managing agent who is the scheduled additional insured, and the sender domain `hvagency.example` resolve to one policy with no competing candidate in the book.

What it exercises:

- **Identification without a policy number.** The heaviest signal is absent, so the match has to be carried by broker reference at 2.5 and insured name at 2.0, corroborated across the risk, broker and cover axes.
- **The broker reference doing the work.** This is the case the `broker_reference` signal exists for — the broker's own scheme reference is the only hard identifier present.
- **Reporter is not the insured.** The reporter is the broker; the managing agent is the site contact; the insured is a Delaware LP that never appears as a sender.

### rivergate-dropped-load

**Rivergate Commons Phase II — dropped load during a crane pick** — Construction — builder's risk

- **Policy number on the notice:** `BR-3358-20471`
- **Broker reference:** `AV/BR/2025/3358`
- **Insured:** Rivergate Development Partners, LLC
- **Claimant:** Vanterra Construction Group, LLC
- **Date of loss:** 26 February 2026
- **Loss location:** 2650 Rivergate Parkway, Charlotte, NC 28273
- **Cause:** Dropped load — wind gust during a crane pick
- **Estimated loss:** USD 407,200
- **Expected match:** `BR-3358-20471`
- **Confidence:** Exact

Policy number stated, and corroborated by the project name and contract number, the site address, both named insureds appearing as insured and claimant, the broker reference and the sender domain.

What it exercises:

- **Construction identity signals.** `project_name` at 1.6 and `contract_number` at 2.2 both fire and both agree, which on a construction risk is stronger evidence than the insured name.
- **The insured is not the contractor.** The owner is the first named insured and Vanterra is the contractor and the claimant; both are on the policy.
- **Ruling the other policy out in the notice.** The email states there is no third-party element, which is what keeps this off the contractor's CGL — the discriminator pack 17 makes the matcher work out for itself.

### northfield-water-intrusion

**Northfield Senior Living — water intrusion into an undried structure** — Construction — builder's risk

- **Policy number on the notice:** *not stated*
- **Broker reference:** `KRP/CAR/2025/6612`
- **Insured:** Sundale Property Group, LLC
- **Claimant:** Halcyon Builders, LLC
- **Date of loss:** 18 October 2025
- **Loss location:** 1725 Northfield Commons Drive, Madison, WI 53704
- **Cause:** Water intrusion — wind-driven rain into an incomplete structure
- **Estimated loss:** USD 915,000
- **Expected match:** `BR-6612-77309`
- **Confidence:** Strong

No policy number. The broker reference `KRP/CAR/2025/6612`, the project name and contract number, the site address, both owner entities and the general contractor all match one construction policy. Sundale's other policy is a Tulsa habitational property risk that cannot answer a Wisconsin construction loss.

What it exercises:

- **One insured, two policies, different lines.** Sundale Property Group is the first named insured on both `CP-7735-19042` and `BR-6612-77309`. `line_of_business` and `project_name` are what separate them.
- **Project identity over insured identity.** The project name and contract number resolve the risk more reliably than the insured, which is the case the construction signals exist for.
- **Reporter, insured and contractor are three different parties**, on three different domains, none of which is the carrier.

### cypress-landing-copper-theft

**Cypress Landing Data Hall B — copper theft from the works** — Construction — builder's risk

- **Policy number on the notice:** `IM-2298-66401`
- **Broker reference:** `SRR/BR/2025/1147`
- **Insured:** Meridian Grid Holdings, LLC
- **Claimant:** Ironbark Constructors, Inc.
- **Date of loss:** 11 April 2026
- **Loss location:** 14900 South Cypress Landing Way, Mesa, AZ 85212
- **Cause:** Theft — copper stripped from the works
- **Estimated loss:** USD 1,318,100
- **Expected match:** `BR-1147-30926`
- **Confidence:** Possible

The notice quotes `IM-2298-66401`, which is a **real policy for the same corporate group but the wrong one for this loss**. The `policy_number` signal will find that policy and score it — and every other signal contradicts it: the project name, the contract number, the site address, the broker reference and the owner as insured all belong to `BR-1147-30926`. A correct answer overrides the stated number on the weight of the other eleven signals.

What it exercises:

- **The heaviest signal pointing at the wrong answer.** `policy_number` carries 5.0 and resolves cleanly to the equipment floater. Everything else — project, contract, site, broker reference, insured — points at the builder's risk. This is the case that tests whether the ladder reads *which axes* agreed rather than only the total.
- **A defensible split.** USD 54,100 genuinely does belong on `IM-2298-66401`, so a two-policy allocation is a better answer than either policy alone.
- **A warning that is not a rank.** The guard patrol gap is a warranty question and must not lower the builder's risk candidate below the equipment floater.

### sable-creek-falling-brick

**Sable Creek Medical Office Building — falling brick, third-party injury** — Liability — commercial general liability

- **Policy number on the notice:** `GL-5529-41836`
- **Broker reference:** `AV/GL/2025/5529`
- **Insured:** Vanterra Construction Group, LLC
- **Claimant:** Rosalind Achterberg-Nwankwo
- **Date of loss:** 17 March 2026
- **Loss location:** 4405 Sable Creek Drive, Fort Mill, SC 29715
- **Cause:** Falling brick — banding failure during a telehandler unload
- **Estimated loss:** USD 663,000
- **Expected match:** `GL-5529-41836`
- **Confidence:** Exact

Policy number stated, with the insured, the designated project P-3 on the policy's own schedule, the broker reference and the sender domain all agreeing. The loss type is third-party bodily injury and property damage, which is the liability policy and not the insured's builder's risk.

What it exercises:

- **Line of business as a discriminator.** Vanterra is a named insured on a builder's risk too. This loss is `liability`; that one is `construction`.
- **Claimant is a third party, not the insured.** `claimant_name` is a member of the public and must not be read as the insured.
- **A scheduled project on a liability policy.** P-3 appears on the GL's location schedule, so `risk_location` corroborates on an axis a property-only reading would miss.

### cherry-creek-water-damage

**Cherry Creek Medical Campus — escape of water from a new chilled water riser** — Liability — commercial general liability

- **Policy number on the notice:** *not stated*
- **Broker reference:** `FRC/GL/2025/8804`
- **Insured:** Beacon Mechanical Services, Inc.
- **Claimant:** Cherry Creek Medical Campus Owner, LLC
- **Date of loss:** 7 February 2026
- **Loss location:** 3400 South Cherry Creek Drive North, Denver, CO 80209
- **Cause:** Escape of water — grooved coupling separation on a newly installed riser
- **Estimated loss:** USD 590,000
- **Expected match:** `GL-8804-27153`
- **Confidence:** Possible

No policy number and the insured holds two policies with the same carrier, the same broker and the same broker domain. The discriminators are the loss type — four fifths is third-party damage to an occupied building, which is liability — and the fact that the building owner is a scheduled additional insured on the liability policy for this very project. The `FRC/GL/…` broker reference points the same way.

What it exercises:

- **Two policies, one insured, one carrier, one broker.** Insured name, insured domain, broker name and broker domain are all identical across both candidates and therefore discriminate nothing. Only line of business and the broker reference separate them.
- **A split allocation is the right answer.** The schedule states which head belongs on which policy, so a matcher that returns one policy and ignores the other is only four-fifths right.
- **A condition that changes the retention, not the rank.** The water damage control condition is a warning.

### driver-middle-school-open-roof

**Driver Middle School — rain into an open roof during a re-roof** — Liability — commercial general liability

- **Policy number on the notice:** `GL-3376-90284`
- **Broker reference:** `CB/ROOF/2024/3376`
- **Insured:** Tidewater Roofing & Exteriors, LLC
- **Claimant:** Suffolk City Public Schools
- **Date of loss:** 18 June 2025
- **Loss location:** 4652 Driver Lane, Suffolk, VA 23435
- **Cause:** Water damage — rain into a roof left open during tear-off
- **Estimated loss:** USD 510,000
- **Expected match:** `GL-3376-90284`
- **Confidence:** Exact

Policy number stated, corroborated by the insured, the school and division which are scheduled certificate holders on this policy, the contract number, the broker reference and the sender domain.

What it exercises:

- **A named certificate holder as the risk location.** The loss is at a third party's premises, not the insured's yard, so `risk_location` must reach the certificate holder schedule rather than the insured's own address.
- **Conditions precedent evidenced rather than asserted.** The consultant's report addresses each limb of the open roof condition on documents.
- **A head of loss expressly not claimed.** Liquidated damages are named and disclaimed.

### ironbark-excavator-fire

**Ironbark Constructors — excavator fire in transit on I-10** — Inland marine — contractors equipment

- **Policy number on the notice:** `IM-2298-66401`
- **Broker reference:** `SRR/IM/2025/2298`
- **Insured:** Ironbark Constructors, Inc.
- **Claimant:** Ironbark Constructors, Inc.
- **Date of loss:** 14 November 2025
- **Loss location:** Interstate 10 westbound, milepost 168, near Casa Grande, AZ
- **Cause:** Fire — hydraulic line failure onto the exhaust while in transit
- **Estimated loss:** USD 715,950
- **Expected match:** `IM-2298-66401`
- **Confidence:** Exact

Policy number stated and confirmed at item level: the damaged machine is scheduled Item 001 with a matching serial number and limit, both yard addresses are on the policy, and the broker reference and sender domain agree.

What it exercises:

- **Identity down to the serial number.** The match is corroborated by an asset identifier that appears on the policy schedule, which is stronger than an address.
- **A loss location that is on no schedule.** The loss happened on a highway. `risk_location` cannot help, and the match must carry on the other axes — which is how a floater should behave, because the whole point is that the property moves.
- **Another policy expressly ruled out.** The trailer and highway damage are named as belonging to a motor policy that is not in the book.

### aurora-jobsite-theft

**Aurora Gateway Logistics Park — jobsite theft of tools and installation copper** — Inland marine — equipment and installation

- **Policy number on the notice:** *not stated*
- **Broker reference:** `FRC/IM/2025/7741`
- **Insured:** Beacon Mechanical Services, Inc.
- **Claimant:** Beacon Mechanical Services, Inc.
- **Date of loss:** 13 September 2025
- **Loss location:** 19400 East 32nd Parkway, Aurora, CO 80011
- **Cause:** Theft — forced entry to a job trailer and a shared container
- **Estimated loss:** USD 224,300
- **Expected match:** `IM-7741-15530`
- **Confidence:** Strong

No policy number, but the broker reference `FRC/IM/2025/7741` is the one on this policy, two of the stolen items are scheduled equipment on it, and the loss spans both sections of a combined equipment and installation floater. The insured's liability policy cannot answer first-party theft of the insured's own property.

What it exercises:

- **The broker reference distinguishing two policies of one insured.** Insured name and both domains are identical across `GL-8804-27153` and `IM-7741-15530`; the reference and the line of business are the only separators.
- **Scheduled items as corroboration.** Two stolen tools appear on the equipment schedule.
- **Excess-of-builder's-risk.** The notice raises the general contractor's policy, which affects order of response, not identification.

### harborline-delaware-freeze

**Harborline Delaware terminal — frozen sprinkler line** — Commercial property

- **Policy number on the notice:** *not stated*
- **Broker reference:** `TR/PROP/2025/4471`
- **Insured:** Harborline Cold Storage & Logistics, LLC
- **Claimant:** Harborline Cold Storage & Logistics, LLC
- **Date of loss:** 1 February 2026
- **Loss location:** 419 Delaware Terminal Road, New Castle, DE 19720
- **Cause:** Freeze — frozen sprinkler branch line
- **Estimated loss:** USD 1,240,000
- **Expected match:** `CP-4471-88210`
- **Confidence:** Strong

No policy number. The loss address is scheduled Location 003 on the Harborline policy — the insured's secondary premises, not its headquarters — and the broker reference, the insured name, the loss payee for that premises and the sender domain all agree.

What it exercises:

- **Matching to a secondary scheduled location.** The insured's mailing address is in Baltimore and the loss is in Delaware. A matcher that compares the loss address only to `primary_location` misses it; it has to search the whole `policy_locations` schedule.
- **Naming the location that matched.** The candidate card should show Location 003 and *its* sum insured, not the Baltimore headline.
- **A second loss on a policy already claimed on.** Pack 1 is the same policy, a different premises and a different peril.

### windrow-grove-building-k-fire

**Windrow Grove Apartments — dryer fire in Building K** — Commercial property

- **Policy number on the notice:** *not stated*
- **Broker reference:** `KRP/HAB/2025/7735`
- **Insured:** Sundale Property Group, LLC
- **Claimant:** Windrow Grove Apartments, LP
- **Date of loss:** 21 November 2025
- **Loss location:** 6120 East 91st Street, Tulsa, OK 74137 — Building K
- **Cause:** Fire — lint ignition at a clothes dryer
- **Estimated loss:** USD 2,610,000
- **Expected match:** `CP-7735-19042`
- **Confidence:** Strong

No policy number. The complex name and address are scheduled Location 001, the claimant is the additional named insured, the managing agent named in the fire report is the scheduled additional insured, the reported pre-incident structure value of USD 3,150,000 is the per-building limit on the schedule, and the broker reference agrees.

What it exercises:

- **A specialist document that carries no policy reference at all.** A fire department report never states a policy number; everything identifying comes from the covering email and the loss notice.
- **Building letter against building number.** The report calls it Building K and cross-references Building 06 on the site plan, which is how the schedule names it.
- **Two losses, one policy, one year.** This and pack 2 are both on `CP-7735-19042`, different perils and different buildings — the duplicate-candidate check should not confuse them.

### fairmount-mezzanine-collapse

**Fairmount Textile Mills — storage mezzanine collapse** — Commercial property

- **Policy number on the notice:** `CP-2210-55870`
- **Broker reference:** `RK/MFG/2024/2210`
- **Insured:** Fairmount Textile Mills Holdings, Inc.
- **Claimant:** Fairmount Technical Fabrics, Inc.
- **Date of loss:** 14 May 2025
- **Loss location:** 1180 Woodruff Industrial Road, Greenville, SC 29607 — Building 1D
- **Cause:** Collapse — bolted connection failure on a storage mezzanine
- **Estimated loss:** USD 8,700,000
- **Expected match:** `CP-2210-55870`
- **Confidence:** Exact

Policy number stated and corroborated by the insured, a different subsidiary as claimant, Building 1D on the location schedule, the broker reference and the wholesale broker's sender domain.

What it exercises:

- **A second, different subsidiary.** Pack 3 named Piedmont Dye & Finish; this names Fairmount Technical Fabrics. Both are additional named insureds on the same policy.
- **Two live coverage questions raised in the notice itself.** Neither is a matching signal; both belong in warnings.
- **Two claims on one policy in one term.** Together with pack 3 this exercises the duplicate-candidate path on genuinely distinct losses.

### kestrel-ridge-vehicle-impact

**Kestrel Ridge Retail Center — vehicle into the anchor storefront** — Commercial property

- **Policy number on the notice:** `CP-9038-64115`
- **Broker reference:** `HV/RET/2025/9083`
- **Insured:** Kestrel Ridge Holdings, LP
- **Claimant:** Kestrel Ridge Holdings, LP
- **Date of loss:** 6 November 2025
- **Loss location:** 4840 North Eagle Ridge Boulevard, Boise, ID 83713 — Building 02
- **Cause:** Impact by vehicle — pickup through the anchor storefront
- **Estimated loss:** USD 408,500
- **Expected match:** `CP-9083-64115`
- **Confidence:** Possible

The notice states `CP-9038-64115`, which **does not exist** — it is a digit transposition of `CP-9083-64115`, and the broker flags the doubt in the email. Exact lookup returns nothing. The OCR-folded and edit-distance rungs of the `policy_number` comparator should reach it, and if they do not, the broker reference, the address matching scheduled Location 002, the insured, the managing agent and the sender domain all resolve it.

What it exercises:

- **A policy number that is wrong by one transposition.** This is what the `policy_number` comparator's edit-distance rung exists for. A matcher that only does exact lookup returns NO_MATCH on a policy that is plainly in the book.
- **The notice admits the doubt.** A good answer uses that rather than trusting the stated string.
- **Matching to Building 02 rather than the centre.** The address given is the anchor's own street number, which is scheduled Location 002, not the primary location.

### rivergate-copper-theft

**Rivergate Commons Phase II — copper theft from the works** — Construction — builder's risk

- **Policy number on the notice:** *not stated*
- **Broker reference:** `AV/BR/2025/3358`
- **Insured:** Rivergate Development Partners, LLC
- **Claimant:** Vanterra Construction Group, LLC
- **Date of loss:** 17 January 2026
- **Loss location:** 2650 Rivergate Parkway, Charlotte, NC 28273
- **Cause:** Theft — copper stripped from the partially completed works
- **Estimated loss:** USD 394,400
- **Expected match:** `BR-3358-20471`
- **Confidence:** Possible

No policy number, and the reporter's own company is a named insured on the builder's risk *and* the named insured on a liability policy placed through the same broker — with this very project scheduled as designated project P-1 on the liability policy. The discriminator is loss type: every item is first-party damage to or theft of insured project property, and there is no third-party element at all.

What it exercises:

- **One project on two policies.** Rivergate Commons Phase II appears as the project on `BR-3358-20471` and as designated project P-1 on `GL-5529-41836`, both through broker Ashcombe Vail. `project_name` and `risk_location` fire for both candidates.
- **The broker reference as the tiebreak.** `AV/BR/…` versus `AV/GL/…` is the cleanest signal separating them, which is exactly why the reference carries 2.5.
- **Property that belongs to neither insured.** The subcontractor's tool kits are named and excluded from the claim in the notice.

### northfield-site-vandalism

**Northfield Senior Living — overnight vandalism and theft on site** — Construction — builder's risk

- **Policy number on the notice:** *not stated*
- **Broker reference:** `KRP/CAR/2025/6612`
- **Insured:** Sundale Property Group, LLC
- **Claimant:** Halcyon Builders, LLC
- **Date of loss:** 3 January 2026
- **Loss location:** 1725 Northfield Commons Drive, Madison, WI 53704
- **Cause:** Malicious damage and theft
- **Estimated loss:** USD 380,000
- **Expected match:** `BR-6612-77309`
- **Confidence:** Strong

No policy number. The project name and contract number, the site address, both owner entities, the general contractor and the broker reference all match. The building configuration described in the police report — three under construction plus an existing single-storey clinic being converted — matches the policy's project description, and only this policy carries an existing structure section.

What it exercises:

- **A specialist document that identifies the insured only loosely.** The police report says 'Sundale Property Group of Tulsa' and never states an entity suffix or a policy.
- **Existing structure damage as a corroborating signal.** The renovation element is unique to this policy in the book.
- **A warranty gap disclosed by the reporter.** The patrol interval is raised in the email; it is a warning, not a rank.

### cypress-landing-haboob

**Cypress Landing Data Hall B — haboob and wet microburst** — Construction — builder's risk

- **Policy number on the notice:** *not stated*
- **Broker reference:** `SRR/BR/2025/1147`
- **Insured:** Meridian Grid Holdings, LLC
- **Claimant:** Ironbark Constructors, Inc.
- **Date of loss:** 7 July 2026
- **Loss location:** 14900 South Cypress Landing Way, Mesa, AZ 85212
- **Cause:** Windstorm — haboob followed by a wet microburst
- **Estimated loss:** USD 5,900,000
- **Expected match:** `BR-1147-30926`
- **Confidence:** Strong

No policy number. The project name and contract number, the site address, both named insureds, the broker reference and the sender domain all match, and the loss is first-party damage to the works and to owner-supplied equipment scheduled on this policy.

What it exercises:

- **A second loss on the policy that pack 7 mis-referenced.** Here the same project is notified with no policy number at all, and must still reach `BR-1147-30926`.
- **Equipment damage that is not equipment-floater damage.** The switchgear is owner-supplied permanent works, scheduled on the builder's risk, not on the contractor's floater.
- **Deductible selection as a warning.** Which deductible applies turns on whether the equipment had entered commissioning; it changes the net, not the rank.

### tidewater-rooftop-equipment

**Hampton Roads Logistics Center — crane load into a tenant's rooftop plant** — Liability — commercial general liability

- **Policy number on the notice:** *not stated*
- **Broker reference:** `CB/ROOF/2024/3376`
- **Insured:** Tidewater Roofing & Exteriors, LLC
- **Claimant:** Chandler Vale Cold Chain Solutions
- **Date of loss:** 26 August 2025
- **Loss location:** 1900 Bainbridge Logistics Way, Chesapeake, VA 23320
- **Cause:** Impact — crane load swung into a tenant's rooftop condensing units
- **Estimated loss:** USD 182,000
- **Expected match:** `GL-3376-90284`
- **Confidence:** Strong

No policy number. The insured's name and Virginia contractor licence, the broker reference `CB/ROOF/2024/3376`, the sender domain and the building owner — a scheduled certificate holder on this policy — all agree, and the loss is third-party property damage arising from the insured's operations.

What it exercises:

- **Damaged property belonging to a party who is not an insured and not a certificate holder.** The tenant is neither; the building owner is. Identification must not depend on the claimant being on the policy.
- **A second loss on a policy already claimed on.** Pack 10 is the same policy, a different project and a different peril.
- **A recovery question raised in the notice.** The crane hire indemnity affects subrogation, not which policy answers.

### larkspur-fleet-collision

**Larkspur Landscaping — fleet collision with third-party property damage** — Motor — commercial fleet

- **Policy number on the notice:** *not stated*
- **Broker reference:** `GRS/AUTO/2026/0114`
- **Insured:** Larkspur Landscaping & Grounds Management, LLC
- **Claimant:** Ballenger Creek Auto & Tyre
- **Date of loss:** 14 March 2026
- **Loss location:** Ballenger Creek Pike at Elmer Derr Road, Frederick, MD 21703
- **Cause:** Collision — avoiding action, trailer jackknife and departure from the roadway
- **Estimated loss:** USD 320,000
- **Expected match:** `NO_MATCH`
- **Confidence:** No Match

Larkspur Landscaping & Grounds Management, LLC appears nowhere in the book as a named insured, joint name, principal, contractor or loss payee. Greenbel Risk Services is not a broker on any policy and `greenbelrisk.example` is not a broker domain in the book. The loss location is not on any schedule, and the lines sought — commercial motor, workers compensation and standalone environmental — are not written at all.

What it exercises:

- **A plausible near-miss on line of business.** A Bobcat track loader is exactly the class of plant scheduled on two inland marine policies in the book, and the loss is in Maryland where a commercial property policy sits. Neither is a match.
- **No identifying value hits the pool.** With no policy number, no known broker reference, no known domain and no scheduled location, the candidate query should fall back to country plus line of business plus in-force — and the gate should still reject every one of them.
- **Rejecting is the correct answer**, and should be reported with a reason rather than as an empty result.

### meadowcrest-frozen-sprinkler

**Meadowcrest at Zionsville — frozen sprinkler line in an assisted living community** — Commercial property

- **Policy number on the notice:** `CP-6650-11223`
- **Broker reference:** `PWA/PROP/2025/0022`
- **Insured:** Meadowcrest Assisted Living Communities, Inc.
- **Claimant:** Meadowcrest at Zionsville Operating Co., LLC
- **Date of loss:** 26 January 2026
- **Loss location:** 1180 North Ford Road, Zionsville, IN 46077
- **Cause:** Freeze — frozen sprinkler branch line in the east wing attic
- **Estimated loss:** USD 2,900,000
- **Expected match:** `NO_MATCH`
- **Confidence:** No Match

The stated number `CP-6650-11223` does not exist in the book and is not a near variant of anything in it. The insured, both additional named insureds, the broker, the broker reference, the sender domain, the loss location and the state all fail to match. The notice itself discloses that the number came from a possibly stale certificate.

What it exercises:

- **A policy number that looks exactly right and is not.** It shares the `CP-` prefix and the four-plus-five digit shape of the four property policies in the book. Containment and edit-distance rungs must not manufacture a match out of it.
- **Distractors on three axes at once.** Senior living occupancy resembles the Northfield project; the frozen sprinkler cause is identical to pack 13; resident relocation resembles the Windrow Grove tenant relocation head. None of them is this policy.
- **No Indiana risk exists anywhere in the book**, which is the cleanest discriminator.

### bayview-terrace-riser

**Bayview Terrace Condominiums — domestic hot water riser failure** — Commercial property — condominium association

- **Policy number on the notice:** *not stated*
- **Broker reference:** `HCM/BT/2026/0502`
- **Insured:** Bayview Terrace Condominium Association, Inc.
- **Claimant:** Bayview Terrace Condominium Association, Inc.
- **Date of loss:** 2 May 2026
- **Loss location:** 1200 Bayview Terrace Drive, Bremerton, WA 98312
- **Cause:** Escape of water — pitting corrosion failure of a copper hot water riser
- **Estimated loss:** USD 697,400
- **Expected match:** `NO_MATCH`
- **Confidence:** No Match

Bayview Terrace Condominium Association is not a named insured, joint name, managing agent, mortgagee or loss payee anywhere in the book. Harborcrest Community Management is not a broker in the book and `harborcrestmgmt.example` is not a broker domain. Bremerton, Washington is on no schedule and no policy in the book covers a Washington risk or a condominium association form.

What it exercises:

- **The closest habitational near-miss in the set.** Multifamily water loss with resident displacement, reported by a property manager with no policy number — structurally the same shape as packs 4 and 16, which both match. Entity and state are the discriminators.
- **A reporter who is neither broker nor insured.** The sender is the managing agent, so `broker_domain` should not fire at all rather than firing wrongly.
- **A cause that invites a coverage argument** — pitting corrosion against wear and tear — which is irrelevant to identification and must stay out of the score.

### cobalt-ridge-trench-collapse

**Overland Park sewer rehabilitation — trench collapse and serious injury** — Construction — casualty

- **Policy number on the notice:** *not stated*
- **Broker reference:** `OMI/CAS/2026/0033`
- **Insured:** Cobalt Ridge Utility Contractors, LLC
- **Claimant:** City of Overland Park
- **Date of loss:** 19 May 2026
- **Loss location:** 2200 block of West Carbondale Avenue, Overland Park, KS 66214
- **Cause:** Excavation collapse — wall failure beyond the end of the trench box
- **Estimated loss:** USD 1,150,000
- **Expected match:** `NO_MATCH`
- **Confidence:** No Match

Cobalt Ridge Utility Contractors, LLC appears nowhere in the book. Ozark Meridian Insurance Partners is not a broker on any policy and `ozarkmeridian.example` is not a broker domain. Contract `OP-2026-SS-14` is not a contract number or project reference in the book, the site is on no schedule, and the notice states there is no builder's risk or owner-controlled programme on the contract.

What it exercises:

- **The strongest construction near-miss in the set.** A contractor incident with third-party property damage, employee injuries, equipment damage and a contract number — structurally identical to packs 5, 8 and 19, all of which match.
- **A contract number that will hit the SQL pool and must still be rejected.** `OP-2026-SS-14` is long enough to be searched against all four reference columns; nothing in the book contains it.
- **Two policies in the book carry earth-movement exclusions triggered by the insured's own trenching** — precisely this fact pattern, for a different insured in a different state. Peril similarity is not identity.

