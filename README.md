# Flohmarkt – Table Registration

A small Flask web app for allocating tables at a flea market: visitors
register, pick a free table (either from a simple grid or an optional
uploaded floor plan with clickable markers), and pay the table fee — either
directly via PayPal (server-side Orders API v2, table booked immediately on
payment) or via bank transfer (SEPA), where the table is held for 48 hours
until an admin manually confirms the incoming payment. Which of the two
payment methods are actually offered is configurable (see `PAYMENT_METHODS`
below). Unpaid PayPal holds expire automatically after 10 minutes.

Discounted prices for members are handled via voucher codes rather than
automatic detection. An admin area (`/admin`) shows all registrations, lets
you manage the floor plan and voucher codes, confirm SEPA payments, and
edit the wording of the automated emails.

The public-facing UI text is in German (the app targets a German-speaking
flea market); code, comments, and this README are in English since the
repository is public.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt gunicorn

cp .env.example .env
# fill in the values below

python app.py        # dev server → http://localhost:5000
```

For production, run behind a WSGI server (e.g. `gunicorn -w 2 --timeout 120 -b 0.0.0.0:8000 app:app`)
and a reverse proxy with HTTPS — HTTPS is required for live PayPal payments.

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `NUM_TABLES` | Number of tables to create (raising this and restarting adds the new tables) | `30` |
| `PRICE_STANDARD` | Regular table fee | `15.00` |
| `PRICE_INTERNAL` | Discounted fee applied with a valid voucher code | same as `PRICE_STANDARD` |
| `CURRENCY` | ISO currency code for PayPal | `EUR` |
| `PAYMENT_METHODS` | Which payment methods to offer: `paypal,sepa`, `sepa`, or `paypal`; invalid/empty falls back to both | `paypal,sepa` |
| `PAYPAL_CLIENT_ID` | PayPal REST app Client ID | — |
| `PAYPAL_CLIENT_SECRET` | PayPal REST app Secret | — |
| `PAYPAL_MODE` | `sandbox` or `live` | `sandbox` |
| `PAYPAL_WEBHOOK_ID` | Enables the `/webhooks/paypal` fallback (see below); leave empty to disable | — |
| `SEPA_HOLD_HOURS` | How long a bank-transfer reservation holds a table before it expires unconfirmed | `48` |
| `DB_PATH` | Path to the SQLite database file | `flohmarkt.db` |
| `ADMIN_PASSWORD` | Password for `/admin` | — |
| `SECRET_KEY` | Required random session secret, at least 32 characters; unsafe values fail startup (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`) | — |
| `SESSION_COOKIE_SECURE` | Set to `false` to test locally over plain HTTP | `true` |
| `FLASK_DEBUG` | Enable Flask debug mode (only relevant when run via `python app.py`) | `false` |
| `SMTP_HOST` | SMTP server for outgoing emails; leave empty to disable all email sending | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP login, if required | — |
| `SMTP_FROM` | Sender address | same as `SMTP_USER` |
| `SMTP_USE_TLS` | Use STARTTLS with certificate verification | `true` |
| `DISABLE_BACKGROUND_TASKS` | Disable expiry, reminder scheduling, and outbox processing (tests/maintenance only) | `false` |

`SECRET_KEY` is required at startup; configure `ADMIN_PASSWORD` to enable admin login.
PayPal credentials are needed when PayPal is offered. Configure SMTP before
offering bank transfers so visitors can receive the bank details.

## Which payment methods are offered

Controlled by `PAYMENT_METHODS` (comma-separated, e.g. `paypal,sepa` for
both, `sepa` for bank transfer only, `paypal` for PayPal only). With both
enabled, visitors see a choice on the registration form; with only one
enabled, the choice is hidden entirely and that method is used
automatically — no code change needed, just restart the app with the new
value. An invalid or empty value falls back to both being enabled.

Note this is set once via the environment, not toggleable per-request from
the admin UI — changing it requires updating `.env` (or the ConfigMap, in
Kubernetes) and restarting the app.

## Bank transfer (SEPA) as a payment option

At registration, visitors choose between PayPal and bank transfer (if both
are enabled — see above). Bank transfer:
1. Holds the table for `SEPA_HOLD_HOURS` (default 48h) instead of the
   10-minute PayPal hold.
2. Queues an email for background delivery with the bank details, amount, and a payment
   reference (`FLOHMARKT-<table number>`) so incoming transfers can be
   matched — see **Editable emails** below for where those bank details
   (IBAN/BIC/account holder) actually live.
3. Requires an admin to manually confirm the incoming payment in `/admin`
   (button "Zahlungseingang erfassen"). Confirming an active reservation
   books the table and queues a confirmation email. A payment for an expired
   or cancelled reservation is recorded for manual review without taking
   another visitor's table.

If the transfer never arrives, the reservation and table are released
automatically once `SEPA_HOLD_HOURS` has passed, same as an expired PayPal
hold.

## Editable emails

Payment confirmations, SEPA notices, and payment reminders can be edited in `/admin/emails`, including subject and body, with
placeholders that get substituted at send time:

- **Confirmation** (`{{name}}`, `{{tisch}}`, `{{preis}}`, `{{gutschein}}`)
- **SEPA notice/reminder** (`{{name}}`, `{{tisch}}`, `{{preis}}`, `{{referenz}}`, `{{frist}}`)

Deadlines are stored in UTC and displayed in `Europe/Berlin`, including
daylight-saving time. Email subjects must be a single line.

