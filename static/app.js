'use strict';

const byId = id => document.getElementById(id);
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const config = {
  priceStandard: Number(document.body.dataset.priceStandard),
  priceInternal: Number(document.body.dataset.priceInternal),
  currency: document.body.dataset.currency,
};
const money = (amount, currency = config.currency) => new Intl.NumberFormat('de-DE', {
  style: 'currency', currency,
}).format(amount);
let selectedTable = null;
let booking = null;
let currentStep = null;
let tables = [];
let floorplan = null;
let tableView = 'plan';
let voucherValid = false;
let voucherGeneration = 0;
let voucherTimer;
let countdownTimer;
let clockOffset = 0;
let paypalGeneration = 0;
let paypalButtons = null;
let checking = false;
let submitting = false;
let capturing = false;

async function apiFetch(url, payload) {
  let response;
  try {
    response = await fetch(url, {
      cache: 'no-store',
      ...(payload === undefined ? {} : {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': csrfToken},
        body: JSON.stringify(payload),
      }),
    });
  } catch (_) {
    throw new Error('Die Verbindung wurde unterbrochen. Bitte prüfe deine Internetverbindung und versuche es erneut.');
  }
  const data = await response.json().catch(() => null);
  if (!response.ok || data === null) {
    const error = new Error(data?.error || 'Die Anfrage konnte nicht abgeschlossen werden. Bitte versuche es erneut.');
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function showStep(step, focus = true) {
  if (currentStep === 'pay' && step !== 'pay') {
    ++paypalGeneration;
    const previousButtons = paypalButtons;
    paypalButtons = null;
    if (previousButtons) {
      try { Promise.resolve(previousButtons.close()).catch(() => {}); } catch (_) { /* SDK already closed. */ }
    }
  }
  currentStep = step;
  document.querySelectorAll('main > section').forEach(section => {
    section.hidden = section.id !== 'step-' + step;
  });
  const progress = step === 'select' ? 'select' : step === 'form' ? 'form' : 'finish';
  document.querySelectorAll('.booking-progress li').forEach(item => {
    if (item.dataset.step === progress) item.setAttribute('aria-current', 'step');
    else item.removeAttribute('aria-current');
  });
  if (focus) {
    const heading = byId('step-' + step).querySelector('h2');
    heading.focus({preventScroll: true});
    heading.scrollIntoView({behavior: 'smooth', block: 'start'});
  }
}

function updateSummary() {
  const method = document.querySelector('input[name="payment_method"]:checked').value;
  byId('summary-standard').textContent = money(config.priceStandard);
  byId('summary-method').textContent = method === 'paypal' ? 'PayPal' : 'Überweisung';
  byId('summary-discount-row').hidden = !voucherValid;
  byId('summary-discount').textContent = money(config.priceStandard - config.priceInternal);
  byId('live-price-label').textContent = money(voucherValid ? config.priceInternal : config.priceStandard);
  byId('submit-booking').textContent = method === 'paypal' ? 'Weiter zu PayPal' : 'Kostenpflichtig reservieren';
}

const statusLabels = {free: 'frei', held: 'reserviert', booked: 'vergeben'};
function tableButton(table, isMarker) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = (isMarker ? 'plan-table-marker ' : 'public-table ') + table.status;
  const status = statusLabels[table.status] || 'nicht verfügbar';
  button.textContent = isMarker ? String(table.number) : `Tisch ${table.number}`;
  button.setAttribute('aria-label', `Tisch ${table.number}, ${status}`);
  button.title = `Tisch ${table.number}, ${status}`;
  button.disabled = table.status !== 'free';
  if (!isMarker) {
    const label = document.createElement('small');
    label.textContent = status;
    button.appendChild(label);
  }
  button.addEventListener('click', () => selectTable(table.number));
  return button;
}

function renderTables() {
  const count = tables.filter(table => table.status === 'free').length;
  byId('availability').textContent = count
    ? `${count} von ${tables.length} Tischen frei`
    : 'Aktuell sind alle Tische reserviert oder vergeben. Bitte schau später noch einmal vorbei.';
  const hasPlan = Boolean(floorplan?.image_url && floorplan.tables?.length);
  byId('table-view-switch').hidden = !hasPlan;
  const usePlan = hasPlan && tableView === 'plan';
  byId('grid').hidden = usePlan;
  byId('floorplan-view').hidden = !usePlan;
  byId('plan-help').hidden = !usePlan;
  ['plan', 'list'].forEach(view => {
    const active = view === (usePlan ? 'plan' : 'list');
    byId('view-' + view).setAttribute('aria-pressed', String(active));
    byId('view-' + view).classList.toggle('secondary', !active);
  });
  byId('grid').replaceChildren(...tables.map(table => tableButton(table, false)));
  byId('floorplan-inner').querySelectorAll('.plan-table-marker').forEach(marker => marker.remove());
  if (hasPlan) {
    byId('floorplan-image').src = floorplan.image_url;
    const lookup = new Map(tables.map(table => [table.number, table]));
    floorplan.tables.forEach(position => {
      const table = lookup.get(position.number);
      if (!table) return; // Missing server state must never imply availability.
      const marker = tableButton(table, true);
      marker.style.left = position.x + '%';
      marker.style.top = position.y + '%';
      byId('floorplan-inner').appendChild(marker);
    });
  }
}

async function refreshTables() {
  const button = byId('refresh-tables');
  button.disabled = true;
  byId('availability').textContent = 'Verfügbarkeit wird geladen …';
  byId('tables-error').textContent = '';
  try {
    tables = await apiFetch('/api/tables');
    try {
      floorplan = await apiFetch('/api/floorplan-config');
    } catch (_) {
      floorplan = null;
      byId('tables-error').textContent = 'Der Lageplan konnte nicht geladen werden. Du kannst einen Tisch aus der Liste auswählen.';
    }
    renderTables();
  } catch (error) {
    byId('tables-error').textContent = error.message;
    byId('availability').textContent = 'Die aktuelle Verfügbarkeit konnte nicht geladen werden.';
    // Do not keep stale clickable availability after a failed refresh.
    byId('grid').replaceChildren();
    byId('floorplan-view').hidden = true;
    byId('table-view-switch').hidden = true;
    byId('plan-help').hidden = true;
  } finally {
    button.disabled = false;
  }
}

function selectTable(number) {
  selectedTable = number;
  byId('selected-table-label').textContent = String(number);
  byId('form-error').textContent = '';
  byId('choose-another-table').hidden = true;
  updateSummary();
  showStep('form');
}
async function returnToSelection() {
  if (submitting) return;
  showStep('select');
  await refreshTables();
}

byId('view-plan').addEventListener('click', () => {tableView = 'plan'; renderTables();});
byId('view-list').addEventListener('click', () => {tableView = 'list'; renderTables();});
byId('refresh-tables').addEventListener('click', refreshTables);
byId('floorplan-image').addEventListener('error', () => {
  floorplan = null;
  renderTables();
  byId('tables-error').textContent = 'Das Planbild konnte nicht geladen werden. Bitte nutze die Tischliste.';
});
byId('back-btn').addEventListener('click', returnToSelection);
byId('choose-another-table').addEventListener('click', returnToSelection);
document.querySelectorAll('input[name="payment_method"]').forEach(input => input.addEventListener('change', updateSummary));

byId('voucher').addEventListener('input', () => {
  const generation = ++voucherGeneration;
  const code = byId('voucher').value.trim();
  clearTimeout(voucherTimer);
  voucherValid = false;
  updateSummary();
  const feedback = byId('voucher-feedback');
  feedback.className = 'hint';
  feedback.textContent = code ? 'Gutschein wird geprüft …' : '';
  if (!code) return;
  voucherTimer = setTimeout(async () => {
    try {
      const data = await apiFetch('/api/check-voucher?code=' + encodeURIComponent(code));
      if (generation !== voucherGeneration) return;
      voucherValid = data.valid;
      feedback.textContent = data.valid ? 'Gutschein gültig – Mitgliederrabatt wird angewendet.' : 'Dieser Gutscheincode ist ungültig oder bereits aufgebraucht.';
      feedback.className = data.valid ? 'hint success' : 'hint error-text';
    } catch (error) {
      if (generation !== voucherGeneration) return;
      feedback.textContent = error.message;
      feedback.className = 'hint error-text';
    }
    updateSummary();
  }, 400);
});

function setBooking(data) {
  booking = data;
  clockOffset = Date.parse(data.server_time) - Date.now();
  clearInterval(countdownTimer);
}
function showBooking(data, focus = true, renderPayment = true) {
  setBooking(data);
  const amount = money(data.price, data.currency);
  if (data.status === 'review') {
    byId('review-summary').textContent = `Tisch ${data.table} · ${amount}`;
    showStep('payment-review', focus);
  } else if (data.status === 'paid') {
    byId('done-table-label').textContent = String(data.table);
    byId('done-price').textContent = amount;
    byId('done-method').textContent = data.payment_method === 'paypal' ? 'PayPal' : 'Überweisung';
    showStep('done', focus);
  } else if (data.status !== 'pending' || Date.parse(data.expires_at) <= Date.now() + clockOffset) {
    byId('expired-table').textContent = String(data.table);
    showStep('expired', focus);
  } else if (data.payment_method === 'sepa') {
    byId('sepa-table-label').textContent = String(data.table);
    byId('sepa-price-label').textContent = amount;
    byId('sepa-reference-label').value = data.reference;
    byId('sepa-deadline-label').textContent = data.deadline;
    showStep('sepa-pending', focus);
  } else {
    byId('pay-table-label').textContent = String(data.table);
    byId('pay-price-label').textContent = amount;
    byId('pay-deadline').textContent = data.deadline;
    showStep('pay', focus);
    updateCountdown();
    countdownTimer = setInterval(updateCountdown, 1000);
    if (renderPayment) renderPaypalButtons();
  }
}
function updateCountdown() {
  if (!booking || currentStep !== 'pay') return;
  const seconds = Math.max(0, Math.ceil((Date.parse(booking.expires_at) - Date.now() - clockOffset) / 1000));
  byId('pay-countdown').textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
  if (seconds === 0) {
    clearInterval(countdownTimer);
    // A concurrent approval may still complete: do not discard its callback.
    byId('expired-table').textContent = String(booking.table);
    showStep('expired');
    if (!capturing) checkBooking();
  }
}

byId('reg-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting) return;
  submitting = true;
  byId('submit-booking').disabled = true;
  byId('back-btn').disabled = true;
  byId('submit-booking').textContent = 'Reservierung wird angelegt …';
  byId('form-error').textContent = '';
  byId('reg-form').setAttribute('aria-busy', 'true');
  try {
    const data = await apiFetch('/api/register', {
      name: byId('name').value,
      email: byId('email').value,
      phone: byId('phone').value,
      table: selectedTable,
      voucher: byId('voucher').value,
      payment_method: document.querySelector('input[name="payment_method"]:checked').value,
    });
    showBooking(data);
  } catch (error) {
    byId('form-error').textContent = error.message;
    byId('form-error').focus();
    if (error.status === 409) byId('choose-another-table').hidden = false;
  } finally {
    submitting = false;
    byId('submit-booking').disabled = false;
    byId('back-btn').disabled = false;
    byId('reg-form').removeAttribute('aria-busy');
    updateSummary();
  }
});

