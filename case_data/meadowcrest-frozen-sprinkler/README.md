# Meadowcrest at Zionsville — frozen sprinkler line in an assisted living community

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Meadowcrest Assisted Living Communities, Inc. |
| Broker | Pemberton Wexler Associates, Inc. — Anselm Duchamp-Rivera, Producer |
| Policy number on the notice | `CP-6650-11223` |
| Broker reference | `PWA/PROP/2025/0022` |
| Date of loss | 26 January 2026, 03:50 EST, discovered 04:15 |
| Loss location | 1180 North Ford Road, Zionsville, IN 46077 |
| Cause | Freeze — frozen sprinkler branch line in the east wing attic |
| Estimated loss | USD 2,900,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | **NO_MATCH** |
| Confidence | No Match |

The stated number `CP-6650-11223` does not exist in the book and is not a near variant of anything in it. The insured, both additional named insureds, the broker, the broker reference, the sender domain, the loss location and the state all fail to match. The notice itself discloses that the number came from a possibly stale certificate.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
meadowcrest-frozen-sprinkler-loss-notice.pdf 2-page completed loss notice
meadowcrest-restoration-report.pdf restoration contractor's report
meadowcrest-reinstatement-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A policy number that looks exactly right and is not.** It shares the `CP-` prefix and the four-plus-five digit shape of the four property policies in the book. Containment and edit-distance rungs must not manufacture a match out of it.
* **Distractors on three axes at once.** Senior living occupancy resembles the Northfield project; the frozen sprinkler cause is identical to pack 13; resident relocation resembles the Windrow Grove tenant relocation head. None of them is this policy.
* **No Indiana risk exists anywhere in the book**, which is the cleanest discriminator.
