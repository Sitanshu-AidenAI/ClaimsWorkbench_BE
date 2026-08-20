# Overland Park sewer rehabilitation — trench collapse and serious injury

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Construction — casualty |
| Insured | Cobalt Ridge Utility Contractors, LLC |
| Broker | Ozark Meridian Insurance Partners — Henrietta Balogun-Sawicki, Account Executive |
| Policy number on the notice | *not stated* |
| Broker reference | `OMI/CAS/2026/0033` |
| Date of loss | 19 May 2026, 09:34 CDT |
| Loss location | 2200 block of West Carbondale Avenue, Overland Park, KS 66214 |
| Cause | Excavation collapse — wall failure beyond the end of the trench box |
| Estimated loss | USD 1,150,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | **NO_MATCH** |
| Confidence | No Match |

Cobalt Ridge Utility Contractors, LLC appears nowhere in the book. Ozark Meridian Insurance Partners is not a broker on any policy and `ozarkmeridian.example` is not a broker domain. Contract `OP-2026-SS-14` is not a contract number or project reference in the book, the site is on no schedule, and the notice states there is no builder's risk or owner-controlled programme on the contract.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
cobalt-ridge-trench-collapse-loss-notice.pdf 2-page completed loss notice
cobalt-ridge-geotechnical-report.pdf geotechnical investigation report
cobalt-ridge-loss-schedule.csv   loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **The strongest construction near-miss in the set.** A contractor incident with third-party property damage, employee injuries, equipment damage and a contract number — structurally identical to packs 5, 8 and 19, all of which match.
* **A contract number that will hit the SQL pool and must still be rejected.** `OP-2026-SS-14` is long enough to be searched against all four reference columns; nothing in the book contains it.
* **Two policies in the book carry earth-movement exclusions triggered by the insured's own trenching** — precisely this fact pattern, for a different insured in a different state. Peril similarity is not identity.
