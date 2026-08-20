# Northfield Senior Living — overnight vandalism and theft on site

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
| Date of loss | 3 January 2026, between 22:00 and 05:30 |
| Loss location | 1725 Northfield Commons Drive, Madison, WI 53704 |
| Cause | Malicious damage and theft |
| Estimated loss | USD 380,000 |

## Expected matching result

| | |
|---|---|
| Expected policy | `BR-6612-77309` |
| Confidence | Strong |

No policy number. The project name and contract number, the site address, both owner entities, the general contractor and the broker reference all match. The building configuration described in the police report — three under construction plus an existing single-storey clinic being converted — matches the policy's project description, and only this policy carries an existing structure section.

## What's here

```
broker-notification.eml          the covering email — subject, body, envelope
northfield-site-vandalism-loss-notice.pdf 2-page completed loss notice
northfield-police-incident-report.pdf police incident report
northfield-vandalism-schedule.csv schedule of damage and theft
```

The policy wording is **not** in this pack by design. It lives in `../../policy/`
as the library, and the book it is matched against is loaded by
`scripts/load_policy_book.py`. A pack carrying its own policy schedule would hand
the matcher the answer.

A fifth document appears on the case without being in this folder: the
notification body is written out as `notification-body.txt` before indexing, so a
value quoted from the covering email carries a citation like any other file.

## What this pack exercises

* **A specialist document that identifies the insured only loosely.** The police report says 'Sundale Property Group of Tulsa' and never states an entity suffix or a policy.
* **Existing structure damage as a corroborating signal.** The renovation element is unique to this policy in the book.
* **A warranty gap disclosed by the reporter.** The patrol interval is raised in the email; it is a warning, not a rank.
