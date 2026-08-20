# Sable Creek Medical Office Building — falling brick, third-party injury

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Liability — commercial general liability |
| Insured | Vanterra Construction Group, LLC |
| Broker | Ashcombe Vail Commercial Insurance Services — Gerald Nwosu-Fitzgerald, Construction Claims Handler |
| Policy number on the notice | `GL-5529-41836` |
| Broker reference | `AV/GL/2025/5529` |
| Date of loss | 17 March 2026, 10:20 EDT |
| Loss location | 4405 Sable Creek Drive, Fort Mill, SC 29715 |
| Cause | Falling brick — banding failure during a telehandler unload |
| Estimated loss | USD 663,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `GL-5529-41836` |
| Confidence | Exact |

Policy number stated, with the insured, the designated project P-3 on the policy's own schedule, the broker reference and the sender domain all agreeing. The loss type is third-party bodily injury and property damage, which is the liability policy and not the insured's builder's risk.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
sable-creek-falling-brick-loss-notice.pdf 2-page completed loss notice
sable-creek-liability-investigation-report.pdf liability investigation report
sable-creek-quantum-schedule.csv quantum schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Line of business as a discriminator.** Vanterra is a named insured on a builder's risk too. This loss is `liability`; that one is `construction`.
* **Claimant is a third party, not the insured.** `claimant_name` is a member of the public and must not be read as the insured.
* **A scheduled project on a liability policy.** P-3 appears on the GL's location schedule, so `risk_location` corroborates on an axis a property-only reading would miss.