async function renderPaypalButtons() {
  const generation = ++paypalGeneration;
  const registrationId = booking.registration_id;
  const originalBooking = {...booking};
  if (paypalButtons) {
    try { await paypalButtons.close(); } catch (_) { /* Already closed by SDK. */ }
    paypalButtons = null;
  }
  if (generation !== paypalGeneration || currentStep !== 'pay') return;
  byId('paypal-button-container').replaceChildren();
  byId('reload-paypal').hidden = true;
  if (!window.paypal) {
    byId('pay-error').textContent = 'PayPal konnte nicht geladen werden. Bitte versuche es erneut. Deine Reservierung bleibt bis zur angegebenen Frist bestehen.';
    byId('reload-paypal').hidden = false;
    return;
  }
  byId('pay-error').textContent = '';
  try {
    paypalButtons = window.paypal.Buttons({
      createOrder: async () => {
        try {
          if (!booking || booking.registration_id !== registrationId || currentStep !== 'pay') {
            throw new Error('Bitte prüfe zuerst den aktuellen Buchungsstatus.');
          }
          const data = await apiFetch('/api/create-order', {registration_id: registrationId});
          return data.order_id;
        } catch (error) {
          byId('pay-error').textContent = error.message;
          await checkBooking();
          throw error;
        }
      },
      onApprove: async data => {
        await capturePayment(data.orderID, originalBooking);
      },
      onCancel: () => {
        byId('pay-error').textContent = 'Die PayPal-Zahlung wurde abgebrochen. Du kannst sie bis zum Ende der Reservierungsfrist erneut öffnen.';
      },
      onError: () => {
        byId('pay-error').textContent = 'Die Zahlung konnte nicht bestätigt werden. Bitte prüfe zuerst den Zahlungsstatus, bevor du es erneut versuchst.';
      },
    });
    await paypalButtons.render('#paypal-button-container');
  } catch (_) {
    byId('pay-error').textContent = 'PayPal konnte nicht geöffnet werden. Bitte prüfe den Zahlungsstatus oder lade PayPal erneut.';
    byId('reload-paypal').hidden = false;
  }
}

