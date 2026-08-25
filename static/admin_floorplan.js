let selectedNumber = null;

const statusEl = document.getElementById("picker-status");
const planInner = document.getElementById("plan-inner");
const planImage = document.getElementById("plan-image");
const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

document.querySelectorAll(".picker-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".picker-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedNumber = parseInt(btn.dataset.number, 10);
    statusEl.textContent =
      "Tisch " + selectedNumber + " ausgewählt – jetzt auf den Plan klicken, um die Position zu setzen.";
  });
});

// Wie nah (in Prozentpunkten der Bildbreite/-höhe) ein Klick an einen bereits
// platzierten Tisch heranreichen muss, damit der neue Tisch auf dessen
// X- bzw. Y-Position "einrastet". X und Y werden unabhängig voneinander
// geprüft, sodass sich Reihen und Spalten organisch aus den bereits
// gesetzten Tischen ergeben, ohne vorher eine feste Rastergröße festlegen
// zu müssen.
const SNAP_THRESHOLD = 1.5;

function getExistingPositions(excludeNumber) {
  const positions = [];
  planInner.querySelectorAll(".plan-marker").forEach((marker) => {
    const num = parseInt(marker.dataset.number, 10);
    if (num === excludeNumber) return;
    positions.push({
      number: num,
      x: parseFloat(marker.style.left),
      y: parseFloat(marker.style.top),
    });
  });
  return positions;
}

function snapToExisting(rawX, rawY, excludeNumber) {
  const positions = getExistingPositions(excludeNumber);
  let snappedX = null;
  let snappedY = null;

  positions.forEach((p) => {
    const dx = Math.abs(p.x - rawX);
    if (dx <= SNAP_THRESHOLD && (snappedX === null || dx < Math.abs(snappedX.x - rawX))) {
      snappedX = p;
    }
    const dy = Math.abs(p.y - rawY);
    if (dy <= SNAP_THRESHOLD && (snappedY === null || dy < Math.abs(snappedY.y - rawY))) {
      snappedY = p;
    }
  });

  return {
    x: snappedX ? snappedX.x : rawX,
    y: snappedY ? snappedY.y : rawY,
    snappedX: snappedX !== null,
    snappedY: snappedY !== null,
  };
}

if (planImage) {
  planImage.addEventListener("click", async (e) => {
    if (!selectedNumber) {
      statusEl.textContent = "Bitte zuerst links eine Tischnummer auswählen.";
      return;
    }
    const rect = planImage.getBoundingClientRect();
    const rawX = ((e.clientX - rect.left) / rect.width) * 100;
    const rawY = ((e.clientY - rect.top) / rect.height) * 100;
    const { x, y, snappedX, snappedY } = snapToExisting(rawX, rawY, selectedNumber);

    await fetch("/admin/api/set-position", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({ number: selectedNumber, x, y }),
    });

    upsertMarker(selectedNumber, x, y);
    document
      .querySelector('.picker-btn[data-number="' + selectedNumber + '"]')
      .classList.add("placed");

    let snapNote = "";
    if (snappedX && snappedY) snapNote = " (an bestehendem Tisch ausgerichtet)";
    else if (snappedX) snapNote = " (senkrecht ausgerichtet)";
    else if (snappedY) snapNote = " (waagerecht ausgerichtet)";
    statusEl.textContent = "Tisch " + selectedNumber + " platziert" + snapNote + ". Nächsten Tisch wählen oder fertig.";
  });
}

function upsertMarker(number, x, y) {
  let marker = planInner.querySelector('.plan-marker[data-number="' + number + '"]');
  if (!marker) {
    marker = document.createElement("div");
    marker.className = "plan-marker";
    marker.dataset.number = number;
    marker.textContent = number;
    planInner.appendChild(marker);
  }
  marker.style.left = x + "%";
  marker.style.top = y + "%";
}
