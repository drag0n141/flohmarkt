let selectedNumber = null;

const statusEl = document.getElementById("picker-status");
const planContainer = document.getElementById("plan-container");
const planImage = document.getElementById("plan-image");

document.querySelectorAll(".picker-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".picker-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    selectedNumber = parseInt(btn.dataset.number, 10);
    statusEl.textContent =
      "Tisch " + selectedNumber + " ausgewählt – jetzt auf den Plan klicken, um die Position zu setzen.";
  });
});

if (planImage) {
  planImage.addEventListener("click", async (e) => {
    if (!selectedNumber) {
      statusEl.textContent = "Bitte zuerst links eine Tischnummer auswählen.";
      return;
    }
    const rect = planImage.getBoundingClientRect();
    const x = ((e.clientX - rect.left) / rect.width) * 100;
    const y = ((e.clientY - rect.top) / rect.height) * 100;

    await fetch("/admin/api/set-position", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ number: selectedNumber, x, y }),
    });

    upsertMarker(selectedNumber, x, y);
    document
      .querySelector('.picker-btn[data-number="' + selectedNumber + '"]')
      .classList.add("placed");
    statusEl.textContent = "Tisch " + selectedNumber + " platziert. Nächsten Tisch wählen oder fertig.";
  });
}

function upsertMarker(number, x, y) {
  let marker = planContainer.querySelector('.plan-marker[data-number="' + number + '"]');
  if (!marker) {
    marker = document.createElement("div");
    marker.className = "plan-marker";
    marker.dataset.number = number;
    marker.textContent = number;
    planContainer.appendChild(marker);
  }
  marker.style.left = x + "%";
  marker.style.top = y + "%";
}
