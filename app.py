import hmac
import io
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
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
# Konfiguration – hier bzw. per .env anpassen
# ---------------------------------------------------------------------------
NUM_TABLES = int(os.environ.get("NUM_TABLES", 30))
PRICE_STANDARD = float(os.environ.get("PRICE_STANDARD", os.environ.get("PRICE", 15.00)))
PRICE_INTERNAL = float(os.environ.get("PRICE_INTERNAL", PRICE_STANDARD))
CURRENCY = os.environ.get("CURRENCY", "EUR")

PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET", "")
PAYPAL_MODE = os.environ.get("PAYPAL_MODE", "sandbox")  # "sandbox" oder "live"

PAYPAL_API_BASE = (
    "https://api-m.sandbox.paypal.com"
    if PAYPAL_MODE == "sandbox"
    else "https://api-m.paypal.com"
)

HOLD_MINUTES = 10  # wie lange ein ausgewählter Tisch für die Bezahlung reserviert bleibt
DB_PATH = os.environ.get("DB_PATH", "flohmarkt.db")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "bitte-in-.env-aendern")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY

# Hinter einem Reverse Proxy (Envoy Gateway o. ä.) – für korrekte Client-IPs
# bei Rate-Limiting und Logging.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Hinter HTTPS (Standardfall im Cluster) auf True lassen; für lokales
    # Testen ohne HTTPS über SESSION_COOKIE_SECURE=false in .env abschaltbar.
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "true").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,  # 8 MB – begrenzt u. a. Lageplan-Uploads
    WTF_CSRF_TIME_LIMIT=None,  # Token soll nicht mitten in einer Admin-Session ablaufen
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
        "script-src 'self' https://www.paypal.com https://www.paypalobjects.com; "
        "frame-src https://www.paypal.com; "
        "connect-src 'self' https://www.paypal.com; "
        "img-src 'self' https://www.paypalobjects.com data:; "
        "style-src 'self' 'unsafe-inline'"
    )
    return response


# ---------------------------------------------------------------------------
# Datenbank
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
            pos_x REAL,  -- Position auf dem Lageplan in % (0-100), NULL = kein Lageplan-Marker
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
            voucher_code TEXT
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
    # Migration für bereits existierende Datenbanken mit älterem Schema
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

    existing = db.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
    if existing == 0:
        for i in range(1, NUM_TABLES + 1):
            db.execute("INSERT INTO tables (number, status) VALUES (?, 'free')", (i,))
    db.commit()
    db.close()


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
    """Prüft anhand des tatsächlichen Dateiinhalts (nicht nur der Endung), ob es
    sich um ein echtes, decodierbares Bild handelt."""
    try:
        img = Image.open(io.BytesIO(file_bytes))
        img.verify()
        return True
    except (UnidentifiedImageError, Exception):
        return False


def reserve_voucher(db, code):
    """Prüft einen Gutscheincode und reserviert eine Nutzung (atomar genug für diese Größenordnung).
    Gibt (voucher_row, error_message) zurück – bei Erfolg ist error_message None."""
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
    """Gibt eine reservierte/eingelöste Nutzung eines Gutscheincodes wieder frei
    (bei abgelaufener Reservierung oder Stornierung)."""
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
    """Gibt Tische und ggf. reservierte Gutscheincodes frei, deren Reservierung
    abgelaufen ist, ohne dass bezahlt wurde."""
    cutoff = (datetime.utcnow() - timedelta(minutes=HOLD_MINUTES)).isoformat()

    expired = db.execute(
        "SELECT id, voucher_code FROM registrations WHERE status='pending' AND created_at < ?",
        (cutoff,),
    ).fetchall()
    for reg in expired:
        release_voucher(db, reg["voucher_code"])

    db.execute(
        "UPDATE registrations SET status='cancelled' WHERE status='pending' AND created_at < ?",
        (cutoff,),
    )
    db.execute(
        """
        UPDATE tables SET status='free', held_at=NULL, registration_id=NULL
        WHERE status='held' AND held_at < ?
        """,
        (cutoff,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# PayPal Hilfsfunktionen (serverseitig, Orders API v2)
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


# ---------------------------------------------------------------------------
# Routen
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template(
        "index.html",
        price_standard=PRICE_STANDARD,
        price_internal=PRICE_INTERNAL,
        currency=CURRENCY,
        paypal_client_id=PAYPAL_CLIENT_ID,
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
    """Prüft einen Gutscheincode, OHNE ihn zu reservieren – für die Live-Anzeige
    im Formular, bevor tatsächlich registriert wird."""
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

    now = datetime.utcnow().isoformat()
    cur = db.execute(
        "INSERT INTO registrations (name, email, phone, table_id, status, created_at, price, voucher_code) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?, ?)",
        (name, email, phone, table["id"], now, price, voucher_code),
    )
    registration_id = cur.lastrowid

    db.execute(
        "UPDATE tables SET status='held', held_at=?, registration_id=? WHERE id=?",
        (now, registration_id, table["id"]),
    )
    db.commit()

    return jsonify(
        {
            "registration_id": registration_id,
            "table": table_number,
            "price": price,
            "voucher_applied": voucher_code is not None,
        }
    )


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
        db.execute("UPDATE registrations SET status='paid' WHERE id=?", (reg["id"],))
        db.execute("UPDATE tables SET status='booked' WHERE id=?", (reg["table_id"],))
        db.commit()
        return jsonify({"status": "paid"})

    return jsonify({"error": "Zahlung nicht abgeschlossen.", "paypal_status": status}), 402


# ---------------------------------------------------------------------------
# Admin-Bereich
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
               t.number AS table_number
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
    return render_template(
        "admin_dashboard.html",
        registrations=rows,
        stats=stats,
        num_tables=NUM_TABLES,
        currency=CURRENCY,
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
                code = f"{prefix}-{secrets.token_hex(4).upper()}"
                db.execute(
                    "INSERT INTO vouchers (code, max_uses, used_count, active, created_at) "
                    "VALUES (?, 1, 0, 1, ?)",
                    (code, datetime.utcnow().isoformat()),
                )
                created.append(code)
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
    init_db()
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug_mode, host="0.0.0.0", port=5000)
