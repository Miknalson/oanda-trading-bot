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
| **4 — Sessions** | ✅ fait | Enchaîne plusieurs petits trades vers un objectif **cumulé**, avec une **perte max obligatoire** qui arrête tout net. |

## Sessions : atteindre un objectif en plusieurs petits trades

Une session ne vise pas l'objectif en un seul trade. Elle ouvre un trade,
attend qu'il se ferme (stop-loss ou take-profit touché **chez OANDA**),
encaisse le résultat, puis recommence — jusqu'à l'une de ces conditions :

| Condition d'arrêt | Déclenchement |
|---|---|
| `objective_reached` | Le gain cumulé atteint ton objectif |
| `max_loss_reached` | La perte cumulée atteint ta perte max |
| `max_loss_would_be_exceeded` | Le trade suivant *pourrait* faire dépasser ta perte max — on ne l'ouvre pas |
| `max_trades_reached` | Plafond de trades atteint (`MAX_TRADES_PER_SESSION`) |
| `stopped_by_user` | Tu appelles `POST /api/sessions/{id}/stop` |
| `error` | Erreur OANDA ou interne — on s'arrête plutôt que de trader à l'aveugle |

```bash
curl -X POST http://localhost:8000/api/sessions/start \
  -H 'Content-Type: application/json' \
  -d '{"instrument":"EUR_USD","risk_pct":0.01,"objective_amount":20,
       "max_loss_amount":20,"reward_ratio":1.5,"confirm":true}'
```

Puis `GET /api/sessions/active` pour suivre, `POST /api/sessions/{id}/stop` pour arrêter.

### La perte max est obligatoire

`max_loss_amount` n'a **aucune valeur par défaut** : une session ne peut pas
démarrer sans que tu aies dit combien tu acceptes de perdre au total. Sans ce
plafond, une série de pertes grignoterait le compte indéfiniment — le risque
étant un % du solde restant, chaque perte réduit la mise suivante sans jamais
atteindre zéro, mais l'érosion continuerait tant que la boucle tourne.

Trois protections s'ajoutent, non contournables depuis l'app :

1. **Plafond serveur** : ta perte max est bornée à `MAX_SESSION_LOSS_PCT` du
   solde réel (10% par défaut). Demander plus renvoie une erreur 400.
2. **Vérification avant chaque trade** : si perdre le trade suivant ferait
   dépasser ta perte max, il n'est pas ouvert du tout.
3. **Une seule session à la fois** : sinon plusieurs boucles, chacune
   respectant sa propre limite, pourraient ensemble vider le compte.

Arrêter une session empêche l'ouverture de **nouveaux** trades ; un trade déjà
ouvert n'est pas fermé de force — il garde son stop-loss et son take-profit
chez OANDA et se ferme tout seul. Même chose si le serveur redémarre : la
boucle s'arrête, le trade en cours reste protégé.

Les garde-fous actifs sont lisibles sur `GET /api/limits`.

### Tests

```bash
cd backend && .venv/bin/python tests/test_session_limits.py
```

Les tests utilisent un faux client OANDA (aucun réseau, aucun argent) et
forcent notamment une série de 50 pertes d'affilée pour vérifier que la
session s'arrête bien à la limite au lieu d'éroder le solde.

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
- `MAX_SESSION_LOSS_PCT` plafonne la perte cumulée d'une session, et
  `max_loss_amount` est obligatoire à chaque démarrage de session
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
