# oanda-trading-bot

Scanner de volatilité + assistant de trading, piloté depuis une PWA mobile.
Fonctionne avec **Saxo Bank** (OpenAPI) ou **OANDA** (REST v20).

## Choisir un courtier

Le projet a d'abord été écrit contre OANDA, jusqu'à découvrir que **l'entité
européenne d'OANDA (TMS Brokers, qui sert les clients français depuis 2023)
ne propose pas l'API REST** — son offre passe par MetaTrader 5. Vérification
faite sur les trois candidats :

| Courtier | API accessible sans compte réel ? |
|---|---|
| OANDA (entité EU/TMS) | ❌ Pas d'API — MetaTrader uniquement |
| IG | ❌ Exige un compte réel lié, même pour la démo |
| **Saxo** | ✅ Portail développeur, inscription libre |

**Saxo est donc le défaut.** Son [portail développeur](https://www.developer.saxo/accounts/sim/signup)
ouvre un environnement de simulation — copie du réel, 100 000 $ fictifs — et
délivre un jeton immédiatement, sans vérification d'identité.

⚠️ Ce jeton dure **24 heures**. Parfait pour un backtest ou une session de
test ; pour un bot qui tourne en continu il faudra enregistrer une application
et implémenter le flux OAuth. Le code distingue une expiration de jeton d'une
vraie panne, pour ne pas chercher un bug là où il suffit d'en régénérer un.

## Aucun couplage à un courtier

Le premier jet appelait l'API d'OANDA directement : son format JSON
(`{"mid": {...}}`, `realizedPL`, `orderFillTransaction`) se retrouvait jusque
dans la logique de stratégie. Changer de courtier en devenait une refonte.

`broker.py` fixe maintenant le contrat — des types neutres (`Candle`,
`Quote`, `TradeStatus`...) et un protocole que chaque courtier implémente.
La stratégie, le risque, les sessions et le backtest ne connaissent que ça.
Ajouter un courtier revient à écrire une classe.

Un test (`tests/test_broker_contract.py`) vérifie que chaque client
implémente le contrat en entier, et **relit la logique métier pour s'assurer
qu'aucun format de courtier n'y a resurgi**. C'est ce test qui a rattrapé la
dernière trace au moment du découplage.

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

### Depuis un téléphone, sans rien installer

Ouvre `notebooks/backtest_saxo.ipynb` dans [Google Colab](https://colab.research.google.com/).
Le carnet récupère le code, demande ton jeton Saxo en masqué, vérifie la
connexion, puis lance le backtest et affiche le verdict.

Colab ne voit un dépôt privé que si son autorisation GitHub le couvre — ce
qui est pénible à corriger depuis un téléphone. Deux contournements :

- **Rendre le dépôt public.** L'historique a été audité : aucun `.env`, aucune
  clé, aucun jeton n'y figure, et `.gitignore` couvre `.env` depuis le premier
  commit. Le lien devient alors direct :
  `colab.research.google.com/github/<user>/<repo>/blob/main/notebooks/backtest_saxo.ipynb`
- **Utiliser `notebooks/cellule_autonome.py`**, qui ne dépend d'aucun accès
  GitHub. Ce fichier est **généré** depuis les vrais modules par
  `scripts/build_standalone.py` — à relancer après toute évolution du code,
  jamais à modifier à la main, sinon la cellule mesurerait autre chose que le
  bot réellement testé.

### Depuis un ordinateur

```bash
cd backend
.venv/bin/python -m app.backtest --instrument EUR_USD --granularity H4 --count 6000
```

### Remonter l'historique par pages

Les courtiers plafonnent chaque requête : 1200 bougies chez Saxo, 5000 chez
OANDA. Sur H4 cela ne donnait que ~65 trades — trop peu pour conclure quoi que
ce soit (voir *malchance* plus bas). `fetch_history` enchaîne donc les
requêtes en reculant dans le temps : `Mode=UpTo` + `Time` chez Saxo, `to` chez
OANDA, d'où le champ `Candle.time`.

Le danger de cette boucle est précis. Si le courtier **ignore** le bornage, il
renvoie à chaque tour la même fenêtre ; empiler ces réponses fabrique un
historique de doublons, et un backtest dessus tourne, sort des chiffres
crédibles, et ne mesure rien — invisible dans le résultat. La boucle vérifie
donc à chaque page que le courtier a réellement reculé :

> Si la bougie la plus **récente** de la nouvelle page est celle de la page
> précédente, le bornage n'a pas été pris en compte.

Ce test ne peut pas se déclencher à tort sur un historique épuisé : dans ce
cas la fenêtre renvoyée serait plus ancienne, pas identique. En cas de
détection, la récupération échoue franchement au lieu de rendre un historique
long et faux. Sans horodatage sur les bougies, il n'y a rien à quoi se borner :
une seule page est renvoyée, et c'est journalisé.

⚠️ Les domaines de Saxo sont inaccessibles depuis l'environnement où ce code a
été écrit : le contrat `Mode`/`Time` vient de sources tierces concordantes, pas
de la documentation officielle lue directement. C'est précisément pourquoi le
garde-fou ci-dessus existe — si l'hypothèse est fausse, on l'apprend par une
erreur explicite et non par des résultats silencieusement faux.

### Coût de calcul

Les indicateurs ne reçoivent qu'une **fenêtre glissante** des dernières
bougies (`INDICATOR_WINDOW`), pas tout l'historique. Leur passer l'historique
entier à chaque barre donnait le même résultat en temps quadratique : la suite
de tests est passée de 46 s à 2,2 s une fois la pagination en place. Un test
vérifie que l'optimisation est exactement neutre, trade par trade.

Le `max(0, ...)` sur le début de la fenêtre n'est pas décoratif : un indice
négatif découperait la fin du tableau, donc des bougies **futures** — le
regard vers le futur est l'erreur qui rend un backtest flatteur et faux, et
elle se glisserait là sans rien casser.

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

### Deux phrases qu'un backtest ne doit jamais dire

**« Aucun trade sur la période. »** Ça ressemble à un marché calme, et c'est
presque toujours l'inverse : un spread trop large pour qu'une entrée soit
rentable. Trois causes produisent la même ligne vide et appellent trois
décisions opposées — pas assez d'historique, aucune tendance détectée, toutes
les entrées refusées pour cause de spread. Le résultat compte donc les
bougies écartées et leur motif (`no_trade_reason()`), et la ligne vide n'est
plus muette :

> H1 — aucun trade : sur 1148 bougies examinées : 1148 entrées refusées car
> le spread (0.00025) dépassait 15% de la distance du stop — le marché
> n'était pas calme, il était trop cher

Le mécanisme est mesurable : l'ATR sur H1 vaut environ le quart de celui sur
H4, donc le stop est quatre fois plus proche, donc le même spread en ronge
quatre fois plus. H1 n'est pas « sans signal », il est **trop cher pour le
spread de ce relevé-là**.

**Le spread ne se relève pas, il se balaie.** Il change d'heure en heure, et
marché fermé il est élargi et figé. Le carnet vérifie donc `Quote.tradeable`
avant de retenir un spread live, et teste de toute façon une fourchette
(0,8 / 1,2 / 2,0 pip) : le spread est le facteur dominant du résultat, pas un
paramètre de réglage. Deux tests couvrent ça — l'un vérifie sur l'arbre
syntaxique que le spread live reste sous sa condition, l'autre exécute
réellement les cellules du carnet marché ouvert puis marché fermé.

**« Perdant. »** sur un petit échantillon. Un taux de réussite sous le seuil
d'équilibre peut très bien n'être que de la malchance. Le résultat calcule
donc la probabilité exacte d'un tel tirage si la stratégie était pile à
l'équilibre (`p_value`, loi binomiale calculée en logarithmes — `math.comb`
déborde du flottant au-delà de quelques centaines de tirages) et refuse de
trancher au-dessus de 5 % : le verdict devient **NON CONCLUANT**, avec le
nombre de trades qu'il faudrait (`trades_needed()`).

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

**Ces chiffres viennent de données simulées, pas du marché.**

### Le premier passage sur données réelles (Saxo, EUR/USD, 1200 bougies)

| Intervalle | Trades | Réussite | Seuil | P/L | Malchance | Verdict |
|---|---|---|---|---|---|---|
| H1 | — | — | — | — | — | AUCUN TRADE (spread > 15 % du stop) |
| H4 | 65 | 33,8 % | 41,0 % | −29,18 | 14,6 % | NON CONCLUANT |

**Ce passage ne condamne pas la stratégie et ne la sauve pas**, pour trois
raisons distinctes.

*Trop peu de trades.* 65 trades à 7 points sous le seuil, c'est un tirage qui
arriverait par pure malchance environ une fois sur sept. Il en faudrait
environ **134** au même taux pour conclure.

*Un spread de marché fermé.* Le relevé a été fait un samedi soir, marché des
changes fermé (il ferme le vendredi vers 21 h UTC et rouvre le dimanche vers
21 h UTC). Saxo affichait alors **0,00052, soit 5,2 pips** sur EUR/USD, là où
les heures d'ouverture donnent 0,6 à 1,5 pip. Ce spread élargi et figé a été
appliqué aux 1200 bougies de cotations *en semaine* : il a fait refuser
100 % des entrées sur H1, et décalé de 5,2 pips au lieu de 1 les stops et
objectifs de H4. `analysis.py` refusait déjà de trader sur un `Quote` non
négociable ; le carnet, lui, ne regardait pas. Il le fait maintenant, et
**balaie une fourchette de spreads** au lieu d'en relever un seul — parce
qu'un verdict ne vaut que pour le spread qui l'a produit.

*1200 bougies ne suffisent pas.* C'était la limite d'une requête chez Saxo.
`fetch_history` pagine désormais : 6000 bougies H4 donnent de l'ordre de 300
trades, au-delà des ~134 nécessaires pour trancher. Ce qui suit reste à
mesurer aux heures d'ouverture du marché.

**À quoi s'attendre, honnêtement :** le verdict le plus probable est
*perdant*. Un croisement de moyennes mobiles avec stop sur l'ATR est une
stratégie classique et publique ; après spread et financement, ce genre de
règle est net négatif le plus souvent. Plus de données sert à obtenir une
réponse solide, pas à en espérer une meilleure.

## Notifications : paliers 25 / 50 / 75 / 100 %

Pendant une session, tu es notifié à chaque quart de progression — dans les
**deux directions** :

| Palier | Vers l'objectif | Vers la perte max |
|---|---|---|
| 25% | 📈 25% de l'objectif | ⚠️ 25% de la perte max |
| 50% | 📈 50% de l'objectif | ⚠️ 50% de la perte max |
| 75% | 📈 75% de l'objectif | ⚠️ 75% de la perte max |
| 100% | 🎉 Objectif atteint | 🛑 Perte max atteinte |

**Toute fin de session est signalée**, y compris celles qui n'atteignent
aucun palier : plafond de trades, spread devenu trop cher, erreur, arrêt
manuel. Sans ça, la session s'arrêterait en silence — plus aucun ordre passé,
et rien pour le dire. Le code vérifie si un palier 100 % vient d'être émis :
si oui la fin est déjà annoncée, sinon une notification dédiée part. Pas de
doublon, jamais de silence.

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

## Où est l'application ?

Un seul processus sert **tout** : l'API et la page. `app/main.py` monte
`frontend/` en fichiers statiques à la racine, après toutes les routes `/api`.
Deux raisons, et la seconde est la vraie :

- une seule origine, donc **aucun CORS** à régler — et c'est un réglage CORS
  (`allow_methods=["GET"]` avec un endpoint en POST) qui avait déjà bloqué
  tous les envois d'ordres ;
- une seule URL. Sur un téléphone, « ouvrir l'app » doit être un lien, pas un
  serveur à lancer à la main.

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # puis renseigne SAXO_ACCESS_TOKEN
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Puis `http://localhost:8000` — l'app, pas seulement l'API. Depuis un téléphone
sur le même Wi-Fi, remplace `localhost` par l'IP de la machine.

### Sur l'écran d'accueil de l'iPhone

Safari → Partager → **Sur l'écran d'accueil**. La page devient une icône et
s'ouvre en plein écran (`display: standalone` dans le manifeste). Cette étape
n'est pas cosmétique : **iOS n'autorise les notifications push que pour une
page installée sur l'écran d'accueil**. Sans elle, les paliers 25/50/75/100 %
ne peuvent pas arriver sur le téléphone.

### Mise en ligne

Le `Dockerfile` à la racine construit l'ensemble — un seul conteneur, prêt pour
n'importe quel hébergeur qui impose son port par la variable `PORT` (Render,
Fly, Railway, Scaleway…).

