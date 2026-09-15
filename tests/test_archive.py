import sqlite3

import pytest
from test_booking import mod, client_for, register, connect, get_reg  # noqa: F401


def admin(mod):
    client, headers = client_for(mod)
    with client.session_transaction() as session:
        session["is_admin"] = True
    return client, headers


def archive(client, headers, keep=(), archive_name="Flohmarkt 2026", new_name="Flohmarkt 2027"):
    return client.post(
        "/admin/event",
        headers=headers,
        data={
            "action": "archive",
            "confirm": "yes",
            "archive_name": archive_name,
            "new_name": new_name,
            "keep": list(keep) + ["tables", "tariffs", "deadlines"],
        },
    )


def events(mod):
    with connect(mod) as db:
        return db.execute("SELECT * FROM events ORDER BY id").fetchall()


def place_floorplan(mod, tmp_path, monkeypatch, name="floorplan.png"):
    """Pretend a plan image was uploaded and two tables were placed on it."""
    monkeypatch.setattr(mod, "UPLOAD_FOLDER", str(tmp_path))
    (tmp_path / name).write_bytes(b"not-a-real-image-but-a-real-file")
    with connect(mod) as db:
        db.execute(
            "INSERT INTO settings (key, value) VALUES ('floorplan_image', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (name,),
        )
        db.execute("UPDATE tables SET pos_x=10.5, pos_y=20.5 WHERE number=1")
        db.execute("UPDATE tables SET pos_x=30.0, pos_y=40.0 WHERE number=2")


def test_archiving_frees_tables_and_keeps_bookings_in_the_archive(mod):
    client, headers = admin(mod)
    reg_id = register(client, headers, table=3, payment_method="sepa").json["registration_id"]
    client.post(f"/admin/confirm-sepa/{reg_id}", headers=headers)
    assert get_reg(mod, reg_id)["status"] == "paid"

    page = client.get("/admin/event")
    assert page.status_code == 200
    assert "Archivieren und neu starten" in page.text
    assert "Noch kein Flohmarkt archiviert." in page.text

    assert archive(client, headers).status_code == 302

    old, new = events(mod)
    assert (old["name"], new["name"]) == ("Flohmarkt 2026", "Flohmarkt 2027")
    assert old["archived_at"] is not None and new["archived_at"] is None
    assert get_reg(mod, reg_id)["event_id"] == old["id"]

    with connect(mod) as db:
        statuses = {r[0] for r in db.execute("SELECT DISTINCT status FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL)")}
    assert statuses == {"free"}

    dashboard = client.get("/admin").text
    assert "Example Person" not in dashboard
    assert "Flohmarkt 2027" in dashboard

    detail = client.get(f"/admin/event/{old['id']}").text
    assert "Example Person" in detail
    assert "Flohmarkt 2026" in detail

    # The freed table can be booked again by someone else.
    assert register(client, headers, table=3).status_code == 200
    assert get_reg(mod, reg_id)["status"] == "paid"


def test_archiving_requires_confirmation_and_a_name(mod):
    client, headers = admin(mod)
    assert (
        client.post(
            "/admin/event",
            headers=headers,
            data={"action": "archive", "archive_name": "Ohne Haken"},
        ).status_code
        == 302
    )
    assert (
        client.post(
            "/admin/event",
            headers=headers,
            data={"action": "archive", "confirm": "yes", "archive_name": "  "},
        ).status_code
        == 302
    )
    assert len(events(mod)) == 1
    assert events(mod)[0]["archived_at"] is None


def test_pending_reservation_and_unsent_mail_are_cancelled_on_archive(mod):
    client, headers = admin(mod)
    reg_id = register(client, headers, payment_method="sepa").json["registration_id"]
    with connect(mod) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM email_outbox WHERE sent_at IS NULL").fetchone()[0] == 1
        )

    archive(client, headers)

    assert get_reg(mod, reg_id)["status"] == "cancelled"
    with connect(mod) as db:
        row = db.execute("SELECT cancelled_at FROM email_outbox").fetchone()
    assert row["cancelled_at"] is not None


@pytest.mark.parametrize("keep", [("floorplan", "positions"), ("floorplan",), ("positions",), ()])
def test_plan_and_table_positions_are_carried_over_independently(mod, tmp_path, monkeypatch, keep):
    client, headers = admin(mod)
    place_floorplan(mod, tmp_path, monkeypatch)
    archive(client, headers, keep=keep)

    with connect(mod) as db:
        image = db.execute("SELECT value FROM settings WHERE key='floorplan_image'").fetchone()
        placed = db.execute(
            "SELECT COUNT(*) FROM tables WHERE event_id=(SELECT id FROM events WHERE archived_at IS NULL) AND pos_x IS NOT NULL AND pos_y IS NOT NULL"
        ).fetchone()[0]

    assert (image is not None) is ("floorplan" in keep)
    assert (tmp_path / "floorplan.png").exists() is ("floorplan" in keep)
    assert placed == (2 if "positions" in keep else 0)

    # The archive keeps its own copy of the plan either way.
    old = events(mod)[0]
    assert (tmp_path / f"floorplan-event{old['id']}.png").exists()
    detail = client.get(f"/admin/event/{old['id']}").text
    assert f"floorplan-event{old['id']}.png" in detail
    assert "10.5%" in detail


