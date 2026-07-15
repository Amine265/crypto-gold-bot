#!/usr/bin/env python3
"""Bot Crypto & Or — surveille BTC, ETH et l'or (via PAXG), envoie des alertes
Telegram quand un signal est actif et met à jour docs/data.json pour le cockpit
GitHub Pages. Bibliothèque standard uniquement (aucune dépendance)."""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime, timezone

DATA_FILE = os.path.join(os.path.dirname(__file__), "docs", "data.json")

# Seuils de variation sur 24 h (en %) qui déclenchent un signal
ASSETS = {
    "bitcoin":  {"name": "Bitcoin",  "symbol": "BTC",  "threshold": 5.0},
    "ethereum": {"name": "Ethereum", "symbol": "ETH",  "threshold": 5.0},
    "pax-gold": {"name": "Or (PAXG)", "symbol": "OR", "threshold": 1.5},
}

COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=" + ",".join(ASSETS) + "&vs_currencies=usd&include_24hr_change=true"
)
ETH_RPC_URL = "https://cloudflare-eth.com"
HISTORY_MAX = 168  # 7 jours en pas horaire


def http_json(url, payload=None, headers=None):
    data = None
    req_headers = {"User-Agent": "crypto-gold-bot/1.0", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode()
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_prices():
    raw = http_json(COINGECKO_URL)
    prices = {}
    for coin_id, meta in ASSETS.items():
        entry = raw.get(coin_id)
        if not entry or "usd" not in entry:
            raise RuntimeError(f"Réponse CoinGecko incomplète pour {coin_id}: {raw}")
        prices[coin_id] = {
            "name": meta["name"],
            "symbol": meta["symbol"],
            "price_usd": round(float(entry["usd"]), 2),
            "change_24h": round(float(entry.get("usd_24h_change") or 0.0), 2),
            "threshold": meta["threshold"],
        }
    return prices


def compute_signal(asset):
    if asset["change_24h"] >= asset["threshold"]:
        return "hausse"
    if asset["change_24h"] <= -asset["threshold"]:
        return "baisse"
    return None


def fetch_wallet(address):
    """Solde ETH d'une adresse publique via un RPC public. Optionnel et tolérant aux pannes."""
    try:
        result = http_json(ETH_RPC_URL, payload={
            "jsonrpc": "2.0", "method": "eth_getBalance",
            "params": [address, "latest"], "id": 1,
        })
        wei = int(result["result"], 16)
        return {"address": address, "eth_balance": round(wei / 1e18, 6)}
    except Exception as exc:  # le solde du wallet ne doit jamais faire échouer le run
        print(f"::warning::Lecture du wallet impossible : {exc}")
        return {"address": address, "eth_balance": None}


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = http_json(url, payload={
        "chat_id": chat_id, "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": True,
    })
    if not resp.get("ok"):
        raise RuntimeError(f"Échec de l'envoi Telegram : {resp}")


def fmt_price(value):
    return f"{value:,.2f}".replace(",", " ") + " $"


def main():
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    wallet_addr = os.environ.get("MY_WALLET", "").strip()
    is_manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"

    if not token or not chat_id:
        print("::error::TELEGRAM_TOKEN ou TELEGRAM_CHAT_ID manquant (secrets du dépôt).")
        sys.exit(1)

    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}

    prices = fetch_prices()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    previous_signals = {
        coin_id: (previous.get("assets", {}).get(coin_id) or {}).get("signal")
        for coin_id in ASSETS
    }

    alerts = []
    for coin_id, asset in prices.items():
        signal = compute_signal(asset)
        asset["signal"] = signal
        # Anti-spam : on n'alerte que lorsque le signal change
        if signal and signal != previous_signals.get(coin_id):
            arrow = "📈" if signal == "hausse" else "📉"
            alerts.append(
                f"{arrow} <b>{asset['name']}</b> : {asset['change_24h']:+.2f}% sur 24 h "
                f"(seuil ±{asset['threshold']}%) — {fmt_price(asset['price_usd'])}"
            )

    wallet = fetch_wallet(wallet_addr) if wallet_addr else None

    history = previous.get("history", [])
    history.append({
        "t": now,
        "btc": prices["bitcoin"]["price_usd"],
        "eth": prices["ethereum"]["price_usd"],
        "gold": prices["pax-gold"]["price_usd"],
    })
    history = history[-HISTORY_MAX:]

    data = {
        "updated_at": now,
        "assets": prices,
        "wallet": wallet,
        "alerts_sent": len(alerts),
        "history": history,
    }
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"docs/data.json mis à jour ({now}) — {len(alerts)} alerte(s).")

    if alerts:
        send_telegram(token, chat_id, "🚨 <b>Bot Crypto &amp; Or</b>\n\n" + "\n".join(alerts))
        print("Alerte(s) envoyée(s) sur Telegram.")
    elif is_manual:
        # Lancement manuel : message de confirmation pour vérifier la chaîne Telegram
        lines = [
            f"• {a['name']} : {fmt_price(a['price_usd'])} ({a['change_24h']:+.2f}%/24 h)"
            for a in prices.values()
        ]
        if wallet and wallet["eth_balance"] is not None:
            lines.append(f"• Wallet : {wallet['eth_balance']} ETH")
        send_telegram(
            token, chat_id,
            "✅ <b>Bot Crypto &amp; Or opérationnel</b>\n"
            "Aucun signal actif pour le moment.\n\n" + "\n".join(lines),
        )
        print("Message de confirmation envoyé sur Telegram (lancement manuel).")
    else:
        print("Aucun signal actif : pas de message Telegram (exécution planifiée).")


if __name__ == "__main__":
    main()
