# Rivergate Commons Phase II — dropped load during a crane pick

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Construction — builder's risk |
| Insured | Rivergate Development Partners, LLC |
| Broker | Ashcombe Vail Commercial Insurance Services — Gerald Nwosu-Fitzgerald, Construction Claims Handler |
| Policy number on the notice | `BR-3358-20471` |
| Broker reference | `AV/BR/2025/3358` |
| Date of loss | 26 February 2026, 14:52 EST |
| Loss location | 2650 Rivergate Parkway, Charlotte, NC 28273 |
| Cause | Dropped load — wind gust during a crane pick |
| Estimated loss | USD 407,200 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-3358-20471` |
| Confidence | Exact |

Policy number stated, and corroborated by the project name and contract number, the site address, both named insureds appearing as insured and claimant, the broker reference and the sender domain.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
rivergate-dropped-load-loss-notice.pdf 2-page completed loss notice
rivergate-structural-engineers-report.pdf structural engineer's report
rivergate-reinstatement-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Construction identity signals.** `project_name` at 1.6 and `contract_number` at 2.2 both fire and both agree, which on a construction risk is stronger evidence than the insured name.
* **The insured is not the contractor.** The owner is the first named insured and Vanterra is the contractor and the claimant; both are on the policy.
* **Ruling the other policy out in the notice.** The email states there is no third-party element, which is what keeps this off the contractor's CGL — the discriminator pack 17 makes the matcher work out for itself.
