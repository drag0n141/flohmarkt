# Flohmarkt – Table Registration

A small Flask web app for allocating tables at a flea market: visitors
register, pick a free table (either from a simple grid or an optional
uploaded floor plan with clickable markers), and pay the table fee directly
via PayPal (server-side Orders API v2 – a table is only booked once payment
is confirmed). Unpaid holds expire automatically after 10 minutes.

Discounted prices for staff are handled via voucher codes rather than
automatic detection. An admin area (`/admin`) shows all registrations and
lets you manage the floor plan and voucher codes.

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
| `DB_PATH` | Path to the SQLite database file | `flohmarkt.db` |
| `ADMIN_PASSWORD` | Password for `/admin` | — |
| `SECRET_KEY` | Flask session secret (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`) | — |
| `SESSION_COOKIE_SECURE` | Set to `false` to test locally over plain HTTP | `true` |
| `FLASK_DEBUG` | Enable Flask debug mode (only relevant when run via `python app.py`) | `false` |
| `SMTP_HOST` | SMTP server for the payment confirmation email; leave empty to disable it entirely | — |
| `SMTP_PORT` | SMTP port | `587` |
| `SMTP_USER` / `SMTP_PASSWORD` | SMTP login, if required | — |
| `SMTP_FROM` | Sender address | same as `SMTP_USER` |
| `SMTP_USE_TLS` | Use STARTTLS | `true` |

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ADMIN_PASSWORD`, and
`SECRET_KEY` are required; everything else has a sensible default.

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
