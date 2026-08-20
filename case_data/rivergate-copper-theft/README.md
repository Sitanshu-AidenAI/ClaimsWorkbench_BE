# Rivergate Commons Phase II — copper theft from the works

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
| Policy number on the notice | *not stated* |
| Broker reference | `AV/BR/2025/3358` |
| Date of loss | 17 January 2026, overnight, discovered 18 January 07:40 |
| Loss location | 2650 Rivergate Parkway, Charlotte, NC 28273 |
| Cause | Theft — copper stripped from the partially completed works |
| Estimated loss | USD 394,400 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-3358-20471` |
| Confidence | Possible |

No policy number, and the reporter's own company is a named insured on the builder's risk *and* the named insured on a liability policy placed through the same broker — with this very project scheduled as designated project P-1 on the liability policy. The discriminator is loss type: every item is first-party damage to or theft of insured project property, and there is no third-party element at all.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
rivergate-copper-theft-loss-notice.pdf 2-page completed loss notice
rivergate-site-security-report.pdf site security and loss report
rivergate-theft-schedule.csv     schedule of theft and damage
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **One project on two policies.** Rivergate Commons Phase II appears as the project on `BR-3358-20471` and as designated project P-1 on `GL-5529-41836`, both through broker Ashcombe Vail. `project_name` and `risk_location` fire for both candidates.
* **The broker reference as the tiebreak.** `AV/BR/…` versus `AV/GL/…` is the cleanest signal separating them, which is exactly why the reference carries 2.5.
* **Property that belongs to neither insured.** The subcontractor's tool kits are named and excluded from the claim in the notice.
