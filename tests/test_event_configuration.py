"""Event setup, legacy migration and immutable booking terms."""

import json
from datetime import datetime, timedelta

import pytest
from test_booking import mod, client_for, register, connect, get_reg  # noqa: F401
from test_archive import admin, archive

LEGACY_ENVS = ("PRICE", "PRICE_STANDARD", "PRICE_INTERNAL", "NUM_TABLES", "SEPA_HOLD_HOURS")


def event_id(mod):
    with connect(mod) as db:
        return db.execute("SELECT id FROM events WHERE archived_at IS NULL").fetchone()[0]


def pricing(client, headers, mod, **data):
    return client.post("/admin/pricing", headers=headers, data={"event_id": event_id(mod), **data})


def table_action(client, headers, mod, **data):
    return client.post(
        "/admin/tables",
        headers=headers,
        data={"event_id": event_id(mod), **data},
        follow_redirects=True,
    )


def tariff(client, headers, mod, **data):
    return pricing(
        client,
        headers,
        mod,
        action="tariff",
        **{
            "name": "Großer Tisch",
            "price": "22,50",
            "visibility": "public",
            "scope": "all",
            "active": "yes",
            **data,
        }
    )


def test_start_without_envs_can_be_fully_configured_in_admin(mod, monkeypatch, tmp_path):
    for key in LEGACY_ENVS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(mod, "DB_PATH", str(tmp_path / "empty.db"))
    mod.init_db()
    client, headers = admin(mod)
    assert client.get("/").status_code == 200
    assert "Buchung ist derzeit" in client.get("/").text
    assert client.get("/api/tables").json == []
    assert register(client, headers).status_code == 409
    assert client.get("/admin/pricing").status_code == 200
    assert client.get("/admin/floorplan").status_code == 200
    assert (
        pricing(client, headers, mod, action="deadline", duration="3", unit="days").status_code
        == 302
    )
    assert tariff(client, headers, mod, name="Standard").status_code == 302
    page = table_action(client, headers, mod, action="add", start="1", end="2", tariff_id="1")
    assert page.status_code == 200
    reg = register(client, headers, payment_method="sepa").json
    assert reg["price"] == 22.5
    record = get_reg(mod)
    assert mod.deadline_for(record) - datetime.fromisoformat(record["created_at"]) == timedelta(
        days=3
    )
    for key in LEGACY_ENVS:
        monkeypatch.setenv(key, "invalid value ignored after migration")
    mod.init_db()
    assert client.get("/api/tables").json[0]["price"] == 22.5
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM tables").fetchone()[0] == 2


