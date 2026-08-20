# Windrow Grove Apartments — dryer fire in Building K

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Sundale Property Group, LLC |
| Broker | Kettleridge Risk Partners, LLC — Marcus Adeyemi-Croft, Producer |
| Policy number on the notice | *not stated* |
| Broker reference | `KRP/HAB/2025/7735` |
| Date of loss | 21 November 2025, 02:14 CST |
| Loss location | 6120 East 91st Street, Tulsa, OK 74137 — Building K |
| Cause | Fire — lint ignition at a clothes dryer |
| Estimated loss | USD 2,610,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-7735-19042` |
| Confidence | Strong |

No policy number. The complex name and address are scheduled Location 001, the claimant is the additional named insured, the managing agent named in the fire report is the scheduled additional insured, the reported pre-incident structure value of USD 3,150,000 is the per-building limit on the schedule, and the broker reference agrees.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
windrow-grove-building-k-fire-loss-notice.pdf 2-page completed loss notice
windrow-grove-fire-investigation-report.pdf fire investigation report
windrow-grove-fire-loss-schedule.csv fire loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A specialist document that carries no policy reference at all.** A fire department report never states a policy number; everything identifying comes from the covering email and the loss notice.
* **Building letter against building number.** The report calls it Building K and cross-references Building 06 on the site plan, which is how the schedule names it.
* **Two losses, one policy, one year.** This and pack 2 are both on `CP-7735-19042`, different perils and different buildings — the duplicate-candidate check should not confuse them.
