"""Configurable lead time of the payment reminder (Preise & Fristen)."""

from datetime import timedelta

from test_booking import mod, register, connect, get_reg  # noqa: F401
from test_archive import admin, archive
from test_event_configuration import pricing


def reminder_count(db):
    return db.execute("SELECT COUNT(*) FROM email_outbox WHERE kind='reminder'").fetchone()[0]


def lead_hours(mod):
    with connect(mod) as db:
        return db.execute(
            "SELECT reminder_hours FROM events WHERE archived_at IS NULL"
        ).fetchone()[0]


def book_sepa_with_deadline_in(mod, client, headers, hours, total=96):
    register(client, headers, payment_method="sepa")
    with connect(mod) as db:
        db.execute(
            "UPDATE registrations SET expires_at=?",
            ((mod.utcnow() + timedelta(hours=hours)).isoformat(),),
        )
        db.execute(f"UPDATE registrations SET created_at=datetime(expires_at, '-{total} hours')")
        db.commit()


def test_default_lead_time_is_24_hours(mod):
    assert lead_hours(mod) == 24
    client, _ = admin(mod)
    page = client.get("/admin/pricing").text
    assert 'name="reminder_duration"' in page
    assert "24 Stunden vor Ablauf" in client.get("/admin/emails").text


def test_lead_time_can_be_set_in_days(mod):
    client, headers = admin(mod)
    response = pricing(
        client, headers, mod, action="reminder", reminder_duration="2", reminder_unit="days"
    )
    assert response.status_code == 302
    assert lead_hours(mod) == 48
    assert "48 Stunden vor Ablauf" in client.get("/admin/emails").text


def test_reminder_is_sent_at_configured_lead_time(mod):
    client, headers = admin(mod)
    pricing(client, headers, mod, action="reminder", reminder_duration="48", reminder_unit="hours")
    book_sepa_with_deadline_in(mod, client, headers, hours=47)
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        assert reminder_count(db) == 1


def test_reminder_waits_until_configured_lead_time(mod):
    client, headers = admin(mod)
    pricing(client, headers, mod, action="reminder", reminder_duration="12", reminder_unit="hours")
    book_sepa_with_deadline_in(mod, client, headers, hours=20)
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        assert reminder_count(db) == 0


def test_zero_disables_reminder(mod):
    client, headers = admin(mod)
    pricing(client, headers, mod, action="reminder", reminder_duration="0", reminder_unit="hours")
    book_sepa_with_deadline_in(mod, client, headers, hours=1)
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        assert reminder_count(db) == 0
    assert "deaktiviert" in client.get("/admin/emails").text


def test_disabling_drops_unsent_reminder_on_booking_edit(mod):
    client, headers = admin(mod)
    book_sepa_with_deadline_in(mod, client, headers, hours=10)
    with connect(mod) as db:
        mod.send_sepa_reminders(db)
        db.execute("UPDATE email_outbox SET next_attempt_at=9999999999 WHERE kind='reminder'")
        db.commit()
        assert reminder_count(db) == 1
    pricing(client, headers, mod, action="reminder", reminder_duration="0", reminder_unit="hours")
    with connect(mod) as db:
        mod.refresh_pending_emails(db, get_reg(mod))
        assert reminder_count(db) == 0


def test_invalid_lead_time_is_rejected(mod):
    client, headers = admin(mod)
    for data in (
        {"reminder_duration": "-1", "reminder_unit": "hours"},
        {"reminder_duration": "abc", "reminder_unit": "hours"},
        {"reminder_duration": "366", "reminder_unit": "days"},
        {"reminder_duration": "5", "reminder_unit": "weeks"},
    ):
        assert pricing(client, headers, mod, action="reminder", **data).status_code == 400
    assert lead_hours(mod) == 24


def test_lead_time_is_carried_over_with_deadlines(mod):
    client, headers = admin(mod)
    pricing(client, headers, mod, action="reminder", reminder_duration="36", reminder_unit="hours")
    assert archive(client, headers).status_code == 302
    assert lead_hours(mod) == 36
