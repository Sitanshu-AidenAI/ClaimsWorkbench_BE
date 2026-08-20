# FNOL notification packs

Twenty-four notices, each as a pack: a broker's covering email and the documents
attached to it. Same shape as `../demo-data/`, so `app/db/demo.py` reads them without
changes — any directory containing a `broker-notification.eml` is a pack.

Everything is invented. Companies, people, policy numbers and addresses are not real,
and every email address uses a `.example` domain, which RFC 2606 reserves so it can
never be registered.

## What a pack contains

```
<pack>/
  broker-notification.eml     the covering email — subject, body, envelope
  <slug>-loss-notice.pdf      2-page completed loss notice
  <slug>-<specialist>.pdf     engineer, adjuster, police, fire or consultant report
  <slug>-<schedule>.csv       the itemised figures
  README.md                   the scenario and the expected match
```

**No policy schedule in the pack**, unlike `demo-data/`. Those packs demonstrate
extraction and attach the wording; these demonstrate *identification*. Attaching the
schedule would hand the matcher the policy number and destroy the ten notices that
deliberately carry none. The wordings are in `../policy/`.

## Before the packs will match anything

Matching runs against the `policies` table, not against the PDFs in `../policy/`.
Load the book first, or every notice will fail to match for a reason that has nothing
to do with the notice:

```bash
uv run python scripts/load_policy_book.py
```

## Rebuilding

```bash
uv run python scripts/build_fnol_packs.py          # the 24 packs
uv run python scripts/build_fnol_ground_truth.py   # the answer key
```

Scenarios live in `scripts/fnol_scenarios.py`. The answer key is generated from the
same source as the documents, so the two cannot drift apart.

## Distribution

| | Packs |
|---|---|
| `exact` — policy number stated and corroborated | 8 |
| `strong` — no policy number, resolved on context | 8 |
| `possible` — genuinely ambiguous, still resolvable | 4 |
| **NO_MATCH** — nothing in the book answers | 4 |
| **Total** | **24** |

Among the 20 matchable packs: **10 state a policy number, 10 do not.** Every one of the
12 policies is exercised by at least one pack.

## The four hard ones

| Pack | The problem |
|---|---|
| `cypress-landing-copper-theft` | Quotes a **real policy number for the wrong policy** — the contractor's equipment floater, when 96% of the loss is permanent works belonging on the builder's risk. The heaviest signal points at the wrong answer. |
| `cherry-creek-water-damage` | No number, and the insured holds two policies with the same carrier, broker and domains. Only line of business and the broker reference separate them. |
| `kestrel-ridge-vehicle-impact` | States `CP-9038-64115`, a **digit transposition** of a real number. Exact lookup returns nothing. |
| `rivergate-copper-theft` | No number, and the project is scheduled on *both* the builder's risk and the contractor's liability policy, placed through the same broker. |

## The four NO_MATCH packs

Each carries deliberate distractors so that rejecting is a decision rather than an
absence of evidence: a Maryland contractor with a track loader of a class scheduled on
two floaters; a fabricated number in the book's own `CP-` format on a senior-living
frozen-sprinkler loss; a habitational water loss reported by a property manager with no
policy number, structurally identical to two packs that do match; and a trench collapse
whose fact pattern is precisely what two liability policies in the book carry earth
movement exclusions for — for a different insured in a different state.

## All 24