There is no separate config for the bank account details — they're just
part of the SEPA email text, so put the real IBAN/BIC/account holder into
that template before enabling bank transfer as an option. Sensible
defaults are used until something is saved via the admin page.

## PayPal webhook (optional but recommended)

Normally the browser confirms payment itself by calling `/api/capture-order`
right after PayPal approval. If the tab closes after the server captured the payment but before the
local booking was committed, the table
would otherwise just expire after 10 minutes despite the money having been
taken. The webhook is a server-to-server safety net for that case.

Setup:
1. In the PayPal Developer Dashboard, open your app → **Webhooks** → **Add Webhook**.
2. URL: `https://<your-domain>/webhooks/paypal`.
3. Subscribe to at least **Payment capture completed**.
4. Copy the generated **Webhook ID** into `PAYPAL_WEBHOOK_ID`.

Every incoming webhook call is verified against PayPal's own
verify-webhook-signature endpoint before anything in it is trusted, and
processing is idempotent — whether a payment gets confirmed via the
browser call, the webhook, or (in rare cases) both, the table is only
booked and the confirmation email queued only once. SMTP delivery is
at-least-once; see the outbox notes below.


## Booking and payment consistency

Reservation, voucher consumption, cancellation, expiry, and payment finalization
use short SQLite `BEGIN IMMEDIATE` transactions with conditional updates.
The table must still belong to the registration before it can be booked or freed.
PayPal and SMTP requests run outside write transactions.

Browser payment requests require both a CSRF token and the signed browser
session that created the registration. Registration IDs alone do not grant access.
The public page supplies the CSRF token through a meta tag; JSON POST requests
send it in `X-CSRFToken` and use `Content-Type: application/json`.
The signature-verified PayPal webhook does not require a browser session or CSRF.

Each registration stores separate PayPal create/capture idempotency keys before
network requests. Existing orders are reused rather than overwritten. Capture
retries first retrieve PayPal's order state to reconcile ambiguous timeouts.
Completed capture IDs, amount, currency, and the order's registration reference
are checked before booking. A late payment is persisted separately from the table
allocation and shown as requiring review in the admin dashboard, including when
the registration is cancelled. `Klärung erledigt` only marks the review complete;
it never issues a refund. Refunds or alternative allocations must be arranged
outside this application.

SEPA reservations still start immediately and last `SEPA_HOLD_HOURS` (48 hours
by default). This update does not add email verification, CAPTCHA, or additional
reservation limits.

## Email outbox

Messages are rendered and queued transactionally with their booking event.
A unique `(registration_id, kind)` constraint prevents duplicate confirmation
and reminder jobs. Each WSGI worker checks the queue every 30 seconds. A shared
SQLite lease prevents ordinary concurrent delivery by multiple workers; failed
attempts retry with backoff from 30 seconds up to one hour. A crashed worker's
lease becomes available again after five minutes. Reminder flags are set only
after successful SMTP delivery. Pending notices/reminders are cancelled when
no longer relevant. A message already being handed to SMTP cannot be recalled.

SMTP acceptance and the SQLite acknowledgement cannot be one atomic operation.
A crash between those operations can therefore cause a duplicate email; retries
reuse the Message-ID, but recipient servers need not deduplicate it. The admin
page shows pending and failed delivery counts. If `SMTP_HOST` is empty, jobs stay
queued until SMTP is configured; the website does not claim they were delivered.
Editing a template affects newly queued messages, not already rendered jobs.

## Upgrading an existing installation

1. Back up the SQLite database and uploaded floor plan before deployment.
2. Keep the existing strong `SECRET_KEY`; no rotation is needed for this update.
   It must now contain at least 32 characters and must not be an example value.
3. Let active PayPal checkouts finish before switching versions (the normal hold
   lasts 10 minutes). Old registrations have no browser owner binding and cannot
   be claimed using their numeric ID. Existing order mappings still work for
   verified webhooks; pending SEPA registrations can still be handled by admins.
4. Stop the old workers before starting the new version against the database.
   Startup adds the new columns and outbox/payment-receipt tables automatically.
   Concurrent new workers serialize the migration; existing records are retained.
5. Both existing and new SEPA registrations use `FLOHMARKT-<table number>`.
   The reference intentionally remains the same when a table is rebooked.
   Past messages are not reconstructed or resent; previously sent reminder flags
   remain intact. Previously unrecorded payments require manual reconciliation.
6. Keep background tasks enabled and test PayPal sandbox checkout and SMTP
   delivery with the actual deployment configuration before accepting payments.

The Docker command uses `/tmp` for Gunicorn worker temporary files. In Kubernetes,
mount an `emptyDir` volume at `/tmp` and ensure it is writable by appuser (UID 1000).
The mounted volume remains writable with `readOnlyRootFilesystem: true`.
Also mount the database directory and `static/uploads` as writable volumes. Do not use Gunicorn `--preload`:
maintenance threads are started when each worker imports the application.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest -q
node --check static/app.js
node --check static/admin_floorplan.js
node --test tests/test_frontend.cjs
```

Tests use temporary SQLite databases and mocked PayPal/SMTP calls; they never
charge money or send email. They cover competing reservations and vouchers,
rollback, late/repeated payments, ownership/CSRF checks, timeout reconciliation,
email retries and concurrent leases, and upgrades from the legacy schema.
The Node tests exercise frontend request construction and payment-state changes
with a lightweight DOM stub; they do not test browser rendering or the real
PayPal SDK. Run these commands before merging or publishing an image.
The existing image workflow does not yet run this suite automatically; adding
the test job requires permission to edit `.github/workflows/docker-publish.yml`.
