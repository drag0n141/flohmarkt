import hmac
import io
import os
import secrets
import smtplib
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    jsonify,
    request,
    render_template,
    g,
    session,
    redirect,
    url_for,
    flash,
)
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from PIL import Image, UnidentifiedImageError
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration – adjust here or via .env
# ---------------------------------------------------------------------------
NUM_TABLES = int(os.environ.get("NUM_TABLES", 30))
PRICE_STANDARD = float(os.environ.get("PRICE_STANDARD", os.environ.get("PRICE", 15.00)))
PRICE_INTERNAL = float(os.environ.get("PRICE_INTERNAL", PRICE_STANDARD))
CURRENCY = os.environ.get("CURRENCY", "EUR")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # "sandbox" or "live"

PAYPAL_API_BASE = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)

HOLD_MINUTES = 10  # how long a PayPal table hold stays reserved for payment
SEPA_HOLD_HOURS = int(os.environ.get("SEPA_HOLD_HOURS", 48))  # same, for bank transfer
DB_PATH = os.environ.get("DB_PATH", "flohmarkt.db")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "please-change-in-.env")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"

PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")

# Which payment methods are offered, e.g. "paypal,sepa" or just "sepa".
# Unknown entries are ignored; an empty/invalid result falls back to both.
_VALID_PAYMENT_METHODS = ("paypal", "sepa")
_payment_methods_raw = [
    m.strip().lower()
    for m in os.environ.get("PAYMENT_METHODS", "paypal,sepa").split(",")
    if m.strip()
]
ENABLED_PAYMENT_METHODS = [m for m in _payment_methods_raw if m in _VALID_PAYMENT_METHODS]
# De-duplicate while preserving order (in case of "paypal,paypal").
ENABLED_PAYMENT_METHODS = list(dict.fromkeys(ENABLED_PAYMENT_METHODS))
if not ENABLED_PAYMENT_METHODS:
    print(f"[config] PAYMENT_METHODS={os.environ.get('PAYMENT_METHODS')!r} is empty/invalid – falling back to paypal,sepa.")
    ENABLED_PAYMENT_METHODS = ["paypal", "sepa"]
DEFAULT_PAYMENT_METHOD = ENABLED_PAYMENT_METHODS[0]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Behind a reverse proxy (Envoy Gateway or similar) – for correct client IPs
# in rate limiting and logging.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Keep True behind HTTPS (the default case in the cluster); for local
    # testing without HTTPS, disable via SESSION_COOKIE_SECURE=false in .env.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,  # 8 MB – limits floor plan uploads, among other things
    WTF_CSRF_TIME_LIMIT=None,  # token should not expire in the middle of an admin session
)

