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

// Wie nah (in Prozentpunkten der Bildhöhe) ein Klick an die Y-Position eines
// bereits platzierten Tisches heranreichen muss, damit der neue Tisch auf
// dessen Höhe "einrastet" (waagerechte Ausrichtung/Reihenbildung). Nur die
// Y-Achse wird geprüft; die X-Position übernimmt immer exakt den Klickpunkt.
const SNAP_THRESHOLD = 1.5;

function getExistingPositions(excludeNumber) {
  const positions = [];
  planInner.querySelectorAll(".plan-marker").forEach((marker) => {
    const num = parseInt(marker.dataset.number, 10);
    if (num === excludeNumber) return;
    positions.push({
      number: num,
      y: parseFloat(marker.style.top),
    });
  });
  return positions;
}

function snapToExisting(rawY, excludeNumber) {
  const positions = getExistingPositions(excludeNumber);
  let snappedY = null;

  positions.forEach((p) => {
    const dy = Math.abs(p.y - rawY);
    if (dy <= SNAP_THRESHOLD && (snappedY === null || dy < Math.abs(snappedY.y - rawY))) {
      snappedY = p;
    }
  });

  return {
    y: snappedY ? snappedY.y : rawY,
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
    const x = ((e.clientX - rect.left) / rect.width) * 100;
    const rawY = ((e.clientY - rect.top) / rect.height) * 100;
    const { y, snappedY } = snapToExisting(rawY, selectedNumber);

    await fetch("/admin/api/set-position", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrfToken },
      body: JSON.stringify({ number: selectedNumber, x, y }),
    });

    upsertMarker(selectedNumber, x, y);
    document
      .querySelector('.picker-btn[data-number="' + selectedNumber + '"]')
      .classList.add("placed");

    const snapNote = snappedY ? " (waagerecht ausgerichtet)" : "";
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