async function capturePayment(orderId, originalBooking) {
  if (capturing) return;
  capturing = true;
  byId('pay-error').textContent = 'Zahlung wird bestätigt …';
  try {
    await apiFetch('/api/capture-order', {order_id: orderId});
    const data = await apiFetch('/api/booking/check', {registration_id: originalBooking.registration_id});
    if (booking?.registration_id === originalBooking.registration_id) showBooking(data.booking);
  } catch (error) {
    if (booking?.registration_id !== originalBooking.registration_id) return;
    if (['payment_received_unallocated', 'payment_review'].includes(error.data?.status)) {
      showBooking({...originalBooking, status: 'review'});
      byId('payment-review-message').textContent = error.message;
    } else {
      const target = currentStep === 'expired' ? 'expired-error' : 'pay-error';
      byId(target).textContent = error.message + ' Bitte prüfe den Zahlungsstatus. Bezahle nicht erneut, solange der Status unklar ist.';
    }
  } finally { capturing = false; }
}

const checkButtons = ['retry-payment', 'check-sepa', 'check-review', 'check-expired'];
const errorTargets = {pay: 'pay-error', 'sepa-pending': 'sepa-error', 'payment-review': 'review-error', expired: 'expired-error'};
async function checkBooking() {
  if (!booking || checking || capturing) return;
  checking = true;
  const registrationId = booking.registration_id;
  const target = errorTargets[currentStep];
  checkButtons.forEach(id => {byId(id).disabled = true;});
  byId('paypal-button-container').inert = true;
  if (target) byId(target).textContent = 'Buchungsstatus wird geprüft …';
  try {
    const data = await apiFetch('/api/booking/check', {registration_id: registrationId});
    if (!booking || booking.registration_id !== registrationId) return;
    const previous = currentStep;
    showBooking(data.booking, false, previous !== 'pay');
    if (previous === currentStep && target) {
      byId(target).textContent = data.booking.status === 'pending' ? 'Es liegt noch keine Zahlungsbestätigung vor.' : '';
    }
  } catch (error) {
    if (target) byId(target).textContent = error.message;
  } finally {
    checking = false;
    checkButtons.forEach(id => {byId(id).disabled = false;});
    byId('paypal-button-container').inert = false;
  }
}
checkButtons.forEach(id => byId(id).addEventListener('click', checkBooking));

