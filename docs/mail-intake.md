# Outlook mailbox intake

Phase one of the FNOL intake pipeline: a shared Outlook mailbox is polled over
Microsoft Graph, each message becomes an FNOL notification, and its attachments
are stored as claim documents ready for the extraction phase that follows.

**Keycloak is not part of this flow.** The service reads the mailbox as itself,
under application permissions granted to an Entra app registration, using the
client-credentials grant. No user signs in, no user token is exchanged and
nothing in the collection path touches the realm. Keycloak still authorises the
*humans* who trigger a poll or read the ledger through the API — those are two
different questions and they are answered in two different places.

---

## Microsoft Graph setup

### Application permissions

| Permission | Type | Needed for |
| --- | --- | --- |
| `Mail.ReadWrite` | Application | Reading messages and attachments, **and** marking a collected message read |
| `Mail.Read` | Application | Reading only — sufficient when `CWB_GRAPH_MARK_AS_READ=false` |

`Mail.ReadBasic` is **not** enough. It excludes the message body and
attachments, which are the two things intake exists to collect.

**Admin consent is required.** Application permissions are consented to for the
whole tenant by an administrator; there is no per-user consent path for a daemon.

### Restrict the app to the one mailbox

By default an application permission grants access to *every* mailbox in the
tenant. That is far more than intake needs, and an application access policy is
how it is narrowed:

```powershell
# Members of this group are the only mailboxes the app may touch.
New-ApplicationAccessPolicy `
  -AppId <GRAPH_CLIENT_ID> `
  -PolicyScopeGroupId claims-intake-mailboxes@carrier.example `
  -AccessRight RestrictAccess `
  -Description "Claims Workbench FNOL intake — shared claims mailbox only"

Test-ApplicationAccessPolicy -Identity claims@carrier.example -AppId <GRAPH_CLIENT_ID>
```

Policies take up to 30 minutes to apply. Until one exists, treat the client
secret as credentials for the entire tenant's mail, because that is what it is.

### What the app registration needs

1. An app registration in the tenant (single tenant is fine).
2. A client secret, stored wherever this deployment keeps secrets — never in
   the repository, and never in `.env` on a shared machine.
3. The three ids and the mailbox address, set as the environment variables
   below.

---

## Configuration

Every setting is optional; with none of them set, intake is simply off — no
schedule is registered and the API answers `503 graph_not_configured`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `CWB_GRAPH_TENANT_ID` | — | Directory (tenant) id |
| `CWB_GRAPH_CLIENT_ID` | — | Application (client) id |
| `CWB_GRAPH_CLIENT_SECRET` | — | Client secret |
| `CWB_GRAPH_SHARED_MAILBOX` | — | Mailbox to read, e.g. `claims@carrier.example` |
| `CWB_GRAPH_MAIL_FOLDER` | `inbox` | Folder polled |
| `CWB_GRAPH_UNREAD_ONLY` | `false` | Narrow the sweep to unread messages. **Leave off** — see *Why the read flag is not the cursor* |
| `CWB_GRAPH_BATCH_SIZE` | `25` | Messages per *page* |
| `CWB_GRAPH_MAX_PAGES` | `10` | Pages one poll may follow. `BATCH_SIZE × MAX_PAGES` is the ceiling on a single poll |
| `CWB_GRAPH_LOOKBACK_MINUTES` | `1440` | How far back of the newest ledger row each sweep re-reads |
| `CWB_GRAPH_MAX_ATTEMPTS` | `3` | Retries before a message is left for a human |
| `CWB_GRAPH_POLL_ENABLED` | `false` | Register the beat schedule |
| `CWB_GRAPH_POLL_INTERVAL_SECONDS` | `300` | How often beat polls |
| `CWB_GRAPH_MARK_AS_READ` | `true` | Mark collected messages read |
| `CWB_GRAPH_MOVE_TO_FOLDER` | unset | Move collected messages to this folder |
| `CWB_GRAPH_FROM_BROKER` | `true` | Record as `broker_email` rather than `insured_email` |
| `CWB_GRAPH_INCLUDE_INLINE_ATTACHMENTS` | `false` | Store inline images as documents |
| `CWB_GRAPH_TIMEOUT_SECONDS` | `30` | Per-request timeout |

The four credentials are also accepted under the bare names Azure hands them
over in — `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` and
`OUTLOOK_SHARED_MAILBOX` — so nothing has to be renamed on its way into a
deployment.

Attachment limits are the FNOL module's, not a second set:
`CWB_FNOL_MAX_DOCUMENT_BYTES` (25 MB) and `CWB_FNOL_MAX_DOCUMENTS_PER_CASE`
(40). What a broker may email is exactly what an officer may upload.

---

## Running it

