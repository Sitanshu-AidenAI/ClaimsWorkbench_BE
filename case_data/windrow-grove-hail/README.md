# Windrow Grove Apartments — hail and straight-line wind

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
| Policy number on the notice | `CP-7735-19042` |
| Broker reference | `KRP/HAB/2025/7735` |
| Date of loss | 19 April 2026, 19:45 CDT |
| Loss location | 6120 East 91st Street, Tulsa, OK 74137 |
| Cause | Hail and straight-line wind |
| Estimated loss | USD 2,400,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-7735-19042` |
| Confidence | Exact |

Policy number stated and corroborated by the insured, the additional named insured as claimant, the scheduled Location 001 address, the broker reference and the broker's sender domain.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
windrow-grove-hail-loss-notice.pdf 2-page completed loss notice
windrow-grove-roof-consultants-report.pdf roof consultant's report
windrow-grove-reinstatement-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Claimant is not the named insured.** The notice names Sundale Property Group as insured and Windrow Grove Apartments, LP as claimant. Both are on the policy — the second as insured organisation — so `insured_name` must compare against joint names.
* **A coverage argument that is not an identity signal.** The cosmetic damage dispute belongs in warnings, not in the score.
* **Per-location excess.** The candidate card should show Location 001's deductible, not the policy headline.
