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

## Frais : deux coûts qui tirent en sens opposé

Sur OANDA (comptes standard) il n'y a pas de commission : le coût est le
**spread**, l'écart entre le prix d'achat et de vente, payé à chaque ouverture
de position. Une session enchaînant les trades, ce coût se répète.

Ce qui décide de son poids, c'est le rapport `spread / distance du stop-loss`.
Le spread est à peu près fixe, alors que le stop suit la volatilité
(1,5 × ATR) : plus l'intervalle est court, plus le stop est serré, et plus le
spread dévore une part énorme du risque.

Avec un solde de 250 €, 1 % de risque (2,50 €/trade), ratio 1,5, 55 % de réussite :

| Instrument | Stop | Coût/trade | % du risque | Gain net/trade |
|---|---|---|---|---|
| EUR/USD **M1** | 2,2 pips | 1,33 € | **53 %** | **−0,40 € ❌** |
| EUR/USD **M5** | 4,5 pips | 0,67 € | 27 % | +0,27 € |
| EUR/USD **M15** | 7,5 pips | 0,40 € | 16 % | +0,54 € |
| EUR/USD **H1** | 18 pips | 0,17 € | 7 % | +0,77 € |
| EUR/USD **H4** | 37,5 pips | 0,08 € | 3 % | +0,86 € |

**Sur M1 la stratégie perd de l'argent par construction** : même avec 55 % de
trades gagnants, l'espérance est négative. Ce n'est pas de la malchance, c'est
de l'arithmétique.

### Ce que fait le code

- **Prix réels d'exécution** : l'entrée est le `ask` à l'achat et le `bid` à la
  vente, lus sur l'endpoint pricing d'OANDA. Les bougies renvoient des prix
  *médians* : s'en servir masquerait le spread et rendrait toutes les
  suggestions trop optimistes.
- **Coût affiché** : chaque suggestion indique `spread`, `spread_cost` et
  `spread_pct_of_risk`, plus `move_to_win` / `move_to_lose` — le mouvement que
  le marché doit réellement parcourir. Perdre demande toujours *moins* de
  mouvement que gagner : c'est exactement ça, le coût du spread.
- **Refus automatique** : au-delà de `MAX_SPREAD_RATIO` (15 % par défaut), le
  trade est refusé (HTTP 409) et une session en cours s'arrête avec la raison
  `spread_too_wide`, sans passer d'ordre.

Le montant risqué reste exact : stop et objectif sont placés depuis le prix
d'exécution réel, donc une perte au stop vaut bien `risk_pct` du solde.

## Le second frais : le financement

Le spread n'est pas le seul coût. OANDA facture des **intérêts de détention**
(le « swap ») sur toute position gardée après 17 h heure de New York. Deux
différences avec le spread :

- il se **déduit vraiment** du résultat, alors que le spread se paie en pertes
  plus fréquentes ;
- il **grandit avec la durée de détention**, alors que le spread est payé une
  seule fois à l'ouverture.

Dans l'API OANDA, `realizedPL` et `financing` sont deux champs **séparés** :
ne lire que le premier sous-estime le coût réel. Le code lit les deux et les
additionne, et `SessionTrade` conserve `market_pl` et `financing`
distinctement — ils ne se pilotent pas pareil : l'un dépend de la justesse du
trade, l'autre du temps passé en position.

### Les deux frais s'opposent, et l'optimum n'est pas au bout du spectre

| Intervalle | Spread (% du risque) | Financement/trade | Seuil d'équilibre |
|---|---|---|---|
| H1 | 14 % | 0,048 € | **40,8 %** ✅ |
| H4 | 7 % | 0,097 € | 41,6 % |
| D | 3 % | 0,249 € | 44,0 % |

Allonger l'intervalle élargit le stop et **réduit** le poids du spread, mais
garde la position plus longtemps et **augmente** le financement. Le meilleur
compromis n'est donc ni au plus court ni au plus long : dans ce modèle c'est
**H1** qui exige le taux de réussite le plus bas.

