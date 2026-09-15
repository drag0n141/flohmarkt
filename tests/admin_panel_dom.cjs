const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const fixture = JSON.parse(fs.readFileSync(0, 'utf8'));
const dom = new JSDOM(fixture.dashboard, {url: 'https://example.org/admin?q=Example&filter=pending', runScripts: 'outside-only', pretendToBeVisual: true});
const w = dom.window;
const d = w.document;
const tick = () => new Promise(resolve => setTimeout(resolve, 30));
w.matchMedia = () => ({matches: false, addEventListener() {}});
w.scrollTo = () => {};
w.HTMLDialogElement.prototype.showModal = function () {this.open = true;};
w.HTMLDialogElement.prototype.close = function () {this.open = false; this.dispatchEvent(new w.Event('close'));};
let confirmResult = true;
w.confirm = () => confirmResult;
let failSave = false;
let saved = false;
let cancelled = false;
const requests = [];
w.fetch = async (url, options = {}) => {
  requests.push({url: String(url), options});
  const path = new URL(url, w.location.href).pathname;
  if (options.method === 'POST') {
    assert.ok(options.body.get('csrf_token'));
    if (path === '/admin/cancel/1') {
      cancelled = true;
      return {ok: true, text: async () => fixture.cancelled};
    }
    assert.equal(options.body.get('action'), 'contact');
    assert.equal(options.body.get('name'), 'Changed name');
    if (failSave) return {ok: false, text: async () => fixture.error};
    saved = true;
    return {ok: true, text: async () => fixture.saved};
  }
  return {ok: true, text: async () => cancelled ? (path === '/admin' ? fixture.cancelled : fixture.cancelled_detail) : path === '/admin' ? fixture.dashboard : saved ? fixture.saved : fixture.detail};
};
w.eval(fs.readFileSync('static/admin.js', 'utf8'));
(async () => {
  d.querySelector('[data-booking-detail]').click(); await tick();
  assert.equal(d.getElementById('booking-detail-panel').open, true);
  assert.ok(d.querySelector('#detail-panel-body #booking-details'));
  const name = d.querySelector('#detail-panel-body input[name=name]');
  name.value = 'Changed name'; name.dispatchEvent(new w.Event('input', {bubbles: true}));
  confirmResult = false;
  d.getElementById('close-detail-panel').click();
  assert.equal(d.getElementById('booking-detail-panel').open, true);
  failSave = true;
  name.closest('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await tick();
  assert.equal(d.querySelector('#detail-panel-body input[name=name]').value, 'Changed name');
  assert.equal(d.querySelector('#detail-panel-body input[name=version]').value, '0');
  failSave = false;
  d.querySelector('#detail-panel-body input[name=name]').closest('form').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await tick();
  assert.equal(d.querySelector('#detail-panel-body input[name=version]').value, '1');
  assert.ok(requests.some(r => r.url === 'https://example.org/admin?q=Example&filter=pending'));
  assert.equal(w.location.search, '?q=Example&filter=pending');
  d.getElementById('close-detail-panel').click();
  assert.equal(d.getElementById('booking-detail-panel').open, false);
  assert.equal(d.body.classList.contains('detail-panel-open'), false);
  assert.equal(d.activeElement.hasAttribute('data-booking-detail'), true);
  // The replaced overview still opens details through event delegation.
  d.querySelector('[data-booking-detail]').click(); await tick();
  assert.equal(d.getElementById('booking-detail-panel').open, true);
  d.querySelector('#detail-panel-body form[action$="/admin/cancel/1"]').dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); await tick();
  assert.equal(d.querySelector('#detail-panel-body form[action$="/admin/cancel/1"]'), null);
  assert.match(d.getElementById('detail-panel-body').textContent, /storniert/);
  assert.equal(d.getElementById('booking-detail-panel').open, true);
  dom.window.close();
  console.log('Admin detail panel interaction checks passed');
})().catch(error => {console.error(error); dom.window.close(); process.exitCode = 1;});
