// Interface de l'app : scanner, démarrage de session, suivi en direct.
//
// Une session enchaîne plusieurs petits trades vers un objectif cumulé et
// s'arrête net à une perte max que TU fixes. C'est ce que fait réellement le
// backend ; cette page n'est que sa télécommande. Rien n'est exécuté sans un
// clic explicite, et tous les plafonds sont revérifiés côté serveur.

// Même origine par défaut : le backend sert cette page (voir main.py), donc
// les chemins relatifs marchent et il n'y a aucun CORS à régler.
const API_BASE = window.API_BASE || "";

// Rafraîchissement du suivi. Le backend interroge le courtier toutes les
// SESSION_POLL_SECONDS de son côté ; inutile d'aller plus vite ici.
const REFRESH_MS = 5000;

const els = (ids) =>
  Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));

const el = els([
  "env-badge", "account-balance", "limits",
  "granularity", "refresh", "status", "results",
  "start-panel", "start-instrument", "objective-amount", "max-loss-amount",
  "risk-pct", "session-granularity", "preview", "preview-result", "start-session",
  "s-direction", "s-entry", "s-sl", "s-tp", "s-units", "s-risk", "s-ratio",
  "s-rationale", "risk-amount", "scale-warning",
  "live-panel", "live-instrument", "live-status", "live-pl", "live-trades",
  "gain-pct", "gain-bar", "gain-sub", "loss-pct", "loss-bar", "loss-sub",
  "live-milestones", "live-reason", "stop-session",
  "enable-push", "push-status", "positions",
]);

let selectedInstrument = null;
let activeSessionId = null;
let balance = null;
let currency = "";

const money = (n) => (n >= 0 ? "+" : "−") + Math.abs(n).toFixed(2);

