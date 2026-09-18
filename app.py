import hmac
import io
import json
import os
import re
import shutil
import ssl
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo
import secrets
import smtplib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
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
from werkzeug.exceptions import BadRequest, HTTPException, UnsupportedMediaType

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration – adjust here or via .env
# ---------------------------------------------------------------------------
CURRENCY = os.environ.get("CURRENCY", "EUR")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # "sandbox" or "live"

PAYPAL_API_BASE = (
    "https://api-m.sandbox.paypal.com" if PAYPAL_MODE == "sandbox" else "https://api-m.paypal.com"
)

HOLD_MINUTES = 10  # how long a PayPal table hold stays reserved for payment
DB_PATH = os.environ.get("DB_PATH", "flohmarkt.db")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if len(SECRET_KEY) < 32 or SECRET_KEY in {"please-change-in-.env", "a-random-long-string"}:
    raise RuntimeError("SECRET_KEY must be a private random value of at least 32 characters.")

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
    print(
        f"[config] PAYMENT_METHODS={os.environ.get('PAYMENT_METHODS')!r} is empty/invalid – falling back to paypal,sepa."
    )
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


def utcnow():
    """Return naive UTC for compatibility with existing SQLite timestamps."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Name of the very first event and of a new one started without an explicit name.
DEFAULT_EVENT_NAME = "Aktueller Flohmarkt"
ARCHIVED_REGISTRATION_MESSAGE = (
    "Diese Anmeldung gehört zu einem archivierten Flohmarkt und kann nicht mehr geändert werden."
)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=15)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Default lead time of the payment reminder; configurable per event in the admin UI.
DEFAULT_REMINDER_HOURS = 24


def init_db():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.execute("BEGIN IMMEDIATE")
    db.execute("""
        CREATE TABLE IF NOT EXISTS tables (
            id INTEGER PRIMARY KEY,
            number INTEGER UNIQUE NOT NULL,
            status TEXT NOT NULL DEFAULT 'free',  -- free, held, booked
            held_at TEXT,
            registration_id INTEGER,
            pos_x REAL,  -- position on the floor plan in % (0-100), NULL = no floor plan marker
            pos_y REAL
        )
        """)
    db.execute("""
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
        """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS vouchers (
            id INTEGER PRIMARY KEY,
            code TEXT UNIQUE NOT NULL,
            max_uses INTEGER NOT NULL DEFAULT 1,
            used_count INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS faq (
            id INTEGER PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
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
        db.execute(
            "ALTER TABLE registrations ADD COLUMN payment_method TEXT NOT NULL DEFAULT 'paypal'"
        )
    if "reminder_sent" not in reg_cols:
        db.execute("ALTER TABLE registrations ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0")

    # Serialize schema upgrades across WSGI workers and preserve existing bookings.
    additions = {
        "expires_at": "TEXT",
        "create_attempted_at": "TEXT",
        "edit_version": "INTEGER NOT NULL DEFAULT 0",
        "owner_id": "TEXT",
        "create_request_id": "TEXT",
        "capture_request_id": "TEXT",
        "payment_reference": "TEXT",
        "payment_received_at": "TEXT",
        "payment_review": "INTEGER NOT NULL DEFAULT 0",
        "event_id": "INTEGER",
    }
    for column, definition in additions.items():
        if column not in reg_cols:
            db.execute(f"ALTER TABLE registrations ADD COLUMN {column} {definition}")
    # Existing transfers must keep the reference already sent to the customer.
    db.execute(
        "UPDATE registrations SET payment_reference='FLOHMARKT-' || "
        "(SELECT number FROM tables WHERE tables.id=registrations.table_id) "
        "WHERE payment_method='sepa' AND payment_reference IS NULL"
    )
    db.execute("""
        CREATE TABLE IF NOT EXISTS payment_receipts (
            capture_id TEXT PRIMARY KEY,
            registration_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            received_at TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS email_outbox (
            id INTEGER PRIMARY KEY,
            registration_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            locked_until REAL NOT NULL DEFAULT 0,
            lease_token TEXT,
            sent_at TEXT,
            cancelled_at TEXT,
            last_error TEXT,
            UNIQUE(registration_id, kind)
        )
    """)
    db.execute(
        "CREATE INDEX IF NOT EXISTS registrations_paypal_order ON registrations(paypal_order_id)"
    )
    db.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            archived_at TEXT,
            snapshot TEXT  -- JSON copy of the content this event was run with
        )
    """)
    # Exactly one event is active at a time; everything booked belongs to it.
    # Older databases get that event retroactively so existing registrations
    # stay visible instead of disappearing behind the archive filter.
    active_event_row = db.execute("SELECT id FROM events WHERE archived_at IS NULL").fetchone()
    if active_event_row is None:
        oldest = db.execute("SELECT MIN(created_at) FROM registrations").fetchone()[0]
        db.execute(
            "INSERT INTO events (name, created_at) VALUES (?, ?)",
            (DEFAULT_EVENT_NAME, oldest or utcnow().isoformat()),
        )
        active_event_row = db.execute("SELECT id FROM events WHERE archived_at IS NULL").fetchone()
    db.execute("UPDATE registrations SET event_id=? WHERE event_id IS NULL", (active_event_row[0],))
    db.execute("CREATE INDEX IF NOT EXISTS registrations_event ON registrations(event_id)")

    migrate_event_configuration(db, active_event_row[0])

    # Seed a few placeholder FAQ entries on first run so the admin has
    # something concrete to edit/replace rather than an empty list; never
    # re-seeds once the admin has added or removed anything.
    faq_count = db.execute("SELECT COUNT(*) FROM faq").fetchone()[0]
    if faq_count == 0:
        placeholder_faq = [
            ("Muss ich meinen Tisch selbst aufbauen?", "<Bitte hier die Antwort eintragen>"),
            (
                "Was passiert, wenn ich nicht rechtzeitig bezahle?",
                "<Bitte hier die Antwort eintragen>",
            ),
            ("Kann ich meine Reservierung stornieren?", "<Bitte hier die Antwort eintragen>"),
        ]
        now_iso = utcnow().isoformat()
        for question, answer in placeholder_faq:
            db.execute(
                "INSERT INTO faq (question, answer, created_at) VALUES (?, ?, ?)",
                (question, answer, now_iso),
            )

    db.commit()
    db.close()


def migrate_event_configuration(db, event_id):
    """One-time import. Environment variables never override persisted settings."""
    event_cols = {r[1] for r in db.execute("PRAGMA table_info(events)")}
    for name, definition in {
        "sepa_hold_hours": "INTEGER",
        "payment_cutoff": "TEXT",
        "reminder_hours": f"INTEGER NOT NULL DEFAULT {DEFAULT_REMINDER_HOURS}",
    }.items():
        if name not in event_cols:
            db.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    db.execute("""CREATE TABLE IF NOT EXISTS tariffs (
        id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, name TEXT NOT NULL,
        amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
        visibility TEXT NOT NULL DEFAULT 'public', active INTEGER NOT NULL DEFAULT 1,
        all_tables INTEGER NOT NULL DEFAULT 1, UNIQUE(event_id, name))""")
    db.execute("""CREATE TABLE IF NOT EXISTS tariff_tables (
        tariff_id INTEGER NOT NULL, table_id INTEGER NOT NULL,
        PRIMARY KEY(tariff_id, table_id))""")
    cols = {r[1] for r in db.execute("PRAGMA table_info(tables)")}
    if "event_id" not in cols:
        db.execute("""CREATE TABLE tables_new (
            id INTEGER PRIMARY KEY, number INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'free',
            held_at TEXT, registration_id INTEGER, pos_x REAL, pos_y REAL,
            event_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, tariff_id INTEGER,
            UNIQUE(event_id, number))""")
        db.execute(
            "INSERT INTO tables_new (id, number, status, held_at, registration_id, pos_x, pos_y, event_id) "
            "SELECT id, number, status, held_at, registration_id, pos_x, pos_y, ? FROM tables",
            (event_id,),
        )
        db.execute("DROP TABLE tables")
        db.execute("ALTER TABLE tables_new RENAME TO tables")
    for table, additions in {
        "vouchers": {"tariff_id": "INTEGER"},
        "registrations": {
            "tariff_id": "INTEGER",
            "tariff_name": "TEXT",
            "base_price_cents": "INTEGER",
            "price_cents": "INTEGER",
        },
    }.items():
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in additions.items():
            if name not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    if db.execute("SELECT 1 FROM settings WHERE key='event_configuration_v1'").fetchone():
        return
    existing = db.execute("SELECT COUNT(*) FROM tables").fetchone()[0] > 0
    legacy = existing or any(
        key in os.environ
        for key in ("PRICE", "PRICE_STANDARD", "PRICE_INTERNAL", "NUM_TABLES", "SEPA_HOLD_HOURS")
    )
    if legacy:
        standard = money_cents(os.environ.get("PRICE_STANDARD", os.environ.get("PRICE", "15")))
        internal = money_cents(os.environ.get("PRICE_INTERNAL", str(Decimal(standard) / 100)))
        hours = bounded_int(os.environ.get("SEPA_HOLD_HOURS", "48"), 1, 8760)
        db.execute("UPDATE events SET sepa_hold_hours=? WHERE id=?", (hours, event_id))
        standard_id = db.execute(
            "INSERT INTO tariffs(event_id,name,amount_cents) VALUES (?, 'Standard', ?)",
            (event_id, standard),
        ).lastrowid
        internal_id = db.execute(
            "INSERT INTO tariffs(event_id,name,amount_cents,visibility) VALUES (?, 'Intern', ?, 'code')",
            (event_id, internal),
        ).lastrowid
        count = bounded_int(os.environ.get("NUM_TABLES", "0" if existing else "30"), 0, 10000)
        for number in range(1, count + 1):
            db.execute(
                "INSERT OR IGNORE INTO tables(number,event_id) VALUES (?,?)", (number, event_id)
            )
        db.execute("UPDATE tables SET tariff_id=? WHERE event_id=?", (standard_id, event_id))
        db.execute("UPDATE vouchers SET tariff_id=?", (internal_id,))
        # Freeze legacy deadlines before the global configuration disappears.
        for row in db.execute(
            "SELECT id,created_at,payment_method FROM registrations WHERE expires_at IS NULL"
        ).fetchall():
            duration = (
                timedelta(hours=hours) if row[2] == "sepa" else timedelta(minutes=HOLD_MINUTES)
            )
            db.execute(
                "UPDATE registrations SET expires_at=? WHERE id=?",
                ((datetime.fromisoformat(row[1]) + duration).isoformat(), row[0]),
            )
        db.execute(
            "UPDATE registrations SET tariff_name=CASE WHEN voucher_code IS NULL THEN 'Standard' ELSE 'Intern' END, "
            "price_cents=CAST(ROUND(price*100) AS INTEGER), base_price_cents=?",
            (standard,),
        )
    db.execute("INSERT INTO settings(key,value) VALUES ('event_configuration_v1','1')")


def bounded_int(value, minimum, maximum):
    try:
        if len(str(value)) > 10 or not str(value).isdecimal():
            raise ValueError
        number = int(value)
        if not minimum <= number <= maximum:
            raise ValueError
        return number
    except (ValueError, TypeError):
        raise BadRequest(f"Bitte eine ganze Zahl zwischen {minimum} und {maximum} angeben.")


