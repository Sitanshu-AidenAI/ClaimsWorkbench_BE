# Hampton Roads Logistics Center — crane load into a tenant's rooftop plant

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Liability — commercial general liability |
| Insured | Tidewater Roofing & Exteriors, LLC |
| Broker | Coastline Brantley Insurance Advisors — Sylvia Okonjo-Pratt, Producer |
| Policy number on the notice | *not stated* |
| Broker reference | `CB/ROOF/2024/3376` |
| Date of loss | 26 August 2025, 11:15 EDT |
| Loss location | 1900 Bainbridge Logistics Way, Chesapeake, VA 23320 |
| Cause | Impact — crane load swung into a tenant's rooftop condensing units |
| Estimated loss | USD 182,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `GL-3376-90284` |
| Confidence | Strong |

No policy number. The insured's name and Virginia contractor licence, the broker reference `CB/ROOF/2024/3376`, the sender domain and the building owner — a scheduled certificate holder on this policy — all agree, and the loss is third-party property damage arising from the insured's operations.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
tidewater-rooftop-equipment-loss-notice.pdf 2-page completed loss notice
hampton-roads-lifting-incident-report.pdf lifting incident report
hampton-roads-tenant-damage-schedule.csv tenant damage schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Damaged property belonging to a party who is not an insured and not a certificate holder.** The tenant is neither; the building owner is. Identification must not depend on the claimant being on the policy.
* **A second loss on a policy already claimed on.** Pack 10 is the same policy, a different project and a different peril.
* **A recovery question raised in the notice.** The crane hire indemnity affects subrogation, not which policy answers.
