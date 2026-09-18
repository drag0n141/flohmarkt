from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import threading
from unittest.mock import Mock

import pytest
from test_booking import mod, client_for, register, connect, get_reg


def admin(mod):
    client, headers = client_for(mod)
    with client.session_transaction() as session:
        session["is_admin"] = True
    return client, headers


def edit(client, headers, reg_id=1, version=0, **data):
    return client.post(
        f"/admin/registrations/{reg_id}/edit", headers=headers, data=dict(version=version, **data)
    )


def test_edit_page_and_contact_update_refresh_unsent_email(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    page = client.get("/admin/registrations/1/edit")
    assert page.status_code == 200
    assert "Buchungsdaten speichern" in page.text
    assert "Frist verlängern" in page.text
    assert "Tischwechsel speichern" in page.text
    assert (
        edit(
            client,
            headers,
            action="contact",
            name="Correct Name",
            email="correct@example.org",
            phone="012345",
        ).status_code
        == 302
    )
    reg = get_reg(mod)
    assert (reg["name"], reg["email"], reg["phone"]) == (
        "Correct Name",
        "correct@example.org",
        "012345",
    )
    assert reg["edit_version"] == 1
    with connect(mod) as db:
        mail = db.execute("SELECT * FROM email_outbox").fetchone()
        assert mail["recipient"] == "correct@example.org"
        assert "Correct Name" in mail["body"]
    assert (
        edit(client, headers, action="contact", name="Stale", email="stale@example.org").status_code
        == 400
    )
    assert get_reg(mod)["email"] == "correct@example.org"


def test_sent_mail_is_not_modified_or_resent(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute("UPDATE email_outbox SET sent_at='2026-01-01'")
    assert (
        edit(client, headers, action="contact", name="New", email="new@example.org").status_code
        == 302
    )
    with connect(mod) as db:
        mails = db.execute("SELECT * FROM email_outbox").fetchall()
        assert len(mails) == 1 and mails[0]["sent_at"]
        assert mails[0]["recipient"] == "person@example.org"


def test_inflight_email_prevents_edit(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute("UPDATE email_outbox SET lease_token='worker',locked_until=9999999999")
    assert (
        edit(client, headers, action="contact", name="New", email="new@example.org").status_code
        == 400
    )
    assert get_reg(mod)["email"] == "person@example.org"


@pytest.mark.parametrize("method", ["sepa", "paypal"])
def test_extension_used_by_expiry_and_payment(mod, monkeypatch, method):
    client, headers = admin(mod)
    register(client, headers, payment_method=method)
    old = get_reg(mod)
    old_deadline = mod.deadline_for(old)
    assert edit(client, headers, action="extend", hours="24").status_code == 302
    reg = get_reg(mod)
    assert mod.deadline_for(reg) == old_deadline + timedelta(hours=24)
    assert reg["created_at"] == old["created_at"]
    monkeypatch.setattr(mod, "utcnow", lambda: old_deadline + timedelta(hours=1))
    with connect(mod) as db:
        mod.release_stale_holds(db)
        assert get_reg(mod)["status"] == "pending"
        assert mod.finalize_paid_registration(db, 1) == "booked"


def test_extension_postpones_unsent_reminder(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() + timedelta(hours=23)).isoformat(),),
        )
        db.execute("UPDATE registrations SET created_at=datetime(expires_at, '-48 hours')")
        db.commit()
        mod.send_sepa_reminders(db)
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0] == 1
        )
    assert edit(client, headers, action="extend", hours="24").status_code == 302
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0] == 0
        )
        reg = get_reg(mod)
        assert (
            mod.display_deadline(reg)
            in db.execute("SELECT body FROM email_outbox WHERE kind='sepa'").fetchone()[0]
        )


def test_extended_short_sepa_hold_gets_reminder(mod, monkeypatch):
    with connect(mod) as db:
        db.execute("UPDATE events SET sepa_hold_hours=12 WHERE archived_at IS NULL")
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    assert edit(client, headers, action="extend", hours="24").status_code == 302
    deadline = mod.deadline_for(get_reg(mod))
    monkeypatch.setattr(mod, "utcnow", lambda: deadline - timedelta(hours=23))
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0] == 1
        )


