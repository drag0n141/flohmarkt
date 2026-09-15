/* Run with jsdom available in NODE_PATH; the fixture comes from Flask over stdin. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(0, 'utf8');
const source = fs.readFileSync('static/app.js', 'utf8');
const pause = (ms = 30) => new Promise(resolve => setTimeout(resolve, ms));
const now = Date.now();
const baseBooking = {
  registration_id: 1, table: 1, price: 12.5, currency: 'EUR',
  voucher_applied: false, payment_method: 'sepa', status: 'pending',
  reference: 'FLOHMARKT-1', deadline: '15.09.2026 um 12:00 Uhr',
  server_time: new Date(now).toISOString(), expires_at: new Date(now+23*60000).toISOString(),
  has_order: false,
};
async function harness(restored = null, sdk = false) {
  const dom = new JSDOM(html, {url: 'https://example.org', runScripts: 'outside-only', pretendToBeVisual: true});
  const w = dom.window;
  const calls = [];
  const handlers = {};
  const callbacks = [];
  w.matchMedia = () => ({matches: true});
  w.HTMLElement.prototype.scrollIntoView = function () {};
  w.fetch = async (url, options = {}) => {
    const payload = options.body ? JSON.parse(options.body) : undefined;
    calls.push({url, payload});
    if (handlers[url]) return handlers[url](payload);
    if (url === '/api/booking') return response({booking: restored});
    if (url === '/api/tables') return response([{number: 1, status: 'free'}, {number: 2, status: 'free'}, {number: 3, status: 'booked'}]);
    if (url === '/api/floorplan-config') return response({image_url: '/static/example.png', tables: [{number: 1, x: 20, y: 20}]});
    throw new Error('Unexpected fetch: '+url);
  };
  if (sdk) w.paypal = {Buttons(options) {callbacks.push(options); return {render: async () => {}, close: async () => {}};}};
  w.eval(source);
  await pause();
  const id = name => w.document.getElementById(name);
  return {dom, w, calls, handlers, callbacks, id};
}
function response(data, status = 200) {return {ok: status < 400, status, json: async () => data};}
function submit(h) {h.id('reg-form').dispatchEvent(new h.w.Event('submit', {bubbles: true, cancelable: true}));}
function input(h, name, value) {h.id(name).value = value; h.id(name).dispatchEvent(new h.w.Event('input', {bubbles: true}));}

(async () => {
  // Plan is the default even on mobile; list switching and conflict recovery remain available.
  let h = await harness();
  assert.equal(h.id('step-select').hidden, false);
  assert.equal(h.id('grid').children.length, 3);
  assert.equal(h.id('grid').children[2].disabled, true);
  assert.match(h.id('availability').textContent, /2 von 3/);
  assert.equal(h.id('floorplan-view').hidden, false);
  assert.equal(h.id('view-plan').getAttribute('aria-pressed'), 'true');
  h.id('view-list').click();
  h.id('grid').children[0].click();
  assert.equal(h.w.document.activeElement.id, 'form-heading');
  input(h, 'name', 'Anna Beispiel'); input(h, 'email', 'anna@example.org');
  h.w.document.querySelector('[value="sepa"]').checked = true;
  h.w.document.querySelector('[value="sepa"]').dispatchEvent(new h.w.Event('change'));
  assert.equal(h.id('submit-booking').textContent, 'Kostenpflichtig reservieren');
  h.handlers['/api/register'] = () => response({error: 'Dieser Tisch ist leider nicht mehr verfügbar.'}, 409);
  submit(h); await pause();
  assert.equal(h.id('choose-another-table').hidden, false);
  assert.equal(h.w.document.activeElement.id, 'form-error');
  h.id('choose-another-table').click(); await pause();
  h.id('grid').children[1].click();
  assert.equal(h.id('name').value, 'Anna Beispiel');
  assert.equal(h.id('email').value, 'anna@example.org');
  h.handlers['/api/register'] = () => response({...baseBooking, table: 2});
  submit(h); await pause();
  assert.equal(h.id('step-sepa-pending').hidden, false);
  assert.equal(h.id('sepa-table-label').textContent, '2');
  assert.equal(h.id('sepa-reference-label').value, 'FLOHMARKT-1');
  h.dom.window.close();

  // Restore and check transfer confirmation without registering again.
  h = await harness(baseBooking);
  assert.equal(h.id('step-sepa-pending').hidden, false);
  assert.equal(h.calls.some(call => call.url === '/api/register'), false);
  h.handlers['/api/booking/check'] = () => response({booking: {...baseBooking, status: 'paid'}});
  h.id('check-sepa').click(); await pause();
  assert.equal(h.id('step-done').hidden, false);
  assert.equal(h.id('done-table-label').textContent, '1');
  assert.match(h.id('done-heading').textContent, /verbindlich gebucht/);
  h.dom.window.close();

  // Missing SDK is a user-facing error, and the timer uses the server deadline.
  h = await harness({...baseBooking, payment_method: 'paypal'});
  assert.equal(h.id('step-pay').hidden, false);
  assert.match(h.id('pay-countdown').textContent, /^(22|23):/);
  assert.match(h.id('pay-error').textContent, /PayPal konnte nicht geladen/);
  assert.equal(h.id('pay-error').textContent.includes('PAYPAL_CLIENT_ID'), false);
  assert.equal(h.id('reload-paypal').hidden, false);
  h.dom.window.close();

  // Recovered existing order reconciles without creating/capturing a new charge.
  h = await harness({...baseBooking, payment_method: 'paypal', has_order: true}, true);
  // Initial reconciliation deliberately fails in this harness; retry can recover it.
  h.handlers['/api/booking/check'] = () => response({booking: {...baseBooking, payment_method: 'paypal', status: 'paid'}});
  h.id('retry-payment').click(); await pause();
  assert.equal(h.id('step-done').hidden, false);
  assert.equal(h.calls.some(call => ['/api/create-order','/api/capture-order'].includes(call.url)), false);
  h.dom.window.close();

  // A lost capture response must not appear as a successful booking.
  h = await harness({...baseBooking, payment_method: 'paypal'}, true);
  h.handlers['/api/create-order'] = () => response({order_id: 'ORDER1'});
  h.handlers['/api/capture-order'] = () => response({error: 'Zahlungsstatus noch unklar.'}, 503);
  await h.callbacks[0].createOrder();
  await h.callbacks[0].onApprove({orderID: 'ORDER1'});
  assert.equal(h.id('step-done').hidden, true);
  assert.match(h.id('pay-error').textContent, /Bezahle nicht erneut/);
  h.handlers['/api/booking/check'] = () => response({booking: {...baseBooking, status: 'review'}});
  h.id('retry-payment').click(); await pause();
  assert.equal(h.id('step-payment-review').hidden, false);
  h.dom.window.close();
  // Capture confirmation uses fresh booking details (including an admin table move).
  h = await harness({...baseBooking, payment_method: 'paypal'}, true);
  h.handlers['/api/create-order'] = () => response({order_id: 'ORDER2'});
  h.handlers['/api/capture-order'] = () => response({status: 'paid'});
  h.handlers['/api/booking/check'] = () => response({booking: {...baseBooking, status: 'paid', table: 2}});
  await h.callbacks[0].createOrder();
  await h.callbacks[0].onApprove({orderID: 'ORDER2'});
  assert.equal(h.id('step-done').hidden, false);
  assert.equal(h.id('done-table-label').textContent, '2');
  h.dom.window.close();
  console.log('Public booking DOM interaction checks passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
