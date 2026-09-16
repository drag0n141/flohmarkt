import re

from test_booking import mod, connect  # noqa: F401
from test_archive import admin


def test_inventory_is_paginated_and_filters_status_in_german(mod):
    client, _ = admin(mod)
    with connect(mod) as db:
        db.execute("UPDATE tables SET status='held' WHERE number=20")
        db.execute("UPDATE tables SET status='booked' WHERE number=21")
    page = client.get('/admin/floorplan').text
    assert page.count('>Bearbeiten</a>') == 12
    assert 'Seite 1 von 3' in page
    assert 'id="table-edit"' not in page
    page = client.get('/admin/floorplan?filter=held').text
    assert page.count('>Bearbeiten</a>') == 1
    assert '>reserviert</span>' in page
    page = client.get('/admin/floorplan?filter=booked').text
    assert '>gebucht</span>' in page
    assert '>booked<' not in page and '>held<' not in page
    page = client.get('/admin/floorplan?page=3&edit=30').text
    assert page.count('>Bearbeiten</a>') == 6
    assert 'Tisch 30 bearbeiten' in page
    assert 'name="number"' in page
    assert client.get('/admin/floorplan?page=invalid').status_code == 200
    assert 'Keine passenden Tische' in client.get('/admin/floorplan?q=999').text


def test_voucher_tariff_has_separate_column_with_price(mod):
    client, headers = admin(mod)
    client.post('/admin/vouchers', headers=headers, data={'action': 'create', 'code': 'MEMBER', 'tariff_id': '2'})
    page = client.get('/admin/vouchers').text
    assert '<th>Tarif</th>' in page
    assert '<strong>Intern</strong>' in page
    assert '15,00 EUR' in page
    assert re.search(r'MEMBER</code></td>\s*<td>', page)


def test_existing_plan_precedes_inventory_and_upload_is_collapsed(mod):
    client, _ = admin(mod)
    with connect(mod) as db:
        db.execute("INSERT INTO settings(key,value) VALUES('floorplan_image','test.png')")
    page = client.get('/admin/floorplan').text
    assert page.index('id="plan-inner"') < page.index('id="inventory"')
    assert '<details class="card plan-upload" >' in page
    assert 'id="plan-table-search"' in page


def test_picker_interactions(mod):
    import shutil
    import subprocess
    import pytest
    if not shutil.which('node') or subprocess.run(['node', '-e', "require.resolve('jsdom')"], capture_output=True).returncode:
        pytest.skip('Node.js and jsdom are required for the DOM check')
    client, _ = admin(mod)
    with connect(mod) as db:
        db.execute("INSERT INTO settings(key,value) VALUES('floorplan_image','test.png')")
        db.execute('UPDATE tables SET pos_x=10,pos_y=10 WHERE number=1')
    result = subprocess.run(['node', 'tests/inventory_dom.cjs'], input=client.get('/admin/floorplan').text,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
