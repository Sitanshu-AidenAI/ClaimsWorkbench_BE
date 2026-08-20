# Harborline Delaware terminal — frozen sprinkler line

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Commercial property |
| Insured | Harborline Cold Storage & Logistics, LLC |
| Broker | Talbot & Rennick Insurance Brokers, Inc. — Diane Whitcomb-Reyes, Account Executive |
| Policy number on the notice | *not stated* |
| Broker reference | `TR/PROP/2025/4471` |
| Date of loss | 1 February 2026, discovered 05:50 EST |
| Loss location | 419 Delaware Terminal Road, New Castle, DE 19720 |
| Cause | Freeze — frozen sprinkler branch line |
| Estimated loss | USD 1,240,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `CP-4471-88210` |
| Confidence | Strong |

No policy number. The loss address is scheduled Location 003 on the Harborline policy — the insured's secondary premises, not its headquarters — and the broker reference, the insured name, the loss payee for that premises and the sender domain all agree.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
harborline-delaware-freeze-loss-notice.pdf 2-page completed loss notice
harborline-delaware-mitigation-report.pdf emergency mitigation report
harborline-delaware-mitigation-schedule.csv mitigation cost schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **Matching to a secondary scheduled location.** The insured's mailing address is in Baltimore and the loss is in Delaware. A matcher that compares the loss address only to `primary_location` misses it; it has to search the whole `policy_locations` schedule.
* **Naming the location that matched.** The candidate card should show Location 003 and *its* sum insured, not the Baltimore headline.
* **A second loss on a policy already claimed on.** Pack 1 is the same policy, a different premises and a different peril.