| Pack | Line | Number on notice | Broker ref | Expected | Confidence |
|---|---|---|---|---|---|
| [`harborline-ammonia-release`](harborline-ammonia-release/) | Commercial property | `CP-4471-88210` | `TR/PROP/2025/4471` | `CP-4471-88210` | Exact |
| [`windrow-grove-hail`](windrow-grove-hail/) | Commercial property | `CP-7735-19042` | `KRP/HAB/2025/7735` | `CP-7735-19042` | Exact |
| [`fairmount-dye-house-fire`](fairmount-dye-house-fire/) | Commercial property | `CP-2210-55870` | `RK/MFG/2024/2210` | `CP-2210-55870` | Exact |
| [`kestrel-ridge-copper-theft`](kestrel-ridge-copper-theft/) | Commercial property | — | `HV/RET/2025/9083` | `CP-9083-64115` | Strong |
| [`rivergate-dropped-load`](rivergate-dropped-load/) | Construction — builder's risk | `BR-3358-20471` | `AV/BR/2025/3358` | `BR-3358-20471` | Exact |
| [`northfield-water-intrusion`](northfield-water-intrusion/) | Construction — builder's risk | — | `KRP/CAR/2025/6612` | `BR-6612-77309` | Strong |
| [`cypress-landing-copper-theft`](cypress-landing-copper-theft/) | Construction — builder's risk | `IM-2298-66401` | `SRR/BR/2025/1147` | `BR-1147-30926` | Possible |
| [`sable-creek-falling-brick`](sable-creek-falling-brick/) | Liability — commercial general liability | `GL-5529-41836` | `AV/GL/2025/5529` | `GL-5529-41836` | Exact |
| [`cherry-creek-water-damage`](cherry-creek-water-damage/) | Liability — commercial general liability | — | `FRC/GL/2025/8804` | `GL-8804-27153` | Possible |
| [`driver-middle-school-open-roof`](driver-middle-school-open-roof/) | Liability — commercial general liability | `GL-3376-90284` | `CB/ROOF/2024/3376` | `GL-3376-90284` | Exact |
| [`ironbark-excavator-fire`](ironbark-excavator-fire/) | Inland marine — contractors equipment | `IM-2298-66401` | `SRR/IM/2025/2298` | `IM-2298-66401` | Exact |
| [`aurora-jobsite-theft`](aurora-jobsite-theft/) | Inland marine — equipment and installation | — | `FRC/IM/2025/7741` | `IM-7741-15530` | Strong |
| [`harborline-delaware-freeze`](harborline-delaware-freeze/) | Commercial property | — | `TR/PROP/2025/4471` | `CP-4471-88210` | Strong |
| [`windrow-grove-building-k-fire`](windrow-grove-building-k-fire/) | Commercial property | — | `KRP/HAB/2025/7735` | `CP-7735-19042` | Strong |
| [`fairmount-mezzanine-collapse`](fairmount-mezzanine-collapse/) | Commercial property | `CP-2210-55870` | `RK/MFG/2024/2210` | `CP-2210-55870` | Exact |
| [`kestrel-ridge-vehicle-impact`](kestrel-ridge-vehicle-impact/) | Commercial property | `CP-9038-64115` | `HV/RET/2025/9083` | `CP-9083-64115` | Possible |
| [`rivergate-copper-theft`](rivergate-copper-theft/) | Construction — builder's risk | — | `AV/BR/2025/3358` | `BR-3358-20471` | Possible |
| [`northfield-site-vandalism`](northfield-site-vandalism/) | Construction — builder's risk | — | `KRP/CAR/2025/6612` | `BR-6612-77309` | Strong |
| [`cypress-landing-haboob`](cypress-landing-haboob/) | Construction — builder's risk | — | `SRR/BR/2025/1147` | `BR-1147-30926` | Strong |
| [`tidewater-rooftop-equipment`](tidewater-rooftop-equipment/) | Liability — commercial general liability | — | `CB/ROOF/2024/3376` | `GL-3376-90284` | Strong |
| [`larkspur-fleet-collision`](larkspur-fleet-collision/) | Motor — commercial fleet | — | `GRS/AUTO/2026/0114` | **NO_MATCH** | No Match |
| [`meadowcrest-frozen-sprinkler`](meadowcrest-frozen-sprinkler/) | Commercial property | `CP-6650-11223` | `PWA/PROP/2025/0022` | **NO_MATCH** | No Match |
| [`bayview-terrace-riser`](bayview-terrace-riser/) | Commercial property — condominium association | — | `HCM/BT/2026/0502` | **NO_MATCH** | No Match |
| [`cobalt-ridge-trench-collapse`](cobalt-ridge-trench-collapse/) | Construction — casualty | — | `OMI/CAS/2026/0033` | **NO_MATCH** | No Match |

Full reasoning per pack: `_ground_truth/MATCHING_GROUND_TRUTH.md`.
