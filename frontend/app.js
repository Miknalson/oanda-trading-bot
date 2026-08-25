// Phase 1 — lecture seule. Interroge le backend pour afficher les
// instruments les plus volatils. Aucune action d'achat/vente ici.

const API_BASE = window.API_BASE || "http://localhost:8000";

const els = {
  granularity: document.getElementById("granularity"),
  refresh: document.getElementById("refresh"),
  status: document.getElementById("status"),
  results: document.getElementById("results"),
  envBadge: document.getElementById("env-badge"),
  suggestionPanel: document.getElementById("suggestion-panel"),
  sugInstrument: document.getElementById("sug-instrument"),
  riskPct: document.getElementById("risk-pct"),
  objectiveAmount: document.getElementById("objective-amount"),
  analyze: document.getElementById("analyze"),
  suggestionResult: document.getElementById("suggestion-result"),
  launch: document.getElementById("launch"),
  positions: document.getElementById("positions"),
};

let selectedInstrument = null;
let currentSuggestion = null;

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
      <li class="clickable" data-instrument="${r.instrument}">
        <div>
          <div class="instrument">${r.instrument}</div>
          <div class="price">${r.last_price}</div>
        </div>
        <div class="vol">${r.volatility_pct.toFixed(2)}%</div>
      </li>`
    )
    .join("");

  els.results.querySelectorAll("li[data-instrument]").forEach((li) => {
    li.addEventListener("click", () => selectInstrument(li.dataset.instrument));
  });
}

function selectInstrument(instrument) {
  selectedInstrument = instrument;
  currentSuggestion = null;
  els.sugInstrument.textContent = instrument;
  els.suggestionPanel.classList.remove("hidden");
  els.suggestionResult.classList.add("hidden");
  els.suggestionPanel.scrollIntoView({ behavior: "smooth" });
}

async function analyzeSelected() {
  if (!selectedInstrument) return;
  els.analyze.disabled = true;
  els.analyze.textContent = "Analyse en cours…";
  try {
    const riskPct = parseFloat(els.riskPct.value) / 100;
    const objectiveAmount = parseFloat(els.objectiveAmount.value);
    const url = `${API_BASE}/api/analysis/suggest?instrument=${selectedInstrument}&risk_pct=${riskPct}&objective_amount=${objectiveAmount}`;
    const res = await fetch(url);
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    currentSuggestion = data;
    renderSuggestion(data);
  } catch (err) {
    alert(`Erreur d'analyse : ${err.message}`);
  } finally {
    els.analyze.disabled = false;
    els.analyze.textContent = "Analyser";
  }
}

function renderSuggestion(s) {
  document.getElementById("s-direction").textContent =
    s.direction === "buy" ? "Achat 📈" : "Vente 📉";
  document.getElementById("s-entry").textContent = s.entry_price;
  document.getElementById("s-sl").textContent = s.stop_loss_price;
  document.getElementById("s-tp").textContent = s.take_profit_price;
  document.getElementById("s-units").textContent = Math.abs(s.suggested_units);
  document.getElementById("s-risk").textContent = `${s.risk_amount} (${(s.risk_pct * 100).toFixed(1)}%)`;
  document.getElementById("s-ratio").textContent = s.reward_risk_ratio;
  document.getElementById("s-rationale").textContent = s.rationale;
  els.suggestionResult.classList.remove("hidden");
}

async function launchTrade() {
  if (!currentSuggestion) return;
  const confirmed = confirm(
    `Confirmer l'ouverture de ce trade réel sur ${currentSuggestion.instrument} ?\n` +
      `Risque : ${currentSuggestion.risk_amount}\nObjectif : ${currentSuggestion.potential_gain}`
  );
  if (!confirmed) return;

  els.launch.disabled = true;
  els.launch.textContent = "Envoi de l'ordre…";
  try {
    const res = await fetch(`${API_BASE}/api/orders/place`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instrument: currentSuggestion.instrument,
        units: currentSuggestion.suggested_units,
        stop_loss_price: currentSuggestion.stop_loss_price,
        take_profit_price: currentSuggestion.take_profit_price,
        risk_pct: currentSuggestion.risk_pct,
        confirm: true,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    alert("Ordre envoyé avec succès. Suivi dans 'Positions ouvertes'.");
    loadPositions();
  } catch (err) {
    alert(`Erreur lors de l'envoi de l'ordre : ${err.message}`);
  } finally {
    els.launch.disabled = false;
    els.launch.textContent = "Lancer le trade";
  }
}

async function loadPositions() {
  try {
    const res = await fetch(`${API_BASE}/api/positions`);
    const data = await res.json();
    els.positions.innerHTML = (data.trades || [])
      .map(
        (t) => `
        <li>
          <div>
            <div class="instrument">${t.instrument}</div>
            <div class="price">${t.currentUnits} unités @ ${t.price}</div>
          </div>
          <div class="vol">${t.unrealizedPL}</div>
        </li>`
      )
      .join("") || "<li class=\"empty\">Aucune position ouverte</li>";
  } catch (err) {
    // silencieux : le panneau positions n'est pas critique au chargement
  }
}

els.refresh.addEventListener("click", loadScanner);
els.granularity.addEventListener("change", loadScanner);
els.analyze.addEventListener("click", analyzeSelected);
els.launch.addEventListener("click", launchTrade);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}

loadHealth();
loadScanner();
loadPositions();
setInterval(loadPositions, 15000);
