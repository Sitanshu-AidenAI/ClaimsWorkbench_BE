# Driver Middle School — rain into an open roof during a re-roof

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
| Policy number on the notice | `GL-3376-90284` |
| Broker reference | `CB/ROOF/2024/3376` |
| Date of loss | 18 June 2025, 16:40 EDT |
| Loss location | 4652 Driver Lane, Suffolk, VA 23435 |
| Cause | Water damage — rain into a roof left open during tear-off |
| Estimated loss | USD 510,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `GL-3376-90284` |
| Confidence | Exact |

Policy number stated, corroborated by the insured, the school and division which are scheduled certificate holders on this policy, the contract number, the broker reference and the sender domain.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
driver-middle-school-open-roof-loss-notice.pdf 2-page completed loss notice
driver-school-roofing-consultants-report.pdf roofing consultant's report
driver-school-reinstatement-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A named certificate holder as the risk location.** The loss is at a third party's premises, not the insured's yard, so `risk_location` must reach the certificate holder schedule rather than the insured's own address.
* **Conditions precedent evidenced rather than asserted.** The consultant's report addresses each limb of the open roof condition on documents.
* **A head of loss expressly not claimed.** Liquidated damages are named and disclaimed.
