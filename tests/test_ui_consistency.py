"""Guards the UI conventions unified in the consistency pass: status wording,
table badges, stat cards, row actions and the login shell."""

from test_booking import mod, client_for, register, connect  # noqa: F401
from test_admin_ui import admin


def test_status_wording_is_vergeben_everywhere(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method="sepa")
    with connect(mod) as db:
        db.execute("UPDATE tables SET status='booked' WHERE number=1")
    inventory = client.get("/admin/floorplan?filter=booked").text
    assert '<span class="badge booked">vergeben</span>' in inventory
    assert ">Vergeben</option>" in inventory
    assert "Gebucht" not in inventory
    dashboard = client.get("/admin?view=plan").text
    assert "Grün = frei" not in dashboard


def test_stat_cards_replace_inline_stats(mod):
    client, _ = admin(mod)
    for url in ("/admin", "/admin/event"):
        page = client.get(url).text
        assert 'class="stats stat-cards"' in page
        assert 'class="stats">' not in page


def test_archive_rows_use_row_actions(mod):
    client, headers = admin(mod)
    headers = dict(headers)
    client.post("/admin/event", data={"action": "archive", "archive_name": "Alt", "new_name": "Neu", "confirm": "yes"},
                headers=headers)
    page = client.get("/admin/event").text
    assert "Weitere Aktionen" not in page
    assert 'class="row-actions"' in page
    assert "<th>Aktionen</th>" in page


def test_login_uses_admin_shell(mod):
    client, _ = client_for(mod)
    page = client.get("/admin/login").text
    assert 'class="admin-body"' in page
    assert 'class="admin-login"' in page
    assert "wrap narrow" not in page


def test_registration_heading_names_the_table(mod):
    client, headers = admin(mod)
    register(client, headers, table=4, payment_method="sepa")
    page = client.get("/admin/registrations/1/edit").text
    assert "Buchung für Tisch 4" in page
    assert "Buchung 1 bearbeiten" not in page
