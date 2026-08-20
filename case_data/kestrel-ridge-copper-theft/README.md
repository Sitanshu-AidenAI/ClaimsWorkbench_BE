# Kestrel Ridge Retail Center — copper theft and consequent water damage

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
| Policy number on the notice | *not stated* |
| Broker reference | `HV/RET/2025/9083` |
| Date of loss | 2 March 2026, between 23:00 and 03:00, discovered 06:35 |
| Loss location | 4820 - 4890 North Eagle Ridge Boulevard, Boise, ID 83713 |
| Cause | Theft of copper, and water damage consequent on a cut live water line |
| Estimated loss | USD 425,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-9083-64115` |
| Confidence | Strong |

No policy number is stated. The broker reference `HV/RET/2025/9083`, the property address matching scheduled Location 001, the named insured, the managing agent who is the scheduled additional insured, and the sender domain `hvagency.example` resolve to one policy with no competing candidate in the book.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
kestrel-ridge-copper-theft-loss-notice.pdf 2-page completed loss notice
kestrel-ridge-restoration-assessment.pdf restoration contractor's assessment
kestrel-ridge-mitigation-schedule.csv mitigation and reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Identification without a policy number.** The heaviest signal is absent, so the match has to be carried by broker reference at 2.5 and insured name at 2.0, corroborated across the risk, broker and cover axes.
* **The broker reference doing the work.** This is the case the `broker_reference` signal exists for — the broker's own scheme reference is the only hard identifier present.
* **Reporter is not the insured.** The reporter is the broker; the managing agent is the site contact; the insured is a Delaware LP that never appears as a sender.
