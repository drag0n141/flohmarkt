let selectedTable = null;
let registrationId = null;
let floorplanConfig = null; // { image_url, tables: [{number, x, y}] } oder null

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
const stepDone = document.getElementById("step-done");

async function loadFloorplanConfig() {
  if (floorplanConfig !== null) return floorplanConfig;
  const res = await fetch("/api/floorplan-config");
  floorplanConfig = await res.json();
  return floorplanConfig;
}

async function loadTables() {
  const [tablesRes, config] = await Promise.all([fetch("/api/tables"), loadFloorplanConfig()]);
  const tables = await tablesRes.json();
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
    const res = await fetch("/api/check-voucher?code=" + encodeURIComponent(code));
    const data = await res.json();
    if (data.valid) {
      feedback.textContent = "Gutschein gültig – Mitgliederrabatt wird angewendet.";
      feedback.className = "hint success";
      updateLivePrice(true);
    } else {
      feedback.textContent = "Dieser Gutscheincode ist ungültig oder bereits aufgebraucht.";
      feedback.className = "hint error-text";
      updateLivePrice(false);
    }
  }, 400);
});

document.getElementById("back-btn").addEventListener("click", () => {
  stepForm.hidden = true;
  stepSelect.hidden = false;
  loadTables();
});

document.getElementById("reg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const errorEl = document.getElementById("form-error");
  errorEl.textContent = "";

  const payload = {
    name: document.getElementById("name").value,
    email: document.getElementById("email").value,
    phone: document.getElementById("phone").value,
    table: selectedTable,
    voucher: document.getElementById("voucher").value,
  };

  const res = await fetch("/api/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();

  if (!res.ok) {
    errorEl.textContent = data.error || "Ein Fehler ist aufgetreten.";
    if (res.status === 409) loadTables(); // Tisch war schon weg -> Grid aktualisieren
    return;
  }

  registrationId = data.registration_id;
  document.getElementById("pay-table-label").textContent = "Tisch " + data.table;
  document.getElementById("pay-price-label").textContent =
    data.price.toFixed(2).replace(".", ",") + " €" + (data.voucher_applied ? " (Mitgliederrabatt)" : "");
  stepForm.hidden = true;
  stepPay.hidden = false;
  renderPaypalButtons();
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
      const res = await fetch("/api/create-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ registration_id: registrationId }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Bestellung fehlgeschlagen");
      return data.order_id;
    },
    onApprove: async (data) => {
      const res = await fetch("/api/capture-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order_id: data.orderID }),
      });
      const result = await res.json();
      if (!res.ok) {
        payError.textContent = result.error || "Zahlung konnte nicht abgeschlossen werden.";
        return;
      }
      document.getElementById("done-table-label").textContent = document.getElementById(
        "pay-table-label"
      ).textContent;
      stepPay.hidden = true;
      stepDone.hidden = false;
    },
    onError: (err) => {
      console.error(err);
      payError.textContent = "Es gab ein Problem bei der Zahlung. Bitte erneut versuchen.";
    },
  }).render("#paypal-button-container");
}

loadTables();
