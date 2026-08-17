# Notifications

The desk's doorbell. When a broker's email lands in the shared mailbox and when
the pipeline finishes with it, a row appears behind the bell in the top bar so a
handler finds out without watching the intake board.

This is **not** a second audit trail. The audit trail
(`app.services.fnol.audit`) answers "what happened to this notice" for somebody
who came looking; a notification interrupts somebody who did not. That is why the
two carry different content — an audit event records a state transition in the
vocabulary of the pipeline, a notification carries the sender, the subject and the
time an email arrived, because that is what tells a handler whether the thing that
just landed is theirs.

---

## What produces one

Four kinds, in `app.domain.enums.NotificationKind`. Nothing else emits.

| Kind | Raised by | Title / body |
| --- | --- | --- |
| `fnol.email_received` | `MailIntakeService._announce`, in the same transaction as the notice | **New FNOL email received** — "Processing has started." |
| `fnol.processing_started` | `FNOLPipeline.run`, beside the `PIPELINE_STARTED` audit event | **Processing FNOL-…** — "Reading the notification and its documents." |
| `fnol.processing_succeeded` | `FNOLPipeline.run`, on completion | **FNOL-… processed successfully** — "FNOL processed successfully and added to the intake queue." |
| `fnol.processing_failed` | `FNOLPipeline.run`, in its `except` | **FNOL-… could not be processed** — "FNOL processing failed. Review required." |

A follow-up email on a notice that already exists is announced too, with a
different sentence: "Filed against the existing notification." A handler working
a claim needs to know the broker sent the survey report; the ledger row alone does
not tell them.

### Tone is a meaning, not a colour

`NotificationTone` is chosen on the server, not derived on the client from `kind`,
because the same kind warrants two tones:

* processing that completed with **nothing** blocking → `success`
* processing that completed with **six open exceptions** → `warning`

The run succeeded either way — the pipeline did its job — but a green tick on a
notice nobody can progress until a human matches a policy would tell the handler
the opposite of what they need to know. `critical` is reserved for
`processing_failed`, which is the one outcome where the notice is on the desk
carrying nothing and nothing else will move it: the beat does not re-claim a
`failed` case, an officer has to.

---

## Two tables, and why

**`notifications` is desk-wide.** A broker's email arrives at the claims desk, not
at a person. Intake runs under the service's own Graph credentials with no human
in the loop, so there is nobody to address the row to, and assigning one at
collection time would mean inventing a routing rule the business has not stated.

**`notification_reads` is per-person**, keyed on the access token's `sub` claim.
This is the reason it is a second table rather than a `read_at` column: a shared
read state would mean one officer clearing their badge clears it for the whole
desk, which is the failure mode that makes a shared inbox unusable. The *absence*
of a row is the unread state, so creating a notification writes nothing here and
the common case costs nothing.

Keyed on the token subject rather than a local user id because there is no local
user table — Keycloak owns identity, and `sub` is the one identifier stable across
a username change.

### Idempotency

`notifications.dedupe_key` is **unique in Postgres**, not checked in the service
alone. Everything that emits here runs on a schedule or under a retry, and two
workers polling one mailbox at one moment is a race no amount of application code
wins by itself.

| Event | Key | Consequence |
| --- | --- | --- |
| `fnol.email_received` | `…:{graph_message_id}` | One email is one arrival. A message that failed twice before succeeding announces itself once. A mailbox with `CWB_GRAPH_MARK_AS_READ=false` re-lists every message every poll and produces **no** further notifications. |
| the three pipeline events | `…:{case_id}:{processing_started_at}` | One key per *run*. Two deliveries of one Celery task carry the same instant and produce one row; a genuine second run — an officer pressing "reprocess" — carries a new instant and is announced, because the handler was told how the first one went. |

`NotificationService.record` looks the key up before inserting rather than
catching an `IntegrityError`, because the caller's transaction is the one holding
the claim notification and a poisoned transaction would lose it.