csrf = CSRFProtect(app)
limiter = Limiter(get_remote_address, app=app, default_limits=["200 per hour"])


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        # 'unsafe-inline' is required because the PayPal SDK injects its own
        # inline bootstrap script that we have no control over (no nonce/hash
        # we could pin without breaking on every PayPal SDK update).
        "script-src 'self' https://www.paypal.com https://www.paypalobjects.com "
        "https://www.sandbox.paypal.com 'unsafe-inline'; "
        # PayPal's sandbox checkout is served from a different host than
        # live (www.sandbox.paypal.com vs www.paypal.com); both need to be
        # allowed since PAYPAL_MODE can be either.
        "frame-src https://www.paypal.com https://www.sandbox.paypal.com; "
        "connect-src 'self' https://www.paypal.com https://www.sandbox.paypal.com; "
        "img-src 'self' https://www.paypalobjects.com data:; "
        "style-src 'self' 'unsafe-inline'"
    )
    return response


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY,
            number INTEGER UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'free',  -- free, held, booked
            held_at TEXT,
            registration_id INTEGER,
            pos_x REAL,  -- position on the floor plan in % (0-100), NULL = no floor plan marker
            pos_y REAL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS registrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT,
            table_id INTEGER NOT NULL,
            paypal_order_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',  -- pending, paid, cancelled
            created_at TEXT NOT NULL,
            price REAL,
            voucher_code TEXT,
            payment_method TEXT NOT NULL DEFAULT 'paypal'  -- paypal, sepa
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS faq (
            id INTEGER PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    # Migration for existing databases with an older schema
    table_cols = [r[1] for r in db.execute("PRAGMA table_info(tables)").fetchall()]
    if "pos_x" not in table_cols:
        db.execute("ALTER TABLE tables ADD COLUMN pos_x REAL")
    if "pos_y" not in table_cols:
        db.execute("ALTER TABLE tables ADD COLUMN pos_y REAL")

    reg_cols = [r[1] for r in db.execute("PRAGMA table_info(registrations)").fetchall()]
    if "price" not in reg_cols:
        db.execute("ALTER TABLE registrations ADD COLUMN price REAL")
    if "voucher_code" not in reg_cols:
        db.execute("ALTER TABLE registrations ADD COLUMN voucher_code TEXT")
    if "payment_method" not in reg_cols:
        db.execute("ALTER TABLE registrations ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'paypal'")

    # Insert any missing tables up to NUM_TABLES. Uses MAX(number) rather than
    # COUNT(*) so that raising NUM_TABLES later and restarting adds the new
    # tables instead of only seeding once on the very first run.
    existing_max = db.execute("SELECT COALESCE(MAX(number), 0) FROM tables").fetchone()[0]
    if existing_max < NUM_TABLES:
        for i in range(existing_max + 1, NUM_TABLES + 1):
            db.execute("INSERT INTO tables (number, status) VALUES (?, 'free')", (i,))

    # Seed a few placeholder FAQ entries on first run so the admin has
    # something concrete to edit/replace rather than an empty list; never
    # re-seeds once the admin has added or removed anything.
    faq_count = db.execute("SELECT COUNT(*) FROM faq").fetchone()[0]
    if faq_count == 0:
        placeholder_faq = [
            ("Muss ich meinen Tisch selbst aufbauen?", "<Bitte hier die Antwort eintragen>"),
            ("Was passiert, wenn ich nicht rechtzeitig bezahle?", "<Bitte hier die Antwort eintragen>"),
            ("Kann ich meine Reservierung stornieren?", "<Bitte hier die Antwort eintragen>"),
        ]
        now_iso = datetime.utcnow().isoformat()
        for question, answer in placeholder_faq:
            db.execute(
                "INSERT INTO faq (question, answer, created_at) VALUES (?, ?, ?)",
                (question, answer, now_iso),
            )

    db.commit()
    db.close()


# Called at import time so the schema exists whether the app is started via
# `python app.py` or via a WSGI server like gunicorn (which only imports the
# module and never runs the `__main__` block below).
init_db()


def get_setting(db, key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db, key, value):
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    db.commit()


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def is_valid_image(file_bytes):
    """Checks the actual file content (not just the extension) to verify it's
    a genuine, decodable image."""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img.verify()
        return True
    except (UnidentifiedImageError, Exception):
        return False


def reserve_voucher(db, code):
    """Validates a voucher code and reserves one use (atomic enough for this scale).
    Returns (voucher_row, error_message) – error_message is None on success."""
    code = (code or "").strip()
    if not code:
        return None, None

    voucher = db.execute(
        "SELECT * FROM vouchers WHERE code = ? COLLATE NOCASE", (code,)
    ).fetchone()
    if voucher is None or not voucher["active"]:
        return None, "Dieser Gutscheincode ist ungültig."
    if voucher["used_count"] >= voucher["max_uses"]:
        return None, "Dieser Gutscheincode wurde bereits vollständig eingelöst."

    db.execute("UPDATE vouchers SET used_count = used_count + 1 WHERE id=?", (voucher["id"],))
    return voucher, None


def release_voucher(db, code):
    """Releases a reserved/redeemed use of a voucher code again
    (on an expired reservation or a cancellation)."""
    if not code:
        return
    db.execute(
        "UPDATE vouchers SET used_count = MAX(0, used_count - 1) WHERE code = ? COLLATE NOCASE",
        (code.strip(),),
    )


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def release_stale_holds(db):
    """Releases tables (and any reserved voucher codes) whose hold has expired
    without payment having been completed. PayPal holds expire after
    HOLD_MINUTES; SEPA (bank transfer) holds get a much longer window
    (SEPA_HOLD_HOURS), so each registration's own payment method decides
    which cutoff applies to it."""
    now = datetime.utcnow()
    paypal_cutoff = (now - timedelta(minutes=HOLD_MINUTES)).isoformat()
    sepa_cutoff = (now - timedelta(hours=SEPA_HOLD_HOURS)).isoformat()

    expired = db.execute(
        """
        SELECT id, table_id, voucher_code FROM registrations
        WHERE status='pending' AND (
            (COALESCE(payment_method, 'paypal') = 'sepa' AND created_at < ?) OR
            (COALESCE(payment_method, 'paypal') != 'sepa' AND created_at < ?)
        )
        """,
        (sepa_cutoff, paypal_cutoff),
    ).fetchall()

    for reg in expired:
        release_voucher(db, reg["voucher_code"])
        db.execute("UPDATE registrations SET status='cancelled' WHERE id=?", (reg["id"],))
        db.execute(
            "UPDATE tables SET status='free', held_at=NULL, registration_id=NULL WHERE id=? AND status='held'",
            (reg["table_id"],),
        )
    db.commit()


# ---------------------------------------------------------------------------
# PayPal helper functions (server-side, Orders API v2)
# ---------------------------------------------------------------------------
def paypal_get_access_token():
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v1/oauth2/token",
        auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
        data={"grant_type": "client_credentials"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def paypal_create_order(amount, reference_id):
    token = paypal_get_access_token()
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "reference_id": reference_id,
                    "amount": {"currency_code": CURRENCY, "value": f"{amount:.2f}"},
                    "description": f"Standgebühr Flohmarkt (Reservierung {reference_id})",
                }
            ],
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def paypal_capture_order(order_id):
    token = paypal_get_access_token()
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def paypal_verify_webhook_signature(headers, event_body):
    """Verifies that a webhook POST genuinely came from PayPal, using PayPal's
    own verification endpoint (simpler and less error-prone than reimplementing
    the certificate/signature check locally)."""
    if not PAYPAL_WEBHOOK_ID:
        print("[webhook] PAYPAL_WEBHOOK_ID not set – rejecting webhook.")
        return False

    token = paypal_get_access_token()
    payload = {
        "transmission_id": headers.get("Paypal-Transmission-Id"),
        "transmission_time": headers.get("Paypal-Transmission-Time"),
        "cert_url": headers.get("Paypal-Cert-Url"),
        "auth_algo": headers.get("Paypal-Auth-Algo"),
        "transmission_sig": headers.get("Paypal-Transmission-Sig"),
        "webhook_id": PAYPAL_WEBHOOK_ID,
        "webhook_event": event_body,
    }
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v1/notifications/verify-webhook-signature",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("verification_status") == "SUCCESS"


