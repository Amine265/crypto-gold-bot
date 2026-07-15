"""
Bot d'analyse Crypto + Or avec alertes Telegram
------------------------------------------------
- Récupère les prix horaires via l'API CoinGecko (gratuite, sans clé)
- Actifs suivis : Bitcoin, Ethereum, Or (via PAXG, jeton adossé à 1 once d'or)
- Indicateurs : RSI(14), croisement SMA 20/50, croisement MACD (12/26/9)
- Envoie une alerte Telegram quand un signal d'ACHAT ou de VENTE apparaît
- Conçu pour tourner via GitHub Actions (exécution toutes les heures)

⚠️ Outil d'aide à la décision uniquement. Ce n'est pas un conseil financier.
"""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import requests

# ------------------------- Configuration -------------------------

ASSETS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "pax-gold": "OR (once, via PAXG)",
}

RSI_PERIOD = 14
RSI_OVERSOLD = 30      # en dessous -> signal d'achat potentiel
RSI_OVERBOUGHT = 70    # au-dessus  -> signal de vente potentiel
SMA_FAST = 20
SMA_SLOW = 50

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STATE_FILE = Path("state.json")  # évite d'envoyer 2x la même alerte
DATA_FILE = Path("docs/data.json")  # données publiées pour le cockpit

# ------------------------- Données marché -------------------------

def get_hourly_prices(coin_id: str) -> pd.DataFrame:
    """Prix horaires des 14 derniers jours via CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    r = requests.get(
        url,
        params={"vs_currency": "usd", "days": 14},
        timeout=30,
        headers={"Accept": "application/json"},
    )
    r.raise_for_status()
    df = pd.DataFrame(r.json()["prices"], columns=["ts", "price"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms")
    return df

# ------------------------- Indicateurs -------------------------

def rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def macd(prices: pd.Series):
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    return macd_line, signal_line


def analyse(df: pd.DataFrame) -> dict:
    """Calcule les indicateurs et détecte les signaux frais (dernière bougie)."""
    p = df["price"]
    df = df.copy()
    df["rsi"] = rsi(p, RSI_PERIOD)
    df["sma_fast"] = p.rolling(SMA_FAST).mean()
    df["sma_slow"] = p.rolling(SMA_SLOW).mean()
    df["macd"], df["macd_sig"] = macd(p)

    last, prev = df.iloc[-1], df.iloc[-2]
    buy, sell = [], []

    # RSI : franchissement des seuils
    if prev["rsi"] >= RSI_OVERSOLD > last["rsi"]:
        buy.append(f"RSI passé en zone de survente ({last['rsi']:.1f})")
    if prev["rsi"] <= RSI_OVERBOUGHT < last["rsi"]:
        sell.append(f"RSI passé en zone de surachat ({last['rsi']:.1f})")

    # Croisement de moyennes mobiles
    if prev["sma_fast"] <= prev["sma_slow"] and last["sma_fast"] > last["sma_slow"]:
        buy.append(f"Croisement haussier SMA{SMA_FAST}/SMA{SMA_SLOW} (golden cross)")
    if prev["sma_fast"] >= prev["sma_slow"] and last["sma_fast"] < last["sma_slow"]:
        sell.append(f"Croisement baissier SMA{SMA_FAST}/SMA{SMA_SLOW} (death cross)")

    # Croisement MACD
    if prev["macd"] <= prev["macd_sig"] and last["macd"] > last["macd_sig"]:
        buy.append("Croisement haussier du MACD")
    if prev["macd"] >= prev["macd_sig"] and last["macd"] < last["macd_sig"]:
        sell.append("Croisement baissier du MACD")

    return {
        "price": last["price"],
        "rsi": last["rsi"],
        "buy": buy,
        "sell": sell,
    }

# ------------------------- Telegram -------------------------

def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()

# ------------------------- État (anti-doublons) -------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ------------------------- Programme principal -------------------------

def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("ERREUR : définis TELEGRAM_TOKEN et TELEGRAM_CHAT_ID.")
        return 1

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    state = load_state()
    data = load_data()
    data.setdefault("market", {})
    data.setdefault("signals", [])
    alerts = []

    for coin_id, label in ASSETS.items():
        try:
            df = get_hourly_prices(coin_id)
            res = analyse(df)
        except Exception as e:
            print(f"[{label}] erreur : {e}")
            continue

        # Publication pour le cockpit
        sparkline = [round(v, 2) for v in df["price"].iloc[::14].tail(48).tolist()]
        data["market"][coin_id] = {
            "label": label,
            "price": round(res["price"], 2),
            "rsi": round(res["rsi"], 1),
            "sparkline": sparkline,
        }
        for s in res["buy"]:
            data["signals"].insert(0, {"time": now, "asset": label, "type": "achat", "reason": s})
        for s in res["sell"]:
            data["signals"].insert(0, {"time": now, "asset": label, "type": "vente", "reason": s})

        signature = "|".join(res["buy"] + res["sell"])
        already_sent = state.get(coin_id) == signature and signature != ""

        if (res["buy"] or res["sell"]) and not already_sent:
            lines = [f"<b>{label}</b> — {res['price']:,.2f} $ | RSI {res['rsi']:.1f}"]
            for s in res["buy"]:
                lines.append(f"🟢 <b>ACHAT</b> : {s}")
            for s in res["sell"]:
                lines.append(f"🔴 <b>VENTE</b> : {s}")
            alerts.append("\n".join(lines))

        state[coin_id] = signature
        print(f"[{label}] prix={res['price']:,.2f}$ rsi={res['rsi']:.1f} "
              f"achat={res['buy']} vente={res['sell']}")

    if alerts:
        msg = "🚨 <b>Signaux de marché</b>\n\n" + "\n\n".join(alerts)
        msg += "\n\n<i>Indicateurs techniques — pas un conseil financier.</i>"
        send_telegram(msg)
        print("Alerte Telegram envoyée.")
    elif os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        # Lancement manuel : confirmation que la chaîne Telegram fonctionne
        lines = [
            f"• {m['label']} : {m['price']:,.2f} $ | RSI {m['rsi']}"
            for m in data["market"].values()
        ]
        send_telegram(
            "✅ <b>Bot Crypto &amp; Or opérationnel</b>\n"
            "Aucun nouveau signal pour le moment.\n\n" + "\n".join(lines)
        )
        print("Message de confirmation envoyé sur Telegram (lancement manuel).")
    else:
        print("Aucun nouveau signal.")

    save_state(state)
    data["signals"] = data["signals"][:50]
    data["market_updated"] = now
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
