# Ironbark Constructors — excavator fire in transit on I-10

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Inland marine — contractors equipment |
| Insured | Ironbark Constructors, Inc. |
| Broker | Sonoran Ridge Risk Advisors, LLC — Desmond Achterberg, Producer |
| Policy number on the notice | `IM-2298-66401` |
| Broker reference | `SRR/IM/2025/2298` |
| Date of loss | 14 November 2025, 13:05 MST |
| Loss location | Interstate 10 westbound, milepost 168, near Casa Grande, AZ |
| Cause | Fire — hydraulic line failure onto the exhaust while in transit |
| Estimated loss | USD 715,950 |

## Expected matching result

| | |
|---|---|
| Expected policy | `IM-2298-66401` |
| Confidence | Exact |

Policy number stated and confirmed at item level: the damaged machine is scheduled Item 001 with a matching serial number and limit, both yard addresses are on the policy, and the broker reference and sender domain agree.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
ironbark-excavator-fire-loss-notice.pdf 2-page completed loss notice
ironbark-equipment-assessors-report.pdf equipment assessor's report
ironbark-equipment-loss-schedule.csv equipment loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Identity down to the serial number.** The match is corroborated by an asset identifier that appears on the policy schedule, which is stronger than an address.
* **A loss location that is on no schedule.** The loss happened on a highway. `risk_location` cannot help, and the match must carry on the other axes — which is how a floater should behave, because the whole point is that the property moves.
* **Another policy expressly ruled out.** The trailer and highway damage are named as belonging to a motor policy that is not in the book.
