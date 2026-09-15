"""Session-bound recovery and read-only payment reconciliation."""
from datetime import timedelta
from unittest.mock import Mock

import pytest
import requests

from test_booking import mod, client_for, register, connect, get_reg, order_for  # noqa: F401


def check(client, headers, registration_id=1):
    return client.post('/api/booking/check', headers=headers, json={'registration_id': registration_id})


def test_booking_recovery_is_bound_to_browser_owner(mod):
    owner, headers = client_for(mod)
    other, other_headers = client_for(mod)
    created = register(owner, headers, payment_method='sepa').json
    recovered = owner.get('/api/booking')
    assert recovered.headers['Cache-Control'] == 'no-store'
    assert recovered.json['booking']['registration_id'] == created['registration_id']
    assert recovered.json['booking']['reference'] == 'FLOHMARKT-1'
    assert other.get('/api/booking').json == {'booking': None}
    assert check(other, other_headers).status_code == 404
    assert check(owner, {}).status_code == 400
    assert 'owner_id' not in recovered.text
    assert 'person@example.org' not in recovered.text


def test_repeated_registration_recovers_without_duplicate_email_or_voucher(mod):
    client, headers = client_for(mod)
    with connect(mod) as db:
        db.execute("INSERT INTO vouchers (code,max_uses,used_count,active,created_at) VALUES ('ONCE',1,0,1,?)", (mod.utcnow().isoformat(),))
    first = register(client, headers, payment_method='sepa', voucher='ONCE').json
    second = register(client, headers, payment_method='sepa', voucher='ONCE').json
    assert second['registration_id'] == first['registration_id']
    with connect(mod) as db:
        assert db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM email_outbox').fetchone()[0] == 1
        assert db.execute('SELECT used_count FROM vouchers').fetchone()[0] == 1


def test_deadline_uses_server_configuration_and_admin_extension(mod, monkeypatch):
    monkeypatch.setattr(mod, 'HOLD_MINUTES', 23)
    client, headers = client_for(mod)
    created = register(client, headers, payment_method='paypal').json
    reg = get_reg(mod)
    assert created['expires_at'].startswith((mod.deadline_for(reg)).isoformat())
    from datetime import datetime
    assert datetime.fromisoformat(created['expires_at']).replace(tzinfo=None) - datetime.fromisoformat(reg['created_at']) == timedelta(minutes=23)
    with connect(mod) as db:
        db.execute('UPDATE registrations SET expires_at=? WHERE id=1', ((mod.utcnow()+timedelta(hours=2)).isoformat(),))
    assert client.get('/api/booking').json['booking']['expires_at'] != created['expires_at']


def test_recovery_reports_expiry_and_does_not_restore_archived_event(mod):
    client, headers = client_for(mod)
    register(client, headers, payment_method='sepa')
    with connect(mod) as db:
        db.execute('UPDATE registrations SET expires_at=?', ((mod.utcnow()-timedelta(minutes=1)).isoformat(),))
    assert client.get('/api/booking').json['booking']['status'] == 'cancelled'
    with client.session_transaction() as session:
        session['is_admin'] = True
    client.post('/admin/event', headers=headers, data={'action':'archive','confirm':'yes','archive_name':'Previous'})
    assert client.get('/api/booking').json == {'booking': None}
    assert check(client, headers).status_code == 404


@pytest.mark.parametrize('state', ['APPROVED', 'COMPLETED'])
def test_status_check_never_captures_or_creates_an_order(mod, monkeypatch, state):
    client, headers = client_for(mod)
    register(client, headers, payment_method='paypal')
    with connect(mod) as db:
        db.execute("UPDATE registrations SET paypal_order_id='ORDER1'")
    reg = get_reg(mod)
    monkeypatch.setattr(mod, 'paypal_get_order', Mock(return_value=order_for(reg, status=state)))
    capture = Mock(side_effect=AssertionError('Status check must not charge'))
    create = Mock(side_effect=AssertionError('Status check must not create orders'))
    monkeypatch.setattr(mod, 'paypal_capture_order', capture)
    monkeypatch.setattr(mod, 'paypal_create_order', create)
    result = check(client, headers)
    assert result.status_code == 200
    assert result.json['booking']['status'] == ('paid' if state == 'COMPLETED' else 'pending')
    capture.assert_not_called()
    create.assert_not_called()


def test_uncertain_payment_check_preserves_booking_for_retry(mod, monkeypatch):
    client, headers = client_for(mod)
    register(client, headers)
    with connect(mod) as db:
        db.execute("UPDATE registrations SET paypal_order_id='ORDER1'")
    monkeypatch.setattr(mod, 'paypal_get_order', Mock(side_effect=requests.Timeout))
    assert check(client, headers).status_code == 503
    assert get_reg(mod)['status'] == 'pending'


def test_late_payment_is_recovered_as_review_when_table_reallocated(mod, monkeypatch):
    owner, headers = client_for(mod)
    other, other_headers = client_for(mod)
    register(owner, headers)
    with connect(mod) as db:
        db.execute("UPDATE registrations SET paypal_order_id='ORDER1', expires_at=?", ((mod.utcnow()-timedelta(minutes=1)).isoformat(),))
    owner.get('/api/booking')
    register(other, other_headers, payment_method='sepa')
    monkeypatch.setattr(mod, 'paypal_get_order', Mock(return_value=order_for(get_reg(mod))))
    result = check(owner, headers)
    assert result.json['booking']['status'] == 'review'
    with connect(mod) as db:
        assert db.execute('SELECT registration_id FROM tables WHERE number=1').fetchone()[0] == 2


def test_public_booking_dom_interactions(mod):
    """Optional DOM checks: npm install --prefix /tmp/flohmarkt-ui-test jsdom;
    NODE_PATH=/tmp/flohmarkt-ui-test/node_modules python -m pytest -q
    """
    import shutil
    import subprocess
    from pathlib import Path
    if not shutil.which('node'):
        pytest.skip('Node.js is required for DOM interaction checks')
    available = subprocess.run(['node', '-e', "require.resolve('jsdom')"], capture_output=True)
    if available.returncode:
        pytest.skip('Install jsdom and set NODE_PATH to run DOM interaction checks')
    client, _ = client_for(mod)
    result = subprocess.run(
        ['node', 'tests/public_booking_dom.cjs'],
        input=client.get('/').text, text=True, capture_output=True,
        cwd=Path(__file__).resolve().parents[1], timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
