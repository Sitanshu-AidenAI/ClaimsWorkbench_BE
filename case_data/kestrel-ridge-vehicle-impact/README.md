# Kestrel Ridge Retail Center — vehicle into the anchor storefront

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Kestrel Ridge Holdings, LP |
| Broker | Harkness-Vaillancourt Insurance Agency — Renata Sjoberg-Ellis, Account Manager |
| Policy number on the notice | `CP-9038-64115` |
| Broker reference | `HV/RET/2025/9083` |
| Date of loss | 6 November 2025, 07:52 MST |
| Loss location | 4840 North Eagle Ridge Boulevard, Boise, ID 83713 — Building 02 |
| Cause | Impact by vehicle — pickup through the anchor storefront |
| Estimated loss | USD 408,500 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-9083-64115` |
| Confidence | Possible |

The notice states `CP-9038-64115`, which **does not exist** — it is a digit transposition of `CP-9083-64115`, and the broker flags the doubt in the email. Exact lookup returns nothing. The OCR-folded and edit-distance rungs of the `policy_number` comparator should reach it, and if they do not, the broker reference, the address matching scheduled Location 002, the insured, the managing agent and the sender domain all resolve it.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
kestrel-ridge-vehicle-impact-loss-notice.pdf 2-page completed loss notice
kestrel-ridge-structural-assessment.pdf structural assessment
kestrel-ridge-vehicle-impact-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A policy number that is wrong by one transposition.** This is what the `policy_number` comparator's edit-distance rung exists for. A matcher that only does exact lookup returns NO_MATCH on a policy that is plainly in the book.
* **The notice admits the doubt.** A good answer uses that rather than trusting the stated string.
* **Matching to Building 02 rather than the centre.** The address given is the anchor's own street number, which is scheduled Location 002, not the primary location.
