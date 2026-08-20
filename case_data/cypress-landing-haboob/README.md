# Cypress Landing Data Hall B — haboob and wet microburst

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
| Policy number on the notice | *not stated* |
| Broker reference | `SRR/BR/2025/1147` |
| Date of loss | 7 July 2026, 18:34 MST |
| Loss location | 14900 South Cypress Landing Way, Mesa, AZ 85212 |
| Cause | Windstorm — haboob followed by a wet microburst |
| Estimated loss | USD 5,900,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-1147-30926` |
| Confidence | Strong |

No policy number. The project name and contract number, the site address, both named insureds, the broker reference and the sender domain all match, and the loss is first-party damage to the works and to owner-supplied equipment scheduled on this policy.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
cypress-landing-haboob-loss-notice.pdf 2-page completed loss notice
cypress-landing-electrical-assessment.pdf electrical equipment assessment
cypress-landing-storm-damage-schedule.csv storm damage schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A second loss on the policy that pack 7 mis-referenced.** Here the same project is notified with no policy number at all, and must still reach `BR-1147-30926`.
* **Equipment damage that is not equipment-floater damage.** The switchgear is owner-supplied permanent works, scheduled on the builder's risk, not on the contractor's floater.
* **Deductible selection as a warning.** Which deductible applies turns on whether the equipment had entered commissioning; it changes the net, not the rank.
