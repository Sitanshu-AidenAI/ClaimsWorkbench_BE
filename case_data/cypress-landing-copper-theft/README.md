# Cypress Landing Data Hall B — copper theft from the works

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Construction — builder's risk |
| Insured | Meridian Grid Holdings, LLC |
| Broker | Sonoran Ridge Risk Advisors, LLC — Desmond Achterberg, Producer |
| Policy number on the notice | `IM-2298-66401` |
| Broker reference | `SRR/BR/2025/1147` |
| Date of loss | 11 April 2026, between 01:20 and 03:05, discovered 06:15 |
| Loss location | 14900 South Cypress Landing Way, Mesa, AZ 85212 |
| Cause | Theft — copper stripped from the works |
| Estimated loss | USD 1,318,100 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-1147-30926` |
| Confidence | Possible |

The notice quotes `IM-2298-66401`, which is a **real policy for the same corporate group but the wrong one for this loss**. The `policy_number` signal will find that policy and score it — and every other signal contradicts it: the project name, the contract number, the site address, the broker reference and the owner as insured all belong to `BR-1147-30926`. A correct answer overrides the stated number on the weight of the other eleven signals.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
cypress-landing-copper-theft-loss-notice.pdf 2-page completed loss notice
cypress-landing-risk-managers-report.pdf risk manager's allocation report
cypress-landing-loss-allocation-schedule.csv loss allocation schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **The heaviest signal pointing at the wrong answer.** `policy_number` carries 5.0 and resolves cleanly to the equipment floater. Everything else — project, contract, site, broker reference, insured — points at the builder's risk. This is the case that tests whether the ladder reads *which axes* agreed rather than only the total.
* **A defensible split.** USD 54,100 genuinely does belong on `IM-2298-66401`, so a two-policy allocation is a better answer than either policy alone.
* **A warning that is not a rank.** The guard patrol gap is a warranty question and must not lower the builder's risk candidate below the equipment floater.
