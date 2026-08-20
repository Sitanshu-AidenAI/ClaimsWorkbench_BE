# Fairmount Textile Mills — storage mezzanine collapse

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
| Date of loss | 14 May 2025, 15:40 EDT |
| Loss location | 1180 Woodruff Industrial Road, Greenville, SC 29607 — Building 1D |
| Cause | Collapse — bolted connection failure on a storage mezzanine |
| Estimated loss | USD 8,700,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-2210-55870` |
| Confidence | Exact |

Policy number stated and corroborated by the insured, a different subsidiary as claimant, Building 1D on the location schedule, the broker reference and the wholesale broker's sender domain.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
fairmount-mezzanine-collapse-loss-notice.pdf 2-page completed loss notice
fairmount-mezzanine-engineers-report.pdf structural engineer's report
fairmount-mezzanine-loss-schedule.csv loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A second, different subsidiary.** Pack 3 named Piedmont Dye & Finish; this names Fairmount Technical Fabrics. Both are additional named insureds on the same policy.
* **Two live coverage questions raised in the notice itself.** Neither is a matching signal; both belong in warnings.
* **Two claims on one policy in one term.** Together with pack 3 this exercises the duplicate-candidate path on genuinely distinct losses.
