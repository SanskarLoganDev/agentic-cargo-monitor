document.addEventListener("DOMContentLoaded", () => {
  const tempRange = document.querySelector("[data-temp-range]");
  const tempDisplay = document.querySelector("[data-temp-display]");
  const shockRange = document.querySelector("[data-shock-range]");
  const shockDisplay = document.querySelector("[data-shock-display]");
  const humidityRange = document.querySelector("[data-humidity-range]");
  const humidityBubble = document.querySelector("[data-humidity-bubble]");
  const pendingApprovalsCountEl = document.querySelector("[data-pending-approvals-count]");
  const flightStatus = document.querySelector("[data-flight-status]");
  const saveUpdatesBtn = document.querySelector("[data-save-updates]");
  const approveBtn = document.getElementById("approveBtn");
  const escalateBtn = document.getElementById("escalateBtn");
  const approvalStatus = document.getElementById("approvalStatus");
  const pendingUpdates = {};

  async function refreshPendingApprovalsCount() {
    if (!pendingApprovalsCountEl) return;

    try {
      const response = await fetch("/api/pending-approvals/count", { cache: "no-store" });
      if (!response.ok) throw new Error("Count fetch failed");

      const result = await response.json();
      pendingApprovalsCountEl.textContent = Number.isFinite(result?.count) ? String(result.count) : "0";
    } catch {
      // Keep the currently displayed value if polling fails.
    }
  }

  function markDirty(field, value) {
    pendingUpdates[field] = value;
    if (saveUpdatesBtn) {
      saveUpdatesBtn.disabled = false;
      saveUpdatesBtn.classList.add("is-dirty");
      saveUpdatesBtn.textContent = "Save Updates";
    }
  }

  function clearDirtyState() {
    Object.keys(pendingUpdates).forEach((key) => delete pendingUpdates[key]);
    if (saveUpdatesBtn) {
      saveUpdatesBtn.disabled = true;
      saveUpdatesBtn.classList.remove("is-dirty");
      saveUpdatesBtn.textContent = "Saved";
      setTimeout(() => {
        if (saveUpdatesBtn) saveUpdatesBtn.textContent = "Save Updates";
      }, 2000);
    }
  }

  function updateTemp() {
    if (!tempRange || !tempDisplay) return;
    const value = parseFloat(tempRange.value);
    const min = parseFloat(tempRange.min);
    const max = parseFloat(tempRange.max);
    const pct = ((value - min) / (max - min)) * 100;
    tempDisplay.textContent = `${value.toFixed(1)}\u00B0C`;
    tempRange.style.background =
      `linear-gradient(90deg, #77e9ff 0%, #77e9ff ${pct}%, rgba(255,255,255,0.12) ${pct}%, rgba(255,255,255,0.12) 100%)`;
    markDirty("temperature_celsius", value);
  }

  function updateShock() {
    if (!shockRange || !shockDisplay) return;
    const value = parseFloat(shockRange.value);
    const min = parseFloat(shockRange.min);
    const max = parseFloat(shockRange.max);
    const pct = ((value - min) / (max - min)) * 100;
    shockDisplay.textContent = `${value.toFixed(2)}G`;
    shockRange.style.background =
      `linear-gradient(90deg, #77e9ff 0%, #77e9ff ${pct}%, rgba(255,255,255,0.12) ${pct}%, rgba(255,255,255,0.12) 100%)`;
    markDirty("shock_g", value);
  }

  function updateHumidity() {
    if (!humidityRange || !humidityBubble) return;
    const min = parseFloat(humidityRange.min);
    const max = parseFloat(humidityRange.max);
    const value = parseFloat(humidityRange.value);
    const percent = ((value - min) / (max - min)) * 100;
    humidityBubble.textContent = `${Math.round(value)}%`;
    humidityBubble.style.left = `${percent}%`;
    humidityRange.style.background =
      `linear-gradient(90deg, #77e9ff 0%, #77e9ff ${percent}%, rgba(255,255,255,0.12) ${percent}%, rgba(255,255,255,0.12) 100%)`;
    markDirty("humidity_percent", value);
  }

  if (tempRange) {
    updateTemp();
    tempRange.addEventListener("input", updateTemp);
  }

  if (shockRange) {
    updateShock();
    shockRange.addEventListener("input", updateShock);
  }

  if (humidityRange) {
    updateHumidity();
    humidityRange.addEventListener("input", updateHumidity);
  }

  // ── Telemetry Live Charts ──────────────────────────────────────────
  const MAX_POINTS = 30;

  function makeChartData(color) {
    return {
      labels: Array(MAX_POINTS).fill(""),
      datasets: [{
        data: Array(MAX_POINTS).fill(null),
        borderColor: color,
        backgroundColor: color.replace(")", ", 0.08)").replace("rgb", "rgba"),
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.4,
        fill: true
      }]
    };
  }

  const chartOptions = {
    responsive: true,
    animation: { duration: 600, easing: "easeInOutQuart" },
    plugins: { legend: { display: false }, tooltip: { enabled: false } },
    scales: {
      x: { display: false },
      y: {
        display: true,
        grid: { color: "rgba(255,255,255,0.06)" },
        ticks: { color: "rgba(255,255,255,0.4)", font: { size: 10 }, maxTicksLimit: 4 }
      }
    }
  };

  const tempChartEl = document.getElementById("tempChart");
  const humidityChartEl = document.getElementById("humidityChart");

  let tempChart = null;
  let humidityChart = null;

  if (tempChartEl) {
    tempChart = new Chart(tempChartEl, {
      type: "line",
      data: makeChartData("rgb(119,233,255)"),
      options: JSON.parse(JSON.stringify(chartOptions))
    });
  }

  if (humidityChartEl) {
    humidityChart = new Chart(humidityChartEl, {
      type: "line",
      data: makeChartData("rgb(167,139,250)"),
      options: JSON.parse(JSON.stringify(chartOptions))
    });
  }

  function pushPoint(chart, value) {
    if (!chart) return;
    chart.data.labels.push("");
    chart.data.labels.shift();
    chart.data.datasets[0].data.push(value);
    chart.data.datasets[0].data.shift();
    chart.update("none");
  }

  // Feed chart from slider values every 0.8 seconds
  setInterval(() => {
    const tVal = tempRange ? parseFloat(tempRange.value) : null;
    const hVal = humidityRange ? parseFloat(humidityRange.value) : null;
    if (tVal !== null) pushPoint(tempChart, tVal);
    if (hVal !== null) pushPoint(humidityChart, hVal);
  }, 800);

  // Also push a point whenever sliders change manually
  if (tempRange) tempRange.addEventListener("input", () => pushPoint(tempChart, parseFloat(tempRange.value)));
  if (humidityRange) humidityRange.addEventListener("input", () => pushPoint(humidityChart, parseFloat(humidityRange.value)));
  // ──────────────────────────────────────────────────────────────────

  if (flightStatus) {
    flightStatus.addEventListener("change", () => {
      markDirty("flight_delay_status", flightStatus.value);
    });
  }

  if (saveUpdatesBtn) {
    saveUpdatesBtn.addEventListener("click", async () => {
      const medicineKey = saveUpdatesBtn.dataset.medicineKey;
      if (!Object.keys(pendingUpdates).length) return;

      saveUpdatesBtn.disabled = true;
      saveUpdatesBtn.textContent = "Saving...";

      try {
        const response = await fetch(`/api/shipment/${medicineKey}/save-updates`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ updates: { ...pendingUpdates } })
        });

        const result = await response.json();
        if (!response.ok) throw new Error(result.message || "Save failed");
        clearDirtyState();
      } catch {
        saveUpdatesBtn.disabled = false;
        saveUpdatesBtn.classList.add("is-dirty");
        saveUpdatesBtn.textContent = "Retry Save";
      }
    });
  }

  if (approveBtn && saveUpdatesBtn) {
    approveBtn.addEventListener("click", async () => {
      const medicineKey = saveUpdatesBtn.dataset.medicineKey;
      if (approvalStatus) approvalStatus.textContent = "Submitting approval...";

      try {
        const response = await fetch(`/api/approval/${medicineKey}/approve`, { method: "POST" });
        const result = await response.json();
        if (!response.ok) throw new Error(result.message || "Approval failed");
        if (approvalStatus) approvalStatus.textContent = "Approval submitted successfully.";
      } catch {
        if (approvalStatus) approvalStatus.textContent = "Approval failed. Please retry.";
      }
    });
  }

  if (pendingApprovalsCountEl) {
    refreshPendingApprovalsCount();
    setInterval(refreshPendingApprovalsCount, 5000);
  }

  if (escalateBtn) {
    escalateBtn.addEventListener("click", () => {
      if (approvalStatus) approvalStatus.textContent = "Escalated to regional manager";
    });
  }

  const uploadForm = document.getElementById("uploadForm");
  const uploadStatus = document.getElementById("uploadStatus");

  if (uploadForm) {
    const manifestInput = uploadForm.querySelector('input[type="file"][name="file"]');
    const uploadBtn = uploadForm.querySelector('button[type="submit"]');

    if (uploadBtn && manifestInput) {
      uploadBtn.textContent = "Upload PDF";
      uploadBtn.type = "button";
      uploadBtn.addEventListener("click", () => manifestInput.click());
      manifestInput.addEventListener("change", async () => {
        if (!manifestInput.files || !manifestInput.files.length) return;
        const formData = new FormData(uploadForm);
        try {
          const response = await fetch("/upload", { method: "POST", body: formData });
          const result = await response.json();
          if (uploadStatus) uploadStatus.textContent = result.message || "Upload successful";
        } catch {
          if (uploadStatus) uploadStatus.textContent = "Upload failed";
        }
      });
    }

    uploadForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const formData = new FormData(uploadForm);
      try {
        const response = await fetch("/upload", { method: "POST", body: formData });
        const result = await response.json();
        if (uploadStatus) uploadStatus.textContent = result.message || "Upload successful";
      } catch {
        if (uploadStatus) uploadStatus.textContent = "Upload failed";
      }
    });
  }

  const backBtn = document.getElementById("backBtn");
  if (backBtn) {
    backBtn.addEventListener("click", () => {
      if (window.history.length > 1) {
        window.history.back();
      } else {
        window.location.href = "/";
      }
    });
  }

  const uploadIcon = document.querySelector(".upload-icon");
  if (uploadIcon) {
    uploadIcon.innerHTML = `
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 16V5" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/>
        <path d="M8 9L12 5L16 9" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
        <rect x="4" y="16" width="16" height="4" rx="2" stroke="currentColor" stroke-width="2"/>
      </svg>
    `;
  }
});