def money_cents(value):
    try:
        amount = Decimal(str(value).replace(",", "."))
        if (
            not amount.is_finite()
            or not Decimal("0.01") <= amount <= Decimal("99999.99")
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise BadRequest(
            "Bitte einen Preis zwischen 0,01 und 99.999,99 mit höchstens zwei Nachkommastellen angeben."
        )


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


def active_event(db):
    """The event new bookings belong to. Archived events are read-only history."""
    return db.execute(
        "SELECT * FROM events WHERE archived_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()


def active_event_id(db):
    row = active_event(db)
    return row["id"] if row else None


def event_tariffs(db):
    return db.execute(
        "SELECT * FROM tariffs WHERE event_id=? ORDER BY id", (active_event_id(db),)
    ).fetchall()


def tariff_for(db, table, voucher=None):
    tariff_id = voucher["tariff_id"] if voucher is not None else table["tariff_id"]
    tariff = db.execute(
        "SELECT * FROM tariffs WHERE id=? AND event_id=? AND active=1",
        (tariff_id, active_event_id(db)),
    ).fetchone()
    if tariff is None or (voucher is None and tariff["visibility"] != "public"):
        raise BadRequest("Für diesen Tisch ist kein buchbarer Tarif eingerichtet.")
    if (
        not tariff["all_tables"]
        and not db.execute(
            "SELECT 1 FROM tariff_tables WHERE tariff_id=? AND table_id=?",
            (tariff["id"], table["id"]),
        ).fetchone()
    ):
        raise BadRequest("Dieser Tarif gilt nicht für den ausgewählten Tisch.")
    return tariff


def reminder_lead(db):
    """Lead time of the payment reminder before the deadline; None = disabled."""
    event = active_event(db)
    hours = event["reminder_hours"] if event and "reminder_hours" in event.keys() else None
    if hours is None:
        hours = DEFAULT_REMINDER_HOURS
    return timedelta(hours=hours) if hours > 0 else None


def reminder_due(db, reg, *, now=None, lead=None):
    """Check current event, booking and deadline before queuing or sending."""
    now = now or utcnow()
    lead = reminder_lead(db) if lead is None else lead
    if (lead is None or reg is None or reg["status"] != "pending"
            or reg["payment_method"] != "sepa" or reg["reminder_sent"]
            or reg["event_id"] != active_event_id(db)):
        return False
    deadline = deadline_for(reg)
    return (timedelta(0) < deadline - now <= lead
            and deadline - datetime.fromisoformat(reg["created_at"]) > lead)


def refresh_queued_reminders(db, event_id):
    """Drop obsolete waiting reminders, preserving messages already in flight."""
    rows = db.execute(
        "SELECT o.id AS outbox_id, r.* FROM email_outbox o "
        "JOIN registrations r ON r.id=o.registration_id "
        "WHERE r.event_id=? AND o.kind='reminder' AND o.sent_at IS NULL "
        "AND o.cancelled_at IS NULL AND o.locked_until<=?",
        (event_id, time.time()),
    ).fetchall()
    for reg in rows:
        if not reminder_due(db, reg):
            db.execute("DELETE FROM email_outbox WHERE id=?", (reg["outbox_id"],))


def configuration_ready(db):
    event = active_event(db)
    if not event or not event["sepa_hold_hours"]:
        return False
    if event["payment_cutoff"] and datetime.fromisoformat(event["payment_cutoff"]) <= utcnow():
        return False
    tables = db.execute(
        "SELECT * FROM tables WHERE event_id=? AND active=1", (event["id"],)
    ).fetchall()
    if not tables:
        return False
    try:
        for table in tables:
            tariff_for(db, table)
    except BadRequest:
        return False
    return True


def booking_expiry(db, method, created_at):
    event = active_event(db)
    expiry = datetime.fromisoformat(created_at) + (
        timedelta(hours=event["sepa_hold_hours"])
        if method == "sepa"
        else timedelta(minutes=HOLD_MINUTES)
    )
    if event["payment_cutoff"]:
        expiry = min(expiry, datetime.fromisoformat(event["payment_cutoff"]))
    if expiry <= utcnow():
        raise BadRequest("Die Buchungsfrist ist abgelaufen.")
    return expiry.isoformat()


@contextmanager
def write_transaction(db):
    """Acquire the write lock before reading any state used by a state transition."""
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except Exception:
        db.rollback()
        raise


def json_object():
    if not request.is_json:
        raise UnsupportedMediaType("Content-Type muss application/json sein.")
    data = request.get_json()
    if not isinstance(data, dict):
        raise BadRequest("Ein JSON-Objekt wird erwartet.")
    return data


def text_field(data, key, limit, required=False):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise BadRequest(f"Ungültiges Feld: {key}.")
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise BadRequest(f"Ungültiges Feld: {key}.")
    return value


def positive_id(data, key):
    value = data.get(key)
    if type(value) is not int or not 0 < value <= 2147483647:
        raise BadRequest(f"Ungültiges Feld: {key}.")
    return value


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith(("/api/", "/admin/api/", "/webhooks/")):
        return jsonify(error=error.description), error.code
    return error


@app.errorhandler(sqlite3.OperationalError)
def database_error(error):
    if "locked" not in str(error).lower():
        raise error
    return jsonify(error="Bitte versuche es in wenigen Sekunden erneut."), 503


def deadline_for(reg):
    if "expires_at" in reg.keys() and reg["expires_at"]:
        return datetime.fromisoformat(reg["expires_at"])
    duration = (
        timedelta(hours=48) if reg["payment_method"] == "sepa" else timedelta(minutes=HOLD_MINUTES)
    )
    return datetime.fromisoformat(reg["created_at"]) + duration


def display_deadline(reg):
    return (
        deadline_for(reg)
        .replace(tzinfo=timezone.utc)
        .astimezone(ZoneInfo("Europe/Berlin"))
        .strftime("%d.%m.%Y um %H:%M Uhr")
    )


def browser_owner():
    if "booking_owner" not in session:
        session["booking_owner"] = secrets.token_urlsafe(32)
    return session["booking_owner"]


def owns_registration(reg):
    owner = session.get("booking_owner")
    return bool(reg and owner and reg["owner_id"] and hmac.compare_digest(owner, reg["owner_id"]))


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
    """Validate and reserve one use inside the caller's write transaction.
    Returns (voucher_row, error_message) – error_message is None on success."""
    code = (code or "").strip()
    if not code:
        return None, None

    voucher = db.execute("SELECT * FROM vouchers WHERE code = ? COLLATE NOCASE", (code,)).fetchone()
    if voucher is None or not voucher["active"]:
        return None, "Dieser Gutscheincode ist ungültig."
    if voucher["used_count"] >= voucher["max_uses"]:
        return None, "Dieser Gutscheincode wurde bereits vollständig eingelöst."

    cur = db.execute(
        "UPDATE vouchers SET used_count=used_count+1 WHERE id=? AND active=1 AND used_count<max_uses",
        (voucher["id"],),
    )
    if cur.rowcount != 1:
        return None, "Dieser Gutscheincode wurde bereits vollständig eingelöst."
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
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


# Admin pages render a flashed message as a success notice by default; errors
# have to say so explicitly, otherwise a rejection looks like a confirmation.
def flash_error(message):
    flash(message, "error")


# The database keeps the English status values; the interface is German.
STATUS_LABELS = {"pending": "offen", "paid": "bezahlt", "cancelled": "storniert", "free": "frei", "held": "reserviert", "booked": "vergeben"}


@app.template_filter("status_label")
def status_label(status):
    return STATUS_LABELS.get(status, status)


def cancel_registration_locked(db, reg):
    """Release only this registration's table and voucher under the write lock."""
    if reg["status"] == "cancelled":
        return False
    release_voucher(db, reg["voucher_code"])
    db.execute(
        "UPDATE registrations SET status='cancelled', "
        "payment_review=CASE WHEN status='paid' OR payment_received_at IS NOT NULL "
        "THEN 1 ELSE payment_review END WHERE id=?",
        (reg["id"],),
    )
    db.execute(
        "UPDATE tables SET status='free', held_at=NULL, registration_id=NULL "
        "WHERE id=? AND registration_id=?",
        (reg["table_id"], reg["id"]),
    )
    db.execute(
        "UPDATE email_outbox SET cancelled_at=? WHERE registration_id=? AND sent_at IS NULL",
        (utcnow().isoformat(), reg["id"]),
    )
    return True


def updated_table_number(db, reg):
    return db.execute("SELECT number FROM tables WHERE id=?", (reg["table_id"],)).fetchone()[0]


def free_table_by_number(db, number):
    """Resolve a submitted table number to a free, active table of the current event."""
    if len(number) > 10 or not number.isdecimal() or not 1 <= int(number) <= 2147483647:
        raise BadRequest("Bitte einen gültigen Zieltisch auswählen.")
    target = db.execute(
        "SELECT * FROM tables WHERE number=? AND active=1 AND event_id=?",
        (int(number), active_event_id(db)),
    ).fetchone()
    if target is None or target["status"] != "free":
        raise BadRequest("Der Zieltisch ist nicht mehr frei. Bitte wähle einen anderen Tisch.")
    return target


def restore_registration_locked(db, reg, form):
    """Give a cancelled booking from the history a free table again (write lock held).

    Money already recorded makes the booking 'paid' and the table 'booked';
    otherwise the booking becomes 'pending' with a fresh payment deadline.
    Price, voucher and bank transfer reference of the original booking are kept.
    """
    if reg["status"] != "cancelled":
        raise BadRequest("Nur stornierte Buchungen können einem Tisch zugewiesen werden.")
    target = free_table_by_number(db, form.get("table", ""))
    target_tariff = tariff_for(db, target)
    if target_tariff["amount_cents"] != round(reg["price"] * 100) and form.get("keep_price") != "yes":
        raise BadRequest(
            f"Der Zieltisch kostet regulär {target_tariff['amount_cents'] / 100:.2f} {CURRENCY}. Bitte die Zuweisung zum bisherigen Buchungspreis ausdrücklich bestätigen."
        )
    if reg["voucher_code"]:
        _, error = reserve_voucher(db, reg["voucher_code"])
        if error:
            raise BadRequest(f"Der Gutschein {reg['voucher_code']} kann nicht erneut eingelöst werden: {error}")
    now = utcnow().isoformat()
    # Money that arrived after the hold expired (bank transfer) can be recorded
    # in the same step instead of restoring first and confirming afterwards.
    paid = bool(reg["payment_received_at"]) or form.get("payment_received") == "yes"
    if paid:
        db.execute(
            "UPDATE registrations SET status='paid', table_id=?, payment_review=0, expires_at=NULL, "
            "payment_received_at=COALESCE(payment_received_at, ?) WHERE id=?",
            (target["id"], now, reg["id"]),
        )
        cur = db.execute(
            "UPDATE tables SET status='booked', held_at=?, registration_id=? WHERE id=? AND status='free'",
            (now, reg["id"], target["id"]),
        )
    else:
        expires_at = booking_expiry(db, reg["payment_method"], now)
        db.execute(
            "UPDATE registrations SET status='pending', table_id=?, expires_at=? WHERE id=?",
            (target["id"], expires_at, reg["id"]),
        )
        cur = db.execute(
            "UPDATE tables SET status='held', held_at=?, registration_id=? WHERE id=? AND status='free'",
            (now, reg["id"], target["id"]),
        )
    if cur.rowcount != 1:
        raise RuntimeError("Table ownership changed inside a write transaction")
    return paid


def release_stale_holds(db):
    with write_transaction(db):
        now = utcnow()
        pending = db.execute("SELECT * FROM registrations WHERE status='pending'").fetchall()
        for reg in pending:
            if deadline_for(reg) <= now:
                cancel_registration_locked(db, reg)


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


def paypal_create_order(amount, reference_id, request_id):
    token = paypal_get_access_token()
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id,
            "Prefer": "return=representation",
        },
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


def paypal_capture_order(order_id, request_id):
    token = paypal_get_access_token()
    resp = requests.post(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "PayPal-Request-Id": request_id,
            "Prefer": "return=representation",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def paypal_get_order(order_id):
    token = paypal_get_access_token()
    resp = requests.get(
        f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
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

DEFAULT_REMINDER_SUBJECT = "Erinnerung: Zahlung für Tisch {{tisch}} noch offen"
DEFAULT_REMINDER_BODY = """Hallo {{name}},

deine Reservierung für Tisch {{tisch}} läuft bald ab – bitte überweise den Betrag zeitnah,
damit der Tisch nicht automatisch wieder freigegeben wird.

Zu zahlender Betrag: {{preis}}
Verwendungszweck: {{referenz}}
Zahlungsfrist: {{frist}}

Falls du bereits überwiesen hast, kannst du diese Nachricht ignorieren.

Bis zum Flohmarkt!"""

EMAIL_TEMPLATE_DEFAULTS = {
    "confirmation": (DEFAULT_CONFIRMATION_SUBJECT, DEFAULT_CONFIRMATION_BODY),
    "sepa": (DEFAULT_SEPA_SUBJECT, DEFAULT_SEPA_BODY),
    "reminder": (DEFAULT_REMINDER_SUBJECT, DEFAULT_REMINDER_BODY),
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


def render_registration_email(db, kind, reg):
    """Render current templates with the registration's current details."""
    table = db.execute("SELECT number FROM tables WHERE id=?", (reg["table_id"],)).fetchone()
    subject, body = get_email_template(db, kind)
    values = dict(
        name=reg["name"],
        tisch=table["number"],
        preis=f"{reg['price']:.2f} {CURRENCY}",
        gutschein=reg["voucher_code"] or "",
        referenz=reg["payment_reference"] or "",
        frist=display_deadline(reg),
    )
    return render_email_template(subject, **values), render_email_template(body, **values)


def queue_email(db, kind, reg):
    """Insert the rendered message in the same transaction as its business event."""
    subject, body = render_registration_email(db, kind, reg)
    db.execute(
        "INSERT INTO email_outbox (registration_id, kind, recipient, subject, body) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(registration_id,kind) DO NOTHING",
        (
            reg["id"],
            kind,
            reg["email"],
            subject,
            body,
        ),
    )


def deliver_email(row):
    msg = EmailMessage()
    msg["Subject"] = row["subject"]
    msg["From"] = SMTP_FROM
    msg["To"] = row["recipient"]
    # Reuse a stable Message-ID on retries, including after a worker crash.
    domain = SMTP_FROM.rsplit("@", 1)[-1] or "localhost"
    msg["Message-ID"] = f"<flohmarkt-{row['registration_id']}-{row['kind']}@{domain}>"
    msg.set_content(row["body"])
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls(context=ssl.create_default_context())
        if SMTP_USER:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(msg)


def process_email_outbox(db, limit=20):
    """Lease work across processes; never hold a database lock during SMTP I/O."""
    if not SMTP_HOST:
        return
    for _ in range(limit):
        now = time.time()
        token = uuid.uuid4().hex
        with write_transaction(db):
            row = db.execute(
                "SELECT * FROM email_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL "
                "AND next_attempt_at<=? AND locked_until<=? ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return
            if row["kind"] == "reminder":
                reg = db.execute(
                    "SELECT * FROM registrations WHERE id=?", (row["registration_id"],)
                ).fetchone()
                if not reminder_due(db, reg):
                    db.execute("DELETE FROM email_outbox WHERE id=?", (row["id"],))
                    continue
            db.execute(
                "UPDATE email_outbox SET lease_token=?, locked_until=?, attempts=attempts+1 WHERE id=?",
                (token, now + 300, row["id"]),
            )
        try:
            deliver_email(row)
        except Exception as error:
            with write_transaction(db):
                db.execute(
                    "UPDATE email_outbox SET last_error=?, next_attempt_at=?, locked_until=0, lease_token=NULL "
                    "WHERE id=? AND lease_token=?",
                    (
                        type(error).__name__,
                        time.time() + min(3600, 30 * 2 ** min(row["attempts"], 7)),
                        row["id"],
                        token,
                    ),
                )
            app.logger.warning(
                "Email delivery failed for outbox id=%s (%s)", row["id"], type(error).__name__
            )
        else:
            with write_transaction(db):
                db.execute(
                    "UPDATE email_outbox SET sent_at=?, locked_until=0, lease_token=NULL, last_error=NULL "
                    "WHERE id=? AND lease_token=?",
                    (utcnow().isoformat(), row["id"], token),
                )
                if row["kind"] == "reminder":
                    db.execute(
                        "UPDATE registrations SET reminder_sent=1 WHERE id=?",
                        (row["registration_id"],),
                    )


def finalize_paid_registration(db, registration_id, captures=None, *, manual_sepa=False):
    """Record received money separately from allocation, using fresh locked state."""
    with write_transaction(db):
        reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if reg is None:
            return "not_found"
        if manual_sepa:
            # Validate inside the same write lock as payment confirmation so a
            # stale admin page cannot confirm a cancelled or archived booking.
            table = db.execute("SELECT * FROM tables WHERE id=?", (reg["table_id"],)).fetchone()
            if (
                reg["payment_method"] != "sepa"
                or reg["event_id"] != active_event_id(db)
                or reg["status"] != "pending"
                or reg["payment_received_at"]
                or deadline_for(reg) <= utcnow()
                or table is None
                or table["status"] != "held"
                or table["registration_id"] != registration_id
            ):
                return "manual_confirmation_not_allowed"
        now = utcnow().isoformat()
        if captures is not None:
            for capture in captures:
                existing = db.execute(
                    "SELECT registration_id FROM payment_receipts WHERE capture_id=?",
                    (capture["id"],),
                ).fetchone()
                if existing and existing["registration_id"] != registration_id:
                    raise ValueError("Capture is already assigned to another registration")
                db.execute(
                    "INSERT OR IGNORE INTO payment_receipts VALUES (?, ?, ?, ?, ?)",
                    (
                        capture["id"],
                        registration_id,
                        capture["amount"]["value"],
                        capture["amount"]["currency_code"],
                        now,
                    ),
                )
        db.execute(
            "UPDATE registrations SET payment_received_at=COALESCE(payment_received_at, ?) WHERE id=?",
            (now, registration_id),
        )
        table = db.execute("SELECT * FROM tables WHERE id=?", (reg["table_id"],)).fetchone()
        if (
            reg["status"] == "paid"
            and table["status"] == "booked"
            and table["registration_id"] == registration_id
        ):
            return "already_booked"
        if reg["status"] == "cancelled" and reg["payment_received_at"]:
            return "payment_received_unallocated"
        if (
            reg["status"] != "pending"
            or deadline_for(reg) <= utcnow()
            or table["status"] != "held"
            or table["registration_id"] != registration_id
        ):
            if reg["status"] == "pending":
                cancel_registration_locked(db, reg)
            db.execute("UPDATE registrations SET payment_review=1 WHERE id=?", (registration_id,))
            return "payment_received_unallocated"
        cur = db.execute(
            "UPDATE tables SET status='booked' WHERE id=? AND status='held' AND registration_id=?",
            (reg["table_id"], registration_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Table ownership changed inside a write transaction")
        db.execute(
            "UPDATE registrations SET status='paid' WHERE id=? AND status='pending'",
            (registration_id,),
        )
        db.execute(
            "UPDATE email_outbox SET cancelled_at=? WHERE registration_id=? AND kind IN ('reminder','sepa') AND sent_at IS NULL",
            (now, registration_id),
        )
        queue_email(db, "confirmation", reg)
        return "booked"


def send_sepa_reminders(db):
    now = utcnow()
    with write_transaction(db):
        lead = reminder_lead(db)
        if lead is None:
            return
        candidates = db.execute(
            "SELECT * FROM registrations WHERE status='pending' AND payment_method='sepa' "
            "AND reminder_sent=0"
        ).fetchall()
        for reg in candidates:
            if reminder_due(db, reg, now=now, lead=lead):
                queue_email(db, "reminder", reg)


def _background_loop():
    while True:
        try:
            with app.app_context():
                db = get_db()
                release_stale_holds(db)
                send_sepa_reminders(db)
                process_email_outbox(db)
        except Exception:
            app.logger.exception("Background maintenance failed")
        time.sleep(30)


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
    browser_owner()
    db = get_db()
    event_title = get_setting(db, "event_title", DEFAULT_EVENT_TITLE)
    event_info = get_setting(db, "event_info", DEFAULT_EVENT_INFO)
    faq_items = db.execute("SELECT id, question, answer FROM faq ORDER BY id").fetchall()
    return render_template(
        "index.html",
        currency=CURRENCY,
        paypal_client_id=PAYPAL_CLIENT_ID,
        payment_methods_enabled=ENABLED_PAYMENT_METHODS,
        sepa_hold_hours=active_event(db)["sepa_hold_hours"],
        event_title=event_title,
        event_info=event_info,
        faq_items=faq_items,
        booking_ready=configuration_ready(db),
        event_id=active_event_id(db),
    )


@app.route("/api/tables")
def api_tables():
    db = get_db()
    release_stale_holds(db)
    rows = db.execute(
        "SELECT * FROM tables WHERE event_id=? ORDER BY number", (active_event_id(db),)
    ).fetchall()
    ready = configuration_ready(db)
    result = []
    for row in rows:
        try:
            tariff = tariff_for(db, row)
        except BadRequest:
            tariff = None
        result.append(
            {
                "number": row["number"],
                "status": row["status"] if ready and row["active"] and tariff else "disabled",
                "price": tariff["amount_cents"] / 100 if tariff else None,
                "tariff_name": tariff["name"] if tariff else None,
            }
        )
    return jsonify(result)


@app.route("/api/floorplan-config")
def api_floorplan_config():
    db = get_db()
    image = get_setting(db, "floorplan_image")
    rows = db.execute(
        "SELECT number, pos_x, pos_y FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND pos_x IS NOT NULL AND pos_y IS NOT NULL ORDER BY number"
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

    table = db.execute(
        "SELECT * FROM tables WHERE number=? AND event_id=? AND active=1",
        (request.args.get("table"), active_event_id(db)),
    ).fetchone()
    if table is None:
        return jsonify(valid=False)
    try:
        tariff = tariff_for(db, table, voucher)
    except BadRequest as exc:
        return jsonify(valid=False, error=exc.description)
    return jsonify(valid=True, price=tariff["amount_cents"] / 100, tariff_name=tariff["name"])


@app.route("/api/register", methods=["POST"])
@limiter.limit("20 per minute")
def api_register():
    data = json_object()
    name = text_field(data, "name", 200, required=True)
    email = text_field(data, "email", 254, required=True)
    if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", email):
        raise BadRequest("Bitte gib eine gültige E-Mail-Adresse an.")
    phone = text_field(data, "phone", 50)
    table_number = positive_id(data, "table")
    voucher_input = text_field(data, "voucher", 128)
    payment_method = text_field(data, "payment_method", 20) or DEFAULT_PAYMENT_METHOD
    if payment_method not in ENABLED_PAYMENT_METHODS:
        raise BadRequest("Diese Zahlungsart ist nicht verfügbar.")
    owner = browser_owner()
    db = get_db()
    release_stale_holds(db)
    with write_transaction(db):
        if "event_id" in data and data["event_id"] != active_event_id(db):
            return (
                jsonify(error="Die Veranstaltung wurde gewechselt. Bitte die Seite neu laden."),
                409,
            )
        if not configuration_ready(db):
            return (
                jsonify(
                    error="Die Buchung ist noch nicht eingerichtet oder die Buchungsfrist ist abgelaufen."
                ),
                409,
            )
        table = db.execute(
            "SELECT * FROM tables WHERE number=? AND event_id=? AND active=1",
            (table_number, active_event_id(db)),
        ).fetchone()
        if table is None:
            return jsonify(error="Tisch existiert nicht."), 404
        if table["status"] != "free":
            existing = db.execute(
                "SELECT * FROM registrations WHERE id=?", (table["registration_id"],)
            ).fetchone()
            if (
                owns_registration(existing)
                and existing["event_id"] == active_event_id(db)
                and existing["status"] in ("pending", "paid")
            ):
                return jsonify(public_booking(existing, table_number))
            return jsonify(error="Dieser Tisch ist leider nicht mehr verfügbar."), 409
        base_tariff = tariff_for(db, table)
        tariff, voucher_code = base_tariff, None
        if voucher_input:
            voucher, error = reserve_voucher(db, voucher_input)
            if error:
                raise BadRequest(error)
            tariff, voucher_code = tariff_for(db, table, voucher), voucher["code"]
        if (
            "expected_price" in data
            and money_cents(data["expected_price"]) != tariff["amount_cents"]
        ):
            raise BadRequest(
                "Der Preis wurde inzwischen geändert. Bitte den Tisch erneut auswählen und den aktuellen Preis prüfen."
            )
        price = tariff["amount_cents"] / 100
        now = utcnow().isoformat()
        expires_at = booking_expiry(db, payment_method, now)
        cur = db.execute(
            "INSERT INTO registrations (name, email, phone, table_id, status, created_at, price, "
            "voucher_code, payment_method, owner_id, create_request_id, capture_request_id, event_id) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                email,
                phone,
                table["id"],
                now,
                price,
                voucher_code,
                payment_method,
                owner,
                str(uuid.uuid4()),
                str(uuid.uuid4()),
                active_event_id(db),
            ),
        )
        registration_id = cur.lastrowid
        cur = db.execute(
            "UPDATE tables SET status='held', held_at=?, registration_id=? "
            "WHERE id=? AND status='free'",
            (now, registration_id, table["id"]),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Table ownership changed inside a write transaction")
        # Keep the table-only transfer reference chosen for this event.
        reference = f"FLOHMARKT-{table_number}"
        db.execute(
            "UPDATE registrations SET payment_reference=?, expires_at=?, tariff_id=?, tariff_name=?, base_price_cents=?, price_cents=? WHERE id=?",
            (
                reference,
                expires_at,
                tariff["id"],
                tariff["name"],
                base_tariff["amount_cents"],
                tariff["amount_cents"],
                registration_id,
            ),
        )
        reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if payment_method == "sepa":
            queue_email(db, "sepa", reg)
    return jsonify(public_booking(reg, table_number))


def public_booking(reg, table_number):
    """Only disclose this summary after validating the browser owner."""
    status = "review" if reg["payment_review"] else reg["status"]
    return {
        "registration_id": reg["id"],
        "table": table_number,
        "price": reg["price"],
        "currency": CURRENCY,
        "voucher_applied": bool(reg["voucher_code"]),
        "payment_method": reg["payment_method"],
        "status": status,
        "reference": reg["payment_reference"],
        "deadline": display_deadline(reg),
        "expires_at": deadline_for(reg).replace(tzinfo=timezone.utc).isoformat(),
        "server_time": utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "has_order": bool(reg["paypal_order_id"]),
    }


def owned_current_booking(db, registration_id=None):
    owner = session.get("booking_owner")
    if not owner:
        return None
    if registration_id is None:
        return db.execute(
            "SELECT r.*, t.number AS table_number FROM registrations r "
            "JOIN tables t ON t.id=r.table_id WHERE r.owner_id=? AND r.event_id=? "
            "ORDER BY r.id DESC LIMIT 1",
            (owner, active_event_id(db)),
        ).fetchone()
    return db.execute(
        "SELECT r.*, t.number AS table_number FROM registrations r "
        "JOIN tables t ON t.id=r.table_id WHERE r.id=? AND r.owner_id=? AND r.event_id=?",
        (registration_id, owner, active_event_id(db)),
    ).fetchone()


@app.route("/api/booking")
@limiter.limit("60 per minute")
def api_booking():
    db = get_db()
    release_stale_holds(db)
    reg = owned_current_booking(db)
    response = jsonify(booking=public_booking(reg, reg["table_number"]) if reg else None)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/booking/check", methods=["POST"])
@limiter.limit("20 per minute")
def api_check_booking():
    registration_id = positive_id(json_object(), "registration_id")
    db = get_db()
    reg = owned_current_booking(db, registration_id)
    if reg is None:
        return jsonify(error="Buchung nicht gefunden."), 404
    # Reconcile an existing order only. Checking status never initiates a charge.
    if reg["payment_method"] == "paypal" and reg["paypal_order_id"] and reg["status"] != "paid":
        try:
            order = paypal_get_order(reg["paypal_order_id"])
            record_completed_order(db, reg, order)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            return paypal_unavailable()
    release_stale_holds(db)
    reg = owned_current_booking(db, registration_id)
    if reg is None:
        return jsonify(error="Dieser Flohmarkt wurde inzwischen archiviert."), 409
    response = jsonify(booking=public_booking(reg, reg["table_number"]))
    response.headers["Cache-Control"] = "no-store"
    return response


def paypal_unavailable():
    return (
        jsonify(
            error="Der Zahlungsstatus konnte noch nicht sicher ermittelt werden. "
            "Bitte versuche es erneut. Deine bestehende Zahlung wird dabei geprüft."
        ),
        503,
    )


def booking_response(outcome):
    if outcome in ("booked", "already_booked"):
        return jsonify(status="paid", booking_status=outcome)
    if outcome == "payment_review":
        return (
            jsonify(
                status=outcome,
                error="Die Zahlungsdaten müssen vom Veranstalter geprüft werden. "
                "Eine erfolgreiche Tischbuchung konnte noch nicht bestätigt werden.",
            ),
            409,
        )
    return (
        jsonify(
            status=outcome,
            error="Deine Zahlung ist eingegangen, aber der Tisch konnte nicht "
            "zugeordnet werden. Bitte kontaktiere den Veranstalter zur Klärung oder Erstattung.",
        ),
        409,
    )


def completed_captures(order, reg):
    """Validate the complete server-side order before allocating a table."""
    if order.get("id") != reg["paypal_order_id"]:
        raise ValueError("Unexpected PayPal order ID")
    if order.get("status") != "COMPLETED":
        return None
    units = order.get("purchase_units", [])
    if len(units) != 1 or units[0].get("reference_id") != str(reg["id"]):
        raise ValueError("Unexpected purchase units or registration reference")
    captures = units[0].get("payments", {}).get("captures", [])
    if not captures or any(c.get("status") != "COMPLETED" for c in captures):
        raise ValueError("Order does not contain completed captures")
    total = Decimal("0")
    seen = set()
    for capture in captures:
        capture_id = capture.get("id")
        if (
            not isinstance(capture_id, str)
            or not re.fullmatch(r"[A-Za-z0-9]{1,64}", capture_id)
            or capture_id in seen
        ):
            raise ValueError("Invalid or duplicate capture ID")
        seen.add(capture_id)
        amount = capture.get("amount", {})
        value = Decimal(amount.get("value", "NaN"))
        if amount.get("currency_code") != CURRENCY or not value.is_finite() or value <= 0:
            raise ValueError("Unexpected capture amount or currency")
        total += value
    if total != Decimal(f"{reg['price']:.2f}"):
        raise ValueError("Captured amount does not match the registration")
    return captures


def record_completed_order(db, reg, order):
    try:
        captures = completed_captures(order, reg)
    except (ValueError, InvalidOperation, TypeError, KeyError, AttributeError):
        with write_transaction(db):
            db.execute("UPDATE registrations SET payment_review=1 WHERE id=?", (reg["id"],))
        app.logger.error("PayPal order validation failed for registration id=%s", reg["id"])
        return "payment_review"
    if captures is None:
        return None
    return finalize_paid_registration(db, reg["id"], captures)


@app.route("/api/create-order", methods=["POST"])
@limiter.limit("20 per minute")
def api_create_order():
    registration_id = positive_id(json_object(), "registration_id")
    db = get_db()
    release_stale_holds(db)
    with write_transaction(db):
        reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if not owns_registration(reg):
            return jsonify(error="Registrierung nicht gefunden."), 404
        if reg["status"] != "pending" or reg["payment_method"] != "paypal" or reg["payment_review"]:
            return jsonify(error="Diese Registrierung kann nicht mehr bezahlt werden."), 409
        if reg["paypal_order_id"]:
            return jsonify(order_id=reg["paypal_order_id"])
        # Extended holds can outlive PayPal's idempotency retention. Do not
        # blindly retry an unresolved creation after the safe retry window.
        if reg["create_attempted_at"] and utcnow() - datetime.fromisoformat(
            reg["create_attempted_at"]
        ) >= timedelta(hours=5):
            db.execute("UPDATE registrations SET payment_review=1 WHERE id=?", (registration_id,))
            return booking_response("payment_review")
        db.execute(
            "UPDATE registrations SET create_attempted_at=COALESCE(create_attempted_at, ?) WHERE id=?",
            (utcnow().isoformat(), registration_id),
        )
        # Persist one key before network I/O so retries and workers use the same operation.
        if not reg["create_request_id"]:
            db.execute(
                "UPDATE registrations SET create_request_id=?, capture_request_id=? WHERE id=?",
                (str(uuid.uuid4()), str(uuid.uuid4()), registration_id),
            )
            reg = db.execute(
                "SELECT * FROM registrations WHERE id=?", (registration_id,)
            ).fetchone()
    try:
        order = paypal_create_order(reg["price"], str(registration_id), reg["create_request_id"])
        order_id = order.get("id")
        if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", order_id):
            raise ValueError("Invalid PayPal order ID")
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        app.logger.warning(
            "PayPal order creation unresolved for registration id=%s", registration_id
        )
        return paypal_unavailable()
    with write_transaction(db):
        current = db.execute(
            "SELECT * FROM registrations WHERE id=?", (registration_id,)
        ).fetchone()
        if current["paypal_order_id"] and current["paypal_order_id"] != order_id:
            db.execute("UPDATE registrations SET payment_review=1 WHERE id=?", (registration_id,))
            return jsonify(error="Die Zahlung muss vom Veranstalter geprüft werden."), 409
        db.execute(
            "UPDATE registrations SET paypal_order_id=? WHERE id=? AND paypal_order_id IS NULL",
            (order_id, registration_id),
        )
        # Retain the mapping even if a cancellation happened during the API request.
        if current["status"] != "pending" or deadline_for(current) <= utcnow():
            return (
                jsonify(error="Diese Reservierung ist inzwischen abgelaufen oder storniert."),
                409,
            )
    return jsonify(order_id=order_id)


@app.route("/api/capture-order", methods=["POST"])
@limiter.limit("20 per minute")
def api_capture_order():
    order_id = text_field(json_object(), "order_id", 64, required=True)
    if not re.fullmatch(r"[A-Za-z0-9]+", order_id):
        raise BadRequest("Ungültige Bestellnummer.")
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE paypal_order_id=?", (order_id,)).fetchone()
    if not owns_registration(reg):
        return jsonify(error="Bestellung nicht gefunden."), 404
    # Reconcile first: a previous capture may have succeeded despite a timeout.
    try:
        order = paypal_get_order(order_id)
        outcome = record_completed_order(db, reg, order)
        if outcome:
            return booking_response(outcome)
        release_stale_holds(db)
        with write_transaction(db):
            reg = db.execute("SELECT * FROM registrations WHERE id=?", (reg["id"],)).fetchone()
            if reg["payment_review"]:
                return booking_response("payment_review")
            if reg["status"] != "pending":
                return jsonify(error="Diese Reservierung kann nicht mehr bezahlt werden."), 409
            if not reg["capture_request_id"]:
                db.execute(
                    "UPDATE registrations SET capture_request_id=? WHERE id=?",
                    (str(uuid.uuid4()), reg["id"]),
                )
                reg = db.execute("SELECT * FROM registrations WHERE id=?", (reg["id"],)).fetchone()
        try:
            order = paypal_capture_order(order_id, reg["capture_request_id"])
            if order.get("status") == "COMPLETED" and not order.get("purchase_units"):
                order = paypal_get_order(order_id)
        except requests.RequestException:
            # A failed response is not proof that no money moved.
            order = paypal_get_order(order_id)
            if order.get("status") != "COMPLETED":
                return paypal_unavailable()
        outcome = record_completed_order(db, reg, order)
        if outcome:
            return booking_response(outcome)
        return jsonify(error="Zahlung noch nicht abgeschlossen. Bitte versuche es erneut."), 402
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        app.logger.warning("PayPal capture unresolved for registration id=%s", reg["id"])
        return paypal_unavailable()


@app.route("/webhooks/paypal", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per minute")
def paypal_webhook():
    event = json_object()
    try:
        if not paypal_verify_webhook_signature(request.headers, event):
            return jsonify(error="invalid signature"), 400
        if event.get("event_type") == "PAYMENT.CAPTURE.COMPLETED":
            resource = event.get("resource", {})
            order_id = resource.get("supplementary_data", {}).get("related_ids", {}).get("order_id")
            if not isinstance(order_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", order_id):
                raise BadRequest("Invalid order ID")
            db = get_db()
            reg = db.execute(
                "SELECT * FROM registrations WHERE paypal_order_id=?", (order_id,)
            ).fetchone()
            if reg is None:
                # A creation request may still be persisting the order mapping.
                return jsonify(error="Order mapping not available yet"), 503
            outcome = record_completed_order(db, reg, paypal_get_order(order_id))
            if outcome is None:
                return jsonify(error="Capture not available yet"), 503
        return jsonify(ok=True)
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
        app.logger.warning("PayPal webhook processing unresolved")
        return jsonify(error="Payment verification temporarily unavailable"), 503


# ---------------------------------------------------------------------------
# Admin area
# ---------------------------------------------------------------------------
@app.context_processor
def admin_context():
    if request.endpoint and request.endpoint.startswith("admin_") and session.get("is_admin"):
        return {"admin_event_name": active_event(get_db())["name"]}
    return {}


@app.template_filter("admin_datetime")
def admin_datetime(value):
    if not value:
        return "–"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M")


@app.template_filter("admin_date")
def admin_date(value):
    return admin_datetime(value).split(" ")[0]


@app.template_filter("admin_money")
def admin_money(value):
    if value is None:
        return "–"
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("10 per minute")
def admin_login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode()):
            session.permanent = True
            session["is_admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash_error("Falsches Passwort.")
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

    view = request.args.get("view")
    view = view if view in ("history", "plan") else "active"
    search = request.args.get("q", "").strip()[:200]
    booking_filter = request.args.get("filter", "")
    booking_filter = booking_filter if booking_filter in ("pending", "review") else ""
    status_filter = (
        "r.status = 'cancelled'"
        if view == "history"
        else "(r.status != 'cancelled' OR r.payment_review=1)"
    )

    event = active_event(db)
    rows = db.execute(
        f"""
        SELECT r.id, r.name, r.email, r.phone, r.status, r.created_at, r.price, r.voucher_code,
               r.payment_method, r.payment_reference, r.payment_received_at, r.payment_review, r.expires_at, t.number AS table_number
        FROM registrations r
        JOIN tables t ON t.id = r.table_id
        WHERE {status_filter} AND r.event_id = ?
        ORDER BY t.number
        """,
        (event["id"],),
    ).fetchall()
    if search:
        term = search.casefold()
        rows = [
            r
            for r in rows
            if any(
                term in str(r[key] or "").casefold()
                for key in ("name", "email", "payment_reference")
            )
            or term == str(r["table_number"])
        ]
    if booking_filter == "pending":
        rows = [r for r in rows if r["status"] == "pending" and not r["payment_received_at"]]
    elif booking_filter == "review":
        rows = [r for r in rows if r["payment_review"]]
    stats = db.execute(
        "SELECT status, COUNT(*) AS n FROM tables WHERE active=1 AND event_id=(SELECT id FROM events WHERE archived_at IS NULL) GROUP BY status"
    ).fetchall()
    stats = {r["status"]: r["n"] for r in stats}

    image = get_setting(db, "floorplan_image")
    plan_tables = db.execute("""
        SELECT number, status, pos_x, pos_y
        FROM tables
        WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND pos_x IS NOT NULL AND pos_y IS NOT NULL
        ORDER BY number
        """).fetchall()

    return render_template(
        "admin_dashboard.html",
        registrations=rows,
        search=search,
        booking_filter=booking_filter,
        display_deadline=display_deadline,
        stats=stats,
        num_tables=db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=? AND active=1", (active_event_id(db),)
        ).fetchone()[0],
        currency=CURRENCY,
        floorplan_image_url=url_for("static", filename=f"uploads/{image}") if image else None,
        plan_tables=plan_tables,
        view=view,
        event=event,
        pending_emails=db.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL"
        ).fetchone()[0],
        failed_emails=db.execute(
            "SELECT COUNT(*) FROM email_outbox WHERE sent_at IS NULL AND cancelled_at IS NULL AND last_error IS NOT NULL"
        ).fetchone()[0],
    )


def refresh_pending_emails(db, reg):
    """Re-render unsent jobs only after an explicit admin booking edit."""
    rows = db.execute(
        "SELECT * FROM email_outbox WHERE registration_id=? "
        "AND sent_at IS NULL AND cancelled_at IS NULL",
        (reg["id"],),
    ).fetchall()
    lead = reminder_lead(db)
    for row in rows:
        # A postponed reminder must become eligible again at the new deadline.
        if row["kind"] == "reminder" and not reminder_due(db, reg, lead=lead):
            db.execute("DELETE FROM email_outbox WHERE id=?", (row["id"],))
            continue
        subject, body = render_registration_email(db, row["kind"], reg)
        db.execute(
            "UPDATE email_outbox SET recipient=?, subject=?, body=?, next_attempt_at=0, "
            "last_error=NULL WHERE id=?",
            (reg["email"], subject, body, row["id"]),
        )


@app.route("/admin/registrations/<int:registration_id>/edit", methods=["GET", "POST"])
@login_required
def admin_edit_registration(registration_id):
    db = get_db()
    release_stale_holds(db)
    error = None
    status = 200
    if request.method == "POST":
        try:
            with write_transaction(db):
                reg = db.execute(
                    "SELECT * FROM registrations WHERE id=?", (registration_id,)
                ).fetchone()
                if reg is None:
                    raise BadRequest("Registrierung nicht gefunden.")
                if reg["event_id"] != active_event_id(db):
                    raise BadRequest(ARCHIVED_REGISTRATION_MESSAGE)
                version = request.form.get("version", "")
                if (
                    len(version) > 10
                    or not version.isdecimal()
                    or int(version) != reg["edit_version"]
                ):
                    raise BadRequest(
                        "Die Buchung wurde inzwischen bearbeitet. Bitte prüfe die aktuellen Daten und wiederhole die Änderung."
                    )
                busy = db.execute(
                    "SELECT 1 FROM email_outbox WHERE registration_id=? AND sent_at IS NULL "
                    "AND cancelled_at IS NULL AND lease_token IS NOT NULL LIMIT 1",
                    (registration_id,),
                ).fetchone()
                if busy:
                    raise BadRequest(
                        "Eine E-Mail wird gerade verarbeitet. Bitte versuche die Änderung gleich erneut."
                    )
                action = request.form.get("action")
                if action == "contact":
                    name = text_field(request.form, "name", 200, required=True)
                    email = text_field(request.form, "email", 254, required=True)
                    phone = text_field(request.form, "phone", 50)
                    if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", email):
                        raise BadRequest("Bitte gib eine gültige E-Mail-Adresse an.")
                    db.execute(
                        "UPDATE registrations SET name=?, email=?, phone=? WHERE id=?",
                        (name, email, phone, registration_id),
                    )
                elif action in ("extend", "move"):
                    table = db.execute(
                        "SELECT * FROM tables WHERE id=?", (reg["table_id"],)
                    ).fetchone()
                    if reg["status"] == "pending" and deadline_for(reg) <= utcnow():
                        raise BadRequest("Die Reservierungsfrist ist bereits abgelaufen.")
                    expected = "held" if reg["status"] == "pending" else "booked"
                    if (
                        reg["status"] not in ("pending", "paid")
                        or table is None
                        or table["registration_id"] != registration_id
                        or table["status"] != expected
                    ):
                        raise BadRequest(
                            "Diese Buchung hat keinen aktiven Tisch mehr. Sie kann nicht verlängert oder umgebucht werden."
                        )
                    if action == "extend":
                        hours = request.form.get("hours", "")
                        if reg["status"] != "pending":
                            raise BadRequest("Nur offene Reservierungen können verlängert werden.")
                        if len(hours) > 3 or not hours.isdecimal() or not 1 <= int(hours) <= 720:
                            raise BadRequest(
                                "Bitte eine Verlängerung zwischen 1 und 720 Stunden angeben."
                            )
                        db.execute(
                            "UPDATE registrations SET expires_at=? WHERE id=?",
                            (
                                (deadline_for(reg) + timedelta(hours=int(hours))).isoformat(),
                                registration_id,
                            ),
                        )
                    else:
                        target = free_table_by_number(db, request.form.get("table", ""))
                        target_tariff = tariff_for(db, target)
                        if (
                            target_tariff["amount_cents"] != round(reg["price"] * 100)
                            and request.form.get("keep_price") != "yes"
                        ):
                            raise BadRequest(
                                f"Der Zieltisch kostet regulär {target_tariff['amount_cents'] / 100:.2f} {CURRENCY}. Bitte den Wechsel zum bisherigen Buchungspreis ausdrücklich bestätigen."
                            )
                        db.execute(
                            "UPDATE tables SET status=?, held_at=?, registration_id=? WHERE id=? AND status='free'",
                            (expected, table["held_at"], registration_id, target["id"]),
                        )
                        db.execute(
                            "UPDATE tables SET status='free', held_at=NULL, registration_id=NULL WHERE id=? AND registration_id=?",
                            (table["id"], registration_id),
                        )
                        # Preserve the reference already communicated for bank transfers.
                        db.execute(
                            "UPDATE registrations SET table_id=? WHERE id=?",
                            (target["id"], registration_id),
                        )
                elif action == "restore":
                    restore_registration_locked(db, reg, request.form)
                else:
                    raise BadRequest("Unbekannte Aktion.")
                db.execute(
                    "UPDATE registrations SET edit_version=edit_version+1 WHERE id=?",
                    (registration_id,),
                )
                updated = db.execute(
                    "SELECT * FROM registrations WHERE id=?", (registration_id,)
                ).fetchone()
                refresh_pending_emails(db, updated)
            if action == "restore":
                flash(
                    f"Buchung wiederhergestellt: Tisch {updated_table_number(db, updated)} ist jetzt "
                    + ("vergeben." if updated["status"] == "paid" else f"bis {display_deadline(updated)} reserviert.")
                    + " Es wird keine E-Mail automatisch versendet – bitte informiere den Teilnehmer."
                )
            else:
                flash(
                    "Änderung gespeichert. Noch nicht versendete E-Mails wurden aktualisiert. Bitte informiere den Teilnehmer bei Bedarf über die Änderung."
                )
            return redirect(url_for("admin_edit_registration", registration_id=registration_id))
        except BadRequest as exc:
            error, status = exc.description, 400
    reg = db.execute(
        "SELECT r.*, t.number AS table_number FROM registrations r "
        "JOIN tables t ON t.id=r.table_id WHERE r.id=?",
        (registration_id,),
    ).fetchone()
    if reg is None:
        return "Registrierung nicht gefunden.", 404
    if reg["event_id"] != active_event_id(db):
        flash_error(ARCHIVED_REGISTRATION_MESSAGE)
        return redirect(url_for("admin_event_detail", event_id=reg["event_id"]))
    return (
        render_template(
            "admin_registration_edit.html",
            reg=reg,
            error=error,
            deadline=display_deadline(reg),
            currency=CURRENCY,
            free_tables=db.execute(
                "SELECT t.number, p.amount_cents FROM tables t JOIN tariffs p ON p.id=t.tariff_id WHERE t.status='free' AND t.active=1 AND p.active=1 AND p.visibility='public' AND t.event_id=(SELECT id FROM events WHERE archived_at IS NULL) ORDER BY t.number"
            ).fetchall(),
        ),
        status,
    )


@app.route("/admin/cancel/<int:registration_id>", methods=["POST"])
@login_required
def admin_cancel(registration_id):
    db = get_db()
    with write_transaction(db):
        reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
        if reg is not None and reg["event_id"] != active_event_id(db):
            flash_error(ARCHIVED_REGISTRATION_MESSAGE)
        elif reg and cancel_registration_locked(db, reg):
            flash("Tisch wurde freigegeben. Bereits eingegangene Zahlungen bitte separat klären.")
        else:
            flash_error("Diese Registrierung ist bereits storniert oder existiert nicht.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/confirm-sepa/<int:registration_id>", methods=["POST"])
@login_required
def admin_confirm_sepa(registration_id):
    db = get_db()
    reg = db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()
    if reg is None or reg["payment_method"] != "sepa":
        flash_error("Überweisungsregistrierung nicht gefunden.")
    elif reg["event_id"] != active_event_id(db):
        flash_error(ARCHIVED_REGISTRATION_MESSAGE)
    else:
        outcome = finalize_paid_registration(db, registration_id, manual_sepa=True)
        if outcome in ("booked", "already_booked"):
            flash("Zahlung bestätigt – der Tisch ist vergeben.")
        else:
            flash_error(
                "Zahlungseingang kann nur für eine offene, gültige Überweisungsreservierung erfasst werden."
            )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/resolve-payment/<int:registration_id>", methods=["POST"])
@login_required
def admin_resolve_payment(registration_id):
    with write_transaction(get_db()):
        get_db().execute("UPDATE registrations SET payment_review=0 WHERE id=?", (registration_id,))
    flash("Zahlung als geprüft markiert. Es wurde keine automatische Erstattung ausgelöst.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/pricing", methods=["GET", "POST"])
@login_required
def admin_pricing():
    db = get_db()
    if request.method == "POST":
        try:
            with write_transaction(db):
                event = active_event(db)
                if request.form.get("event_id") != str(event["id"]):
                    raise BadRequest(
                        "Die Veranstaltung wurde gewechselt. Bitte die Seite neu laden."
                    )
                action = request.form.get("action")
                if action == "deadline":
                    duration = bounded_int(request.form.get("duration"), 1, 8760)
                    unit = request.form.get("unit")
                    if unit not in ("hours", "days"):
                        raise BadRequest("Ungültige Zeiteinheit.")
                    hours = duration * (24 if unit == "days" else 1)
                    if hours > 8760:
                        raise BadRequest("Die Frist darf höchstens 365 Tage betragen.")
                    cutoff = request.form.get("cutoff", "").strip()
                    if cutoff:
                        try:
                            local = datetime.fromisoformat(cutoff)
                            if local.tzinfo is not None:
                                raise ValueError
                            cutoff = (
                                local.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                                .astimezone(timezone.utc)
                                .replace(tzinfo=None)
                            )
                            if cutoff <= utcnow():
                                raise ValueError
                        except ValueError:
                            raise BadRequest(
                                "Bitte einen zukünftigen Termin in deutscher Ortszeit angeben."
                            )
                        cutoff = cutoff.isoformat()
                    db.execute(
                        "UPDATE events SET sepa_hold_hours=?,payment_cutoff=? WHERE id=?",
                        (hours, cutoff or None, event["id"]),
                    )
                elif action == "reminder":
                    duration = bounded_int(request.form.get("reminder_duration"), 0, 8760)
                    unit = request.form.get("reminder_unit")
                    if unit not in ("hours", "days"):
                        raise BadRequest("Ungültige Zeiteinheit.")
                    hours = duration * (24 if unit == "days" else 1)
                    if hours > 8760:
                        raise BadRequest("Die Erinnerung darf höchstens 365 Tage vorher erfolgen.")
                    db.execute(
                        "UPDATE events SET reminder_hours=? WHERE id=?", (hours, event["id"])
                    )
                    refresh_queued_reminders(db, event["id"])
                elif action == "tariff":
                    name = text_field(request.form, "name", 80, required=True)
                    cents = money_cents(request.form.get("price"))
                    visibility = request.form.get("visibility")
                    if visibility not in ("public", "code"):
                        raise BadRequest("Ungültige Verfügbarkeit.")
                    active = int(request.form.get("active") == "yes")
                    if request.form.get("scope") not in ("all", "selected"):
                        raise BadRequest("Ungültige Tarifgültigkeit.")
                    all_tables = int(request.form.get("scope") == "all")
                    numbers = {
                        bounded_int(n, 1, 2147483647) for n in request.form.getlist("table_ids")
                    }
                    valid_ids = {
                        row[0]
                        for row in db.execute(
                            "SELECT id FROM tables WHERE event_id=?", (event["id"],)
                        )
                    }
                    if not numbers <= valid_ids or (not all_tables and not numbers):
                        raise BadRequest(
                            "Bitte mindestens einen gültigen Tisch auswählen oder 'Alle Tische' verwenden."
                        )
                    tariff_id = request.form.get("tariff_id")
                    if tariff_id:
                        if not db.execute(
                            "SELECT 1 FROM tariffs WHERE id=? AND event_id=?",
                            (tariff_id, event["id"]),
                        ).fetchone():
                            raise BadRequest("Tarif nicht gefunden.")
                        db.execute(
                            "UPDATE tariffs SET name=?,amount_cents=?,visibility=?,active=?,all_tables=? WHERE id=?",
                            (name, cents, visibility, active, all_tables, tariff_id),
                        )
                    else:
                        tariff_id = db.execute(
                            "INSERT INTO tariffs(event_id,name,amount_cents,visibility,active,all_tables) VALUES (?,?,?,?,?,?)",
                            (event["id"], name, cents, visibility, active, all_tables),
                        ).lastrowid
                    db.execute("DELETE FROM tariff_tables WHERE tariff_id=?", (tariff_id,))
                    if not all_tables:
                        db.executemany(
                            "INSERT INTO tariff_tables(tariff_id,table_id) VALUES (?,?)",
                            [(tariff_id, n) for n in numbers],
                        )
                    if visibility == "public" and request.form.get("assign") == "yes":
                        targets = valid_ids if all_tables else numbers
                        db.executemany(
                            "UPDATE tables SET tariff_id=? WHERE id=?",
                            [(tariff_id, n) for n in targets],
                        )
                else:
                    raise BadRequest("Unbekannte Aktion.")
            flash(
                "Einstellungen gespeichert. Bestehende Buchungspreise und Fristen bleiben erhalten."
            )
            return redirect(url_for("admin_pricing"))
        except (BadRequest, sqlite3.IntegrityError) as exc:
            flash_error(
                exc.description
                if isinstance(exc, BadRequest)
                else "Dieser Tarifname existiert bereits."
            )
            # Show submitted data on errors, including new tariff forms.
            return render_pricing(db), 400
    return render_pricing(db)


def render_pricing(db):
    event = active_event(db)
    cutoff = (
        (
            datetime.fromisoformat(event["payment_cutoff"])
            .replace(tzinfo=timezone.utc)
            .astimezone(ZoneInfo("Europe/Berlin"))
            .strftime("%Y-%m-%dT%H:%M")
        )
        if event["payment_cutoff"]
        else ""
    )
    assignments = {}
    for row in db.execute("SELECT tariff_id,table_id FROM tariff_tables"):
        assignments.setdefault(row[0], []).append(row[1])
    return render_template(
        "admin_pricing.html",
        event=event,
        tariffs=event_tariffs(db),
        tables=db.execute(
            "SELECT * FROM tables WHERE event_id=? ORDER BY number", (event["id"],)
        ).fetchall(),
        assignments=assignments,
        ready=configuration_ready(db),
        cutoff=cutoff,
        currency=CURRENCY,
    )


@app.route("/admin/tables", methods=["POST"])
@login_required
def admin_tables():
    db = get_db()
    try:
        with write_transaction(db):
            event_id = active_event_id(db)
            if request.form.get("event_id") != str(event_id):
                raise BadRequest("Die Veranstaltung wurde gewechselt. Bitte die Seite neu laden.")
            action = request.form.get("action")
            if action == "add":
                start = bounded_int(request.form.get("start"), 1, 2147483647)
                end = bounded_int(
                    request.form.get("end") or str(start), start, min(start + 999, 2147483647)
                )
                tariff_id = request.form.get("tariff_id") or None
                if (
                    tariff_id
                    and not db.execute(
                        "SELECT 1 FROM tariffs WHERE id=? AND event_id=? AND visibility='public' AND all_tables=1",
                        (tariff_id, event_id),
                    ).fetchone()
                ):
                    raise BadRequest("Bitte einen öffentlichen Tarif für alle Tische auswählen.")
                for number in range(start, end + 1):
                    db.execute(
                        "INSERT INTO tables(number,event_id,tariff_id) VALUES (?,?,?)",
                        (number, event_id, tariff_id),
                    )
            elif action in ("edit", "delete"):
                table = db.execute(
                    "SELECT * FROM tables WHERE id=? AND event_id=?",
                    (request.form.get("table_id"), event_id),
                ).fetchone()
                if table is None:
                    raise BadRequest("Tisch nicht gefunden.")
                referenced = db.execute(
                    "SELECT 1 FROM registrations WHERE table_id=? LIMIT 1", (table["id"],)
                ).fetchone()
                if action == "delete":
                    if referenced or table["status"] != "free":
                        raise BadRequest(
                            "Tische mit Buchungshistorie können nur deaktiviert werden, sobald sie frei sind."
                        )
                    db.execute("DELETE FROM tariff_tables WHERE table_id=?", (table["id"],))
                    db.execute("DELETE FROM tables WHERE id=?", (table["id"],))
                else:
                    number = bounded_int(request.form.get("number"), 1, 2147483647)
                    active = int(request.form.get("active") == "yes")
                    if (number != table["number"] and referenced) or (
                        not active and table["status"] != "free"
                    ):
                        raise BadRequest(
                            "Tische mit Buchungshistorie dürfen nicht umnummeriert, belegte Tische nicht deaktiviert werden."
                        )
                    tariff_id = request.form.get("tariff_id") or None
                    if tariff_id:
                        tariff = db.execute(
                            "SELECT * FROM tariffs WHERE id=? AND event_id=? AND visibility='public'",
                            (tariff_id, event_id),
                        ).fetchone()
                        if not tariff or (
                            not tariff["all_tables"]
                            and not db.execute(
                                "SELECT 1 FROM tariff_tables WHERE tariff_id=? AND table_id=?",
                                (tariff_id, table["id"]),
                            ).fetchone()
                        ):
                            raise BadRequest("Dieser öffentliche Tarif gilt nicht für den Tisch.")
                    db.execute(
                        "UPDATE tables SET number=?,active=?,tariff_id=? WHERE id=?",
                        (number, active, tariff_id, table["id"]),
                    )
            else:
                raise BadRequest("Unbekannte Aktion.")
        flash("Tischverwaltung gespeichert.")
    except (BadRequest, sqlite3.IntegrityError) as exc:
        flash_error(
            exc.description
            if isinstance(exc, BadRequest)
            else "Mindestens eine Tischnummer existiert bereits. Es wurde nichts geändert."
        )
    return redirect(url_for("admin_floorplan", q=request.form.get("q", "")[:80],
                            filter=request.form.get("filter", "all"),
                            page=request.form.get("page", 1, type=int),
                            per_page=request.form.get("per_page", 12, type=int), _anchor="inventory"))


@app.route("/admin/vouchers", methods=["GET", "POST"])
@login_required
def admin_vouchers():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")
        tariff_id = request.form.get("tariff_id")
        if action in ("create", "bulk_generate"):
            tariff = db.execute(
                "SELECT * FROM tariffs WHERE id=? AND event_id=? AND active=1 AND visibility='code'",
                (tariff_id, active_event_id(db)),
            ).fetchone()
            if tariff is None:
                flash_error("Bitte einen aktiven Gutscheintarif auswählen.")
                return redirect(url_for("admin_vouchers"))

        if action == "create":
            code = (request.form.get("code") or "").strip()
            try:
                max_uses = max(1, int(request.form.get("max_uses") or 1))
            except ValueError:
                max_uses = 1
            if not code:
                flash_error("Bitte einen Code angeben.")
            else:
                try:
                    db.execute(
                        "INSERT INTO vouchers (code, max_uses, used_count, active, created_at, tariff_id) "
                        "VALUES (?, ?, 0, 1, ?, ?)",
                        (code, max_uses, utcnow().isoformat(), tariff_id),
                    )
                    db.commit()
                    flash(f"Gutscheincode „{code}“ wurde angelegt.")
                except sqlite3.IntegrityError:
                    flash_error("Dieser Code existiert bereits.")

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
                            "INSERT INTO vouchers (code, max_uses, used_count, active, created_at, tariff_id) "
                            "VALUES (?, 1, 0, 1, ?, ?)",
                            (code, utcnow().isoformat(), tariff_id),
                        )
                        created.append(code)
                        break
                    except sqlite3.IntegrityError:
                        # Extremely unlikely code collision – try again with a new one.
                        continue
                else:
                    flash_error(
                        "Ein Code konnte nach mehreren Versuchen nicht eindeutig generiert werden – bitte erneut versuchen."
                    )
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

    vouchers = db.execute(
        "SELECT v.*, p.name AS tariff_name, p.amount_cents AS tariff_amount_cents, p.active AS tariff_active FROM vouchers v LEFT JOIN tariffs p ON p.id=v.tariff_id ORDER BY v.created_at DESC"
    ).fetchall()
    return render_template(
        "admin_vouchers.html",
        vouchers=vouchers,
        tariffs=[t for t in event_tariffs(db) if t["visibility"] == "code" and t["active"]],
        currency=CURRENCY,
    )


@app.route("/admin/emails", methods=["GET", "POST"])
@login_required
def admin_emails():
    db = get_db()

    if request.method == "POST":
        kind = request.form.get("kind")
        if kind in ("confirmation", "sepa", "reminder"):
            subject = (request.form.get("subject") or "").strip()
            body = (request.form.get("body") or "").strip()
            if subject and body and "\r" not in subject and "\n" not in subject:
                set_setting(db, f"email_{kind}_subject", subject)
                set_setting(db, f"email_{kind}_body", body)
                flash("E-Mail-Text wurde gespeichert.")
            else:
                flash_error(
                    "Betreff und Text dürfen nicht leer sein; der Betreff darf keine Zeilenumbrüche enthalten."
                )
        return redirect(url_for("admin_emails"))

    confirmation_subject, confirmation_body = get_email_template(db, "confirmation")
    sepa_subject, sepa_body = get_email_template(db, "sepa")
    reminder_subject, reminder_body = get_email_template(db, "reminder")
    return render_template(
        "admin_emails.html",
        confirmation_subject=confirmation_subject,
        confirmation_body=confirmation_body,
        sepa_subject=sepa_subject,
        sepa_body=sepa_body,
        reminder_subject=reminder_subject,
        reminder_body=reminder_body,
        sepa_hold_hours=active_event(db)["sepa_hold_hours"],
        reminder_hours=active_event(db)["reminder_hours"],
    )


@app.route("/admin/page", methods=["GET", "POST"])
@login_required
def admin_page():
    db = get_db()

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        info = (request.form.get("info") or "").strip()
        if not title:
            flash_error("Der Titel darf nicht leer sein.")
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
                flash_error("Frage und Antwort dürfen nicht leer sein.")
            else:
                db.execute(
                    "INSERT INTO faq (question, answer, created_at) VALUES (?, ?, ?)",
                    (question, answer, utcnow().isoformat()),
                )
                db.commit()
                flash("Frage wurde hinzugefügt.")

        elif action == "update":
            faq_id = request.form.get("faq_id")
            question = (request.form.get("question") or "").strip()
            answer = (request.form.get("answer") or "").strip()
            if not question or not answer:
                flash_error("Frage und Antwort dürfen nicht leer sein.")
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


# ---------------------------------------------------------------------------
# Archiving an event and starting the next one
# ---------------------------------------------------------------------------
# What the admin can carry over into the new event. Everything not listed here
# is always reset, because it belongs to the event that just ended.
CARRY_OVER_OPTIONS = (
    "floorplan",
    "positions",
    "page",
    "faq",
    "emails",
    "vouchers",
    "tariffs",
    "deadlines",
    "tables",
)

ARCHIVED_IMAGE_PATTERN = re.compile(r"floorplan-event\d+\.[a-z0-9]{1,5}")


def archived_image_url(filename):
    """Only serve names this application generated when archiving."""
    if not filename or not ARCHIVED_IMAGE_PATTERN.fullmatch(filename):
        return None
    return url_for("static", filename=f"uploads/{filename}")


def build_event_snapshot(db, event_id, floorplan_image):
    """Freeze the content an event was run with, so the archive stays readable
    after the next event has replaced plan, texts and FAQ."""
    totals = db.execute(
        "SELECT COUNT(*) AS total, "
        "COALESCE(SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END), 0) AS paid, "
        "COALESCE(SUM(CASE WHEN status='paid' THEN price ELSE 0 END), 0) AS revenue "
        "FROM registrations WHERE event_id=?",
        (event_id,),
    ).fetchone()
    plan_tables = db.execute(
        "SELECT number, pos_x, pos_y FROM tables "
        "WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND pos_x IS NOT NULL AND pos_y IS NOT NULL ORDER BY number"
    ).fetchall()
    faq_items = db.execute("SELECT question, answer FROM faq ORDER BY id").fetchall()
    return {
        "event_title": get_setting(db, "event_title", DEFAULT_EVENT_TITLE),
        "event_info": get_setting(db, "event_info", DEFAULT_EVENT_INFO),
        "floorplan_image": floorplan_image,
        "num_tables": db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=?", (event_id,)
        ).fetchone()[0],
        "tariffs": [dict(t) for t in event_tariffs(db)],
        "sepa_hold_hours": active_event(db)["sepa_hold_hours"],
        "reminder_hours": active_event(db)["reminder_hours"],
        "payment_cutoff": active_event(db)["payment_cutoff"],
        "inventory": [
            dict(t) for t in db.execute("SELECT * FROM tables WHERE event_id=?", (event_id,))
        ],
        "currency": CURRENCY,
        "tables": [dict(row) for row in plan_tables],
        "faq": [dict(row) for row in faq_items],
        "stats": {
            "registrations": totals["total"],
            "paid": totals["paid"],
            "revenue": round(totals["revenue"] or 0, 2),
        },
    }


def archive_event(db, archive_name, new_name, keep):
    """Close the active event and open an empty one. Only the entries in `keep`
    survive; all tables become free again in either case."""
    event = active_event(db)
    now = utcnow().isoformat()

    # Copy the plan image out of the way first: the archive keeps its own copy,
    # so a later upload for the new event cannot overwrite the old plan.
    current_image = get_setting(db, "floorplan_image")
    archived_image = None
    if current_image:
        source = os.path.join(UPLOAD_FOLDER, current_image)
        candidate = f"floorplan-event{event['id']}.{current_image.rsplit('.', 1)[-1].lower()}"
        if os.path.exists(source) and ARCHIVED_IMAGE_PATTERN.fullmatch(candidate):
            shutil.copyfile(source, os.path.join(UPLOAD_FOLDER, candidate))
            archived_image = candidate

    with write_transaction(db):
        if active_event_id(db) != event["id"]:
            raise BadRequest(
                "Die Veranstaltung wurde inzwischen archiviert. Bitte die Seite neu laden."
            )
        # Nothing may stay reserved or trigger a reminder for a finished event.
        pending = db.execute(
            "SELECT * FROM registrations WHERE status='pending' AND event_id=?",
            (event["id"],),
        ).fetchall()
        for reg in pending:
            cancel_registration_locked(db, reg)
        db.execute(
            "UPDATE email_outbox SET cancelled_at=? WHERE sent_at IS NULL AND cancelled_at IS NULL "
            "AND registration_id IN (SELECT id FROM registrations WHERE event_id=?)",
            (now, event["id"]),
        )

        snapshot = build_event_snapshot(db, event["id"], archived_image)
        db.execute(
            "UPDATE events SET name=?, archived_at=?, snapshot=? WHERE id=? AND archived_at IS NULL",
            (archive_name, now, json.dumps(snapshot, ensure_ascii=False), event["id"]),
        )

        if "floorplan" not in keep:
            db.execute("DELETE FROM settings WHERE key='floorplan_image'")
        if "page" not in keep:
            db.execute("DELETE FROM settings WHERE key IN ('event_title', 'event_info')")
        if "emails" not in keep:
            db.execute("DELETE FROM settings WHERE key LIKE 'email\\_%' ESCAPE '\\'")
        if "faq" not in keep:
            db.execute("DELETE FROM faq")
        if "vouchers" in keep:
            # Codes stay valid for the new event, their redemptions start over.
            db.execute("UPDATE vouchers SET used_count=0")
        else:
            db.execute("DELETE FROM vouchers")

        new_id = db.execute(
            "INSERT INTO events (name, created_at, sepa_hold_hours, reminder_hours) "
            "VALUES (?, ?, ?, ?)",
            (
                new_name,
                now,
                event["sepa_hold_hours"] if "deadlines" in keep else None,
                event["reminder_hours"] if "deadlines" in keep else DEFAULT_REMINDER_HOURS,
            ),
        ).lastrowid
        tariff_map, table_map = {}, {}
        if "tariffs" in keep:
            for tariff in db.execute(
                "SELECT * FROM tariffs WHERE event_id=?", (event["id"],)
            ).fetchall():
                tariff_map[tariff["id"]] = db.execute(
                    "INSERT INTO tariffs(event_id,name,amount_cents,visibility,active,all_tables) VALUES (?,?,?,?,?,?)",
                    (
                        new_id,
                        tariff["name"],
                        tariff["amount_cents"],
                        tariff["visibility"],
                        tariff["active"],
                        tariff["all_tables"],
                    ),
                ).lastrowid
        if "tables" in keep:
            for table in db.execute(
                "SELECT * FROM tables WHERE event_id=?", (event["id"],)
            ).fetchall():
                table_map[table["id"]] = db.execute(
                    "INSERT INTO tables(number,event_id,active,tariff_id,pos_x,pos_y) VALUES (?,?,?,?,?,?)",
                    (
                        table["number"],
                        new_id,
                        table["active"],
                        tariff_map.get(table["tariff_id"]),
                        table["pos_x"] if "positions" in keep else None,
                        table["pos_y"] if "positions" in keep else None,
                    ),
                ).lastrowid
        for old_id, new_tariff in tariff_map.items():
            for row in db.execute(
                "SELECT table_id FROM tariff_tables WHERE tariff_id=?", (old_id,)
            ).fetchall():
                if row[0] in table_map:
                    db.execute(
                        "INSERT INTO tariff_tables VALUES (?,?)", (new_tariff, table_map[row[0]])
                    )
        if "vouchers" in keep:
            for voucher in db.execute("SELECT id,tariff_id FROM vouchers").fetchall():
                new_tariff = tariff_map.get(voucher["tariff_id"])
                db.execute(
                    "UPDATE vouchers SET tariff_id=?,active=CASE WHEN ? IS NULL THEN 0 ELSE active END WHERE id=?",
                    (new_tariff, new_tariff, voucher["id"]),
                )

    if "floorplan" not in keep and current_image:
        old_path = os.path.join(UPLOAD_FOLDER, current_image)
        if os.path.exists(old_path):
            os.remove(old_path)
    return event


@app.route("/admin/event", methods=["GET", "POST"])
@login_required
def admin_event():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")

        if action == "rename":
            name = (request.form.get("name") or "").strip()
            if not name or len(name) > 120:
                flash_error("Bitte einen Namen mit höchstens 120 Zeichen angeben.")
            else:
                db.execute("UPDATE events SET name=? WHERE id=?", (name, active_event_id(db)))
                db.commit()
                flash("Name wurde gespeichert.")

        elif action == "archive":
            archive_name = (request.form.get("archive_name") or "").strip()
            new_name = (request.form.get("new_name") or "").strip() or DEFAULT_EVENT_NAME
            keep = {value for value in request.form.getlist("keep") if value in CARRY_OVER_OPTIONS}
            if request.form.get("confirm") != "yes":
                flash_error("Bitte bestätige das Archivieren mit dem Häkchen.")
            elif not archive_name or len(archive_name) > 120 or len(new_name) > 120:
                flash_error("Bitte Namen mit höchstens 120 Zeichen angeben.")
            else:
                archive_event(db, archive_name, new_name, keep)
                flash(
                    f"„{archive_name}“ wurde archiviert. „{new_name}“ ist gestartet – "
                    "alle Tische sind wieder frei."
                )

        elif action == "delete":
            event_id = request.form.get("event_id")
            event = db.execute(
                "SELECT * FROM events WHERE id=? AND archived_at IS NOT NULL", (event_id,)
            ).fetchone()
            if event is None:
                flash_error("Nur bereits archivierte Flohmärkte können gelöscht werden.")
            else:
                snapshot = json.loads(event["snapshot"]) if event["snapshot"] else {}
                with write_transaction(db):
                    db.execute(
                        "DELETE FROM email_outbox WHERE registration_id IN "
                        "(SELECT id FROM registrations WHERE event_id=?)",
                        (event["id"],),
                    )
                    db.execute(
                        "DELETE FROM payment_receipts WHERE registration_id IN "
                        "(SELECT id FROM registrations WHERE event_id=?)",
                        (event["id"],),
                    )
                    db.execute("DELETE FROM registrations WHERE event_id=?", (event["id"],))
                    db.execute("DELETE FROM events WHERE id=?", (event["id"],))
                image = snapshot.get("floorplan_image")
                if image and ARCHIVED_IMAGE_PATTERN.fullmatch(image):
                    path = os.path.join(UPLOAD_FOLDER, image)
                    if os.path.exists(path):
                        os.remove(path)
                flash(f"Das Archiv „{event['name']}“ wurde gelöscht.")

        return redirect(url_for("admin_event"))

    event = active_event(db)
    summary = dict(
        registrations=db.execute(
            "SELECT COUNT(*) FROM registrations WHERE event_id=? AND status!='cancelled'",
            (event["id"],),
        ).fetchone()[0],
        booked=db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND status='booked'"
        ).fetchone()[0],
        held=db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND status='held'"
        ).fetchone()[0],
        positions=db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND pos_x IS NOT NULL AND pos_y IS NOT NULL"
        ).fetchone()[0],
        faq=db.execute("SELECT COUNT(*) FROM faq").fetchone()[0],
        vouchers=db.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0],
        floorplan=bool(get_setting(db, "floorplan_image")),
    )
    archived = db.execute("""
        SELECT e.id, e.name, e.created_at, e.archived_at,
               (SELECT COUNT(*) FROM registrations r WHERE r.event_id = e.id) AS registrations
        FROM events e
        WHERE e.archived_at IS NOT NULL
        ORDER BY e.archived_at DESC
        """).fetchall()
    return render_template(
        "admin_event.html",
        event=event,
        summary=summary,
        archived=archived,
        event_title=get_setting(db, "event_title", DEFAULT_EVENT_TITLE),
    )


@app.route("/admin/event/<int:event_id>")
@login_required
def admin_event_detail(event_id):
    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if event is None:
        return "Flohmarkt nicht gefunden.", 404
    if event["archived_at"] is None:
        return redirect(url_for("admin_event"))

    snapshot = json.loads(event["snapshot"]) if event["snapshot"] else {}
    registrations = db.execute(
        "SELECT r.*, t.number AS table_number FROM registrations r "
        "JOIN tables t ON t.id = r.table_id WHERE r.event_id=? ORDER BY t.number",
        (event_id,),
    ).fetchall()
    return render_template(
        "admin_event_detail.html",
        event=event,
        snapshot=snapshot,
        registrations=registrations,
        currency=snapshot.get("currency", CURRENCY),
        floorplan_image_url=archived_image_url(snapshot.get("floorplan_image")),
    )


@app.route("/admin/floorplan", methods=["GET", "POST"])
@login_required
def admin_floorplan():
    db = get_db()

    if request.method == "POST":
        file = request.files.get("floorplan")
        if file and file.filename and allowed_file(file.filename):
            file_bytes = file.read()
            if not is_valid_image(file_bytes):
                flash_error("Die Datei ist kein gültiges Bild.")
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
            flash_error("Bitte eine gültige Bilddatei auswählen (png, jpg, jpeg, webp).")
        return redirect(url_for("admin_floorplan"))

    image = get_setting(db, "floorplan_image")
    tables = db.execute(
        "SELECT * FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) ORDER BY number"
    ).fetchall()
    query = request.args.get("q", "").strip()[:80]
    inventory_filter = request.args.get("filter", "all")
    if inventory_filter not in ("all", "free", "held", "booked", "inactive", "unplaced"):
        inventory_filter = "all"
    filtered = [t for t in tables if (not query or query in str(t["number"])) and (
        inventory_filter == "all"
        or (inventory_filter in ("free", "held", "booked") and t["active"] and t["status"] == inventory_filter)
        or (inventory_filter == "inactive" and not t["active"])
        or (inventory_filter == "unplaced" and (t["pos_x"] is None or t["pos_y"] is None))
    )]
    page_sizes = (12, 25, 50, 100)
    per_page = request.args.get("per_page", 12, type=int)
    if per_page not in page_sizes:
        per_page = 12
    page_count = max(1, (len(filtered) + per_page - 1) // per_page)
    page = min(page_count, max(1, request.args.get("page", 1, type=int)))
    selected = next((t for t in tables if str(t["id"]) == request.args.get("edit")), None)
    return render_template(
        "admin_floorplan.html",
        inventory=filtered[(page - 1) * per_page:page * per_page],
        per_page=per_page, page_sizes=page_sizes,
        inventory_count=len(filtered), page=page, page_count=page_count,
        query=query, inventory_filter=inventory_filter, selected=selected,
        image_url=url_for("static", filename=f"uploads/{image}") if image else None,
        tables=tables,
        tariffs=event_tariffs(db),
        event=active_event(db),
    )


@app.route("/admin/api/set-position", methods=["POST"])
@login_required
def admin_set_position():
    data = json_object()
    number = positive_id(data, "number")
    x = data.get("x")
    y = data.get("y")
    if any(type(value) not in (int, float) or not 0 <= value <= 100 for value in (x, y)):
        raise BadRequest("Positionen müssen Zahlen zwischen 0 und 100 sein.")

    db = get_db()
    db.execute(
        "UPDATE tables SET pos_x=?, pos_y=? WHERE number=? AND event_id=(SELECT id FROM events WHERE archived_at IS NULL)",
        (x, y, number),
    )
    db.commit()
    return jsonify({"ok": True})


@app.route("/admin/api/clear-position", methods=["POST"])
@login_required
def admin_clear_position():
    data = json_object()
    number = positive_id(data, "number")

    db = get_db()
    db.execute(
        "UPDATE tables SET pos_x=NULL, pos_y=NULL WHERE number=? AND event_id=(SELECT id FROM events WHERE archived_at IS NULL)",
        (number,),
    )
    db.commit()
    return jsonify({"ok": True})


# Start only after all functions and routes have been defined.
if os.environ.get("DISABLE_BACKGROUND_TASKS", "false").lower() != "true":
    threading.Thread(target=_background_loop, daemon=True).start()


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