def test_price_and_deadline_changes_only_apply_to_new_bookings(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    before = dict(get_reg(mod))
    assert (
        tariff(client, headers, mod, tariff_id="1", name="Neuer Standard", price="25").status_code
        == 302
    )
    assert (
        pricing(client, headers, mod, action="deadline", duration="72", unit="hours").status_code
        == 302
    )
    assert dict(get_reg(mod)) == before
    second = register(client, headers, table=2, payment_method="sepa").json
    assert second["price"] == 25
    assert get_reg(mod, 2)["tariff_name"] == "Neuer Standard"
    assert get_reg(mod, 2)["price_cents"] == 2500
    assert mod.deadline_for(get_reg(mod, 2)) - datetime.fromisoformat(
        get_reg(mod, 2)["created_at"]
    ) == timedelta(hours=72)


@pytest.mark.parametrize("price", ["0", "-1", "NaN", "Infinity", "0.001", "100000", "no"])
def test_invalid_prices_do_not_change_existing_tariffs(mod, price):
    client, headers = admin(mod)
    assert tariff(client, headers, mod, tariff_id="1", price=price).status_code == 400
    assert client.get("/api/tables").json[0]["price"] == 15


def test_restricted_voucher_tariff_and_rollback(mod):
    client, headers = admin(mod)
    assert (
        tariff(
            client,
            headers,
            mod,
            name="Mitglieder groß",
            price="12",
            visibility="code",
            scope="selected",
            table_ids=["2"],
        ).status_code
        == 302
    )
    client.post(
        "/admin/vouchers",
        headers=headers,
        data={"action": "create", "code": "SPECIAL", "max_uses": "2", "tariff_id": "3"},
    )
    assert not client.get("/api/check-voucher?code=SPECIAL&table=1").json["valid"]
    quote = client.get("/api/check-voucher?code=SPECIAL&table=2").json
    assert quote == {"valid": True, "price": 12, "tariff_name": "Mitglieder groß"}
    assert register(client, headers, voucher="SPECIAL").status_code == 400
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers WHERE code='SPECIAL'").fetchone()[0] == 0
    assert (
        register(client, headers, table=2, voucher="SPECIAL", expected_price=12).json["price"] == 12
    )


def test_stale_quote_rejected_without_consuming_voucher(mod):
    client, headers = admin(mod)
    client.post(
        "/admin/vouchers",
        headers=headers,
        data={"action": "create", "code": "SPECIAL", "tariff_id": "2"},
    )
    tariff(client, headers, mod, tariff_id="2", visibility="code", price="12")
    assert register(client, headers, voucher="SPECIAL", expected_price=15).status_code == 400
    with connect(mod) as db:
        assert db.execute("SELECT used_count FROM vouchers").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0


def test_cutoff_caps_new_deadlines_and_closes_bookings(mod, monkeypatch):
    client, headers = admin(mod)
    cutoff = mod.utcnow() + timedelta(hours=1)
    with connect(mod) as db:
        db.execute("UPDATE events SET payment_cutoff=?", (cutoff.isoformat(),))
    register(client, headers, payment_method="sepa")
    assert mod.deadline_for(get_reg(mod)) == cutoff
    monkeypatch.setattr(mod, "utcnow", lambda: cutoff + timedelta(seconds=1))
    assert register(client, headers, table=2).status_code == 409
    assert all(t["status"] == "disabled" for t in client.get("/api/tables").json)


def test_table_inventory_guards_and_atomic_range(mod):
    client, headers = admin(mod)
    page = table_action(client, headers, mod, action="add", start="30", end="32", tariff_id="1")
    assert "Es wurde nichts geändert" in page.text
    assert len(client.get("/api/tables").json) == 30
    assert (
        "gespeichert"
        in table_action(
            client, headers, mod, action="add", start="31", end="32", tariff_id="1"
        ).text
    )
    register(client, headers, payment_method="sepa")
    page = table_action(
        client, headers, mod, action="edit", table_id="1", number="1", tariff_id="1"
    )
    assert "belegte Tische nicht deaktiviert" in page.text
    assert (
        "nur deaktiviert" in table_action(client, headers, mod, action="delete", table_id="1").text
    )
    client.post("/admin/cancel/1", headers=headers)
    table_action(client, headers, mod, action="edit", table_id="1", number="1", tariff_id="1")
    assert client.get("/api/tables").json[0]["status"] == "disabled"
    assert register(client, headers).status_code == 404
    table_action(client, headers, mod, action="delete", table_id="32")
    mod.init_db()
    assert 32 not in [t["number"] for t in client.get("/api/tables").json]


def test_move_to_more_expensive_table_requires_explicit_price_confirmation(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    tariff(client, headers, mod, scope="selected", table_ids=["2"], assign="yes")
    data = {"action": "move", "table": "2", "version": "0"}
    response = client.post("/admin/registrations/1/edit", headers=headers, data=data)
    assert response.status_code == 400 and "22.50" in response.text
    assert get_reg(mod)["table_id"] == 1
    response = client.post(
        "/admin/registrations/1/edit", headers=headers, data={**data, "keep_price": "yes"}
    )
    assert response.status_code == 302
    assert get_reg(mod)["price"] == 15
    assert get_reg(mod)["table_id"] == 2


def test_archive_clones_config_and_preserves_history(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    old_table = get_reg(mod)["table_id"]
    archive(client, headers, keep=["positions"])
    current = client.get("/api/tables").json
    assert len(current) == 30
    assert current[0]["price"] == 15
    table_action(
        client,
        headers,
        mod,
        action="edit",
        table_id="31",
        number="101",
        tariff_id="3",
        active="yes",
    )
    with connect(mod) as db:
        assert db.execute("SELECT number FROM tables WHERE id=?", (old_table,)).fetchone()[0] == 1
        snapshot = json.loads(db.execute("SELECT snapshot FROM events WHERE id=1").fetchone()[0])
        assert snapshot["tariffs"][0]["amount_cents"] == 1500
        assert snapshot["sepa_hold_hours"] == 48
    assert client.get("/admin/event/1").status_code == 200


def test_new_event_without_carryover_needs_setup_and_env_does_not_reseed(mod):
    client, headers = admin(mod)
    client.post(
        "/admin/event",
        headers=headers,
        data={"action": "archive", "confirm": "yes", "archive_name": "Old"},
    )
    mod.init_db()
    assert client.get("/api/tables").json == []
    assert register(client, headers).status_code == 409
    assert client.get("/admin/pricing").status_code == 200


def test_mutation_protection(mod):
    client, headers = client_for(mod)
    assert (
        client.post("/admin/pricing", headers=headers, data={"action": "deadline"}).status_code
        == 302
    )
    client, headers = admin(mod)
    assert client.post("/admin/pricing", data={"action": "deadline"}).status_code == 400
    assert (
        client.post(
            "/admin/pricing", headers=headers, data={"action": "deadline", "event_id": "999"}
        ).status_code
        == 400
    )


def test_legacy_migration_freezes_values_and_deadlines(mod, tmp_path, monkeypatch):
    import sqlite3

    path = str(tmp_path / "legacy.db")
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE tables(id INTEGER PRIMARY KEY, number INTEGER UNIQUE NOT NULL,status TEXT NOT NULL DEFAULT 'free',held_at TEXT,registration_id INTEGER);
            CREATE TABLE registrations(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT NOT NULL,phone TEXT,table_id INTEGER NOT NULL,paypal_order_id TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,price REAL,voucher_code TEXT,payment_method TEXT NOT NULL);
            INSERT INTO tables VALUES(1,1,'held','2026-09-15T10:00:00',1);
            INSERT INTO registrations VALUES(1,'Legacy','legacy@example.org',NULL,1,NULL,'pending','2026-09-15T10:00:00',7.5,'CODE','sepa');
        """)
    monkeypatch.setenv("SEPA_HOLD_HOURS", "72")
    monkeypatch.setenv("PRICE_STANDARD", "19.50")
    monkeypatch.setenv("PRICE_INTERNAL", "7.50")
    monkeypatch.setattr(mod, "DB_PATH", path)
    mod.init_db()
    reg = dict(get_reg(mod))
    assert reg["price"] == 7.5 and reg["price_cents"] == 750
    assert reg["expires_at"] == "2026-09-18T10:00:00"
    assert reg["tariff_name"] == "Intern"
    for key in LEGACY_ENVS:
        monkeypatch.delenv(key, raising=False)
    mod.init_db()
    assert dict(get_reg(mod)) == reg
    with connect(mod) as db:
        assert db.execute("SELECT amount_cents FROM tariffs WHERE id=1").fetchone()[0] == 1950


def test_stale_event_page_cannot_book_into_next_event(mod):
    client, headers = admin(mod)
    previous = event_id(mod)
    archive(client, headers)
    assert register(client, headers, event_id=previous, expected_price=15).status_code == 409
    with connect(mod) as db:
        assert db.execute("SELECT COUNT(*) FROM registrations").fetchone()[0] == 0