# ---------------------------------------------------------------------------
# Editable email templates
# ---------------------------------------------------------------------------
DEFAULT_CONFIRMATION_SUBJECT = "Bestätigung: Tisch {{tisch}} beim Flohmarkt"
DEFAULT_CONFIRMATION_BODY = """Hallo {{name}},

vielen Dank für deine Zahlung – dein Tisch ist jetzt verbindlich gebucht.

Tisch: {{tisch}}
Standgebühr: {{preis}}

Bis zum Flohmarkt!"""

DEFAULT_SEPA_SUBJECT = "Deine Reservierung: Tisch {{tisch}} beim Flohmarkt (Überweisung)"
DEFAULT_SEPA_BODY = """Hallo {{name}},

dein Tisch ist für dich reserviert, bis die Zahlung bei uns eingegangen ist.

Tisch: {{tisch}}
Zu zahlender Betrag: {{preis}}
Verwendungszweck: {{referenz}}

Bitte überweise den Betrag bis spätestens {{frist}} an:

IBAN: <bitte im Admin-Bereich unter E-Mail-Texte eintragen>
BIC: <bitte eintragen>
Kontoinhaber: <bitte eintragen>

Geht die Zahlung nicht rechtzeitig bei uns ein, wird die Reservierung automatisch storniert.

Bis zum Flohmarkt!"""