async function api(path, options) {
  const res = await fetch(`${API_BASE}${path}`, options);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ---------------------------------------------------------------- compte
async function loadHealthAndLimits() {
  try {
    const health = await api("/health");
    el["env-badge"].textContent = health.warning;
    el["env-badge"].className = `badge ${health.environment}`;
  } catch {
    el["env-badge"].textContent = "backend injoignable";
    el["env-badge"].className = "badge error";
  }

  try {
    const limits = await api("/api/limits");
    el["limits"].textContent =
      `Plafonds serveur : ${(limits.max_risk_pct * 100).toFixed(1)} % par trade, ` +
      `${(limits.max_session_loss_pct * 100).toFixed(0)} % du solde par session, ` +
      `${limits.max_trades_per_session} trades max. ` +
      (limits.orders_allowed
        ? "Envoi d'ordres ACTIVÉ."
        : "Envoi d'ordres désactivé (LIVE_TRADING_CONFIRMED=false).");
  } catch {
    el["limits"].textContent = "";
  }
}

async function loadAccount() {
  try {
    const data = await api("/api/account");
    balance = data.balance;
    currency = data.currency;
    el["account-balance"].textContent =
      `${balance.toLocaleString("fr-FR", { minimumFractionDigits: 2 })} ${currency}`;
    refreshScale();
  } catch (err) {
    el["account-balance"].textContent = `indisponible (${err.message})`;
  }
}

// ---------------------------------------------------------------- scanner
async function loadScanner() {
  el["status"].textContent = "Chargement…";
  el["results"].innerHTML = "";
  try {
    const data = await api(
      `/api/scanner/top-volatile?granularity=${el["granularity"].value}&limit=15`
    );
    render(data.results);
    el["status"].textContent =
      `${data.results.length} instruments — ${new Date().toLocaleTimeString("fr-FR")}`;
  } catch (err) {
    el["status"].textContent = `Erreur : ${err.message}`;
  }
}

function render(results) {
  el["results"].innerHTML = results
    .map(
      (r) => `
      <li class="clickable" data-instrument="${r.instrument}">
        <div>
          <div class="instrument">${r.instrument}</div>
          <div class="price">${r.last_price}</div>
        </div>
        <div class="vol">${r.volatility_pct.toFixed(2)} %</div>
      </li>`
    )
    .join("") || '<li class="empty">Aucun instrument</li>';

  el["results"].querySelectorAll("li[data-instrument]").forEach((li) => {
    li.addEventListener("click", () => selectInstrument(li.dataset.instrument));
  });
}

function selectInstrument(instrument) {
  selectedInstrument = instrument;
  el["start-instrument"].textContent = instrument;
  el["start-panel"].classList.remove("hidden");
  el["preview-result"].classList.add("hidden");
  el["start-panel"].scrollIntoView({ behavior: "smooth", block: "start" });
}

// Traduit le risque en argent et prévient du seul réglage qui empêche toute
// session de démarrer : un risque par trade supérieur à la perte max. Le
// serveur le refuse de toute façon, mais découvrir « 0 trade » sans
// explication est la pire façon de l'apprendre.
function refreshScale() {
  if (balance === null) return;
  const riskPct = parseFloat(el["risk-pct"].value) / 100;
  const maxLoss = parseFloat(el["max-loss-amount"].value);
  const riskAmount = balance * riskPct;

  el["risk-amount"].textContent =
    Number.isFinite(riskAmount) ? `Soit ${riskAmount.toFixed(2)} ${currency} par trade.` : "";

  const impossible = Number.isFinite(maxLoss) && maxLoss > 0 && riskAmount > maxLoss;
  el["scale-warning"].classList.toggle("hidden", !impossible);
  if (impossible) {
    el["scale-warning"].textContent =
      `Un seul trade risque ${riskAmount.toFixed(2)} ${currency}, plus que la perte ` +
      `max de la session (${maxLoss.toFixed(2)}) : aucun trade ne pourrait être ` +
      `ouvert. Baisse le risque à ${((maxLoss / balance) * 100).toFixed(1)} % ` +
      `au maximum, ou monte la perte max à ${riskAmount.toFixed(2)}.`;
  }
}

// ------------------------------------------------------- aperçu du trade
async function previewTrade() {
  if (!selectedInstrument) return;
  const objective = parseFloat(el["objective-amount"].value);
  const riskPct = parseFloat(el["risk-pct"].value) / 100;
  if (!(objective > 0)) {
    alert("Renseigne un objectif de gain.");
    return;
  }

  el["preview"].disabled = true;
  el["preview"].textContent = "Analyse…";
  try {
    const s = await api(
      `/api/analysis/suggest?instrument=${selectedInstrument}` +
        `&risk_pct=${riskPct}&objective_amount=${objective}` +
        `&granularity=${el["session-granularity"].value}`
    );
    el["s-direction"].textContent = s.direction === "buy" ? "Achat 📈" : "Vente 📉";
    el["s-entry"].textContent = s.entry_price;
    el["s-sl"].textContent = s.stop_loss_price;
    el["s-tp"].textContent = s.take_profit_price;
    el["s-units"].textContent = Math.abs(s.suggested_units);
    el["s-risk"].textContent = `${s.risk_amount} (${(s.risk_pct * 100).toFixed(1)} %)`;
    el["s-ratio"].textContent = s.reward_risk_ratio;
    el["s-rationale"].textContent = s.rationale;
    el["preview-result"].classList.remove("hidden");
  } catch (err) {
    alert(`Analyse impossible : ${err.message}`);
  } finally {
    el["preview"].disabled = false;
    el["preview"].textContent = "Voir le premier trade";
  }
}

// ------------------------------------------------------------- sessions
async function startSession() {
  if (!selectedInstrument) {
    alert("Choisis d'abord un marché dans la liste.");
    return;
  }
  const objective = parseFloat(el["objective-amount"].value);
  const maxLoss = parseFloat(el["max-loss-amount"].value);
  const riskPct = parseFloat(el["risk-pct"].value) / 100;

  // La perte max n'a pas de valeur par défaut, côté serveur comme ici : une
  // session ne doit pas pouvoir démarrer sans que tu aies dit combien tu
  // acceptes de perdre.
  if (!(maxLoss > 0)) {
    alert("La perte maximale est obligatoire : sans elle, rien ne borne tes pertes.");
    el["max-loss-amount"].focus();
    return;
  }
  if (!(objective > 0)) {
    alert("Renseigne un objectif de gain.");
    return;
  }

  const ok = confirm(
    `Lancer une session sur ${selectedInstrument} ?\n\n` +
      `Objectif : +${objective}\n` +
      `Perte max : −${maxLoss}\n` +
      `Risque par trade : ${(riskPct * 100).toFixed(1)} %\n\n` +
      `Le bot enchaînera de petits trades jusqu'à l'un des deux seuils.`
  );
  if (!ok) return;

  el["start-session"].disabled = true;
  el["start-session"].textContent = "Démarrage…";
  try {
    const session = await api("/api/sessions/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        instrument: selectedInstrument,
        risk_pct: riskPct,
        objective_amount: objective,
        max_loss_amount: maxLoss,
        granularity: el["session-granularity"].value,
        confirm: true,
      }),
    });
    el["start-panel"].classList.add("hidden");
    renderSession(session);
  } catch (err) {
    alert(`Démarrage refusé : ${err.message}`);
  } finally {
    el["start-session"].disabled = false;
    el["start-session"].textContent = "Lancer la session";
  }
}

async function stopSession() {
  if (!activeSessionId) return;
  if (!confirm("Arrêter la session ? Aucun nouveau trade ne sera ouvert.")) return;
  try {
    renderSession(await api(`/api/sessions/${activeSessionId}/stop`, { method: "POST" }));
  } catch (err) {
    alert(`Arrêt impossible : ${err.message}`);
  }
}

const STATUS_LABELS = {
  running: "en cours",
  objective_reached: "objectif atteint 🎉",
  max_loss_reached: "perte max atteinte 🛑",
  max_loss_would_be_exceeded: "arrêtée avant de dépasser la perte max",
  max_trades_reached: "plafond de trades atteint",
  spread_too_wide: "marché trop cher — arrêtée",
  stopped_manually: "arrêtée manuellement",
  error: "erreur",
};

