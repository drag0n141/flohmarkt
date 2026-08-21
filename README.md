# Flohmarkt – Table Registration

A small Flask web app for allocating tables at a flea market: registration,
table selection (with live availability), and payment of the table fee
directly via PayPal. Payment is created and captured **server-side** via the
PayPal Orders API (v2) – a table is only finalized after payment is
confirmed.

The public-facing UI text (form labels, flash messages) is in German, as
this app is built for a German-speaking flea market. Code, comments, and
this README are in English since the repository is public.

## Flow
1. Visitor picks a free table from the grid.
2. Form with name/email/phone → table is held for 10 minutes.
3. PayPal payment via the embedded Smart Buttons.
4. Once payment is confirmed (server-side capture), the table is marked as
   booked.

Unpaid holds expire automatically after 10 minutes and the table becomes
free again (constant `HOLD_MINUTES` in `app.py`).

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: NUM_TABLES, PRICE_STANDARD, PRICE_INTERNAL, PAYPAL_CLIENT_ID,
# PAYPAL_CLIENT_SECRET, PAYPAL_MODE, ADMIN_PASSWORD, SECRET_KEY
```

### PayPal credentials
1. Log in at https://developer.paypal.com → "Apps & Credentials".
2. For testing: create a sandbox app → enter Client ID + Secret in `.env`,
   leave `PAYPAL_MODE=sandbox`.
3. For the real event: create a live app (requires a verified PayPal
   business account), enter Client ID + Secret, `PAYPAL_MODE=live`.

### Running locally
```bash
python app.py
```
→ http://localhost:5000

### Production
Don't use the built-in dev server for real traffic. Instead, e.g.:
```bash
pip install gunicorn
gunicorn -w 2 -b 0.0.0.0:8000 app:app
```
behind a reverse proxy with HTTPS (nginx + Let's Encrypt). **HTTPS is
required for live PayPal payments.**

## Discounted price for staff/employees
Handled via **voucher codes** rather than automatic detection – the
registration form has an optional "voucher code" field. If the code is
valid and not (fully) redeemed yet, `PRICE_INTERNAL` applies instead of
`PRICE_STANDARD`; the actual price charged is stored permanently on the
registration.

Managed under `/admin/vouchers`:
- **Create a single code** – e.g. one shared code for all staff with a high
  "max uses" value.
- **Generate multiple single-use codes** – creates random, single-use codes
  (e.g. for individual distribution).
- Codes can be deactivated or deleted.

A code is reserved at registration time (same as the table hold) and is
automatically released again if the 10-minute hold expires or the
registration is cancelled in the admin area.

## Admin area
Available at `/admin/login` (password from `ADMIN_PASSWORD` in `.env`).

- **Table overview** (`/admin`): shows all active registrations with name,
  email, phone, table number, price paid, any voucher code used, and
  status. "Release" manually frees a table (e.g. on cancellation or
  no-show) – a voucher code used for it becomes usable again automatically.
- **Manage floor plan** (`/admin/floorplan`): upload an image of the venue
  (replaces any existing image), then click each table number onto the
  right spot on the image. Once at least one table has a position, the
  public homepage automatically shows the floor plan with colored markers
  (green=free, yellow=held, red=booked) instead of the plain grid. Without
  a floor plan, the grid remains the fallback.

Login uses a server-side session (set `SECRET_KEY` in `.env`).

## Deploying to a Kubernetes cluster
A `kubernetes/` folder (kept outside this repo, provided separately) holds
ready-made manifests (Namespace, ConfigMap, ExternalSecret, PVC,
Deployment/Service, HTTPRoute), plus this repo's own `Dockerfile` and a
GitHub Actions pipeline (`.github/workflows/docker-publish.yml`) that
builds and pushes the image to `ghcr.io/<your-user>/flohmarkt` on every
push (amd64 + arm64).

**Flow:**
1. Push this repo (including `Dockerfile` and `.github/workflows/`) to
   GitHub – the pipeline builds the image automatically. Set the GHCR
   package to "public" afterwards (or configure an `imagePullSecret` with a
   GHCR token), otherwise the cluster can't pull it.
2. Create a 1Password item `flohmarkt` (or similar) with the fields
   `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, `ADMIN_PASSWORD`,
   `SECRET_KEY`, matching your ESO / 1Password Connect setup.
3. Adapt the manifests to your cluster (storage class, Gateway name and
   hostname, secret store name, GitRepository source name, image
   reference) and commit them to your infra repo.
4. Push – Flux (or your GitOps tool) reconciles automatically.

**Notes:**
- SQLite only supports a single concurrent writer, so the app runs with
  `replicas: 1` and a `Recreate` deployment strategy – a rollout causes
  brief downtime, which is fine at this scale.
- The DB file and the uploaded floor plan live on the **same PVC**, split
  across two `subPath` mounts (`/data` and `/app/static/uploads`) so both
  survive restarts.
- `/admin` is protected solely by the app's own password, not by a
  cluster-wide ForwardAuth (e.g. Tinyauth) – if needed, this can be added
  via a dedicated `HTTPRoute`/`SecurityPolicy` scoped to the `/admin` path.
- Without your own `Dockerfile` build, you can also test locally with
  `docker build -t flohmarkt .` and push to your own registry.

## Security
Already built in:
- **CSRF protection** (Flask-WTF) for all admin forms and AJAX calls; the
  public JSON API (`/api/...`) is deliberately exempt, since it isn't
  cookie-authenticated anyway.
- **Rate limiting** (Flask-Limiter) on login (`10/minute`) and the public
  endpoints (`/api/register`, `/api/check-voucher`, etc., `20–30/minute`)
  against brute-forcing the password or voucher codes.
- **Constant-time password comparison** (`hmac.compare_digest`) for the
  admin login.
- **Real image validation** (Pillow) on floor plan uploads instead of just
  checking the file extension, plus an 8 MB upload limit.
- **Secure session cookies** (HttpOnly, SameSite=Lax, Secure – can be
  disabled via `SESSION_COOKIE_SECURE` in `.env` for local HTTP testing).
- **Security headers** (CSP without `unsafe-inline` for scripts,
  X-Frame-Options, X-Content-Type-Options, Referrer-Policy) via
  `after_request`.
- `.gitignore` prevents `.env`, the SQLite DB, or uploaded floor plans from
  being committed accidentally.

Deliberately not built in (usually unnecessary at this scale, but worth
knowing):
- No login lockout after X failed attempts (rate limiting only) – can be
  extended via Flask-Limiter with persistent storage (Redis) if needed.
- Rate limiting uses in-memory storage – fine for `replicas: 1`, but not
  synchronized across instances if you were to run more than one.
- No audit log for admin actions.

## Customization
- Table count/price/currency: via `.env` or directly at the top of
  `app.py`.
- The database is a single SQLite file (`flohmarkt.db`), created
  automatically on first start.
- For a confirmation email after payment: add mail sending (e.g. via
  `smtplib` or a mail API service) inside `api_capture_order()` in
  `app.py`.

## Known limitations
- Race conditions from simultaneously selecting the same table are caught
  via the DB status (`409` error, grid/floor plan reloads).
- Admin login is a single shared password (no multi-user account system).
