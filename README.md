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
| **2 — Validation manuelle** | 🔜 à venir | L'app propose un trade, toi seul confirmes l'exécution. |
| **3 — Semi-automatique** | 🔜 à venir | Exécution automatique jusqu'à un objectif de gain, avec stop-loss obligatoire et garde-fous (montant max engagé, confirmation du premier ordre). |

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
