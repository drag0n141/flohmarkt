const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

// These tests exercise UI state and request construction, not browser rendering.
function harness(respond) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      hidden: ['step-form', 'step-pay', 'step-done', 'step-sepa-pending',
        'step-payment-review', 'retry-payment'].includes(id),
      value: '', textContent: '', innerHTML: '', disabled: false,
      style: {}, dataset: {}, listeners: {},
      classList: { add() {}, remove() {} },
      addEventListener(event, fn) { this.listeners[event] = fn; },
      appendChild() {}, querySelectorAll() { return []; },
      querySelector() { return element('submit'); },
    });
    return elements.get(id);
  }
  let callbacks;
  const calls = [];
  const context = vm.createContext({
    console, setTimeout, clearTimeout,
    document: {
      body: { dataset: { hasPaypal: 'true', priceStandard: '15', priceInternal: '10', currency: 'EUR' } },
      getElementById: element,
      createElement: () => element('created'),
      querySelector: selector => selector.includes('csrf-token') ? { content: 'test-csrf-token' } : { value: 'paypal' },
    },
    window: { paypal: true },
    paypal: { Buttons(value) { callbacks = value; return { render() {} }; } },
    fetch: async (url, options) => {
      calls.push({url, options});
      const result = respond(url, options) || {
        status: 200, body: url === '/api/tables' ? [] : { image_url: null, tables: [] },
      };
      return { ok: result.status < 400, status: result.status, json: async () => result.body };
    },
  });
  vm.runInContext(readFileSync(path.join(__dirname, '../static/app.js'), 'utf8'), context);
  async function register() {
    vm.runInContext('selectedTable = 1', context);
    element('name').value = 'Test';
    element('email').value = 'test@example.org';
    await element('reg-form').listeners.submit({ preventDefault() {}, target: element('reg-form') });
  }
  return { element, calls, register, approve: () => callbacks.onApprove({ orderID: 'ORDER123' }) };
}

const registered = { status: 200, body: { registration_id: 1, table: 1, price: 15, payment_method: 'paypal' } };

test('JSON mutations include CSRF and a failed capture does not display success', async () => {
  const ui = harness(url => url === '/api/register' ? registered :
    url === '/api/capture-order' ? { status: 503, body: { error: 'Retry later' } } : null);
  await ui.register();
  await ui.approve();
  assert.equal(ui.element('step-done').hidden, true);
  assert.equal(ui.element('retry-payment').hidden, false);
  assert.equal(ui.element('pay-error').textContent, 'Retry later');
  for (const call of ui.calls.filter(call => call.options.method === 'POST')) {
    assert.equal(call.options.headers['X-CSRFToken'], 'test-csrf-token');
    assert.equal(call.options.headers['Content-Type'], 'application/json');
  }
});

test('retrying a capture displays success only after the server confirms booking', async () => {
  let captures = 0;
  const ui = harness(url => url === '/api/register' ? registered :
    url === '/api/capture-order' ? (++captures === 1 ? { status: 503, body: {error:'Retry'} } :
      { status: 200, body: {status:'paid', booking_status:'booked'} }) : null);
  await ui.register();
  await ui.approve();
  await ui.element('retry-payment').listeners.click();
  assert.equal(ui.element('step-done').hidden, false);
  assert.equal(ui.element('step-pay').hidden, true);
  assert.equal(ui.element('retry-payment').disabled, false);
});

for (const status of ['payment_received_unallocated', 'payment_review']) {
  test(`${status} shows review instead of a successful booking`, async () => {
    const ui = harness(url => url === '/api/register' ? registered :
      url === '/api/capture-order' ? { status: 409, body: {status, error:'Contact organizer'} } : null);
    await ui.register();
    await ui.approve();
    assert.equal(ui.element('step-payment-review').hidden, false);
    assert.equal(ui.element('step-done').hidden, true);
    assert.equal(ui.element('step-pay').hidden, true);
    assert.equal(ui.element('payment-review-message').textContent, 'Contact organizer');
  });
}

test('SEPA immediately displays the supplied reference and deadline', async () => {
  const ui = harness(url => url === '/api/register' ? { status:200, body:{
    registration_id:1, table:1, price:15, payment_method:'sepa',
    reference:'FLOHMARKT-1', deadline:'14.09.2026 um 12:00 Uhr',
  }} : null);
  await ui.register();
  assert.equal(ui.element('step-sepa-pending').hidden, false);
  assert.equal(ui.element('sepa-reference-label').textContent, 'FLOHMARKT-1');
  assert.equal(ui.element('step-done').hidden, true);
});

test('failed registration restores the submit button and shows the error', async () => {
  const ui = harness(url => url === '/api/register' ? {status:409, body:{error:'Table unavailable'}} : null);
  await ui.register();
  assert.equal(ui.element('submit').disabled, false);
  assert.equal(ui.element('form-error').textContent, 'Table unavailable');
  assert.equal(ui.element('step-pay').hidden, true);
});
