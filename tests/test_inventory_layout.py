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


def test_inventory_page_sizes_and_navigation(mod):
    client, headers = admin(mod)
    with connect(mod) as db:
        db.executemany('INSERT INTO tables(number,event_id,tariff_id) VALUES (?,1,1)',
                       [(number,) for number in range(31, 106)])
    for size in (12, 25, 50, 100):
        page = client.get(f'/admin/floorplan?per_page={size}').text
        assert page.count('>Bearbeiten</a>') == size
        assert f'per_page={size}' in page
        assert f'<option value="{size}" selected' in page
    page = client.get('/admin/floorplan?per_page=25&page=999').text
    assert 'Seite 5 von 5' in page
    assert page.count('>Bearbeiten</a>') == 5
    for invalid in ('0', '-1', '999999', 'invalid'):
        assert client.get(f'/admin/floorplan?per_page={invalid}').text.count('>Bearbeiten</a>') == 12
    page = client.get('/admin/floorplan?per_page=25&q=10').text
    assert page.count('>Bearbeiten</a>') == 7
    assert 'name="q" value="10"' in page
    response = client.post('/admin/tables', headers=headers, data={
        'action':'edit', 'event_id':'1', 'table_id':'30', 'number':'30',
        'active':'yes', 'tariff_id':'1', 'per_page':'25', 'page':'2', 'filter':'free',
    })
    assert 'per_page=25' in response.location
    assert 'page=2' in response.location
    assert 'filter=free' in response.location
