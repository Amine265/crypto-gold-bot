# Bot Crypto & Or — Alertes Telegram, Copytrading paper et Cockpit

Trois briques dans un seul dépôt GitHub, le tout 100% gratuit et sans serveur à gérer :

**1. Signaux de marché** (`bot.py`) — analyse BTC, ETH et l'or (via PAXG) toutes les heures : RSI, croisements SMA 20/50 et MACD, avec alertes Telegram et anti-doublons.

**2. Copytrading paper** (`copytrader.py`) — suit les traders les plus performants du classement public Hyperliquid (ROI 30 jours, compte de plus de 100 000 $), t'alerte sur Telegram dès qu'ils ouvrent, ferment ou retournent une position, et réplique leurs trades dans un portefeuille **virtuel** de 10 000 $ pour mesurer ce que ça donnerait avant de risquer un euro. Aucun ordre réel n'est passé, aucune clé de trading n'est nécessaire.

**3. Cockpit** (`docs/index.html`) — tableau de bord hébergé gratuitement sur GitHub Pages : cadrans RSI style instruments de bord, prix en direct, équité du portefeuille paper avec sa courbe, positions des top traders, journal des signaux, et suivi optionnel de ton wallet en lecture seule.

## Installation

### Étape 1 — Bot Telegram
1. Sur Telegram, cherche **@BotFather**, envoie `/newbot` et suis les instructions pour obtenir ton **token**.
2. Écris un premier message à ton bot, puis ouvre `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` dans un navigateur et note le `"chat":{"id": ...}` : c'est ton **chat_id**.

### Étape 2 — Dépôt GitHub
Crée un dépôt (il doit être **public** pour que GitHub Pages soit gratuit) et téléverse tous les fichiers de ce dossier en conservant la structure, y compris `.github/workflows/` et `docs/`.

### Étape 3 — Secrets
Dans **Settings → Secrets and variables → Actions**, crée : `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, et si tu veux suivre ton wallet dans le cockpit, `MY_WALLET` avec ton **adresse publique** (jamais ta clé privée ni ta seed phrase — le bot n'en a pas besoin et ne doit jamais les connaître).

### Étape 4 — Activer le cockpit
Dans **Settings → Pages**, choisis Source : "Deploy from a branch", branche `main`, dossier `/docs`. Ton cockpit sera en ligne à l'adresse `https://ton-pseudo.github.io/nom-du-depot/` après une ou deux minutes.

### Étape 5 — Tester
Onglet **Actions** → "Bot Crypto & Or" → **Run workflow**. Ensuite tout tourne automatiquement toutes les heures : le bot analyse, alerte, et pousse les données fraîches vers le cockpit. Le fichier `docs/data.json` fourni contient des données de démonstration qui seront remplacées à la première exécution réelle.

## À propos du wallet

Ce projet ne crée volontairement pas de wallet : générer et transmettre une clé privée via Telegram ou un serveur serait dangereux. Crée ton wallet dans une application dédiée (Ledger, Trust Wallet, Rabby...) et donne uniquement ton adresse publique au bot pour le suivi en lecture seule.

## Personnalisation

Dans `copytrader.py` : `TOP_N` (nombre de traders suivis), `MIN_ACCOUNT_VALUE` (filtre de sérieux), `ALLOC_PCT` (part du capital virtuel par trade répliqué). Dans `bot.py` : la liste `ASSETS` (identifiants CoinGecko) et les seuils RSI/SMA.

## Avertissement

Portefeuille virtuel et indicateurs techniques à but informatif uniquement. Les performances passées des traders suivis ne garantissent rien, et rien ici ne constitue un conseil financier. Si un jour tu envisages le passage en trading réel, fais-le avec des montants que tu peux te permettre de perdre, et sache que cela exigerait de confier des clés API de trading à un script : un risque à ne prendre qu'en toute connaissance de cause.

---

# Nouveautés v3

## Trois profils d'investissement simulés

`copytrader.py` fait désormais tourner trois portefeuilles virtuels de 10 000 $ en parallèle, chacun avec sa stratégie : **Prudent** (3 traders à gros comptes, ROI 30j plafonné à 300% pour écarter les profils intenables, pas de short, 3% par position), **Équilibré** (5 traders, long et short, 5% par position) et **Agressif** (8 traders au ROI maximal, 10% par position). Le cockpit affiche les trois courbes côte à côte : après quelques semaines, la comparaison des équités et des creux te dira quelle stratégie tient la route.

## Bot Telegram interactif (`worker/worker.js`)

Un Cloudflare Worker (gratuit, aucun serveur) répond instantanément aux commandes : `/prix`, `/portefeuille`, `/traders`, `/signaux`, avec un bouton qui ouvre le cockpit directement dans Telegram.

Déploiement en 4 étapes :
1. Crée un compte sur dash.cloudflare.com (gratuit), puis Workers & Pages → Create Worker → colle le contenu de `worker/worker.js` → Deploy. Note l'URL (`https://xxx.workers.dev`).
2. Dans le Worker : Settings → Variables → ajoute un **secret** `TELEGRAM_TOKEN` avec ton token BotFather.
3. Enregistre le webhook en ouvrant dans ton navigateur : `https://api.telegram.org/bot<TON_TOKEN>/setWebhook?url=https://xxx.workers.dev` (réponse attendue : `"ok":true`).
4. Bouton menu cockpit : @BotFather → `/mybots` → ton bot → Bot Settings → Menu Button → colle l'URL du cockpit.

Si tu changes de dépôt ou de compte GitHub, mets à jour `DATA_URL` et `COCKPIT_URL` en haut de `worker.js`.

# Nouveautés v4 — Plans de trade SL/TP

Chaque alerte inclut désormais un plan indicatif calibré sur la volatilité réelle de l'actif (ATR 14 périodes) : prix d'entrée, stop-loss à 1,5 ATR, take-profit à ratio risque/gain 1:2 (plus TP1/TP2 et support/résistance 48h pour les signaux de marché). Les portefeuilles paper appliquent ces niveaux : une position répliquée se clôture automatiquement quand son SL (🛑) ou son TP (🎯) est touché, ce qui rend la simulation plus proche d'une gestion réelle. Limite à connaître : le contrôle est horaire, donc une mèche intra-horaire qui toucherait un niveau entre deux passages n'est pas vue — en trading réel, SL et TP seraient des ordres placés sur l'exchange, exécutés instantanément.

# v6 — Agent d'exécution Kraken (spot, achat uniquement)

`agent.py` exécute les signaux d'achat ✅ sur Kraken selon le protocole (deux moitiés, TP1/TP2, SL systématique, SL remonté à l'entrée après TP1). Trois modes pilotés depuis Telegram : **blanc** (validation Kraken sans exécution), **bouton** (chaque trade attend ton ✅), **auto**. Garde-fous codés en dur : achats spot uniquement, 50 $ max/position, 3 trades/jour, pause automatique après 3 SL consécutifs, `/pause` à tout moment, suspension automatique des types de signaux statistiquement perdants (≥5 occurrences, ≥60 % d'échecs), et refus de démarrer si la clé API a des droits de retrait. L'agent tourne aux 15 minutes : les stops "durs" vivent sur Kraken en temps réel, les prises de profit complémentaires sont gérées aux quarts d'heure. Par défaut l'agent démarre **en pause et en mode blanc** — rien ne s'exécute tant que tu ne l'as pas armé toi-même depuis Telegram.
