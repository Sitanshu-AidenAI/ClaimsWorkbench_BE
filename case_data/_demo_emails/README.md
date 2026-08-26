# Two notifications to send in

Send both to **claims.workbench@aidenai.com** — the shared mailbox in `.env`,
polling enabled. Paste the body as plain text; no attachment is needed, because the
body itself is stored as a document (`notification-body.txt`) and extracted from.

Each one matches a policy already on the book, and matches it on the signals that
carry weight in `app.domain.policy_identification`:

| Signal | Weight | In these emails |
| --- | --- | --- |
| Policy number | 5.0 | stated exactly |
| Broker reference | 2.5 | stated exactly |
| Insured name | 2.0 | stated exactly |
| Policy period | 1.5 | date of loss falls inside the term |
| Loss location | 1.3 | the address on the policy's location schedule |
| Insured organisation | 1.2 | stated |
| Insured email domain | 1.0 | the address on the policy |
| Broker name | 0.9 | stated exactly |
| Line of business | 0.6 | construction, matching the policy |

Measured rather than assumed — both notices were run through `identify()` against
the real book:

    01 Meridian Grid   status: confident_match   BR-1147-30926   score=1.000  exact
    02 Rivergate       status: confident_match   BR-3358-20471   score=1.000  exact

Eight signals agree on each, and the policy is `in_force` on the date of loss.

Two of the twelve signals report `not_compared`, and it is worth knowing why. Both
notices state a project name and a contract number, and the **policy book has no
columns for either** — `Policy` carries no `project_name` or `contract_number`, so
there is nothing to compare them against. Those two signals only earn their weight
once the book holds project data. `broker_domain` is likewise `not_compared`,
because the mail arrives from your mailbox rather than the broker's.

`insured_domain` *does* match: each notice quotes the insured's own address as the
book holds it, which is a signal a real broker email would carry.

## Why these two policies

Both are in force on their loss dates, which not every policy on the book is —
several terms have already expired by August 2026.

| | Email 1 | Email 2 |
| --- | --- | --- |
| Policy | `BR-1147-30926` | `BR-3358-20471` |
| Insured | Meridian Grid Holdings, LLC | Rivergate Development Partners, LLC |
| Term | 1 Jul 2025 – 1 Jan 2027 | 15 Apr 2025 – 15 Oct 2026 |
| Limit / excess | $118,500,000 / $100,000 | $65,550,000 / $50,000 |
| Estimated loss | **$4,250,000** | **$185,000** |
| Expected severity | critical, major-loss route | medium |

The two figures are deliberately far apart. $4.25m is over the major-loss threshold
(`major_loss_threshold_minor` = $500,000) and over every handler's authority, so it
routes to the major-loss team and lands blocked on authority — the path you have
just been through. $185,000 sits inside an ordinary handler's limit, so it is the
one you can take from notification to approved without touching a limit.

Both perils are named on their policies (`windstorm` and `water intrusion` on the
first, `fire` on the second), so the coverage check should pass rather than land in
question.
