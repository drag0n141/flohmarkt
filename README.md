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
| `NUM_TABLES` | Number of tables to create on first start | `30` |
| `PRICE_STANDARD` | Regular table fee | `15.00` |
| `PRICE_INTERNAL` | Discounted fee applied with a valid voucher code | same as `PRICE_STANDARD` |
| `CURRENCY` | ISO currency code for PayPal | `EUR` |
| `PAYPAL_CLIENT_ID` | PayPal REST app Client ID | — |
| `PAYPAL_CLIENT_SECRET` | PayPal REST app Secret | — |
| `PAYPAL_MODE` | `sandbox` or `live` | `sandbox` |
| `DB_PATH` | Path to the SQLite database file | `flohmarkt.db` |
| `ADMIN_PASSWORD` | Password for `/admin` | — |
| `SECRET_KEY` | Flask session secret (generate with `python3 -c "import secrets; print(secrets.token_hex(32))"`) | — |
| `SESSION_COOKIE_SECURE` | Set to `false` to test locally over plain HTTP | `true` |
| `FLASK_DEBUG` | Enable Flask debug mode (only relevant when run via `python app.py`) | `false` |

`PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ADMIN_PASSWORD`, and
`SECRET_KEY` are required; everything else has a sensible default.
