# Fairmount Textile Mills — dye house fire

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Fairmount Textile Mills Holdings, Inc. |
| Broker | Ridgeway Kessler Brokerage Services — Priya Raghunathan-Lowe, Wholesale Broker |
| Policy number on the notice | `CP-2210-55870` |
| Broker reference | `RK/MFG/2024/2210` |
| Date of loss | 13 August 2025, 21:04 EDT |
| Loss location | 1180 Woodruff Industrial Road, Greenville, SC 29607 |
| Cause | Fire — hydraulic fluid release onto a hot thermal oil line |
| Estimated loss | USD 30,500,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-2210-55870` |
| Confidence | Exact |

Policy number stated, with the insured, the additional named insured as claimant, scheduled Location 001, the broker reference and the wholesale broker's sender domain all agreeing.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
fairmount-dye-house-fire-loss-notice.pdf 2-page completed loss notice
fairmount-forensic-engineers-report.pdf forensic engineer's report
fairmount-stock-loss-schedule.csv stock loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A subsidiary as claimant.** Piedmont Dye & Finish, LLC is an additional named insured, not the first named insured — `insured_name` must match against joint names.
* **A warranty breach that is not a matching signal.** The lint log gap is a coverage defence recorded in the engineer's report; it must not move the rank.
* **Quantum split across documents.** The stock schedule totals separately from the engineer's reinstatement figures, and the email states a third, higher number.