@pytest.mark.parametrize("paid", [False, True])
def test_move_preserves_payment_reference_price_and_deadline(mod, paid):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    if paid:
        with connect(mod) as db:
            mod.finalize_paid_registration(db, 1)
    before = get_reg(mod)
    assert edit(client, headers, action="move", table="2").status_code == 302
    after = get_reg(mod)
    assert after["table_id"] == 2
    for key in [
        "price",
        "voucher_code",
        "payment_reference",
        "created_at",
        "expires_at",
        "payment_received_at",
        "status",
    ]:
        assert after[key] == before[key]
    assert after["payment_reference"] == "FLOHMARKT-1"
    with connect(mod) as db:
        source = db.execute("SELECT * FROM tables WHERE id=1").fetchone()
        target = db.execute("SELECT * FROM tables WHERE id=2").fetchone()
        assert source["status"] == "free" and source["registration_id"] is None
        assert target["status"] == ("booked" if paid else "held") and target["registration_id"] == 1
        kind = "confirmation" if paid else "sepa"
        mail = db.execute("SELECT body FROM email_outbox WHERE kind=?", (kind,)).fetchone()[0]
        assert "Tisch: 2" in mail
        if not paid:
            assert "FLOHMARKT-1" in mail


def test_competing_moves_cannot_take_same_table(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    register(client, headers, table=2, payment_method="sepa")
    cookie = client.get_cookie("session").value
    barrier = threading.Barrier(2)

    def move(reg_id):
        c = mod.app.test_client()
        c.set_cookie("session", cookie)
        barrier.wait()
        return edit(c, headers, reg_id=reg_id, action="move", table="3").status_code

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(move, [1, 2])) == [302, 400]
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM tables WHERE status='held'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM registrations WHERE table_id=3").fetchone()[0] == 1


def test_expired_and_paid_reservations_cannot_be_extended(mod):
    client, headers = admin(mod)
    register(client, headers)
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() - timedelta(hours=1)).isoformat(),),
        )
    assert edit(client, headers, action="extend", hours="24").status_code == 400
    assert get_reg(mod)["status"] == "cancelled"
    assert edit(client, headers, action="move", table="2").status_code == 400
    new_id = register(client, headers).json["registration_id"]
    with connect(mod) as db:
        mod.finalize_paid_registration(db, new_id)
    assert edit(client, headers, reg_id=new_id, action="extend", hours="24").status_code == 400


def test_permissions_csrf_and_validation(mod):
    client, headers = client_for(mod)
    register(client, headers)
    assert edit(client, headers, action="extend", hours="24").location.endswith("/admin/login")
    with client.session_transaction() as session:
        session["is_admin"] = True
    assert edit(client, {}, action="extend", hours="24").status_code == 400
    for hours in ["0", "-1", "1.5", "721", "bad"]:
        assert edit(client, headers, action="extend", hours=hours).status_code == 400
    assert edit(client, headers, action="contact", name="New", email="bad").status_code == 400
    assert get_reg(mod)["edit_version"] == 0


def test_migration_defaults_preserve_derived_deadline(mod):
    client, headers = admin(mod)
    register(client, headers)
    old_deadline = mod.deadline_for(get_reg(mod))
    mod.init_db()
    assert get_reg(mod)["expires_at"] == old_deadline.isoformat()
    assert mod.deadline_for(get_reg(mod)) == old_deadline


def test_long_extension_does_not_retry_expired_paypal_creation_key(mod, monkeypatch):
    client, headers = admin(mod)
    register(client, headers)
    assert edit(client, headers, action="extend", hours="24").status_code == 302
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET create_attempted_at=?",
            ((mod.utcnow() - timedelta(hours=6)).isoformat(),),
        )
    create = Mock()
    monkeypatch.setattr(mod, "paypal_create_order", create)
    response = client.post("/api/create-order", headers=headers, json={"registration_id": 1})
    assert response.status_code == 409
    assert response.json["status"] == "payment_review"
    create.assert_not_called()


def test_failed_email_refresh_rolls_back_table_move(mod, monkeypatch):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    monkeypatch.setattr(
        mod, "refresh_pending_emails", Mock(side_effect=RuntimeError("render failure"))
    )
    with pytest.raises(RuntimeError):
        edit(client, headers, action="move", table="2")
    assert get_reg(mod)["table_id"] == 1
    assert get_reg(mod)["edit_version"] == 0
    with connect(mod) as db:
        assert db.execute("SELECT status FROM tables WHERE id=2").fetchone()[0] == "free"


def test_postponed_reminder_reappears_at_new_deadline(mod, monkeypatch):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() + timedelta(hours=23)).isoformat(),),
        )
        db.execute("UPDATE registrations SET created_at=datetime(expires_at, '-48 hours')")
        db.commit()
        mod.send_sepa_reminders(db)
    assert edit(client, headers, action="extend", hours="24").status_code == 302
    deadline = mod.deadline_for(get_reg(mod))
    monkeypatch.setattr(mod, "utcnow", lambda: deadline - timedelta(hours=23))
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        rows = db.execute("SELECT * FROM email_outbox WHERE kind='reminder'").fetchall()
        assert len(rows) == 1
        assert mod.display_deadline(get_reg(mod)) in rows[0]["body"]