byId('reload-paypal').addEventListener('click', async () => {
  const button = byId('reload-paypal');
  button.disabled = true;
  try {
    if (!window.paypal) {
      const original = byId('paypal-sdk');
      if (!original) throw new Error('PayPal ist momentan nicht verfügbar. Bitte kontaktiere den Veranstalter.');
      await new Promise((resolve, reject) => {
        const script = document.createElement('script');
        const timeout = setTimeout(() => {script.remove(); reject(new Error('PayPal konnte nicht geladen werden. Bitte versuche es später erneut.'));}, 15000);
        script.src = original.dataset.sdkSrc;
        script.onload = () => {clearTimeout(timeout); resolve();};
        script.onerror = () => {clearTimeout(timeout); script.remove(); reject(new Error('PayPal konnte nicht geladen werden. Bitte prüfe deine Verbindung.'));};
        document.head.appendChild(script);
      });
    }
    await renderPaypalButtons();
  } catch (error) { byId('pay-error').textContent = error.message; }
  finally { button.disabled = false; }
});
const sdk = byId('paypal-sdk');
if (sdk) sdk.addEventListener('load', () => {if (currentStep === 'pay') renderPaypalButtons();});

byId('copy-reference').addEventListener('click', async () => {
  const input = byId('sepa-reference-label');
  try {
    await navigator.clipboard.writeText(input.value);
    byId('copy-status').textContent = 'Verwendungszweck kopiert.';
  } catch (_) {
    input.focus(); input.select();
    byId('copy-status').textContent = 'Bitte kopiere den markierten Verwendungszweck über das Kopiermenü deines Geräts.';
  }
});
document.querySelectorAll('[data-new-booking]').forEach(button => button.addEventListener('click', async () => {
  if (capturing || checking) return;
  booking = null;
  clearInterval(countdownTimer);
  ++paypalGeneration;
  await returnToSelection();
}));

async function start() {
  byId('startup-status').hidden = false;
  byId('startup-error').hidden = true;
  byId('retry-startup').disabled = true;
  try {
    const data = await apiFetch('/api/booking');
    if (data.booking) {
      showBooking(data.booking, false);
      if (data.booking.has_order && data.booking.status !== 'paid') await checkBooking();
    } else {
      showStep('select', false);
      await refreshTables();
    }
  } catch (_) {
    byId('startup-error').hidden = false;
  } finally {
    byId('startup-status').hidden = true;
    byId('retry-startup').disabled = false;
  }
}
byId('retry-startup').addEventListener('click', start);
updateSummary();
start();
