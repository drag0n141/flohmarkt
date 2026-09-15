const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

// Dependency-free checks of state transitions and authenticated request construction.
function harness(respond) {
  const elements = new Map();
  const stepNames = ['select', 'form', 'pay', 'done', 'sepa-pending', 'payment-review', 'expired'];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      id, hidden: true, value: '', textContent: '', disabled: false,
      style: {}, dataset: {}, listeners: {},
      classList: {add() {}, remove() {}, toggle() {}},
      addEventListener(event, fn) {this.listeners[event] = fn;},
      appendChild() {}, replaceChildren() {}, querySelectorAll() {return [];},
      querySelector() {return element(id+'-heading');},
      setAttribute() {}, removeAttribute() {}, focus() {}, scrollIntoView() {},
    });
    return elements.get(id);
  }
  let callbacks;
  const calls = [];
  const context = vm.createContext({
    console, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    matchMedia: () => ({matches: false}),
    document: {
      body: {dataset: {priceStandard: '15', priceInternal: '10', currency: 'EUR'}},
      getElementById: element,
      createElement: () => element('created'),
      querySelector: selector => selector.includes('csrf-token') ? {content: 'test-csrf-token'} : {value: 'paypal'},
      querySelectorAll: selector => selector === 'main > section' ? stepNames.map(step => element('step-'+step)) : [],
    },
    window: {paypal: {Buttons(value) {callbacks = value; return {render: async () => {}, close: async () => {}};}}},
    fetch: async (url, options) => {
      calls.push({url, options});
      const result = respond(url, options) || {
        status: 200, body: url === '/api/booking' ? {booking: null} : url === '/api/tables' ? [] : {image_url: null, tables: []},
      };
      return {ok: result.status < 400, status: result.status, json: async () => result.body};
    },
  });
  vm.runInContext(readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  async function register() {
    await new Promise(resolve => setImmediate(resolve)); // Finish initial session recovery.
    vm.runInContext('selectedTable = 1', context);
    element('name').value = 'Test';
    element('email').value = 'test@example.org';
    await element('reg-form').listeners.submit({preventDefault() {}});
    await new Promise(resolve => setImmediate(resolve));
  }
  return {element, calls, register, approve: () => callbacks.onApprove({orderID: 'ORDER123'})};
}
const booking = {
  registration_id: 1, table: 1, price: 15, currency: 'EUR', payment_method: 'paypal', status: 'pending',
  server_time: new Date().toISOString(), expires_at: new Date(Date.now()+600000).toISOString(), deadline: '14.09.2026 um 12:00 Uhr',
};
const registered = {status: 200, body: booking};

test('JSON mutations include CSRF and a failed capture does not display success', async () => {
  const ui = harness(url => url === '/api/register' ? registered :
    url === '/api/capture-order' ? {status: 503, body: {error: 'Retry later'}} : null);
  await ui.register();
  await ui.approve();
  assert.equal(ui.element('step-done').hidden, true);
  assert.match(ui.element('pay-error').textContent, /Retry later/);
  for (const call of ui.calls.filter(call => call.options.method === 'POST')) {
    assert.equal(call.options.headers['X-CSRFToken'], 'test-csrf-token');
    assert.equal(call.options.headers['Content-Type'], 'application/json');
  }
});

test('retry checks the existing payment without repeating capture', async () => {
  let captures = 0;
  const ui = harness(url => url === '/api/register' ? registered :
    url === '/api/capture-order' ? (++captures, {status: 503, body: {error: 'Retry'}}) :
    url === '/api/booking/check' ? {status: 200, body: {booking: {...booking, status: 'paid'}}} : null);
  await ui.register();
  await ui.approve();
  await ui.element('retry-payment').listeners.click();
  assert.equal(captures, 1);
  assert.equal(ui.element('step-done').hidden, false);
  assert.equal(ui.element('step-pay').hidden, true);
  assert.equal(ui.element('retry-payment').disabled, false);
});

for (const status of ['payment_received_unallocated', 'payment_review']) {
  test(`${status} shows review instead of a successful booking`, async () => {
    const ui = harness(url => url === '/api/register' ? registered :
      url === '/api/capture-order' ? {status: 409, body: {status, error: 'Contact organizer'}} : null);
    await ui.register();
    await ui.approve();
    assert.equal(ui.element('step-payment-review').hidden, false);
    assert.equal(ui.element('step-done').hidden, true);
    assert.equal(ui.element('step-pay').hidden, true);
    assert.equal(ui.element('payment-review-message').textContent, 'Contact organizer');
  });
}

test('SEPA immediately displays the supplied reference and deadline', async () => {
  const ui = harness(url => url === '/api/register' ? {status: 200, body: {
    ...booking, payment_method: 'sepa', reference: 'FLOHMARKT-1',
  }} : null);
  await ui.register();
  assert.equal(ui.element('step-sepa-pending').hidden, false);
  assert.equal(ui.element('sepa-reference-label').value, 'FLOHMARKT-1');
  assert.equal(ui.element('sepa-deadline-label').textContent, booking.deadline);
  assert.equal(ui.element('step-done').hidden, true);
});

test('failed registration restores submit and offers another table', async () => {
  const ui = harness(url => url === '/api/register' ? {status: 409, body: {error: 'Table unavailable'}} : null);
  await ui.register();
  assert.equal(ui.element('submit-booking').disabled, false);
  assert.equal(ui.element('form-error').textContent, 'Table unavailable');
  assert.equal(ui.element('choose-another-table').hidden, false);
  assert.equal(ui.element('step-pay').hidden, true);
});
