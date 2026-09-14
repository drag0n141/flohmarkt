"""Covers the shared admin chrome: navigation, flash styling, status wording."""

import pytest
from test_booking import mod, client_for, register, connect, get_reg  # noqa: F401

ADMIN_PAGES = {
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
        '<button type="submit" class="danger small">Freigeben</button>' in client.get("/admin").text
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