def test_content_is_reset_unless_it_is_carried_over(mod):
    client, headers = admin(mod)
    client.post(
        "/admin/page", headers=headers, data={"title": "Herbstflohmarkt", "info": "Halle 3"}
    )
    client.post(
        "/admin/emails",
        headers=headers,
        data={"kind": "confirmation", "subject": "Eigener Betreff", "body": "Eigener Text"},
    )
    client.post(
        "/admin/faq", headers=headers, data={"action": "add", "question": "A", "answer": "B"}
    )

    archive(client, headers, keep=("page",))

    with connect(mod) as db:
        settings = dict(db.execute("SELECT key, value FROM settings").fetchall())
        faq_count = db.execute("SELECT COUNT(*) FROM faq").fetchone()[0]
    assert settings.get("event_title") == "Herbstflohmarkt"
    assert "email_confirmation_subject" not in settings
    assert faq_count == 0

    old = events(mod)[0]
    detail = client.get(f"/admin/event/{old['id']}").text
    assert "Herbstflohmarkt" in detail
    assert "Halle 3" in detail


def test_vouchers_are_reset_or_removed(mod):
    client, headers = admin(mod)
    client.post(
        "/admin/vouchers",
        headers=headers,
        data={"action": "create", "tariff_id": "2", "code": "MG2026", "max_uses": "5"},
    )
    register(client, headers, voucher="MG2026", payment_method="sepa")
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 1

    archive(client, headers, keep=("vouchers",))
    with connect(mod) as db:
        voucher = db.execute("SELECT code, used_count FROM vouchers").fetchone()
    assert (voucher["code"], voucher["used_count"]) == ("MG2026", 0)

    archive(client, headers, archive_name="Zweiter", new_name="Dritter")
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0] == 0


def test_archived_registrations_cannot_be_changed(mod):
    client, headers = admin(mod)
    reg_id = register(client, headers, payment_method="sepa").json["registration_id"]
    client.post(f"/admin/confirm-sepa/{reg_id}", headers=headers)
    archive(client, headers)
    old = events(mod)[0]

    assert (
        client.get(f"/admin/registrations/{reg_id}/edit")
        .headers["Location"]
        .endswith(f"/admin/event/{old['id']}")
    )
    refused = client.post(
        f"/admin/registrations/{reg_id}/edit",
        headers=headers,
        data={
            "action": "contact",
            "version": "0",
            "name": "Neu",
            "email": "neu@example.org",
            "phone": "",
        },
    )
    assert refused.headers["Location"].endswith(f"/admin/event/{old['id']}")
    assert get_reg(mod, reg_id)["name"] == "Example Person"
    assert get_reg(mod, reg_id)["edit_version"] == 0

    client.post(f"/admin/cancel/{reg_id}", headers=headers)
    assert get_reg(mod, reg_id)["status"] == "paid"

    # Releasing an archived booking must not free a table of the new event.
    register(client, headers, table=1)
    client.post(f"/admin/cancel/{reg_id}", headers=headers)
    with connect(mod) as db:
        assert db.execute("SELECT status FROM tables WHERE number=1 AND event_id=(SELECT id FROM events WHERE archived_at IS NULL)").fetchone()[0] == "held"


def test_deleting_an_archive_removes_its_registrations(mod, tmp_path, monkeypatch):
    client, headers = admin(mod)
    place_floorplan(mod, tmp_path, monkeypatch)
    register(client, headers, payment_method="sepa")
    archive(client, headers)
    old = events(mod)[0]

    assert (
        client.post(
            "/admin/event", headers=headers, data={"action": "delete", "event_id": old["id"]}
        ).status_code
        == 302
    )
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM email_outbox").fetchone()[0] == 0
    assert not (tmp_path / f"floorplan-event{old['id']}.png").exists()
    assert client.get(f"/admin/event/{old['id']}").status_code == 404


def test_active_event_cannot_be_deleted(mod):
    client, headers = admin(mod)
    active_id = events(mod)[0]["id"]
    client.post("/admin/event", headers=headers, data={"action": "delete", "event_id": active_id})
    assert len(events(mod)) == 1


def test_legacy_database_keeps_its_registrations_visible(mod, tmp_path, monkeypatch):
    legacy_path = str(tmp_path / "legacy.db")
    with sqlite3.connect(legacy_path) as db:
        db.executescript("""
            CREATE TABLE tables(id INTEGER PRIMARY KEY, number INTEGER UNIQUE NOT NULL,
                status TEXT NOT NULL DEFAULT 'free',held_at TEXT,registration_id INTEGER);
            CREATE TABLE registrations(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,
                phone TEXT,table_id INTEGER NOT NULL,paypal_order_id TEXT,status TEXT NOT NULL,
                created_at TEXT NOT NULL,price REAL,voucher_code TEXT,payment_method TEXT NOT NULL);
            INSERT INTO tables VALUES(1,1,'booked','2026-09-12',7);
            INSERT INTO registrations VALUES(7,'Legacy','legacy@example.org',NULL,1,NULL,'paid',
                '2026-09-12T10:00:00',15,NULL,'sepa');
        """)
    monkeypatch.setattr(mod, "DB_PATH", legacy_path)
    mod.init_db()
    mod.init_db()

    with connect(mod) as db:
        active = db.execute("SELECT * FROM events WHERE archived_at IS NULL").fetchall()
    assert len(active) == 1
    assert get_reg(mod, 7)["event_id"] == active[0]["id"]
    assert active[0]["created_at"] == "2026-09-12T10:00:00"

    client, headers = admin(mod)
    assert "Legacy" in client.get("/admin").text