Le backtest modélise les deux (`--financing 0.02` par défaut ; `0` pour
isoler l'effet du seul spread).

### Recommandations

- **H1** comme point de départ : le meilleur compromis entre les deux frais.
  Jamais M1, et M5 seulement en connaissance de cause.
- **Objectif modeste** : viser 20 € par session avec 2,50 € de risque par trade
  demanderait ~26 trades — au-dessus du plafond de 20. Un objectif de 5 € est
  autrement plus atteignable.
- Le scanner classe par volatilité, ce qui n'est pas un classement par coût :
  vérifie toujours le `spread_pct_of_risk` de la suggestion avant de lancer.

## Backtest : mesurer au lieu de supposer

```bash
cd backend
.venv/bin/python -m app.backtest --instrument EUR_USD --granularity H1 --count 5000
```

Le backtest rejoue la stratégie sur l'historique OANDA et mesure le taux de
réussite **réel**, frais compris. Il réutilise les fonctions de
`indicators.py` — les mêmes qu'en production. Backtester une logique
différente de celle qui tradera ne prouverait rien.

### Deux règles qui font la différence entre une mesure et une illusion

**Aucun regard vers le futur.** À la bougie `i`, seules les bougies `0..i`
sont visibles, et l'entrée se fait à l'ouverture de `i+1`. Un test vérifie
que les décisions passées ne changent pas quand on ajoute des bougies
futures.

**Le spread se paie en pertes plus fréquentes, pas en déduction.** On achète
au `ask` et on revend au `bid` : le stop se déclenche *plus tôt*
(`entrée - distance + spread`) et l'objectif *plus tard*
(`entrée + cible + spread`). Le gain et la perte en euros restent exacts, le
seuil d'équilibre reste `1/(1+ratio)`, et c'est le taux de réussite mesuré
qui baisse. Une première version déduisait le spread du P/L tout en
comparant les niveaux sur les prix médians : le coût n'était compté qu'à
moitié et le backtest affichait un P/L positif avec un taux sous le seuil —
arithmétiquement impossible.

### Validation du backtester

Le test décisif : sur une **marche aléatoire** (aucun avantage exploitable),
un ratio de 1,5 impose mathématiquement ~40 % de réussite. Le backtester
mesure **39,7 %**. Un outil qui regarderait le futur afficherait bien plus.

Autres garde-fous testés : une bougie touchant stop *et* objectif compte en
perte (on ne sait pas lequel est arrivé en premier) ; le spread dégrade
toujours réussite et P/L ; le P/L est cohérent avec le taux de réussite
mesuré ; un spread ruineux n'ouvre aucun trade.

### Ce que disent les premiers résultats

Sur données synthétiques couvrant l'éventail des comportements de marché
plausibles (EUR/USD, spread 1,2 pip, ratio 1,5, seuil 40 %) :

| Persistance des tendances | Réussite | P/L net | |
|---|---|---|---|
| 0,0 (marche aléatoire) | 37,2 % | −124 € | ❌ |
| 0,2 | 37,7 % | −94 € | ❌ |
| 0,4 | 37,6 % | −71 € | ❌ |
| 0,5 | 38,4 % | −33 € | ❌ |
| 0,6 (tendances très marquées) | 40,2 % | +2 € | ✅ |

La stratégie n'atteint l'équilibre qu'avec une persistance de tendance
extrême. Les marchés de change réels s'en approchent rarement sur les
intervalles courts.

**Ces chiffres viennent de données simulées, pas du marché.** Le verdict
réel demande l'historique OANDA, donc une clé API. La commande ci-dessus le
produira.

## Notifications : paliers 25 / 50 / 75 / 100 %

Pendant une session, tu es notifié à chaque quart de progression — dans les
**deux directions** :

| Palier | Vers l'objectif | Vers la perte max |
|---|---|---|
| 25% | 📈 25% de l'objectif | ⚠️ 25% de la perte max |
| 50% | 📈 50% de l'objectif | ⚠️ 50% de la perte max |
| 75% | 📈 75% de l'objectif | ⚠️ 75% de la perte max |
| 100% | 🎉 Objectif atteint | 🛑 Perte max atteinte |

Chaque palier n'est envoyé **qu'une fois**. Le P/L d'une session oscille
(un trade gagnant, un perdant, un gagnant...) : sans mémoire, tu recevrais la
même notification « 50% » à chaque aller-retour autour du seuil. Un « plus
haut atteint » est donc conservé par direction.

Le palier 100% de perte est émis même quand le garde-fou arrête la session
*avant* d'ouvrir le trade de trop — le P/L plafonne alors juste sous la
limite (ex. -19,97 sur -20,00) et le seuil ne serait jamais franchi
naturellement. Sans ce traitement, aucune notification n'arriverait au moment
précis où elle est la plus utile.

### Activer le push sur le téléphone

Sans configuration, les paliers sont enregistrés et lisibles via
`GET /api/sessions/{id}/milestones` — suffisant quand l'app est ouverte. Pour
les recevoir **écran verrouillé**, il faut des clés VAPID :

```bash
cd backend
.venv/bin/python -c "from app.notifier import generate_vapid_keys; generate_vapid_keys()"
# colle les 3 lignes affichées dans backend/.env, puis relance uvicorn
```

Côté PWA, appelle `enablePushNotifications()` (dans `frontend/push.js`) depuis
un **clic utilisateur** — les navigateurs refusent une demande de permission
déclenchée automatiquement au chargement.

Sur iPhone, les notifications push ne fonctionnent que si la PWA a été ajoutée
à l'écran d'accueil (Partager → Sur l'écran d'accueil) et servie en HTTPS.

Un échec d'envoi n'interrompt jamais une session : le palier reste enregistré
et le trading continue.

### Tests

```bash
cd backend
.venv/bin/python tests/test_session_limits.py
.venv/bin/python tests/test_milestones.py
.venv/bin/python tests/test_spread.py
.venv/bin/python tests/test_backtest.py
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
