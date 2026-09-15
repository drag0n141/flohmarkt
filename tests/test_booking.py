import importlib
import os
import re
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def mod(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-with-at-least-32-characters")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-admin")
    monkeypatch.setenv("DISABLE_BACKGROUND_TASKS", "true")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bookings.db"))
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("NUM_TABLES", "30")
    monkeypatch.setenv("PRICE_STANDARD", "15")
    monkeypatch.setenv("PRICE_INTERNAL", "15")
    monkeypatch.setenv("SEPA_HOLD_HOURS", "48")
    app = importlib.import_module("app")
    monkeypatch.setattr(app, "DB_PATH", str(tmp_path / "bookings.db"))
    monkeypatch.setattr(app, "SMTP_HOST", "")
    monkeypatch.setattr(app, "ENABLED_PAYMENT_METHODS", ["paypal", "sepa"])
    monkeypatch.setattr(app, "ADMIN_PASSWORD", "test-admin")
    app.app.config.update(TESTING=True, SESSION_COOKIE_SECURE=False, WTF_CSRF_ENABLED=True)
    app.limiter.enabled = False
    app.init_db()
    return app


def client_for(mod):
    client = mod.app.test_client()
    page = client.get("/").text
    token = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return client, {"X-CSRFToken": token}


def register(client, headers, table=1, **extra):
    return client.post(
        "/api/register",
        headers=headers,
        json=dict(
            {"name": "Example Person", "email": "person@example.org", "phone": "", "table": table},
            **extra,
        ),
    )


def connect(mod):
    db = sqlite3.connect(mod.DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def get_reg(mod, registration_id=1):
    with connect(mod) as db:
        return db.execute("SELECT * FROM registrations WHERE id=?", (registration_id,)).fetchone()


def order_for(reg, status="COMPLETED"):
    return {
        "id": reg["paypal_order_id"],
        "status": status,
        "purchase_units": [
            {
                "reference_id": str(reg["id"]),
                "payments": {
                    "captures": [
                        {
                            "id": f'CAPTURE{reg["id"]}',
                            "status": "COMPLETED",
                            "amount": {"value": f'{reg["price"]:.2f}', "currency_code": "EUR"},
                        }
                    ]
                },
            }
        ],
    }


def prepare_order(mod, monkeypatch):
    client, headers = client_for(mod)
    reg_id = register(client, headers).json["registration_id"]
    monkeypatch.setattr(mod, "paypal_create_order", Mock(return_value={"id": "ORDER123"}))
    assert (
        client.post(
            "/api/create-order", headers=headers, json={"registration_id": reg_id}
        ).status_code
        == 200
    )
    return client, headers, get_reg(mod, reg_id)


def test_parallel_reservation_has_one_winner(mod):
    clients = [client_for(mod) for _ in range(2)]
    barrier = threading.Barrier(2)

    def reserve(pair):
        barrier.wait()
        return register(*pair).status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve, clients)) == [200, 409]
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 1
        assert db.execute("SELECT registration_id FROM tables WHERE number=1").fetchone()[0] == 1


def test_parallel_voucher_is_used_once(mod):
    with connect(mod) as db:
        db.execute(
            "INSERT INTO vouchers(code,max_uses,created_at,tariff_id) VALUES('SINGLE',1,'2026-01-01',2)"
        )
    clients = [client_for(mod) for _ in range(2)]
    barrier = threading.Barrier(2)

    def reserve(i):
        barrier.wait()
        return register(*clients[i], table=i + 1, voucher="single").status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [200, 400]
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 1


def test_registration_rolls_back_voucher_and_table_if_queue_fails(mod, monkeypatch):
    client, headers = client_for(mod)
    with connect(mod) as db:
        db.execute(
            "INSERT INTO vouchers(code,max_uses,created_at,tariff_id) VALUES('SINGLE',1,'2026-01-01',2)"
        )
    monkeypatch.setattr(mod, "queue_email", Mock(side_effect=RuntimeError("queue failure")))
    with pytest.raises(RuntimeError):
        register(client, headers, voucher="SINGLE", payment_method="sepa")
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0
        assert db.execute("SELECT status FROM tables WHERE number=1").fetchone()[0] == "free"


