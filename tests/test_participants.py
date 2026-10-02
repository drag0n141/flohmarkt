"""Participant exports must not mix cancelled/archived bookings into attendance."""
import csv
import io
from datetime import timedelta

import pytest
from test_booking import mod, client_for, register, connect  # noqa: F401
from test_admin_ui import admin
from test_archive import archive


def csv_rows(client):
    response = client.get('/admin/participants?format=csv')
    assert response.status_code == 200
    assert response.data.startswith(b'\xef\xbb\xbf')
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'attachment; filename="teilnehmer-veranstaltung-' in response.headers['Content-Disposition']
    return list(csv.DictReader(io.StringIO(response.data.decode('utf-8-sig')), delimiter=';'))


@pytest.mark.parametrize('suffix', ['', '?format=csv'])
def test_export_requires_admin(mod, suffix):
    client, _ = client_for(mod)
    response = client.get('/admin/participants' + suffix)
    assert response.status_code == 302
    assert response.headers['Location'].endswith('/admin/login')


def test_exports_are_sorted_and_include_payment_and_attendance(mod):
    client, headers = admin(mod)
    register(client, headers, table=10, payment_method='sepa', name='Özlem; "Müller"')
    register(client, headers, table=2, payment_method='sepa', name='Anna')
    client.post('/admin/confirm-sepa/2', headers=headers)
    rows = csv_rows(client)
    assert [r['Tisch'] for r in rows] == ['2', '10']
    assert rows[0]['Zahlungsstatus'] == 'bezahlt'
    assert rows[0]['Offener Betrag'] == '0,00'
    assert rows[1]['Name'] == 'Özlem; "Müller"'
    assert rows[1]['Zahlungsstatus'] == 'offen'
    assert rows[1]['Offener Betrag'] == '15,00'
    assert rows[1]['Währung'] == 'EUR'
    assert all(r['Anwesend'] == r['Notizen'] == '' for r in rows)
    page = client.get('/admin/participants')
    assert page.headers['Cache-Control'] == 'no-store'
    assert page.text.index('Anna') < page.text.index('Özlem')
    assert '2 aktive Buchungen' in page.text
    assert 'Anwesend' in page.text and 'Notizen' in page.text
    assert 'person@example.org' not in page.text


def test_excludes_cancelled_expired_and_archived_bookings(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method='sepa', name='Altes Event')
    client.post('/admin/confirm-sepa/1', headers=headers)
    archive(client, headers)
    register(client, headers, table=1, payment_method='sepa', name='Storniert')
    client.post('/admin/cancel/2', headers=headers)
    with connect(mod) as db:
        mod.finalize_paid_registration(db, 2)
    register(client, headers, table=2, payment_method='sepa', name='Abgelaufen')
    register(client, headers, table=3, payment_method='sepa', name='Aktuell')
    with connect(mod) as db:
        db.execute('UPDATE registrations SET expires_at=? WHERE id=3',
                   ((mod.utcnow() - timedelta(hours=1)).isoformat(),))
    assert [r['Name'] for r in csv_rows(client)] == ['Aktuell']
    page = client.get('/admin/participants').text
    for name in ('Altes Event', 'Storniert', 'Abgelaufen'):
        assert name not in page
    assert 'Aktuell' in page


def test_review_and_received_payment_do_not_claim_an_open_amount(mod):
    client, headers = admin(mod)
    register(client, headers, table=1, payment_method='sepa')
    register(client, headers, table=2, payment_method='sepa')
    with connect(mod) as db:
        db.execute('UPDATE registrations SET payment_review=1 WHERE id=1')
        db.execute('UPDATE registrations SET payment_received_at=? WHERE id=2', (mod.utcnow().isoformat(),))
    rows = csv_rows(client)
    assert rows[0]['Zahlungsstatus'] == 'Zahlung prüfen'
    assert rows[0]['Offener Betrag'] == ''
    assert rows[1]['Zahlungsstatus'] == 'bezahlt'
    assert rows[1]['Offener Betrag'] == '0,00'


@pytest.mark.parametrize('name', ['=1+1', ' +SUM(1)', '-1+1', '@SUM(1)', '\t=1+1', '\r=1+1', '\n=1+1'])
def test_spreadsheet_formulas_are_exported_as_literal_text(mod, name):
    client, headers = admin(mod)
    register(client, headers, payment_method='sepa')
    with connect(mod) as db:
        db.execute('UPDATE registrations SET name=?', (name,))
    assert csv_rows(client)[0]['Name'] == "'" + name


def test_print_escapes_html_and_empty_exports_still_work(mod):
    client, headers = admin(mod)
    assert csv_rows(client) == []
    assert 'Keine aktiven Buchungen vorhanden.' in client.get('/admin/participants').text
    register(client, headers, payment_method='sepa', name='<script>alert(1)</script>')
    page = client.get('/admin/participants').text
    assert '<script>alert(1)</script>' not in page
    assert '&lt;script&gt;' in page
    assert client.get('/admin/participants?format=invalid').status_code == 400


def test_export_links_are_available_on_active_and_plan_views_only(mod):
    client, _ = admin(mod)
    for url in ('/admin', '/admin?view=plan', '/admin?q=missing&filter=pending'):
        page = client.get(url).text
        assert 'href="/admin/participants"' in page
        assert 'href="/admin/participants?format=csv"' in page
        assert 'unabhängig von Suche und Filtern' in page
    assert '/admin/participants' not in client.get('/admin?view=history').text