### Emission never fails its caller

A notification is the least important thing in any transaction it takes part in:
losing one costs a handler a badge, losing the claim notification it was
announcing costs the business a claim. So `record()` swallows and logs at `error`
(`notification_not_recorded`), and every producer is safe to call from a path
whose real job is something else.

It does **not** commit. The caller owns the transaction, which is what makes the
notification and the thing it describes atomic — a "processed successfully" row
that survived a rolled-back pipeline would be worse than no notification at all.

### When the notice is deleted

`notifications.fnol_case_id` is `ON DELETE CASCADE` — unlike the mailbox ledger's
`SET NULL`. A panel row saying "FNOL-2026-000123 processed successfully" that
opens onto a 404 is a lie about what happened to that email, and there is no
question a nulled notification usefully answers. `FNOLDeletionService` counts them
into the purge receipt before they go, so an officer can reconcile the badge that
just dropped.

---

## API

All four routes are gated on `FNOL_READ_ROLES`, which is wider than the intake
write roles on purpose: a notification is an *observation* about the desk, and
every role that may look at an intake queue may be told its contents changed.
Nothing here is a decision — which is why marking read is a `POST` on the reader's
own state rather than a write on the notice, and why a `claims-handler` is
admitted where `POST /fnol/{ref}/process` refuses them.

Everything is scoped to `principal.subject`, taken from the verified token and
never from the request body. A client that could name whose panel to mark read
could clear somebody else's.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/notifications` | The panel. `?unread_only=`, `?case_id=`, `?page=`, `?page_size=` |
| `POST` | `/api/v1/notifications/read` | `{"notification_ids": [...]}` — acknowledge what was shown |
| `POST` | `/api/v1/notifications/read-all` | Clear the badge |
| `GET` | `/api/v1/notifications/{id}` | One notification |

```jsonc
// GET /api/v1/notifications
{
  "items": [{
    "id": "…", "kind": "fnol.email_received", "tone": "info",
    "title": "New FNOL email received",
    "body": "Processing has started.",
    "occurred_at": "2026-08-14T08:02:00Z",   // when the broker sent it
    "fnol_case_id": "…", "fnol_reference": "FNOL-2026-000412",
    "read": false,                            // for *this* caller
    "context": { "sender_name": "Marisa Kell", "subject": "FNOL - …",
                 "mailbox": "claims@carrier.example", "attachments_stored": 2,
                 "status": "queued" }
  }],
  "total": 1,
  "unread": 90,        // ledger-wide for this caller, NOT bounded by the page
  "page": 1, "page_size": 20
}
```

`unread` is deliberately not `items.filter(unread).length`: a panel showing the
newest 25 of 90 must still put 90 on the bell.

`context` is a loose JSONB column and is published as-is. Each kind carries the
metadata its own row needs, and a whitelist in the schema would mean a migration
every time a kind wants to say something. The producers are the guard — they put
an envelope and a count in there, never a body or a credential.

**No new configuration.** There is no `CWB_NOTIFY_*`; nothing here is tunable
because nothing here needed to be.

---

## Frontend

| File | Job |
| --- | --- |
| `src/types/notifications.ts` | The wire shapes, snake_case, unrenamed |
| `src/services/notificationsService.ts` | The three calls |
| `src/stores/notificationStore.ts` | State, the poll, optimistic marking |
| `src/components/layout/NotificationPanel.tsx` | The dropdown |
| `src/components/layout/AppTopBar.tsx` | The bell, its badge, and the subscription |

The bell replaces a hardcoded `const UNREAD_NOTIFICATIONS = 3` that carried the
comment *"Placeholder until a notifications endpoint exists."*

### Polling, not a socket

The product has no websocket transport, and adding one for a bell would be a
second delivery mechanism to operate, secure and reconnect. Intake itself is a
mailbox polled every 300 seconds, so an arrival is already minutes old before
anything could push it — a 20-second poll (`POLL_INTERVAL_MS`) is well inside that
noise. `useFnolCase` already establishes the pattern at 2s for a case in flight;
this is the same idea slower, because nothing here is watching a progress bar.

