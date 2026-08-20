# Policy documents and the policy book

Two things, and the difference matters:

| | What it is | Used for |
|---|---|---|
| `POL-*.pdf` | The **wordings** — twelve full policy documents | Ingested into the policy library; a clause is cited from here |
| `policy-book.json` | The **book** — the flat `policies` projection | What matching actually runs against |

`docs/policy-matching-fields.md` §7 draws the same seam. Matching compares a notice to
rows in `policies` + `policy_locations`; it does not compare it to a PDF. **Load the book
or nothing will match:**

```bash
uv run python scripts/load_policy_book.py            # idempotent on policy_number
uv run python scripts/load_policy_book.py --reset    # drop the synthetic rows first
```

The loader lives outside `app/` because this is demo data, not application code, and
every row it owns carries a `CWB-SYN-` external id so `--reset` removes exactly what it
inserted and never a real policy.

## The twelve policies

| Policy | Line | Insured | Broker ref | Broker domain | Period |
|---|---|---|---|---|---|
| `CP-4471-88210` | property | Harborline Cold Storage & Logistics, LLC | `TR/PROP/2025/4471` | `talbotrennick.example` | 2025-03-01 → 2026-03-01 |
| `CP-7735-19042` | property | Sundale Property Group, LLC | `KRP/HAB/2025/7735` | `kettleridgerisk.example` | 2025-06-15 → 2026-06-15 |
| `CP-2210-55870` | property | Fairmount Textile Mills Holdings, Inc. | `RK/MFG/2024/2210` | `ridgewaykessler.example` | 2024-09-01 → 2025-09-01 |
| `CP-9083-64115` | property | Kestrel Ridge Holdings, LP | `HV/RET/2025/9083` | `hvagency.example` | 2025-01-01 → 2026-01-01 |
| `BR-3358-20471` | construction | Rivergate Development Partners, LLC | `AV/BR/2025/3358` | `ashcombevail.example` | 2025-04-15 → 2026-10-15 |
| `BR-6612-77309` | construction | Sundale Property Group, LLC | `KRP/CAR/2025/6612` | `kettleridgerisk.example` | 2025-02-01 → 2027-02-01 |
| `BR-1147-30926` | construction | Meridian Grid Holdings, LLC | `SRR/BR/2025/1147` | `sonoranridgerisk.example` | 2025-07-01 → 2027-01-01 |
| `GL-5529-41836` | liability | Vanterra Construction Group, LLC | `AV/GL/2025/5529` | `ashcombevail.example` | 2025-04-01 → 2026-04-01 |
| `GL-8804-27153` | liability | Beacon Mechanical Services, Inc. | `FRC/GL/2025/8804` | `frcig.example` | 2025-08-01 → 2026-08-01 |
| `GL-3376-90284` | liability | Tidewater Roofing & Exteriors, LLC | `CB/ROOF/2024/3376` | `coastlinebrantley.example` | 2024-11-01 → 2025-11-01 |
| `IM-2298-66401` | engineering | Ironbark Constructors, Inc. | `SRR/IM/2025/2298` | `sonoranridgerisk.example` | 2025-05-01 → 2026-05-01 |
| `IM-7741-15530` | engineering | Beacon Mechanical Services, Inc. | `FRC/IM/2025/7741` | `frcig.example` | 2025-08-01 → 2026-08-01 |

Four commercial property · three builder's risk · three contractor general liability ·
two inland marine. 26 scheduled locations across the twelve, because location matching
has to *search* a schedule and then *name* the entry that matched.

## Files

```
policy/
  POL-<number>_<insured>_<line>.pdf   the wording, 3-4 pages, ingest these
  policy-book.json                    the book — load with scripts/load_policy_book.py
  _ground_truth/
    POL-*.json                        structured extract — what extraction should find
    POL-*.txt                         the source text the PDF was rendered from
```

Answer keys sit in `_ground_truth/` rather than beside the PDFs because `.json` and
`.txt` are both in `ALLOWED_EXTENSIONS` — left in place they could be uploaded with the
wording and leak the expected extraction into the retrieval index.

## Cross-links that make matching hard

| Group | Policies | Why it matters |
|---|---|---|
| Vanterra Construction | `BR-3358-20471` + `GL-5529-41836` | Rivergate Commons Phase II is the builder's risk project *and* designated project P-1 on the liability policy, both through broker Ashcombe Vail. Only the broker reference and the line of business separate them. |
| Sundale Property Group | `CP-7735-19042` + `BR-6612-77309` | Same first named insured, two states, two lines. |
| Ironbark Constructors | `BR-1147-30926` + `IM-2298-66401` | Permanent works against contractors equipment — the floater excludes property destined for the project. |
| Beacon Mechanical | `GL-8804-27153` + `IM-7741-15530` | Same carrier, same broker, same insured and broker domains. Nothing but line of business and the broker reference tells them apart. |

See `../case_data/` for the 24 notification packs and
`../case_data/_ground_truth/MATCHING_GROUND_TRUTH.md` for the expected results.