```bash
# Once, against the real mailbox — for setting it up and watching it work.
make mail-intake                      # uv run python -m app.services.mail
uv run python -m app.services.mail --limit 5

# As a scheduled worker — the answer for anything but a one-off.
# Intake is a beat schedule executed by a worker, so an API on its own looks
# completely healthy and collects nothing. `make dev` starts API + worker + beat
# together and one Ctrl-C stops all three; `make up-all` runs the same three in
# containers, which is what a deployment does.
make up      # infrastructure first
make dev     # API + worker + beat

# On demand through the API, as an intake officer, manager or admin.
curl -X POST localhost:8000/api/v1/mail-intake/poll \
  -H "Authorization: Bearer $TOKEN"

# What has arrived, and what became of it.
curl "localhost:8000/api/v1/mail-intake/messages?status=failed" \
  -H "Authorization: Bearer $TOKEN"
```

`POST /api/v1/mail-intake/poll` requires `fnol-officer`, `claims-manager` or
`claims-admin` — collecting a mailbox creates notifications. Reading the ledger
is open to the wider claims read roles.

`make dev` applies migrations before booting and clears beat's schedule database,
which removes the two ways a correctly configured poll still does not run: a
worker holding an ORM mapping older than the schema, and a stale schedule file.
Restarting the backend is deliberately one gesture — settings are read once per
process, so a `.env` edit that reaches the API but not the worker is a
split-brain that is invisible from the outside.

---

## What one poll does

```
read the watermark from the ledger  (newest collected − LOOKBACK_MINUTES)
list everything received since then (oldest first, following nextLink)
  └─ for each message, in its own transaction:
       already collected?  → skip, re-mark if the flag never landed
       failed too often?   → leave it, with the reason on the row
       otherwise:
         read attachment metadata      (no bytes yet)
         refuse what may not be stored (size, kind, inline, count)
         download the rest
         create the FNOL notification  (deduplicated on Message-ID)
         attach each document          (validated, checksummed, stored)
         mark the case `queued`
         COMMIT
         mark the message read / move it
         COMMIT

reconcile: folder item count vs ledger rows → warn if the folder holds more
```

Four properties are worth relying on:

* **Idempotent.** Every message is checked against the ledger on both its Graph
  id and its RFC 5322 `Message-ID`, and both are unique columns in Postgres. The
  notice layer then checks again on its own keys. A re-delivered email produces
  no second claim notification.
* **Partial failure is normal.** One message failing does not stop the batch,
  and one attachment failing does not lose the message. Both are recorded.
* **Nothing is destructive.** Messages are never deleted. Marking read is the
  only mutation on by default; moving is opt-in.
* **Durability before mailbox state.** The notice is committed before the
  message is marked, so a crash re-collects rather than loses.

---

## What it produces

| Table | Holds |
| --- | --- |
| `mail_intake_messages` | One row per Graph message ever seen: envelope, status, attempts, error, and the FNOL case it became |
| `mail_intake_attachments` | One row per attachment, stored *or refused*, with the reason and a link to the `fnol_documents` row |
| `fnol_cases` | The notification itself, `channel=broker_email`, `processing_state=queued` |
| `fnol_documents` | The attachment bytes' metadata; the bytes are in the configured document store |

Statuses on a message row: `pending`, `processing`, `processed`, `failed`.
On an attachment row: `stored`, `skipped`, `failed` — where `skipped` is a
policy refusal (too large, wrong type, inline, over the per-case count) and
carries a sentence fit to show a human.

### When the notice it produced is deleted

`DELETE /api/v1/fnol/{reference}` removes the ledger rows for that message too,
rather than leaving them with `fnol_case_id` nulled. Two reasons: a row marked
`processed` against a notice that no longer exists is a lie about what happened
to that email, and the ledger is the only thing that stops the mailbox being
collected twice — so keeping it would make the deleted notification permanently
unrecoverable, while removing it lets a re-poll bring the email back in.

The attachment bytes are *not* deleted from here. An attachment's `storage_key`
names the same object the `FNOLDocument` names; the deletion path removes it once,
via the document.

---

## How this connects to the next phase

Intake deliberately stops before intelligence. It leaves:

* an `FNOLCase` in `processing_state = queued`, which is the state the pipeline
  in `app.services.fnol.pipeline` already looks for;
* `FNOLDocument` rows with `source = "email_attachment"`, each validated,
  checksummed and stored, with text extraction already attempted;
* a ledger row linking the two back to the message they came from.

The document ingestion and extraction phase consumes that without knowing
Microsoft Graph exists — it reads queued cases and their documents, the same
ones a browser upload or the `POST /api/v1/fnol/email` endpoint produce. Adding
a second mailbox provider later means another client in
`app.integrations.graph`'s place; `app.services.mail.intake` and everything
below it does not change.

**That phase now exists: see [document-intelligence.md](document-intelligence.md).**
The `process_queued_cases` beat task is what reads `processing_state = queued`, and it
is the only thing on the far side of this seam — an attachment collected here becomes
searchable passages and then extracted fields, each of which can point back at the page
of the file it was read from.