EMAIL_TEMPLATE_DEFAULTS = {
    "confirmation": (DEFAULT_CONFIRMATION_SUBJECT, DEFAULT_CONFIRMATION_BODY),
    "sepa": (DEFAULT_SEPA_SUBJECT, DEFAULT_SEPA_BODY),
}


def render_email_template(template, **placeholders):
    result = template
    for key, value in placeholders.items():
        result = result.replace("{{" + key + "}}", str(value))
    return result


def get_email_template(db, kind):
    """kind: 'confirmation' or 'sepa'. Returns (subject, body) – whatever the
    admin has customized via /admin/emails, falling back to the built-in
    defaults above if not."""
    default_subject, default_body = EMAIL_TEMPLATE_DEFAULTS[kind]
    subject = get_setting(db, f"email_{kind}_subject", default_subject)
    body = get_setting(db, f"email_{kind}_body", default_body)
    return subject, body


def send_templated_email(db, kind, to_email, **placeholders):
    """Renders and sends one of the admin-editable email templates. Silently
    skipped (with a log line) if SMTP is not configured; a send failure never
    breaks the payment/registration flow that triggered it."""
    if not SMTP_HOST:
        print(f"[email] SMTP_HOST not set – skipping {kind} email.")
        return

    subject_tpl, body_tpl = get_email_template(db, kind)
    subject = render_email_template(subject_tpl, **placeholders)
    body = render_email_template(body_tpl, **placeholders)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            if SMTP_USE_TLS:
                smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception as e:
        print(f"[email] Failed to send {kind} email to {to_email}: {e}")


def finalize_paid_registration(db, reg):
    """Marks a registration as paid, books its table, and sends the
    confirmation email. Idempotent: safe to call twice for the same
    registration (e.g. once via the browser's immediate PayPal capture call
    and again via the async PayPal webhook, or if an admin double-clicks the
    SEPA confirmation button) – repeat calls are a no-op."""
    if reg["status"] == "paid":
        return
    if reg["status"] == "cancelled":
        # Payment arrived after the hold had already expired/been cancelled
        # (e.g. the browser closed right after paying, before the capture
        # call could fire, and the webhook took too long; or a SEPA transfer
        # arrived after the 48h window). Don't silently re-book – the table
        # may meanwhile belong to someone else.
        print(
            f"[payment] Payment for already-cancelled registration id={reg['id']} "
            "– needs manual follow-up in the admin area."
        )
        return

    db.execute("UPDATE registrations SET status='paid' WHERE id=?", (reg["id"],))
    db.execute("UPDATE tables SET status='booked' WHERE id=?", (reg["table_id"],))
    db.commit()

    table_row = db.execute("SELECT number FROM tables WHERE id=?", (reg["table_id"],)).fetchone()
    send_templated_email(
        db,
        "confirmation",
        reg["email"],
        name=reg["name"],
        tisch=table_row["number"] if table_row else "?",
        preis=f"{reg['price']:.2f} {CURRENCY}",
        gutschein=reg["voucher_code"] or "",
    )


# ---------------------------------------------------------------------------
# Editable public page content (title + free-text event info block)
# ---------------------------------------------------------------------------
DEFAULT_EVENT_TITLE = "Flohmarkt – Tischvergabe"
DEFAULT_EVENT_INFO = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    db = get_db()
    event_title = get_setting(db, "event_title", DEFAULT_EVENT_TITLE)
    event_info = get_setting(db, "event_info", DEFAULT_EVENT_INFO)
    faq_items = db.execute("SELECT id, question, answer FROM faq ORDER BY id").fetchall()
    return render_template(
        "index.html",
        price_standard=PRICE_STANDARD,
        price_internal=PRICE_INTERNAL,
        currency=CURRENCY,
        paypal_client_id=PAYPAL_CLIENT_ID,
        payment_methods_enabled=ENABLED_PAYMENT_METHODS,
        sepa_hold_hours=SEPA_HOLD_HOURS,
        event_title=event_title,
        event_info=event_info,
        faq_items=faq_items,
    )


@app.route("/api/tables")
def api_tables():
    db = get_db()
    release_stale_holds(db)
    rows = db.execute("SELECT number, status FROM tables ORDER BY number").fetchall()
    return jsonify([{"number": r["number"], "status": r["status"]} for r in rows])


