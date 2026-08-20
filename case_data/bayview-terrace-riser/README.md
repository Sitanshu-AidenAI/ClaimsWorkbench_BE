# Bayview Terrace Condominiums — domestic hot water riser failure

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property — condominium association |
| Insured | Bayview Terrace Condominium Association, Inc. |
| Broker | Harborcrest Community Management, LLC — Thaddeus Oyelowo-Brand, Community Manager |
| Policy number on the notice | *not stated* |
| Broker reference | `HCM/BT/2026/0502` |
| Date of loss | 2 May 2026, between 02:00 and 05:30, discovered 05:35 |
| Loss location | 1200 Bayview Terrace Drive, Bremerton, WA 98312 |
| Cause | Escape of water — pitting corrosion failure of a copper hot water riser |
| Estimated loss | USD 697,400 |

## Expected matching result

| | |
|---|---|
| Expected policy | **NO_MATCH** |
| Confidence | No Match |

Bayview Terrace Condominium Association is not a named insured, joint name, managing agent, mortgagee or loss payee anywhere in the book. Harborcrest Community Management is not a broker in the book and `harborcrestmgmt.example` is not a broker domain. Bremerton, Washington is on no schedule and no policy in the book covers a Washington risk or a condominium association form.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
bayview-terrace-riser-loss-notice.pdf 2-page completed loss notice
bayview-terrace-plumbers-report.pdf plumbing failure report
bayview-terrace-damage-schedule.csv damage schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **The closest habitational near-miss in the set.** Multifamily water loss with resident displacement, reported by a property manager with no policy number — structurally the same shape as packs 4 and 16, which both match. Entity and state are the discriminators.
* **A reporter who is neither broker nor insured.** The sender is the managing agent, so `broker_domain` should not fire at all rather than firing wrongly.
* **A cause that invites a coverage argument** — pitting corrosion against wear and tear — which is irrelevant to identification and must stay out of the score.
