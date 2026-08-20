# Northfield Senior Living — water intrusion into an undried structure

One notice, as it arrives: a covering email and three attachments that agree
with each other.

Everything here is invented — the companies, the people, the policy number and
the addresses are not real, and the email domains use `.example`, which RFC 2606
reserves so it can never be registered.

## The scenario

| | |
|---|---|
| Line of business | Construction — builder's risk |
| Insured | Sundale Property Group, LLC |
| Broker | Kettleridge Risk Partners, LLC — Marcus Adeyemi-Croft, Producer |
| Policy number on the notice | *not stated* |
| Broker reference | `KRP/CAR/2025/6612` |
| Date of loss | 18 October 2025, overnight, discovered 20 October 06:40 |
| Loss location | 1725 Northfield Commons Drive, Madison, WI 53704 |
| Cause | Water intrusion — wind-driven rain into an incomplete structure |
| Estimated loss | USD 915,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-6612-77309` |
| Confidence | Strong |

No policy number. The broker reference `KRP/CAR/2025/6612`, the project name and contract number, the site address, both owner entities and the general contractor all match one construction policy. Sundale's other policy is a Tulsa habitational property risk that cannot answer a Wisconsin construction loss.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
northfield-water-intrusion-loss-notice.pdf 2-page completed loss notice
northfield-drying-and-moisture-report.pdf drying and moisture report
northfield-reinstatement-schedule.csv reinstatement schedule
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **One insured, two policies, different lines.** Sundale Property Group is the first named insured on both `CP-7735-19042` and `BR-6612-77309`. `line_of_business` and `project_name` are what separate them.
* **Project identity over insured identity.** The project name and contract number resolve the risk more reliably than the insured, which is the case the construction signals exist for.
* **Reporter, insured and contractor are three different parties**, on three different domains, none of which is the carrier.