@app.route("/api/floorplan-config")
def api_floorplan_config():
    db = get_db()
    image = get_setting(db, "floorplan_image")
    rows = db.execute(
        "SELECT number, pos_x, pos_y FROM tables WHERE pos_x IS NOT NULL AND pos_y IS NOT NULL ORDER BY number"
    ).fetchall()
    return jsonify(
        {
            "image_url": url_for("static", filename=f"uploads/{image}") if image else None,
            "tables": [{"number": r["number"], "x": r["pos_x"], "y": r["pos_y"]} for r in rows],
        }
    )


@app.route("/api/check-voucher")
@limiter.limit("30 per minute")
def api_check_voucher():
    """Validates a voucher code WITHOUT reserving it – used for the live
    display in the form, before the actual registration happens."""
    code = (request.args.get("code") or "").strip()
    if not code:
        return jsonify({"valid": False})

    db = get_db()
    release_stale_holds(db)
    voucher = db.execute("SELECT * FROM vouchers WHERE code = ? COLLATE NOCASE", (code,)).fetchone()

    if voucher is None or not voucher["active"] or voucher["used_count"] >= voucher["max_uses"]:
        return jsonify({"valid": False})

    return jsonify({"valid": True, "price": PRICE_INTERNAL})


@app.route("/api/register", methods=["POST"])
@csrf.exempt
@limiter.limit("20 per minute")
def api_register():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    table_number = data.get("table")
    voucher_input = (data.get("voucher") or "").strip()

    raw_payment_method = data.get("payment_method")
    if not raw_payment_method:
        payment_method = DEFAULT_PAYMENT_METHOD
    elif raw_payment_method in ENABLED_PAYMENT_METHODS:
        payment_method = raw_payment_method
    else:
        return jsonify({"error": "Diese Zahlungsart ist nicht verfügbar."}), 400

    if not name or not email or not table_number:
        return jsonify({"error": "Name, E-Mail und Tisch sind Pflichtfelder."}), 400

    db = get_db()
    release_stale_holds(db)

    table = db.execute("SELECT * FROM tables WHERE number=?", (table_number,)).fetchone()
    if table is None:
        return jsonify({"error": "Tisch existiert nicht."}), 404
    if table["status"] != "free":
        return jsonify({"error": "Dieser Tisch ist leider nicht mehr verfügbar."}), 409

    price = PRICE_STANDARD
    voucher_code = None
    if voucher_input:
        voucher, error = reserve_voucher(db, voucher_input)
        if error:
            return jsonify({"error": error}), 400
        price = PRICE_INTERNAL
        voucher_code = voucher["code"]

    now = datetime.utcnow()
    now_iso = now.isoformat()
    cur = db.execute(
        "INSERT INTO registrations "
        "(name, email, phone, table_id, status, created_at, price, voucher_code, payment_method) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (name, email, phone, table["id"], now_iso, price, voucher_code, payment_method),
    )
    registration_id = cur.lastrowid

    db.execute(
        "UPDATE tables SET status='held', held_at=?, registration_id=? WHERE id=?",
        (now_iso, registration_id, table["id"]),
    )
    db.commit()

    response = {
        "registration_id": registration_id,
        "table": table_number,
        "price": price,
        "voucher_applied": voucher_code is not None,
        "payment_method": payment_method,
    }

    if payment_method == "sepa":
        # Uses the table number rather than the registration id, per request –
        # simpler for visitors to read out/type, at the cost of the reference
        # no longer being globally unique (a table re-registered via SEPA
        # after an earlier hold expired or was cancelled reuses the same
        # reference).
        reference = f"FLOHMARKT-{table_number}"
        deadline_dt = now + timedelta(hours=SEPA_HOLD_HOURS)
        deadline_str = deadline_dt.strftime("%d.%m.%Y um %H:%M Uhr")
        send_templated_email(
            db,
            "sepa",
            email,
            name=name,
            tisch=table_number,
            preis=f"{price:.2f} {CURRENCY}",
            referenz=reference,
            frist=deadline_str,
        )
        response["reference"] = reference
        response["deadline"] = deadline_str

    return jsonify(response)


