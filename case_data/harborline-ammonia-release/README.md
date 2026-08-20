# Harborline Cold Storage — ammonia release and product loss

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Harborline Cold Storage & Logistics, LLC |
| Broker | Talbot & Rennick Insurance Brokers, Inc. — Diane Whitcomb-Reyes, Account Executive |
| Policy number on the notice | `CP-4471-88210` |
| Broker reference | `TR/PROP/2025/4471` |
| Date of loss | 10 January 2026, 04:20 EST |
| Loss location | 2870 Patapsco Industrial Parkway, Baltimore, MD 21226 |
| Cause | Ammonia release — weld failure on a liquid ammonia header |
| Estimated loss | USD 3,700,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-4471-88210` |
| Confidence | Exact |

The policy number is stated and every corroborating axis agrees: insured name, the loss address is scheduled Location 001, the broker reference is the one on the policy, and the sender domain is the policy's broker domain. This is the control case — if this does not match, nothing will.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
harborline-ammonia-release-loss-notice.pdf 2-page completed loss notice
harborline-refrigeration-engineers-report.pdf refrigeration engineer's report
harborline-product-loss-schedule.csv product loss schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Every signal firing at once.** Policy number, broker reference, insured name, risk location, broker domain and policy period all agree. The ladder should reach `exact` on the number alone and be corroborated on five further axes.
* **Bailee exposure.** Most of the product is customer-owned, so the value at risk sits under the personal-property-of-others limit rather than the insured's own stock.
* **A figure stated twice.** USD 3,700,000 appears in the covering email and on the loss notice; the product schedule totals separately. A citation naming the right document is doing real work.
