let selectedTable = null;
let registrationId = null;
let floorplanConfig = null; // { image_url, tables: [{number, x, y}] } or null

let pendingOrderId = null;
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

async function apiFetch(url, payload) {
  const options = payload === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
    body: JSON.stringify(payload),
  };
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || "Die Anfrage ist fehlgeschlagen. Bitte versuche es erneut.");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

const APP_CONFIG = {
  hasPaypalClientId: document.body.dataset.hasPaypal === "true",
  priceStandard: parseFloat(document.body.dataset.priceStandard),
  priceInternal: parseFloat(document.body.dataset.priceInternal),
  currency: document.body.dataset.currency,
};

const gridEl = document.getElementById("grid");
const floorplanView = document.getElementById("floorplan-view");
const floorplanInner = document.getElementById("floorplan-inner");
const floorplanImage = document.getElementById("floorplan-image");
const stepSelect = document.getElementById("step-select");
const stepForm = document.getElementById("step-form");
const stepPay = document.getElementById("step-pay");
const stepSepaPending = document.getElementById("step-sepa-pending");
const stepDone = document.getElementById("step-done");

async function loadFloorplanConfig() {
  if (floorplanConfig !== null) return floorplanConfig;
  floorplanConfig = await apiFetch("/api/floorplan-config");
  return floorplanConfig;
}

async function loadTables() {
  const [tables, config] = await Promise.all([apiFetch("/api/tables"), loadFloorplanConfig()]);
  const statusByNumber = {};
  tables.forEach((t) => (statusByNumber[t.number] = t.status));

  if (config.image_url && config.tables.length > 0) {
    renderFloorplan(config, statusByNumber);
  } else {
    renderGrid(tables);
  }
}

function renderGrid(tables) {
  floorplanView.hidden = true;
  gridEl.hidden = false;
  gridEl.innerHTML = "";
  tables.forEach((t) => {
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "table-cell " + t.status;
    cell.textContent = t.number;
    cell.disabled = t.status !== "free";
    cell.addEventListener("click", () => selectTable(t.number));
    gridEl.appendChild(cell);
  });
}

function renderFloorplan(config, statusByNumber) {
  gridEl.hidden = true;
  floorplanView.hidden = false;
  floorplanImage.src = config.image_url;

  floorplanView.querySelectorAll(".plan-table-marker").forEach((el) => el.remove());

  config.tables.forEach((t) => {
    const status = statusByNumber[t.number] || "free";
    const marker = document.createElement("button");
    marker.type = "button";
    marker.className = "plan-table-marker " + status;
    marker.style.left = t.x + "%";
    marker.style.top = t.y + "%";
    marker.textContent = t.number;
    marker.disabled = status !== "free";
    marker.title = "Tisch " + t.number + (status === "free" ? "" : " (nicht verfügbar)");
    marker.addEventListener("click", () => selectTable(t.number));
    floorplanInner.appendChild(marker);
  });
}

function selectTable(number) {
  selectedTable = number;
  document.getElementById("selected-table-label").textContent = "Tisch " + number;
  document.getElementById("form-error").textContent = "";
  document.getElementById("voucher").value = "";
  document.getElementById("voucher-feedback").textContent = "";
  updateLivePrice(false);
  stepSelect.hidden = true;
  stepForm.hidden = false;
}

function formatPrice(amount) {
  return amount.toFixed(2).replace(".", ",") + " €";
}

function updateLivePrice(voucherValid) {
  const cfg = APP_CONFIG;
  const amount = voucherValid ? cfg.priceInternal : cfg.priceStandard;
  document.getElementById("live-price-label").textContent =
    formatPrice(amount) + (voucherValid ? " (Mitgliederrabatt)" : "");
}

let voucherCheckTimer = null;
document.getElementById("voucher").addEventListener("input", (e) => {
  const feedback = document.getElementById("voucher-feedback");
  const code = e.target.value.trim();
  clearTimeout(voucherCheckTimer);

  if (!code) {
    feedback.textContent = "";
    feedback.className = "hint";
    updateLivePrice(false);
    return;
  }

  feedback.textContent = "Wird geprüft …";
  feedback.className = "hint";

  voucherCheckTimer = setTimeout(async () => {
    try {
      const data = await apiFetch("/api/check-voucher?code=" + encodeURIComponent(code));
      if (document.getElementById("voucher").value.trim() !== code) return;
      if (data.valid) {
        feedback.textContent = "Gutschein gültig – Mitgliederrabatt wird angewendet.";
        feedback.className = "hint success";
        updateLivePrice(true);
      } else {
        feedback.textContent = "Dieser Gutscheincode ist ungültig oder bereits aufgebraucht.";
        feedback.className = "hint error-text";
        updateLivePrice(false);
      }
    } catch (error) {
      feedback.textContent = error.message;
      feedback.className = "hint error-text";
      updateLivePrice(false);
    }
  }, 400);
});