@app.route("/api/create-order", methods=["POST"])
@csrf.exempt
@limiter.limit("20 per minute")
def api_create_order():
    data = request.get_json(force=True)
    registration_id = data.get("registration_id")

    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
    if reg is None or reg["status"] != "pending":
        return jsonify({"error": "Registrierung nicht gefunden oder bereits abgeschlossen."}), 404
    if reg["payment_method"] != "paypal":
        return jsonify({"error": "Diese Registrierung nutzt keine PayPal-Zahlung."}), 400

    order = paypal_create_order(reg["price"], reference_id=str(registration_id))
    db.execute("UPDATE registrations SET paypal_order_id=? WHERE id=?", (order["id"], registration_id))
    db.commit()
    return jsonify({"order_id": order["id"]})


@app.route("/api/capture-order", methods=["POST"])
@csrf.exempt
@limiter.limit("20 per minute")
def api_capture_order():
    data = request.get_json(force=True)
    order_id = data.get("order_id")

    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE paypal_order_id=?", (order_id,)).fetchone()
    if reg is None:
        return jsonify({"error": "Bestellung nicht gefunden."}), 404

    result = paypal_capture_order(order_id)
    status = result.get("status")

    if status == "COMPLETED":
        finalize_paid_registration(db, reg)
        return jsonify({"status": "paid"})

    return jsonify({"error": "Zahlung nicht abgeschlossen.", "paypal_status": status}), 402


@app.route("/webhooks/paypal", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per minute")
def paypal_webhook():
    """Server-to-server notification from PayPal – the safety net in case the
    browser's own /api/capture-order call never arrives (e.g. tab closed
    right after paying). Every request is signature-verified against
    PAYPAL_WEBHOOK_ID before anything in it is trusted."""
    try:
        event = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "invalid json"}), 400

    if not event or not paypal_verify_webhook_signature(request.headers, event):
        return jsonify({"error": "invalid signature"}), 400

    if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
        order_id = (
            event.get("resource", {})
            .get("supplementary_data", {})
            .get("related_ids", {})
            .get("order_id")
        )
        if order_id:
            db = get_db()
            reg = db.execute(
                "SELECT * FROM registrations WHERE paypal_order_id=?", (order_id,)
            ).fetchone()
            if reg is not None:
                finalize_paid_registration(db, reg)

    # Always 200 for anything we don't act on, too – PayPal retries on
    # non-2xx responses, and event types we don't handle aren't errors.
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Admin area
# ---------------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            session.permanent = True
            session["is_admin"] = True
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        flash("Falsches Passwort.")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("admin_login"))