function renderSession(session) {
  if (!session) {
    el["live-panel"].classList.add("hidden");
    activeSessionId = null;
    return;
  }
  activeSessionId = session.id;
  el["live-panel"].classList.remove("hidden");
  el["live-instrument"].textContent = session.instrument;

  const running = session.status === "running";
  el["live-status"].textContent =
    STATUS_LABELS[session.stop_reason] || STATUS_LABELS[session.status] || session.status;
  el["live-status"].className = `badge ${running ? "running" : "done"}`;

  el["live-pl"].textContent = money(session.realized_pl);
  el["live-pl"].className = `pl ${session.realized_pl >= 0 ? "up" : "down"}`;
  el["live-trades"].textContent = session.trades_count;

  const gain = Math.min(100, session.progress_gain_pct);
  const loss = Math.min(100, session.progress_loss_pct);
  el["gain-pct"].textContent = `${gain.toFixed(0)} %`;
  el["gain-bar"].style.width = `${gain}%`;
  el["gain-sub"].textContent =
    `${money(Math.max(0, session.realized_pl))} sur +${session.objective_amount} visés`;
  el["loss-pct"].textContent = `${loss.toFixed(0)} %`;
  el["loss-bar"].style.width = `${loss}%`;
  el["loss-sub"].textContent =
    `${money(Math.min(0, session.realized_pl))} sur −${session.max_loss_amount} tolérés`;

  // `percent` (25, 50, 75, 100) et non `threshold` (0.25...), et `kind`
  // ("gain"/"loss") et non `direction` : ce sont les noms que to_dict()
  // produit réellement. Les deux fautes étaient muettes — « 0.25 % » à
  // l'écran et une classe CSS « chip undefined » sans aucune couleur.
  el["live-milestones"].innerHTML = (session.milestones || [])
    .map((m) => `<span class="chip ${m.kind}">${m.percent} %</span>`)
    .join("");

  // Le financement est un vrai coût, déjà déduit du P/L affiché : le taire
  // donnerait l'impression que les gains sont plus gros qu'ils ne sont.
  const frais = session.total_financing
    ? ` Financement déjà déduit : ${money(session.total_financing)}.`
    : "";
  el["live-reason"].textContent = (session.error ? `Erreur : ${session.error}.` : "") + frais;

  el["stop-session"].classList.toggle("hidden", !running);
}

async function pollSession() {
  try {
    const data = await api("/api/sessions/active");
    renderSession(data.session);
    // Pas de session active : le panneau de démarrage redevient utile.
    if (!data.session && selectedInstrument) {
      el["start-panel"].classList.remove("hidden");
    }
  } catch {
    // Le backend peut être momentanément injoignable ; on réessaiera.
  }
}

// ------------------------------------------------------------ positions
async function loadPositions() {
  try {
    const data = await api("/api/positions");
    // Noms de champs de l'API NEUTRE (broker.OpenTrade) : units,
    // unrealized_pl. Cette page lisait les noms camelCase d'OANDA
    // (currentUnits, unrealizedPL) hérités d'avant la refonte courtier :
    // ils n'existent plus et s'affichaient en « undefined ».
    el["positions"].innerHTML = (data.trades || [])
      .map(
        (t) => `
        <li>
          <div>
            <div class="instrument">${t.instrument}</div>
            <div class="price">${t.units} unités @ ${t.price}</div>
          </div>
          <div class="vol ${t.unrealized_pl >= 0 ? "up" : "down"}">
            ${money(t.unrealized_pl)}
          </div>
        </li>`
      )
      .join("") || '<li class="empty">Aucune position ouverte</li>';
  } catch {
    // Panneau non critique au chargement.
  }
}

// ------------------------------------------------------------------ push
async function enablePush() {
  el["push-status"].textContent = "Activation…";
  try {
    const res = await window.enablePushNotifications();
    el["push-status"].textContent = `Notifications actives (${res.subscribers} appareil(s)).`;
  } catch (err) {
    el["push-status"].textContent = `Échec : ${err.message}`;
  }
}

// ----------------------------------------------------------------- init
el["refresh"].addEventListener("click", loadScanner);
el["granularity"].addEventListener("change", loadScanner);
el["preview"].addEventListener("click", previewTrade);
el["start-session"].addEventListener("click", startSession);
el["stop-session"].addEventListener("click", stopSession);
el["enable-push"].addEventListener("click", enablePush);
el["risk-pct"].addEventListener("input", refreshScale);
el["max-loss-amount"].addEventListener("input", refreshScale);

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("service-worker.js").catch(() => {});
}

loadHealthAndLimits();
loadAccount();
loadScanner();
pollSession();
loadPositions();
setInterval(pollSession, REFRESH_MS);
setInterval(loadPositions, REFRESH_MS * 3);
