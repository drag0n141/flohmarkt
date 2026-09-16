"""Covers the shared admin chrome: navigation, flash styling, status wording."""

import pytest
from test_booking import mod, client_for, register, connect, get_reg  # noqa: F401

ADMIN_PAGES = {
    "/admin/pricing": "/admin/pricing",
    "/admin": "/admin",
    "/admin/vouchers": "/admin/vouchers",
    "/admin/floorplan": "/admin/floorplan",
    "/admin/emails": "/admin/emails",
    "/admin/page": "/admin/page",
    "/admin/faq": "/admin/faq",
    "/admin/event": "/admin/event",
}


def admin(mod):
    client, headers = client_for(mod)
    with client.session_transaction() as session:
        session["is_admin"] = True
    return client, headers


@pytest.mark.parametrize("url,active", sorted(ADMIN_PAGES.items()))
def test_every_admin_page_renders_the_shared_navigation(mod, url, active):
    client, _ = admin(mod)
    page = client.get(url)
    assert page.status_code == 200
    # The whole navigation is present everywhere, with the current page marked
    # rather than omitted.
    for target in ADMIN_PAGES:
        assert f'href="{target}"' in page.text
    assert f'<a class="active" href="{active}"' in page.text
    assert page.text.count('<a class="active" href="/admin') == 1


def test_subpages_mark_their_parent_entry(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    assert '<a class="active" href="/admin"' in client.get("/admin/registrations/1/edit").text

    client.post(
        "/admin/event",
        headers=headers,
        data={"action": "archive", "confirm": "yes", "archive_name": "Vorbei"},
    )
    detail = client.get("/admin/event/1")
    assert '<a class="active" href="/admin/event"' in detail.text


def test_rejected_action_is_flashed_as_an_error(mod):
    client, headers = admin(mod)
    page = client.post(
        "/admin/page", headers=headers, data={"title": "  ", "info": ""}, follow_redirects=True
    )
    assert 'class="notice error"' in page.text
    assert "Der Titel darf nicht leer sein." in page.text


def test_confirmation_is_not_flashed_as_an_error(mod):
    client, headers = admin(mod)
    page = client.post(
        "/admin/page",
        headers=headers,
        data={"title": "Herbstflohmarkt", "info": ""},
        follow_redirects=True,
    )
    assert 'class="notice"' in page.text
    assert "notice error" not in page.text
    assert "Seiteninhalt wurde gespeichert." in page.text


def test_failed_login_does_not_look_like_a_success(mod):
    client, headers = admin(mod)
    page = client.post("/admin/login", headers=headers, data={"password": "wrong"})
    assert 'class="notice error"' in page.text
    assert "Falsches Passwort." in page.text


def test_booking_status_is_shown_in_german(mod):
    client, headers = admin(mod)
    reg_id = register(client, headers, payment_method="sepa").json["registration_id"]
    assert "offen" in client.get("/admin").text

    client.post(f"/admin/confirm-sepa/{reg_id}", headers=headers)
    dashboard = client.get("/admin").text
    assert "bezahlt" in dashboard
    assert ">paid<" not in dashboard
    # The CSS class still uses the raw value, so the colour coding keeps working.
    assert 'class="badge paid"' in dashboard


def test_destructive_actions_are_marked_as_such(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    assert (
        '<button type="submit" class="danger small">Buchung stornieren und Tisch freigeben</button>' in client.get("/admin/registrations/1/edit").text
    )

    client.post(
        "/admin/event",
        headers=headers,
        data={"action": "archive", "confirm": "yes", "archive_name": "Vorbei"},
    )
    assert (
        '<button type="submit" class="danger small">Löschen</button>'
        in client.get("/admin/event").text
    )


def test_dashboard_search_filters_and_deadline(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method="sepa", name="Anna Beispiel")
    register(client, headers, table=2, payment_method="sepa", name="Berta Muster")
    reg = get_reg(mod, 1)
    for query in ("ANNA", "1", reg["payment_reference"]):
        page = client.get("/admin", query_string={"q": query}).text
        assert "Anna Beispiel" in page
        assert "Berta Muster" not in page
    assert mod.display_deadline(reg) in client.get("/admin").text
    client.post("/admin/confirm-sepa/2", headers=headers)
    page = client.get("/admin?filter=pending").text
    assert "Anna Beispiel" in page and "Berta Muster" not in page
    assert "Keine Buchungen" in client.get("/admin?q=does-not-exist").text


def test_review_filter_preserves_late_payment_workflow(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="paypal", name="Zahlungsprüfung")
    client.post("/admin/cancel/1", headers=headers)
    # Actual late payment receipts still enter review; manual SEPA confirmation
    # of a cancelled booking is no longer allowed.
    with connect(mod) as db:
        mod.finalize_paid_registration(db, 1)
    page = client.get("/admin?filter=review").text
    assert "Zahlungsprüfung" in page
    assert "Klärung erledigt" in page


def test_booking_details_replace_the_action_menu(mod):
    client, headers = admin(mod)
    register(client, headers, payment_method="sepa")
    page = client.get("/admin").text
    assert page.count('data-booking-detail') == 3
    assert 'id="booking-detail-panel"' in page
    assert 'Weitere Aktionen' not in page
    assert 'Buchung stornieren und Tisch freigeben' not in page
    assert 'Zahlungseingang erfassen' in page
    detail = client.get("/admin/registrations/1/edit").text
    assert 'id="booking-details"' in detail
    assert 'Kontaktdaten bearbeiten' in detail
    assert 'Buchung stornieren und Tisch freigeben' in detail


def test_german_admin_formatting(mod):
    assert mod.admin_datetime("2026-01-01T23:30:00") == "02.01.2026 00:30"
    assert mod.admin_datetime("2026-07-01T10:00:00+00:00") == "01.07.2026 12:00"
    assert mod.admin_money(1234.5) == "1.234,50"
    assert mod.admin_money(None) == "–"


def test_detail_panel_form_roundtrip(mod):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('node') or subprocess.run(
        ['node', '-e', "require.resolve('jsdom')"], capture_output=True
    ).returncode:
        pytest.skip('Install jsdom and set NODE_PATH to run panel DOM checks')
    client, headers = admin(mod)
    register(client, headers, payment_method='sepa')
    dashboard = client.get('/admin?q=Example&filter=pending').text
    detail = client.get('/admin/registrations/1/edit').text
    error = client.post('/admin/registrations/1/edit', headers=headers, data={
        'action': 'contact', 'version': '0', 'name': 'Changed name', 'email': 'invalid'
    })
    assert error.status_code == 400
    saved = client.post('/admin/registrations/1/edit', headers=headers, data={
        'action': 'contact', 'version': '0', 'name': 'Changed name', 'email': 'changed@example.org'
    }, follow_redirects=True)
    assert saved.status_code == 200
    cancelled = client.post('/admin/cancel/1', headers=headers, follow_redirects=True)
    cancelled_detail = client.get('/admin/registrations/1/edit')
    result = subprocess.run(['node', 'tests/admin_panel_dom.cjs'], input=json.dumps({
        'dashboard': dashboard, 'detail': detail, 'error': error.text, 'saved': saved.text,
        'cancelled': cancelled.text, 'cancelled_detail': cancelled_detail.text
    }), text=True, capture_output=True, cwd=Path(__file__).resolve().parents[1], timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