Three properties keep it cheap and safe, each tested:

* **Nothing runs without a token.** Keyed on the auth store, so signing in starts
  the poll without a reload and signing out stops it. A signed-out tab quietly
  asking a gated endpoint every twenty seconds forever is the kind of thing nobody
  notices until it is in a log.
* **A hidden tab does not poll.** A desk leaves the workbench open all day behind
  other windows. Becoming visible refreshes immediately, so nothing is stale on
  return.
* **One interval however many subscribers.** Reference-counted, so a remount —
  React strict mode double-invokes effects in development — leaves no second timer.

A failed poll does **not** raise a banner over the page: it is recorded on the
store and shown, with a retry, only inside the panel when somebody opens it. A
queue that is working should not be interrupted because a bell could not count.

Reading a notification marks it read and navigates to the notice — the same
convention as an email client, and why there is no per-row "mark as read" control.
`Mark all read` exists for the other case: a desk coming back to ninety of them.

### A latent bug this fixed

`AppTopBar`'s `<header>` carried `z-sticky` with no `position`, where `z-index` is
inert. The bar was therefore painting *below* the `relative` `<main>` beside it, so
the dropdown opened behind the page. The header is now `relative z-sticky`, which
is what the original class plainly intended.

---

## Testing

`tests/integration/test_mail_intake_pipeline_flow.py` (14 tests) covers the whole
path against real Postgres — email → parsed → attachment stored → notice queued →
claimed → indexed → read → fields written → on the intake board → announced at
each step — plus the failure modes: a malformed envelope, an attachment that will
not download, an oversized attachment refused before transfer, a pipeline stage
that raises, ingestion that refuses, a message abandoned after
`CWB_GRAPH_MAX_ATTEMPTS`, duplicate delivery under both ids, and the panel over
real HTTP.

Microsoft Graph is stubbed and the model provider is absent, so the deterministic
reader runs — which is what makes it cheap enough for every commit, and is also
the path a desk falls back to when the provider is down.

`tests/unit/notifications.test.tsx` (13 tests) covers the bell and panel against a
`fetch` stub answering in the wire shape.

```bash
uv run pytest tests/integration/test_mail_intake_pipeline_flow.py   # needs Postgres
cd ../ClaimsWorkbench_FE && npx vitest run tests/unit/notifications.test.tsx
```

---

## Verifying it end to end by hand

```bash
make up && make migrate        # 0006_notifications is the head
make run                       # API :8000
make worker                    # separate terminal
CWB_GRAPH_POLL_ENABLED=true make beat
cd ../ClaimsWorkbench_FE && npm run dev
```

1. Send an FNOL email to `CWB_GRAPH_SHARED_MAILBOX`, with a PDF attached.
2. Wait for the poll, or force it:
   `curl -X POST localhost:8000/api/v1/mail-intake/poll -H "Authorization: Bearer $TOKEN"`
3. The bell shows **1**; the panel reads "New FNOL email received / Processing has
   started." with the sender, the subject and the attachment count.
4. Within a beat tick `process_queued_cases` claims the notice, and two more rows
   appear: "Processing FNOL-…", then "FNOL-… processed successfully and added to
   the intake queue."
5. `/intake` shows the notice on the board. Clicking the notification opens it and
   stops counting it.
6. For the failure path, point `CWB_AI_*` at an unreachable host and reprocess:
   `curl -X POST localhost:8000/api/v1/fnol/$REF/process -H "Authorization: Bearer $TOKEN"`
   — the panel gains a rose "FNOL processing failed. Review required." naming the
   cause.

Nothing is destructive and nothing is retried into a loop: sending the same email
twice produces one notice and one arrival notification, which is the property
`test_a_second_poll_creates_no_second_notice_and_no_second_notification` pins
down.
