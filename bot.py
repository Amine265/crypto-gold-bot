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
import time
from pathlib import Path

import pandas as pd
import requests

# ------------------------- Configuration -------------------------

ASSETS = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "ripple": "XRP",
    "chainlink": "LINK",
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
    time.sleep(2)  # courtoisie : espace les appels CoinGecko (6 actifs/run)
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

    # Volatilité et niveaux (pour le plan de trade indicatif)
    atr = p.diff().abs().rolling(14).mean().iloc[-1]  # ATR approx. (clôtures horaires)
    return {
        "price": last["price"],
        "rsi": last["rsi"],
        "buy": buy,
        "sell": sell,
        "atr": atr,
        "support": p.tail(48).min(),      # plus bas 48h
        "resistance": p.tail(48).max(),   # plus haut 48h
    }


def build_plan(price: float, atr: float, side: str) -> dict:
    """Plan indicatif basé sur la volatilité : SL à 1,5 ATR, TP1 1:1, TP2 1:2."""
    d = 1 if side == "achat" else -1
    sl = price - d * 1.5 * atr
    return {
        "entry": round(price, 2),
        "sl": round(sl, 2),
        "tp1": round(price + d * 1.5 * atr, 2),
        "tp2": round(price + d * 3.0 * atr, 2),
        "risk_pct": round(abs(price - sl) / price * 100, 2),
    }


# Simulation multi-calibrage : 3 variantes de plan suivies en silence à chaque
# signal d'achat, pour comparer les réglages SL/TP sur données réelles (/calibrages).
# Aucun effet sur les alertes, verdicts ni l'agent.
CALIBRAGES = {
    "A": {"sl": 1.5, "tp1": 1.5, "tp2": 3.0},   # témoin (réglage actuel)
    "B": {"sl": 1.5, "tp1": 2.5, "tp2": 5.0},   # ample
    "C": {"sl": 2.5, "tp1": 2.5, "tp2": 5.0},   # large
}


def enregistrer_calibrages(data: dict, now: str, label: str, coin_id: str,
                           price: float, atr: float) -> None:
    for variante, k in CALIBRAGES.items():
        data.setdefault("calibrages_actifs", []).append({
            "time": now, "asset": label, "coin": coin_id, "variante": variante,
            "entry": round(price, 2),
            "sl": round(price - k["sl"] * atr, 2),
            "tp1": round(price + k["tp1"] * atr, 2),
            "tp2": round(price + k["tp2"] * atr, 2),
            "tp1_franchi": False,
        })


ENVELOPPE_SPOT = 100.0   # $ — même valeur que le worker (/spot)
FRAIS_ORDRE = 0.0025     # estimation par ordre

def verdict_spot(plan: dict) -> tuple[str, str]:
    """Verdict + montants nets (frais inclus), affichés dès l'alerte.
    Règle : ✅ si le net à TP1 est ≥ 0 (au pire, TP1 rembourse les frais
    et le gain se joue à TP2) ; ⚠️ si seul TP2 est gagnant ; ⛔ sinon."""
    taille = min(ENVELOPPE_SPOT, (ENVELOPPE_SPOT * 0.02) / (plan["risk_pct"] / 100))
    frais = taille * FRAIS_ORDRE * 2
    net_tp1 = taille * abs(plan["tp1"] - plan["entry"]) / plan["entry"] - frais
    net_tp2 = taille * abs(plan["tp2"] - plan["entry"]) / plan["entry"] - frais
    plan["net_tp1"] = round(net_tp1, 2)
    plan["net_tp2"] = round(net_tp2, 2)
    nets = (f"Net possible ({taille:.0f} $, frais inclus) : "
            f"TP1 {net_tp1:+.2f} $ · TP2 {net_tp2:+.2f} $")
    if net_tp2 <= 0:
        return "⛔", f"frais ≥ gain même à TP2 — à laisser passer\n{nets}"
    if net_tp1 < 0:
        return "⚠️", f"rentable seulement si TP2 atteint\n{nets}"
    return "✅", f"exploitable en spot\n{nets}"


def plan_text(plan: dict, support: float, resistance: float) -> str:
    return (f"🎯 Entrée ~{plan['entry']:,.2f} $ | SL {plan['sl']:,.2f} $ "
            f"(-{plan['risk_pct']}%) | TP1 {plan['tp1']:,.2f} $ | TP2 {plan['tp2']:,.2f} $\n"
            f"Support 48h {support:,.2f} $ · Résistance {resistance:,.2f} $")

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