def test_parallel_payment_is_idempotent(mod):
    client, headers = client_for(mod)
    reg_id = register(client, headers).json["registration_id"]
    barrier = threading.Barrier(2)

    def pay(_):
        with connect(mod) as db:
            barrier.wait()
            return mod.finalize_paid_registration(db, reg_id)

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(pay, range(2))) == ["already_booked", "booked"]
    with connect(mod) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='confirmation'").fetchone()[0]
            == 1
        )


def test_late_payment_does_not_touch_reassigned_table(mod):
    client, headers = client_for(mod)
    old_id = register(client, headers).json["registration_id"]
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=? WHERE id=?",
            ((mod.utcnow() - timedelta(hours=1)).isoformat(), old_id),
        )
    new_id = register(client, headers).json["registration_id"]
    with connect(mod) as db:
        assert mod.finalize_paid_registration(db, old_id) == "payment_received_unallocated"
        old = db.execute("SELECT * FROM registrations WHERE id=?", (old_id,)).fetchone()
        table = db.execute("SELECT * FROM tables WHERE number=1").fetchone()
        assert old["status"] == "cancelled"
        assert old["payment_received_at"] and old["payment_review"] == 1
        assert (table["status"], table["registration_id"]) == ("held", new_id)
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0


def test_cancellation_and_expiry_only_release_once(mod):
    client, headers = client_for(mod)
    with connect(mod) as db:
        db.execute(
            "INSERT INTO vouchers(code,max_uses,created_at,tariff_id) VALUES('CODE',2,'2026-01-01',2)"
        )
    reg_id = register(client, headers, voucher="CODE").json["registration_id"]
    register(client, headers, table=2, voucher="CODE")
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=? WHERE id=?",
            ((mod.utcnow() - timedelta(hours=1)).isoformat(), reg_id),
        )
    barrier = threading.Barrier(2)

    def expire(_):
        with connect(mod) as db:
            barrier.wait()
            mod.release_stale_holds(db)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(expire, range(2)))
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 1
        assert db.execute("SELECT status FROM tables WHERE number=2").fetchone()[0] == "held"