@app.route("/admin")
@login_required
def admin_dashboard():
    db = get_db()
    release_stale_holds(db)
    rows = db.execute(
        """
        SELECT r.id, r.name, r.email, r.phone, r.status, r.created_at, r.price, r.voucher_code,
               r.payment_method, t.number AS table_number
        FROM registrations r
        JOIN tables t ON t.id = r.table_id
        WHERE r.status != 'cancelled'
        ORDER BY t.number
        """
    ).fetchall()
    stats = db.execute(
        "SELECT status, COUNT(*) AS n FROM tables GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["n"] for r in stats}

    image = get_setting(db, "floorplan_image")
    plan_tables = db.execute(
        """
        SELECT number, status, pos_x, pos_y
        FROM tables
        WHERE pos_x IS NOT NULL AND pos_y IS NOT NULL
        ORDER BY number
        """
    ).fetchall()

    return render_template(
        "admin_dashboard.html",
        registrations=rows,
        stats=stats,
        num_tables=NUM_TABLES,
        currency=CURRENCY,
        floorplan_image_url=url_for("static", filename=f"uploads/{image}") if image else None,
        plan_tables=plan_tables,
    )


@app.route("/admin/cancel/<int:registration_id>", methods=["POST"])
@login_required
def admin_cancel(registration_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
    if reg is not None:
        release_voucher(db, reg["voucher_code"])
        db.execute("UPDATE registrations SET status='cancelled' WHERE id=?", (registration_id,))
        db.execute(
            "UPDATE tables SET status='free', held_at=NULL, registration_id=NULL WHERE id=?",
            (reg["table_id"],),
        )
        db.commit()
        flash("Tisch wurde freigegeben.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/confirm-sepa/<int:registration_id>", methods=["POST"])
@login_required
def admin_confirm_sepa(registration_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
    if reg is None:
        flash("Registrierung nicht gefunden.")
    elif reg["payment_method"] != "sepa":
        flash("Diese Registrierung nutzt keine Überweisung.")
    elif reg["status"] != "pending":
        flash("Diese Registrierung ist nicht mehr offen.")
    else:
        finalize_paid_registration(db, reg)
        flash("Zahlung bestätigt – der Tisch ist jetzt gebucht.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/vouchers", methods=["GET", "POST"])
@login_required
def admin_vouchers():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            code = (request.form.get("code") or "").strip()
            try:
                max_uses = max(1, int(request.form.get("max_uses") or 1))
            except ValueError:
                max_uses = 1
            if not code:
                flash("Bitte einen Code angeben.")
            else:
                try:
                    db.execute(
                        "INSERT INTO vouchers (code, max_uses, used_count, active, created_at) "
                        "VALUES (?, ?, 0, 1, ?)",
                        (code, max_uses, datetime.utcnow().isoformat()),
                    )
                    db.commit()
                    flash(f"Gutscheincode „{code}“ wurde angelegt.")
                except sqlite3.IntegrityError:
                    flash("Dieser Code existiert bereits.")

        elif action == "bulk_generate":
            try:
                count = max(1, min(200, int(request.form.get("count") or 0)))
            except ValueError:
                count = 0
            prefix = (request.form.get("prefix") or "MA").strip() or "MA"
            created = []
            for _ in range(count):
                for attempt in range(5):
                    code = f"{prefix}-{secrets.token_hex(4).upper()}"
                    try:
                        db.execute(
                            "INSERT INTO vouchers (code, max_uses, used_count, active, created_at) "
                            "VALUES (?, 1, 0, 1, ?)",
                            (code, datetime.utcnow().isoformat()),
                        )
                        created.append(code)
                        break
                    except sqlite3.IntegrityError:
                        # Extremely unlikely code collision – try again with a new one.
                        continue
                else:
                    flash("Ein Code konnte nach mehreren Versuchen nicht eindeutig generiert werden – bitte erneut versuchen.")
            db.commit()
            if created:
                flash(f"{len(created)} Einzel-Codes erstellt: " + ", ".join(created))

        elif action == "toggle":
            voucher_id = request.form.get("voucher_id")
            db.execute("UPDATE vouchers SET active = 1 - active WHERE id=?", (voucher_id,))
            db.commit()

        elif action == "delete":
            voucher_id = request.form.get("voucher_id")
            db.execute("DELETE FROM vouchers WHERE id=?", (voucher_id,))
            db.commit()

        return redirect(url_for("admin_vouchers"))

    vouchers = db.execute("SELECT * FROM vouchers ORDER BY created_at DESC").fetchall()
    return render_template(
        "admin_vouchers.html",
        vouchers=vouchers,
        price_standard=PRICE_STANDARD,
        price_internal=PRICE_INTERNAL,
        currency=CURRENCY,
    )


@app.route("/admin/emails", methods=["GET", "POST"])
@login_required
def admin_emails():
    db = get_db()

    if request.method == "POST":
        kind = request.form.get("kind")
        if kind in ("confirmation", "sepa"):
            subject = (request.form.get("subject") or "").strip()
            body = (request.form.get("body") or "").strip()
            if subject and body:
                set_setting(db, f"email_{kind}_subject", subject)
                set_setting(db, f"email_{kind}_body", body)
                flash("E-Mail-Text wurde gespeichert.")
            else:
                flash("Betreff und Text dürfen nicht leer sein.")
        return redirect(url_for("admin_emails"))

    confirmation_subject, confirmation_body = get_email_template(db, "confirmation")
    sepa_subject, sepa_body = get_email_template(db, "sepa")
    return render_template(
        "admin_emails.html",
        confirmation_subject=confirmation_subject,
        confirmation_body=confirmation_body,
        sepa_subject=sepa_subject,
        sepa_body=sepa_body,
        sepa_hold_hours=SEPA_HOLD_HOURS,
    )


@app.route("/admin/page", methods=["GET", "POST"])
@login_required
def admin_page():
    db = get_db()

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        info = (request.form.get("info") or "").strip()
        if not title:
            flash("Der Titel darf nicht leer sein.")
        else:
            set_setting(db, "event_title", title)
            set_setting(db, "event_info", info)
            flash("Seiteninhalt wurde gespeichert.")
        return redirect(url_for("admin_page"))

    event_title = get_setting(db, "event_title", DEFAULT_EVENT_TITLE)
    event_info = get_setting(db, "event_info", DEFAULT_EVENT_INFO)
    return render_template(
        "admin_page.html",
        event_title=event_title,
        event_info=event_info,
    )


@app.route("/admin/faq", methods=["GET", "POST"])
@login_required
def admin_faq():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            question = (request.form.get("question") or "").strip()
            answer = (request.form.get("answer") or "").strip()
            if not question or not answer:
                flash("Frage und Antwort dürfen nicht leer sein.")
            else:
                db.execute(
                    "INSERT INTO faq (question, answer, created_at) VALUES (?, ?, ?)",
                    (question, answer, datetime.utcnow().isoformat()),
                )
                db.commit()
                flash("Frage wurde hinzugefügt.")

        elif action == "update":
            faq_id = request.form.get("faq_id")
            question = (request.form.get("question") or "").strip()
            answer = (request.form.get("answer") or "").strip()
            if not question or not answer:
                flash("Frage und Antwort dürfen nicht leer sein.")
            else:
                db.execute(
                    "UPDATE faq SET question=?, answer=? WHERE id=?",
                    (question, answer, faq_id),
                )
                db.commit()
                flash("Frage wurde aktualisiert.")

        elif action == "delete":
            faq_id = request.form.get("faq_id")
            db.execute("DELETE FROM faq WHERE id=?", (faq_id,))
            db.commit()
            flash("Frage wurde gelöscht.")

        return redirect(url_for("admin_faq"))

    faq_items = db.execute("SELECT id, question, answer FROM faq ORDER BY id").fetchall()
    return render_template("admin_faq.html", faq_items=faq_items)


@app.route("/admin/floorplan", methods=["GET", "POST"])
@login_required
def admin_floorplan():
    db = get_db()

    if request.method == "POST":
        file = request.files.get("floorplan")
        if file and file.filename and allowed_file(file.filename):
            file_bytes = file.read()
            if not is_valid_image(file_bytes):
                flash("Die Datei ist kein gültiges Bild.")
                return redirect(url_for("admin_floorplan"))

            old_image = get_setting(db, "floorplan_image")
            if old_image:
                old_path = os.path.join(UPLOAD_FOLDER, old_image)
                if os.path.exists(old_path):
                    os.remove(old_path)
            ext = secure_filename(file.filename).rsplit(".", 1)[1].lower()
            filename = f"floorplan.{ext}"
            with open(os.path.join(UPLOAD_FOLDER, filename), "wb") as f:
                f.write(file_bytes)
            set_setting(db, "floorplan_image", filename)
            flash("Lageplan wurde hochgeladen.")
        else:
            flash("Bitte eine gültige Bilddatei auswählen (png, jpg, jpeg, webp).")
        return redirect(url_for("admin_floorplan"))

    image = get_setting(db, "floorplan_image")
    tables = db.execute("SELECT number, status, pos_x, pos_y FROM tables ORDER BY number").fetchall()
    return render_template(
        "admin_floorplan.html",
        image_url=url_for("static", filename=f"uploads/{image}") if image else None,
        tables=tables,
    )


@app.route("/admin/api/set-position", methods=["POST"])
@login_required
def admin_set_position():
    data = request.get_json(force=True)
    number = data.get("number")
    x = data.get("x")
    y = data.get("y")

    db = get_db()
    db.execute("UPDATE tables SET pos_x=?, pos_y=? WHERE number=?", (x, y, number))
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/clear-position", methods=["POST"])
@login_required
def admin_clear_position():
    data = request.get_json(force=True)
    number = data.get("number")

    db = get_db()
    db.execute("UPDATE tables SET pos_x=NULL, pos_y=NULL WHERE number=?", (number,))
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