```bash
docker build -t bot-trading .
docker run -p 8000:8000 --env-file backend/.env bot-trading
```

⚠️ L'image n'a **pas** pu être construite ici (pas de démon Docker dans
l'environnement où ce code a été écrit) : les chemins des `COPY` sont vérifiés,
et la commande de lancement est celle qui tourne en local, mais la
construction elle-même reste à valider chez toi.

Un seul worker, volontairement : l'état des sessions vit en mémoire dans le
`SessionManager`. Avec plusieurs workers, chaque requête tomberait sur un
processus différent et la session « active » apparaîtrait puis disparaîtrait
au hasard du routage. Rendre l'état partagé (Redis, base) est le prix à payer
pour passer à l'échelle — inutile pour un utilisateur unique.

⚠️ Deux points avant de mettre en ligne un bot qui peut trader :

- **Le jeton Saxo du portail développeur expire au bout de 24 h.** Un bot censé
  tourner en continu a besoin d'une application OAuth enregistrée, pas de ce
  jeton. En l'état, il faut le renouveler chaque jour.
- **L'URL ne demande aucun mot de passe.** Tant que `LIVE_TRADING_CONFIRMED`
  vaut `false`, aucun ordre ne part. Avant de le passer à `true` sur une URL
  publique, il faut une authentification — sinon n'importe qui connaissant
  l'adresse peut lancer une session sur ton compte.

## Ce que vérifient les tests du front

Le front n'a pas de compilateur : une faute de nom n'y produit aucune erreur,
juste un « undefined » à l'écran ou un bouton qui ne fait rien. Trois bugs de
ce genre ont vécu dans ce dépôt, tous muets :

| Bug | Symptôme | Durée de vie |
|---|---|---|
| `t.currentUnits`, `t.unrealizedPL` (noms camelCase d'OANDA) | « undefined unités » dans les positions | depuis la refonte courtier |
| `export` dans `push.js`, chargé en script classique | bouton de notifications inerte, tout le fichier ignoré | depuis l'origine |
| `m.threshold` au lieu de `m.percent`, `m.direction` au lieu de `m.kind` | paliers affichés « 0.25 % », pastilles sans couleur | découvert en pilotant l'app |

`tests/test_frontend.py` relie donc les trois fichiers entre eux et à l'API :
chaque `id` lu existe dans le HTML (et réciproquement, sinon c'est du code
mort), chaque champ lu sur une réponse existe dans le dataclass correspondant,
chaque URL appelée correspond à une route, et aucun script classique ne
contient d'`export`.

## Ce que fait le scanner (Phase 1)

Pour chaque instrument tradable de ton compte, il récupère les dernières bougies OANDA et calcule l'amplitude `(high - low) / low * 100` sur la période choisie, puis classe les instruments du plus au moins volatil. Résultat exposé sur `GET /api/scanner/top-volatile`.