def test_order_ownership_and_reuse(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    other, other_headers = client_for(mod)
    for path, payload in [
        ("/api/create-order", {"registration_id": reg["id"]}),
        ("/api/capture-order", {"order_id": reg["paypal_order_id"]}),
    ]:
        assert other.post(path, headers=other_headers, json=payload).status_code == 404
    response = client.post(
        "/api/create-order", headers=headers, json={"registration_id": reg["id"]}
    )
    assert response.json == {"order_id": "ORDER123"}
    mod.paypal_create_order.assert_called_once()


def test_order_create_timeout_reuses_persisted_request_id(mod, monkeypatch):
    client, headers = client_for(mod)
    reg_id = register(client, headers).json["registration_id"]
    create = Mock(side_effect=[requests.Timeout(), {"id": "ORDER123"}])
    monkeypatch.setattr(mod, "paypal_create_order", create)
    for expected in (503, 200):
        assert (
            client.post(
                "/api/create-order", headers=headers, json={"registration_id": reg_id}
            ).status_code
            == expected
        )
    assert create.call_args_list[0].args == create.call_args_list[1].args


def test_capture_timeout_reconciles_without_second_capture(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    monkeypatch.setattr(
        mod,
        "paypal_get_order",
        Mock(side_effect=[order_for(reg, "APPROVED"), order_for(reg), order_for(reg)]),
    )
    capture = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(mod, "paypal_capture_order", capture)
    for _ in range(2):
        response = client.post(
            "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
        )
        assert response.status_code == 200
        assert response.json["status"] == "paid"
    capture.assert_called_once_with(reg["paypal_order_id"], reg["capture_request_id"])
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 1


def test_completed_capture_after_cancellation_returns_conflict(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    with connect(mod) as db, mod.write_transaction(db):
        mod.cancel_registration_locked(db, reg)
    monkeypatch.setattr(mod, "paypal_get_order", Mock(return_value=order_for(reg)))
    response = client.post(
        "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
    )
    assert response.status_code == 409
    assert response.json["status"] == "payment_received_unallocated"
    assert get_reg(mod)["status"] == "cancelled"


def test_expired_order_is_not_captured(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() - timedelta(hours=1)).isoformat(),),
        )
    monkeypatch.setattr(mod, "paypal_get_order", Mock(return_value=order_for(reg, "APPROVED")))
    capture = Mock()
    monkeypatch.setattr(mod, "paypal_capture_order", capture)
    assert (
        client.post(
            "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
        ).status_code
        == 409
    )
    capture.assert_not_called()


@pytest.mark.parametrize("change", ["amount", "currency", "reference"])
def test_invalid_payment_is_flagged_and_not_booked(mod, monkeypatch, change):
    client, headers, reg = prepare_order(mod, monkeypatch)
    order = order_for(reg)
    unit = order["purchase_units"][0]
    if change == "reference":
        unit["reference_id"] = "999"
    else:
        unit["payments"]["captures"][0]["amount"][
            "value" if change == "amount" else "currency_code"
        ] = ("0.01" if change == "amount" else "USD")
    monkeypatch.setattr(mod, "paypal_get_order", Mock(return_value=order))
    response = client.post(
        "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
    )
    assert response.status_code == 409
    assert response.json["status"] == "payment_review"
    assert get_reg(mod)["status"] == "pending"
    assert get_reg(mod)["payment_review"] == 1


def test_webhook_replay_and_browser_capture_share_one_confirmation(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    monkeypatch.setattr(mod, "paypal_verify_webhook_signature", Mock(return_value=True))
    monkeypatch.setattr(mod, "paypal_get_order", Mock(return_value=order_for(reg)))
    event = {
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"supplementary_data": {"related_ids": {"order_id": reg["paypal_order_id"]}}},
    }
    for _ in range(2):
        assert mod.app.test_client().post("/webhooks/paypal", json=event).status_code == 200
    assert (
        client.post(
            "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
        ).status_code
        == 200
    )
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 1


def test_invalid_webhook_signature_rejected(mod, monkeypatch):
    monkeypatch.setattr(mod, "paypal_verify_webhook_signature", Mock(return_value=False))
    assert mod.app.test_client().post("/webhooks/paypal", json={}).status_code == 400


def test_csrf_and_content_type_required(mod):
    client, headers = client_for(mod)
    assert register(client, {}).status_code == 400
    response = client.post(
        "/api/register", headers=headers, data='{"table":1}', content_type="text/plain"
    )
    assert response.status_code == 415
    assert client.post("/api/register", headers=headers, json=[]).status_code == 400
    assert register(client, headers, table=True).status_code == 400
    assert register(client, headers, phone=["invalid"]).status_code == 400


def test_login_ignores_external_redirect(mod):
    client, headers = client_for(mod)
    response = client.post(
        "/admin/login?next=https://example.org/phishing",
        headers=headers,
        data={"password": "test-admin"},
    )
    assert response.status_code == 302
    assert response.location == "/admin"
    assert client.get("/admin").status_code == 200


def test_sepa_hold_and_table_reference_remain_unchanged(mod):
    client, headers = client_for(mod)
    first = register(client, headers, payment_method="sepa").json
    reg = get_reg(mod, first["registration_id"])
    assert mod.deadline_for(reg) - datetime.fromisoformat(reg["created_at"]) == timedelta(hours=48)
    with connect(mod) as db, mod.write_transaction(db):
        mod.cancel_registration_locked(db, reg)
    second = register(client, headers, payment_method="sepa").json
    assert first["reference"] == second["reference"] == "FLOHMARKT-1"
    with connect(mod) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE cancelled_at IS NULL").fetchone()[0]
            == 1
        )


def test_deadline_uses_berlin_timezone(mod):
    for created, expected in [
        ("2026-07-01T10:00:00", "03.07.2026 um 12:00 Uhr"),
        ("2026-01-01T10:00:00", "03.01.2026 um 11:00 Uhr"),
    ]:
        assert mod.display_deadline({"created_at": created, "payment_method": "sepa"}) == expected


def test_outbox_retries_and_marks_only_after_success(mod, monkeypatch):
    client, headers = client_for(mod)
    register(client, headers, payment_method="sepa")
    monkeypatch.setattr(mod, "SMTP_HOST", "smtp.example.invalid")
    send = Mock(side_effect=[OSError("SMTP unavailable"), None])
    monkeypatch.setattr(mod, "deliver_email", send)
    with connect(mod) as db:
        mod.process_email_outbox(db)
        row = db.execute("SELECT * FROM email_outbox").fetchone()
        assert row["sent_at"] is None and row["last_error"] == "OSError"
        mod.process_email_outbox(db)
        assert send.call_count == 1
        db.execute("UPDATE email_outbox SET next_attempt_at=0")
        db.commit()
        mod.process_email_outbox(db)
        assert db.execute("SELECT sent_at FROM email_outbox").fetchone()[0]
        mod.process_email_outbox(db)
        assert send.call_count == 2


