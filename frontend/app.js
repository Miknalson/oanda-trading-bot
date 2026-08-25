// Phase 1 — lecture seule. Interroge le backend pour afficher les
// instruments les plus volatils. Aucune action d'achat/vente ici.

const API_BASE = window.API_BASE || "http://localhost:8000";

const els = {
  granularity: document.getElementById("granularity"),
  refresh: document.getElementById("refresh"),
  status: document.getElementById("status"),
  results: document.getElementById("results"),
  envBadge: document.getElementById("env-badge"),
};

async function loadHealth() {
  try {
    const res = await fetch(`${API_BASE}/health`);
    const data = await res.json();
    els.envBadge.textContent = data.warning;
    els.envBadge.className = `badge ${data.environment}`;
  } catch (err) {
    els.envBadge.textContent = "backend injoignable";
  }
}

async function loadScanner() {
  els.status.textContent = "Chargement…";
  els.results.innerHTML = "";
  try {
    const granularity = els.granularity.value;
    const res = await fetch(
      `${API_BASE}/api/scanner/top-volatile?granularity=${granularity}&limit=15`
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    render(data.results);
    els.status.textContent = `${data.results.length} instruments — mis à jour ${new Date().toLocaleTimeString(
      "fr-FR"
    )}`;
  } catch (err) {
    els.status.textContent = `Erreur : ${err.message}`;
  }
}

function render(results) {
  els.results.innerHTML = results
    .map(
      (r) => `
      <li>
        <div>
          <div class="instrument">${r.instrument}</div>
          <div class="price">${r.last_price}</div>
        </div>
        <div class="vol">${r.volatility_pct.toFixed(2)}%</div>
      </li>`
    )
    .join("");
}

els.refresh.addEventListener("click", loadScanner);
els.granularity.addEventListener("change", loadScanner);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}

loadHealth();
loadScanner();