def suivre_plans(data: dict, now: str) -> list[str]:
    """Suit les plans des signaux passés : détecte les franchissements de SL/TP1/TP2.

    Contrôle horaire sur le dernier prix connu — annonce un *niveau de prix
    franchi*, pas l'exécution d'un ordre réel. Les plans expirent après 7 jours.
    """
    from datetime import datetime, timedelta
    alerts, restants = [], []
    for pl in data.get("plans_actifs", []):
        m = data.get("market", {}).get(pl["coin"])
        if not m:
            restants.append(pl)
            continue
        price = m["price"]
        d = 1 if pl["type"] == "achat" else -1
        resultat = None
        if d * (price - pl["sl"]) <= 0:
            resultat = "SL"
        elif d * (price - pl["tp2"]) >= 0:
            resultat = "TP2"
        elif not pl.get("tp1_franchi") and d * (price - pl["tp1"]) >= 0:
            pl["tp1_franchi"] = True
            alerts.append(
                f"🎯 <b>TP1 franchi</b> — signal {pl['type'].upper()} {pl['asset']} "
                f"(entrée {pl['entry']:,.2f} $, prix {price:,.2f} $)\n"
                f"Si tu es en position : pense à remonter le SL de la 2e moitié à l'entrée. "
                f"Vérifie ton ordre sur Kraken.")
        age = datetime.fromisoformat(now) - datetime.fromisoformat(pl["time"])
        if resultat:
            pnl = d * (price - pl["entry"]) / pl["entry"] * 100
            emoji = "🛑" if resultat == "SL" else "🎯"
            alerts.append(
                f"{emoji} <b>{resultat} franchi</b> — signal {pl['type'].upper()} {pl['asset']} "
                f"(entrée {pl['entry']:,.2f} $ → {price:,.2f} $, {pnl:+.2f}%)\n"
                f"<i>Niveau de prix franchi (contrôle horaire) — l'état réel de ton ordre "
                f"est sur Kraken.</i>")
            data.setdefault("signaux_resultats", []).insert(0, {
                "time": now, "signal_time": pl["time"], "asset": pl["asset"],
                "type": pl["type"], "resultat": resultat, "reason": pl.get("reason", ""),
                "tp1_franchi": bool(pl.get("tp1_franchi")) or resultat == "TP2",
                "entry": pl["entry"], "sortie": price})
        elif age > timedelta(days=7):
            data.setdefault("signaux_resultats", []).insert(0, {
                "time": now, "signal_time": pl["time"], "asset": pl["asset"],
                "type": pl["type"], "resultat": "expiré", "reason": pl.get("reason", ""),
                "tp1_franchi": bool(pl.get("tp1_franchi")), "entry": pl["entry"],
                "sortie": price})
        else:
            restants.append(pl)
    data["plans_actifs"] = restants[-20:]
    data["signaux_resultats"] = data.get("signaux_resultats", [])[:50]
    return alerts


def suivre_calibrages(data: dict, now: str) -> None:
    """Jumelle silencieuse de suivre_plans pour les variantes de calibrage.

    Mêmes règles (franchissements sur le dernier prix connu, expiration 7 jours),
    achats uniquement, aucun message Telegram — uniquement de la donnée pour
    la commande /calibrages du worker.
    """
    from datetime import datetime, timedelta
    restants = []
    for pl in data.get("calibrages_actifs", []):
        m = data.get("market", {}).get(pl["coin"])
        if not m:
            restants.append(pl)
            continue
        price = m["price"]
        resultat = None
        if price <= pl["sl"]:
            resultat = "SL"
        elif price >= pl["tp2"]:
            resultat = "TP2"
        elif not pl.get("tp1_franchi") and price >= pl["tp1"]:
            pl["tp1_franchi"] = True
        age = datetime.fromisoformat(now) - datetime.fromisoformat(pl["time"])
        if not resultat and age > timedelta(days=7):
            resultat = "expiré"
        if resultat:
            data.setdefault("calibrages_resultats", []).insert(0, {
                **pl,
                "time": now, "signal_time": pl["time"], "resultat": resultat,
                "tp1_franchi": bool(pl.get("tp1_franchi")) or resultat == "TP2",
                "sortie": price})
        else:
            restants.append(pl)
    data["calibrages_actifs"] = restants[-60:]
    data["calibrages_resultats"] = data.get("calibrages_resultats", [])[:200]


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
            p = build_plan(res["price"], res["atr"], "achat")
            p["verdict"] = verdict_spot(p)[0]
            data["signals"].insert(0, {"time": now, "asset": label, "type": "achat",
                                       "reason": s, "plan": p})
        for s in res["sell"]:
            p = build_plan(res["price"], res["atr"], "vente")
            p["verdict"] = "ℹ️"
            data["signals"].insert(0, {"time": now, "asset": label, "type": "vente",
                                       "reason": s, "plan": p})

        signature = "|".join(res["buy"] + res["sell"])
        already_sent = state.get(coin_id) == signature and signature != ""

        if (res["buy"] or res["sell"]) and not already_sent:
            lines = [f"<b>{label}</b> — {res['price']:,.2f} $ | RSI {res['rsi']:.1f}"]
            for s in res["buy"]:
                lines.append(f"🟢 <b>ACHAT</b> : {s}")
            for s in res["sell"]:
                lines.append(f"🔴 <b>VENTE</b> : {s}")
            side = "achat" if res["buy"] else "vente"
            plan = build_plan(res["price"], res["atr"], side)
            lines.append(plan_text(plan, res["support"], res["resistance"]))
            if side == "achat":
                emoji_v, texte_v = verdict_spot(plan)
                lines.append(f"{emoji_v} <b>Spot :</b> {texte_v}")
                plan["verdict"] = emoji_v
            else:
                lines.append("ℹ️ Vente — non jouable en spot (sortie/contexte uniquement)")
                plan["verdict"] = "ℹ️"
            alerts.append("\n".join(lines))
            data.setdefault("plans_actifs", []).append(
                {"time": now, "asset": label, "coin": coin_id, "type": side,
                 "reason": (res["buy"] + res["sell"])[0],
                 **plan, "tp1_franchi": False})
            if side == "achat":
                enregistrer_calibrages(data, now, label, coin_id,
                                       res["price"], res["atr"])

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

    # Suivi des plans des signaux précédents (SL/TP franchis, pris ou non)
    suivis = suivre_plans(data, now)
    if suivis:
        send_telegram("📡 <b>Suivi des signaux</b>\n\n" + "\n\n".join(suivis))
        print(f"{len(suivis)} franchissement(s) de niveau annoncé(s).")

    # Suivi silencieux des variantes de calibrage (données pour /calibrages)
    suivre_calibrages(data, now)

    save_state(state)
    data["signals"] = data["signals"][:50]
    data["market_updated"] = now
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
