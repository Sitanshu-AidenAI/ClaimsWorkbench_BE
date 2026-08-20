# Cherry Creek Medical Campus — escape of water from a new chilled water riser

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Liability — commercial general liability |
| Insured | Beacon Mechanical Services, Inc. |
| Broker | Front Range Commercial Insurance Group, Inc. — Hollis Baumgartner-Reyes, Account Executive |
| Policy number on the notice | *not stated* |
| Broker reference | `FRC/GL/2025/8804` |
| Date of loss | 7 February 2026, from about 19:30, discovered 07:10 on 8 February |
| Loss location | 3400 South Cherry Creek Drive North, Denver, CO 80209 |
| Cause | Escape of water — grooved coupling separation on a newly installed riser |
| Estimated loss | USD 590,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `GL-8804-27153` |
| Confidence | Possible |

No policy number and the insured holds two policies with the same carrier, the same broker and the same broker domain. The discriminators are the loss type — four fifths is third-party damage to an occupied building, which is liability — and the fact that the building owner is a scheduled additional insured on the liability policy for this very project. The `FRC/GL/…` broker reference points the same way.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
cherry-creek-water-damage-loss-notice.pdf 2-page completed loss notice
cherry-creek-mechanical-failure-report.pdf mechanical failure investigation report
cherry-creek-loss-allocation-schedule.csv loss allocation schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Two policies, one insured, one carrier, one broker.** Insured name, insured domain, broker name and broker domain are all identical across both candidates and therefore discriminate nothing. Only line of business and the broker reference separate them.
* **A split allocation is the right answer.** The schedule states which head belongs on which policy, so a matcher that returns one policy and ignores the other is only four-fifths right.
* **A condition that changes the retention, not the rank.** The water damage control condition is a warning.
