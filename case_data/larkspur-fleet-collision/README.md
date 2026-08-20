# Larkspur Landscaping — fleet collision with third-party property damage

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Motor — commercial fleet |
| Insured | Larkspur Landscaping & Grounds Management, LLC |
| Broker | Greenbel Risk Services, LLC — Wendeline Kirchhoff-Baptiste, Producer |
| Policy number on the notice | *not stated* |
| Broker reference | `GRS/AUTO/2026/0114` |
| Date of loss | 14 March 2026, 07:48 EDT |
| Loss location | Ballenger Creek Pike at Elmer Derr Road, Frederick, MD 21703 |
| Cause | Collision — avoiding action, trailer jackknife and departure from the roadway |
| Estimated loss | USD 320,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | **NO_MATCH** |
| Confidence | No Match |

Larkspur Landscaping & Grounds Management, LLC appears nowhere in the book as a named insured, joint name, principal, contractor or loss payee. Greenbel Risk Services is not a broker on any policy and `greenbelrisk.example` is not a broker domain in the book. The loss location is not on any schedule, and the lines sought — commercial motor, workers compensation and standalone environmental — are not written at all.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
larkspur-fleet-collision-loss-notice.pdf 2-page completed loss notice
larkspur-motor-assessors-report.pdf motor assessor's report
larkspur-damage-schedule.csv     damage schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A plausible near-miss on line of business.** A Bobcat track loader is exactly the class of plant scheduled on two inland marine policies in the book, and the loss is in Maryland where a commercial property policy sits. Neither is a match.
* **No identifying value hits the pool.** With no policy number, no known broker reference, no known domain and no scheduled location, the candidate query should fall back to country plus line of business plus in-force — and the gate should still reject every one of them.
* **Rejecting is the correct answer**, and should be reported with a reason rather than as an empty result.