document.getElementById("back-btn").addEventListener("click", () => {
  stepForm.hidden = true;
  stepSelect.hidden = false;
  refreshTables();
});

document.getElementById("reg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("form-error");
  errorEl.textContent = "";

  // A single enabled payment method is rendered as a hidden input.
  const paymentMethodInput =
    document.querySelector('input[name="payment_method"]:checked') ||
    document.querySelector('input[name="payment_method"]');
  const paymentMethod = paymentMethodInput ? paymentMethodInput.value : "paypal";

  const payload = {
    name: document.getElementById("name").value,
    email: document.getElementById("email").value,
    phone: document.getElementById("phone").value,
    table: selectedTable,
    voucher: document.getElementById("voucher").value,
    payment_method: paymentMethod,
  };

  const submitButton = e.target.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  try {
    const data = await apiFetch("/api/register", payload);

    registrationId = data.registration_id;
    stepForm.hidden = true;

    if (data.payment_method === "sepa") {
      document.getElementById("sepa-table-label").textContent = "Tisch " + data.table;
      document.getElementById("sepa-price-label").textContent =
        data.price.toFixed(2).replace(".", ",") + " €" + (data.voucher_applied ? " (Mitgliederrabatt)" : "");
      document.getElementById("sepa-reference-label").textContent = data.reference;
      document.getElementById("sepa-deadline-label").textContent = data.deadline;
      stepSepaPending.hidden = false;
    } else {
      document.getElementById("pay-table-label").textContent = "Tisch " + data.table;
      document.getElementById("pay-price-label").textContent =
        data.price.toFixed(2).replace(".", ",") + " €" + (data.voucher_applied ? " (Mitgliederrabatt)" : "");
      stepPay.hidden = false;
      renderPaypalButtons();
    }
  } catch (error) {
    errorEl.textContent = error.message;
    if (error.status === 409) refreshTables();
  } finally {
    submitButton.disabled = false;
  }
});

function renderPaypalButtons() {
  const container = document.getElementById("paypal-button-container");
  container.innerHTML = "";
  const payError = document.getElementById("pay-error");

  if (!window.paypal) {
    payError.textContent =
      "PayPal-Buttons konnten nicht geladen werden. Ist PAYPAL_CLIENT_ID gesetzt?";
    return;
  }

  paypal.Buttons({
    createOrder: async () => {
      const data = await apiFetch("/api/create-order", { registration_id: registrationId });
      return data.order_id;
    },
    onApprove: async (data) => {
      pendingOrderId = data.orderID;
      await capturePayment();
    },
    onError: (err) => {
      console.error(err);
      payError.textContent = err.message || "Es gab ein Problem bei der Zahlung. Bitte erneut versuchen.";
    },
  }).render("#paypal-button-container");
}

async function capturePayment() {
  const retry = document.getElementById("retry-payment");
  const errorEl = document.getElementById("pay-error");
  retry.hidden = true;
  retry.disabled = true;
  errorEl.textContent = "";
  try {
    await apiFetch("/api/capture-order", { order_id: pendingOrderId });
    document.getElementById("done-table-label").textContent = document.getElementById("pay-table-label").textContent;
    stepPay.hidden = true;
    stepDone.hidden = false;
  } catch (error) {
    if (["payment_received_unallocated", "payment_review"].includes(error.data?.status)) {
      stepPay.hidden = true;
      document.getElementById("payment-review-message").textContent = error.message;
      document.getElementById("step-payment-review").hidden = false;
    } else {
      errorEl.textContent = error.message;
      retry.hidden = false;
    }
  } finally {
    retry.disabled = false;
  }
}

document.getElementById("retry-payment").addEventListener("click", capturePayment);

async function refreshTables() {
  const errorEl = document.getElementById("tables-error");
  try {
    await loadTables();
    errorEl.textContent = "";
  } catch (error) {
    errorEl.textContent = error.message;
  }
}

refreshTables();