def cancel(client, headers, reg_id=1):
    return client.post(f"/admin/cancel/{reg_id}", headers=headers)


def table_row(mod, number):
    with connect(mod) as db:
        return db.execute("SELECT * FROM tables WHERE number=?", (number,)).fetchone()


def test_history_offers_restore_and_restores_open_booking(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method="sepa")
    assert cancel(client, headers).status_code == 302
    history = client.get("/admin?view=history").text
    assert "Tisch zuweisen" in history
    page = client.get("/admin/registrations/1/edit").text
    assert "Freien Tisch zuweisen" in page
    assert "Tischwechsel speichern" not in page

    assert edit(client, headers, version=get_reg(mod)["edit_version"], action="restore", table="5").status_code == 302
    reg = get_reg(mod)
    assert reg["status"] == "pending"
    assert reg["table_id"] == table_row(mod, 5)["id"]
    assert datetime.fromisoformat(reg["expires_at"]) > mod.utcnow()
    assert reg["payment_reference"] == "FLOHMARKT-1"
    assert reg["edit_version"] == 1
    assert table_row(mod, 5)["status"] == "held"
    assert table_row(mod, 5)["registration_id"] == 1
    assert table_row(mod, 1)["status"] == "free"
    assert "Tisch 5" in client.get("/admin").text


def test_restore_with_recorded_payment_books_table_and_clears_review(mod):
    client, headers = admin(mod)
    register(client, headers, table=2, payment_method="sepa")
    assert client.post("/admin/confirm-sepa/1", headers=headers).status_code == 302
    assert get_reg(mod)["status"] == "paid"
    assert cancel(client, headers).status_code == 302
    reg = get_reg(mod)
    assert reg["status"] == "cancelled" and reg["payment_review"] == 1

    assert edit(client, headers, version=get_reg(mod)["edit_version"], action="restore", table="2").status_code == 302
    reg = get_reg(mod)
    assert reg["status"] == "paid"
    assert reg["payment_review"] == 0
    assert table_row(mod, 2)["status"] == "booked"
    assert table_row(mod, 2)["registration_id"] == 1


def test_restore_rejects_occupied_table_and_active_booking(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method="sepa")
    assert cancel(client, headers).status_code == 302
    other, other_headers = client_for(mod)
    register(other, other_headers, table=3, payment_method="sepa")
    resp = edit(client, headers, version=get_reg(mod)["edit_version"], action="restore", table="3")
    assert resp.status_code == 400
    assert "nicht mehr frei" in resp.text
    assert get_reg(mod)["status"] == "cancelled"
    assert table_row(mod, 1)["status"] == "free"
    # An active booking cannot be "restored".
    resp = edit(client, headers, reg_id=2, version=0, action="restore", table="4")
    assert resp.status_code == 400
    assert "Nur stornierte" in resp.text
    assert table_row(mod, 4)["status"] == "free"


def test_restore_reserves_voucher_again(mod):
    client, headers = admin(mod)
    with connect(mod) as db:
        tariff = db.execute("SELECT id FROM tariffs WHERE name='Intern'").fetchone()["id"]
        db.execute(
            "INSERT INTO vouchers(code,max_uses,used_count,active,created_at,tariff_id) VALUES ('ONCE',1,0,1,?,?)",
            (mod.utcnow().isoformat(), tariff),
        )
    register(client, headers, table=1, payment_method="sepa", voucher="ONCE")
    assert get_reg(mod)["voucher_code"] == "ONCE"
    assert cancel(client, headers).status_code == 302
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 0
    assert edit(client, headers, version=get_reg(mod)["edit_version"], action="restore", table="6").status_code == 302
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 1
    assert get_reg(mod)["status"] == "pending"


def test_restore_can_record_late_bank_transfer_in_one_step(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method="sepa")
    assert cancel(client, headers).status_code == 302
    assert get_reg(mod)["payment_received_at"] is None
    page = client.get("/admin/registrations/1/edit").text
    assert 'name="payment_received"' in page and "checked disabled" not in page
    assert (
        edit(
            client,
            headers,
            version=get_reg(mod)["edit_version"],
            action="restore",
            table="7",
            payment_received="yes",
        ).status_code
        == 302
    )
    reg = get_reg(mod)
    assert reg["status"] == "paid"
    assert reg["payment_received_at"]
    assert reg["expires_at"] is None
    assert table_row(mod, 7)["status"] == "booked"
    assert table_row(mod, 7)["registration_id"] == 1
