# oanda-trading-bot

Scanner de volatilité + assistant de trading via l'[API REST v20 d'OANDA](https://developer.oanda.com/rest-live-v20/introduction/), pensé pour être piloté depuis une PWA mobile.

## ⚠️ Avant de commencer

- **Commence toujours avec un compte OANDA `practice` (démo, argent fictif).** Ne passe en `live` que lorsque tu as testé et que tu comprends précisément ce que le code fait.
- Ce projet manipule potentiellement de l'argent réel une fois en Phase 3. Aucune garantie n'est donnée sur la performance d'une stratégie de trading.
- Ne commit jamais ta clé API ou ton identifiant de compte (`.env` est ignoré par git — utilise `.env.example` comme modèle).

## Feuille de route

| Phase | Statut | Description |
|---|---|---|
| **1 — Scanner** | ✅ fait | Lecture seule : identifie les instruments les plus volatils. Aucun ordre passé. |
| **2 — Validation manuelle** | ⏭️ sautée | On est passés directement à la Phase 3 avec garde-fous stricts (voir ci-dessous). |
| **3 — Semi-automatique** | ✅ fait (v1) | Analyse tendance + volatilité, suggère direction/stop-loss/take-profit/taille de position selon ton risque, **un seul clic** ("Lancer") ouvre l'ordre avec stop-loss et take-profit attachés — OANDA ferme la position tout seul à l'un des deux. |

### ⚠️ Sur la fiabilité de la Phase 3 — à lire avant d'activer le live

Le moteur de suggestion utilise des indicateurs techniques classiques (moyennes
mobiles pour la tendance, ATR pour la volatilité). **Ce ne sont que des
heuristiques** : aucune stratégie de trading ne garantit un taux de réussite
donné, et personne ne peut promettre 50-60% ni aucun autre chiffre à l'avance.
Le code vise une **gestion du risque rigoureuse** (jamais plus de 2% du solde
risqué par trade, plafond appliqué côté serveur, stop-loss obligatoire sur
chaque ordre), pas une martingale magique.

Garde-fous en place :
- `MAX_RISK_PCT` (backend/.env) plafonne le risque par trade côté serveur, quoi
  que le client demande
- `LIVE_TRADING_CONFIRMED=false` par défaut : aucun ordre réel n'est envoyé en
  environnement `live` tant que tu n'actives pas ce flag explicitement
- Chaque ordre passe par un stop-loss ET un take-profit attachés — jamais de
  position "nue" sans limite de perte
- Aucun ordre n'est jamais envoyé sans que tu cliques "Lancer" dans l'app
  (`confirm: true` obligatoire dans la requête)

**Teste toujours en `practice` (compte démo) d'abord**, sur plusieurs
instruments et plusieurs jours, avant d'envisager le `live`.

## Architecture

```
📱 PWA (frontend/)          ☁️ Backend FastAPI (backend/)         🏦 OANDA REST API
   affiche le scanner  <──>   scanner de volatilité          <──>   candles, comptes
   installable sur           (cache, calcul de %)
   iOS/Android
```

Le backend tourne en continu (localement pour commencer, puis sur un petit serveur) pour interroger l'API OANDA sans dépendre de l'app ouverte sur ton téléphone.

## Démarrage — backend

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # puis renseigne OANDA_API_KEY et OANDA_ACCOUNT_ID (compte démo)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Vérifie que ça répond : `curl http://localhost:8000/health`

Récupère ta clé API démo ici : https://www.oanda.com/demo-account/tpa/personal_token (crée d'abord un compte démo gratuit sur oanda.com si tu n'en as pas).

## Démarrage — frontend (PWA)

```bash
cd frontend
python3 -m http.server 5173
```

Ouvre `http://localhost:5173` sur ton téléphone (même réseau Wi-Fi que ton ordinateur), ou déploie `frontend/` sur un hébergeur statique (Netlify, Vercel, GitHub Pages) pour y accéder de partout. Pense à changer `API_BASE` dans `app.js` (ou définir `window.API_BASE`) pour pointer vers ton backend déployé.

## Ce que fait le scanner (Phase 1)

Pour chaque instrument tradable de ton compte, il récupère les dernières bougies OANDA et calcule l'amplitude `(high - low) / low * 100` sur la période choisie, puis classe les instruments du plus au moins volatil. Résultat exposé sur `GET /api/scanner/top-volatile`.
