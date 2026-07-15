# 🛰️ crypto-gold-bot

Bot de surveillance **Bitcoin / Ethereum / Or** qui tourne entièrement sur GitHub Actions :

- **Toutes les heures** (et à la demande), le workflow [`Bot Crypto & Or`](.github/workflows/bot.yml) exécute [`bot.py`](bot.py).
- Le bot récupère les prix via **CoinGecko** (l'or via le jeton **PAXG**, adossé à 1 once d'or).
- Un **signal** est actif quand la variation sur 24 h dépasse le seuil : **±5 %** pour BTC/ETH, **±1,5 %** pour l'or.
- Au **changement de signal**, une alerte est envoyée sur **Telegram** (anti-spam : pas de répétition tant que le signal ne change pas).
- Chaque run met à jour [`docs/data.json`](docs/data.json), affiché par le **cockpit GitHub Pages** : `docs/index.html`.
- En lancement **manuel** (`workflow_dispatch`) sans signal actif, le bot envoie un message de confirmation « ✅ opérationnel » avec les prix du moment.

## Secrets du dépôt (Settings → Secrets and variables → Actions)

| Secret | Rôle |
|---|---|
| `TELEGRAM_TOKEN` | Token du bot Telegram (via @BotFather) |
| `TELEGRAM_CHAT_ID` | Identifiant du chat qui reçoit les alertes |
| `MY_WALLET` | *(optionnel)* adresse **publique** Ethereum `0x…` — le solde ETH est affiché dans le cockpit |

## Cockpit

GitHub Pages sert le dossier `docs/` de la branche `main` :
prix, variation 24 h, badges de signal, historique 7 jours (sparklines) et solde du wallet.

## Lancer manuellement

```bash
gh workflow run "Bot Crypto & Or"
```

> ⚠️ Ceci n'est pas un conseil en investissement.
