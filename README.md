# Flohmarkt – Table Registration

A small Flask web app for allocating tables at a flea market: visitors
register, pick a free table (either from a simple grid or an optional
uploaded floor plan with clickable markers), and pay the table fee — either
directly via PayPal (server-side Orders API v2, table booked immediately on
payment) or via bank transfer (SEPA), where the table is held for 48 hours
until an admin manually confirms the incoming payment. Unpaid PayPal holds
expire automatically after 10 minutes.

Discounted prices for members are handled via voucher codes rather than
automatic detection. An admin area (`/admin`) shows all registrations, lets
you manage the floor plan and voucher codes, confirm SEPA payments, and
edit the wording of both automated emails.

The public-facing UI text is in German (the app targets a German-speaking
flea market); code, comments, and this README are in English since the
repository is public.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# fill in the values below

python app.py        # dev server → http://localhost:5000
```

For production, run behind a WSGI server (e.g. `gunicorn -w 2 -b 0.0.0.0:8000 app:app`)
and a reverse proxy with HTTPS — HTTPS is required for live PayPal payments.

## Environment variables

| Variable | Description | Default |
|---|---|---|
| `NUM_TABLES` | Number of tables to create (raising this and restarting adds the new tables) | `30` |
| `PRICE_STANDARD` | Regular table fee | `15.00` |
| `PRICE_INTERNAL` | Discounted fee applied with a valid voucher code | same as `PRICE_STANDARD` |
| `CURRENCY` | ISO currency code for PayPal | `EUR` |
| `PAYPAL_CLIENT_ID` | PayPal REST app Client ID | — |
| `PAYPAL_CLIENT_SECRET` | PayPal REST app Secret | — |
| `PAYPAL_MODE` | `sandbox` or `live` | `sandbox` |
| `PAYPAL_WEBHOOK_ID` | Enables the `/webhooks/paypal` fallback (see below); leave empty to disable | — |
| `SEPA_HOLD_HOURS` | How long a bank-transfer reservation holds a table before it expires unconfirmed | `48` |
| `DB_PATH` | Path to the SQLite database file | `flohmarkt.db` |
| `ADMIN_PASSWORD` | Password for `/admin` | — |
| `SECRET_KEY` | Flask session secret (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`) | — |
| `SESSION_COOKIE_SECURE` | Set to `false` to test locally over plain HTTP | `true` |
| `FLASK_DEBUG` | Enable Flask debug mode (only relevant when run via `python app.py`) | `false` |
| `SMTP_HOST` | SMTP server for outgoing emails; leave empty to disable all email sending | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP login, if required | — |
| `SMTP_FROM` | Sender address | same as `SMTP_USER` |
| `SMTP_USE_TLS` | Use STARTTLS | `true` |

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ADMIN_PASSWORD`, and
`SECRET_KEY` are required; everything else has a sensible default.

## Bank transfer (SEPA) as a payment option

At registration, visitors choose between PayPal and bank transfer. Bank
transfer:
1. Holds the table for `SEPA_HOLD_HOURS` (default 48h) instead of the
   10-minute PayPal hold.
2. Immediately sends an email with the bank details, amount, and a payment
   reference (`FLOHMARKT-<registration id>`) so incoming transfers can be
   matched — see **Editable emails** below for where those bank details
   (IBAN/BIC/account holder) actually live.
3. Requires an admin to manually confirm the incoming payment in `/admin`
   (button "Zahlung bestätigen" on pending bank-transfer rows). Confirming
   books the table and sends the same payment-confirmation email PayPal
   payments get.

If the transfer never arrives, the reservation and table are released
automatically once `SEPA_HOLD_HOURS` has passed, same as an expired PayPal
hold.

## Editable emails

Both automated emails — the payment confirmation and the SEPA bank-transfer
notice — can be edited in `/admin/emails`, including subject and body, with
placeholders that get substituted at send time:

- **Confirmation** (`{{name}}`, `{{tisch}}`, `{{preis}}`, `{{gutschein}}`)
- **SEPA notice** (`{{name}}`, `{{tisch}}`, `{{preis}}`, `{{referenz}}`, `{{frist}}`)

There is no separate config for the bank account details — they're just
part of the SEPA email text, so put the real IBAN/BIC/account holder into
that template before enabling bank transfer as an option. Sensible
defaults are used until something is saved via the admin page.

## PayPal webhook (optional but recommended)

Normally the browser confirms payment itself by calling `/api/capture-order`
right after PayPal approval. If the tab closes at exactly the wrong moment
(payment captured, but the confirmation call never arrives), the table
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
booked and the confirmation email only sent once.
