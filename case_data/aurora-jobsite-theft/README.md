# Aurora Gateway Logistics Park — jobsite theft of tools and installation copper

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Inland marine — equipment and installation |
| Insured | Beacon Mechanical Services, Inc. |
| Broker | Front Range Commercial Insurance Group, Inc. — Hollis Baumgartner-Reyes, Account Executive |
| Policy number on the notice | *not stated* |
| Broker reference | `FRC/IM/2025/7741` |
| Date of loss | 13 September 2025, overnight, discovered 14 September 06:20 |
| Loss location | 19400 East 32nd Parkway, Aurora, CO 80011 |
| Cause | Theft — forced entry to a job trailer and a shared container |
| Estimated loss | USD 224,300 |

## Expected matching result

| | |
|---|---|
| Expected policy | `IM-7741-15530` |
| Confidence | Strong |

No policy number, but the broker reference `FRC/IM/2025/7741` is the one on this policy, two of the stolen items are scheduled equipment on it, and the loss spans both sections of a combined equipment and installation floater. The insured's liability policy cannot answer first-party theft of the insured's own property.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
aurora-jobsite-theft-loss-notice.pdf 2-page completed loss notice
aurora-theft-investigation-report.pdf theft investigation report
aurora-theft-schedule.csv        schedule of property taken
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **The broker reference distinguishing two policies of one insured.** Insured name and both domains are identical across `GL-8804-27153` and `IM-7741-15530`; the reference and the line of business are the only separators.
* **Scheduled items as corroboration.** Two stolen tools appear on the equipment schedule.
* **Excess-of-builder's-risk.** The notice raises the general contractor's policy, which affects order of response, not identification.