Collection also now **tells the desk**. A message that becomes a notice raises a
`fnol.email_received` notification in the same transaction as the notice, and the
pipeline raises one more when it starts and one when it finishes — so a handler
learns an email arrived without watching the intake board. See
[notifications.md](notifications.md). Nothing in the collection sequence above
depends on it: emission never raises, and a deployment with the panel switched off
collects mail exactly as before.

`tests/integration/test_mail_intake_pipeline_flow.py` is the test that walks this
whole seam in one pass — collection, claiming, indexing, extraction, the board, and
the notifications — alongside `test_mail_intake_flow.py`, which stops at collection.

---

## Layers

| Layer | Package | Holds |
| --- | --- | --- |
| Graph client | `app.integrations.graph` | Tokens, HTTP, Graph JSON → dataclasses. Knows nothing about claims |
| Intake service | `app.services.mail` | The sequence, idempotency, failure recording, mailbox state |
| Notice creation | `app.services.fnol.ingestion` | Envelope → `FNOLCase`, shared with every other email channel |
| Documents | `app.services.documents` | Validation, storage, text extraction — unchanged |
| Persistence | `app.repositories.mail_intake`, `app.models.mail_intake` | The ledger |
| Trigger | `app.api.v1.routes.mail_intake`, `app.workers.tasks` | On demand, and on a schedule |

## Why the read flag is not the cursor

Intake tracks what it has collected in `mail_intake_messages`, keyed on both the
Graph message id and the sender's `Message-ID`. It does **not** use Outlook's
`isRead` flag for that, and the distinction cost a live claim before it was made
explicit.

`isRead` belongs to the claims team. A handler opening the shared mailbox, a
preview pane, a phone syncing the folder or an inbox rule all clear it. With
`UNREAD_ONLY=true`, any message whose flag was cleared before intake reached it
was excluded from every subsequent poll — permanently — and each of those polls
logged a successful run over an empty result. Nothing downstream noticed,
because nothing downstream was waiting for a particular claim.

Two mechanisms replace it:

* **A watermark.** Each poll asks the ledger for the newest message it holds for
  this mailbox and sweeps from `LOOKBACK_MINUTES` before it, so the window is
  bounded by time rather than by page size. Re-reading a message already in the
  ledger costs one indexed query.
* **Paging.** `@odata.nextLink` is followed up to `MAX_PAGES`. Without it, a
  folder holding one more message than `BATCH_SIZE` returned the same oldest
  page to every poll and the newest message was never in the window — the same
  outage from the other direction, and one no configuration change could fix.

`_reconcile` then runs three checks each poll, because a blind sweep and a quiet
mailbox otherwise produce identical logs and something has to contradict the
silence. All three are also returned by `POST /mail-intake/poll`, so none of
them depends on anyone reading a log:

| Check | Signal | Drifts? |
| --- | --- | --- |
| Every listed message left a ledger row | `dropped` / `mail_intake_messages_dropped` | No — exact |
| Unread mail exists and the sweep fetched nothing | `sweep_blind` / `mail_intake_sweep_blind` | No |
| Folder item count vs ledger rows | `mail_intake_backlog_detected` | Yes — rows outlive deleted mail, so treat as a floor |

The first is the one to trust. It compares what the poll *listed* against what it
*recorded*, so it cannot be fooled by a counter the same broken sweep produced,
and it does not care how many rows the ledger has accumulated over time.

---

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `503 graph_not_configured` | One of the four credentials is missing |
| `GraphAuthError: AADSTS7000215` | Wrong client secret |
| `403 ErrorAccessDenied` | Consent missing, or an application access policy excludes this mailbox |
| Nothing collected, mailbox full | `CWB_GRAPH_UNREAD_ONLY=true` and the messages are already read. This is why the default is `false` |
| `mail_intake_backlog_detected` in the log | The folder holds more messages than the ledger has rows for. If it repeats across polls the sweep is not reaching them — check `UNREAD_ONLY`, `LOOKBACK_MINUTES` and `MAX_PAGES` |
| `graph_message_list_truncated` in the log | One poll hit `BATCH_SIZE × MAX_PAGES`. Harmless once (the next poll continues); persistent means the mailbox is filling faster than it drains |
| Config changes have no effect | A worker reads settings once, at import. An `.env` edit needs a **worker and beat restart** — `make dev` restarts all three together. The worker now logs `worker_configuration_stale` every poll until you do, and `graph_mail_client_created` at startup shows what it is actually using |
| `mail_intake_sweep_blind` in the log | The folder reports unread mail and the sweep returned nothing at all. This is the outage signature — the sweep is not reaching the folder |
| `mail_intake_messages_dropped` in the log | Messages were listed and left no ledger row: neither collected nor retryable. A silent loss; needs a human |
| Messages re-listed every poll | `CWB_GRAPH_MARK_AS_READ=false`; they are skipped by the ledger, not re-processed |
| A message stuck at `failed` | Read `last_error` on its ledger row; it stops being retried after `CWB_GRAPH_MAX_ATTEMPTS` |