def test_parallel_outbox_workers_send_once_without_holding_db_lock(mod, monkeypatch):
    client, headers = client_for(mod)
    register(client, headers, payment_method="sepa")
    monkeypatch.setattr(mod, "SMTP_HOST", "smtp.example.invalid")
    started, finish = threading.Event(), threading.Event()

    def deliver(row):
        started.set()
        assert finish.wait(5)

    send = Mock(side_effect=deliver)
    monkeypatch.setattr(mod, "deliver_email", send)

    def process():
        with connect(mod) as db:
            mod.process_email_outbox(db)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(process)
        assert started.wait(5)
        second = pool.submit(process)
        second.result(timeout=5)
        # A separate write must succeed while SMTP is blocked.
        with connect(mod) as db, mod.write_transaction(db):
            db.execute("INSERT INTO settings VALUES('concurrent','ok')")
        finish.set()
        first.result(timeout=5)
    send.assert_called_once()


def test_reminder_retry_does_not_set_sent_flag_early(mod, monkeypatch):
    client, headers = client_for(mod)
    reg_id = register(client, headers, payment_method="sepa").json["registration_id"]
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() + timedelta(hours=23)).isoformat(),),
        )
        db.execute("UPDATE registrations SET created_at=datetime(expires_at, '-48 hours')")
        db.execute("UPDATE email_outbox SET sent_at='already delivered'")
        db.commit()
        mod.send_sepa_reminders(db)
        mod.send_sepa_reminders(db)
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0] == 1
        )
        assert get_reg(mod, reg_id)["reminder_sent"] == 0
        monkeypatch.setattr(mod, "SMTP_HOST", "smtp.example.invalid")
        monkeypatch.setattr(mod, "deliver_email", Mock(side_effect=OSError()))
        mod.process_email_outbox(db)
        assert get_reg(mod, reg_id)["reminder_sent"] == 0
        db.execute("UPDATE email_outbox SET next_attempt_at=0")
        db.commit()
        monkeypatch.setattr(mod, "deliver_email", Mock())
        mod.process_email_outbox(db)
        assert get_reg(mod, reg_id)["reminder_sent"] == 1


def test_expired_reminders_are_not_queued(mod):
    client, headers = client_for(mod)
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() - timedelta(hours=49)).isoformat(),),
        )
        db.commit()
        mod.send_sepa_reminders(db)
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0] == 0
        )


def test_migration_preserves_legacy_reference(mod, tmp_path, monkeypatch):
    legacy_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(legacy_path) as db:
        db.executescript("""
            CREATE TABLE tables(id INTEGER PRIMARY KEY, number INTEGER UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'free',held_at TEXT,registration_id INTEGER);
            CREATE TABLE registrations(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,
                phone TEXT,table_id INTEGER NOT NULL,paypal_order_id TEXT,status TEXT NOT NULL,
                created_at TEXT NOT NULL,price REAL,voucher_code TEXT,payment_method TEXT NOT NULL);
            INSERT INTO tables VALUES(1,1,'held','2026-09-12',7);
            INSERT INTO registrations VALUES(7,'Legacy','legacy@example.org',NULL,1,NULL,'pending',
                '2026-09-12T10:00:00',15,NULL,'sepa');
        """)
    monkeypatch.setattr(mod, "DB_PATH", legacy_path)
    mod.init_db()
    mod.init_db()
    reg = get_reg(mod, 7)
    assert reg["payment_reference"] == "FLOHMARKT-1"
    assert reg["owner_id"] is None
    assert reg["status"] == "pending"
    with connect(mod) as db:
        assert db.execute("SELECT registration_id FROM tables WHERE id=1").fetchone()[0] == 7


@pytest.mark.parametrize("secret", ["", "a-random-long-string", "please-change-in-.env"])
def test_unsafe_secret_fails_startup(mod, secret):
    env = dict(os.environ, SECRET_KEY=secret)
    result = subprocess.run(
        [sys.executable, "-c", "import app"], env=env, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "SECRET_KEY must be a private random value" in result.stderr


def test_parallel_create_uses_one_key_and_never_overwrites_mapping(mod, monkeypatch):
    client, headers = client_for(mod)
    reg_id = register(client, headers).json["registration_id"]
    cookie = client.get_cookie("session")
    barrier = threading.Barrier(2)

    def create(amount, reference_id, request_id):
        barrier.wait(timeout=5)
        return {"id": "ORDER123"}

    create_mock = Mock(side_effect=create)
    monkeypatch.setattr(mod, "paypal_create_order", create_mock)

    def request_order(_):
        other = mod.app.test_client()
        other.set_cookie("session", cookie.value)
        return other.post("/api/create-order", headers=headers, json={"registration_id": reg_id})

    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(request_order, range(2)))
    assert all(r.status_code == 200 and r.json["order_id"] == "ORDER123" for r in responses)
    assert create_mock.call_args_list[0].args == create_mock.call_args_list[1].args
    assert get_reg(mod, reg_id)["paypal_order_id"] == "ORDER123"


def test_parallel_webhook_and_capture_queue_one_message(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    barrier = threading.Barrier(2)
    monkeypatch.setattr(mod, "paypal_verify_webhook_signature", Mock(return_value=True))

    def get_order(order_id):
        barrier.wait(timeout=5)
        return order_for(reg)

    monkeypatch.setattr(mod, "paypal_get_order", get_order)
    event = {
        "event_type": "PAYMENT.CAPTURE.COMPLETED",
        "resource": {"supplementary_data": {"related_ids": {"order_id": reg["paypal_order_id"]}}},
    }
    with ThreadPoolExecutor(2) as pool:
        webhook = pool.submit(lambda: mod.app.test_client().post("/webhooks/paypal", json=event))
        capture = pool.submit(
            lambda: client.post(
                "/api/capture-order", headers=headers, json={"order_id": reg["paypal_order_id"]}
            )
        )
        assert webhook.result(timeout=5).status_code == 200
        assert capture.result(timeout=5).status_code == 200
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 1


def test_abandoned_mail_lease_is_recovered(mod, monkeypatch):
    client, headers = client_for(mod)
    register(client, headers, payment_method="sepa")
    monkeypatch.setattr(mod, "SMTP_HOST", "smtp.example.invalid")
    send = Mock()
    monkeypatch.setattr(mod, "deliver_email", send)
    with connect(mod) as db:
        db.execute(
            "UPDATE email_outbox SET lease_token='dead-worker',locked_until=?",
            (mod.time.time() + 300,),
        )
        db.commit()
        mod.process_email_outbox(db)
        send.assert_not_called()
        db.execute("UPDATE email_outbox SET locked_until=0")
        db.commit()
        mod.process_email_outbox(db)
        send.assert_called_once()
        assert db.execute("SELECT sent_at FROM email_outbox").fetchone()[0]


def test_resolved_late_payment_is_not_reopened_by_replay(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    with connect(mod) as db, mod.write_transaction(db):
        mod.cancel_registration_locked(db, reg)
    with connect(mod) as db:
        assert mod.record_completed_order(db, reg, order_for(reg)) == "payment_received_unallocated"
    with client.session_transaction() as session:
        session["is_admin"] = True
    assert client.post(f'/admin/resolve-payment/{reg["id"]}', headers=headers).status_code == 302
    with connect(mod) as db:
        assert mod.record_completed_order(db, reg, order_for(reg)) == "payment_received_unallocated"
    assert get_reg(mod, reg["id"])["payment_review"] == 0


def test_sepa_late_admin_confirmation_is_visible_in_active_view(mod):
    client, headers = client_for(mod)
    reg_id = register(client, headers, payment_method="sepa").json["registration_id"]
    with connect(mod) as db, mod.write_transaction(db):
        mod.cancel_registration_locked(db, get_reg(mod, reg_id))
    with client.session_transaction() as session:
        session["is_admin"] = True
    assert client.post(f"/admin/confirm-sepa/{reg_id}", headers=headers).status_code == 302
    response = client.get("/admin")
    assert "Zahlung prüfen" in response.text
    assert "Example Person" in response.text
    assert get_reg(mod, reg_id)["status"] == "cancelled"


def test_minimal_capture_response_is_reconciled(mod, monkeypatch):
    client, headers, reg = prepare_order(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "paypal_get_order", Mock(side_effect=[order_for(reg, "APPROVED"), order_for(reg)])
    )
    monkeypatch.setattr(
        mod, "paypal_capture_order", Mock(return_value={"id": "ORDER123", "status": "COMPLETED"})
    )
    response = client.post("/api/capture-order", headers=headers, json={"order_id": "ORDER123"})
    assert response.status_code == 200
    assert get_reg(mod)["status"] == "paid"
