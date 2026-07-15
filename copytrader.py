"""
Copytrading (mode PAPER) — Top traders Hyperliquid
---------------------------------------------------
- Récupère le classement public des traders Hyperliquid
- Sélectionne les plus performants sur 30 jours (filtres de sérieux)
- Détecte leurs ouvertures / fermetures / retournements de positions
- T'alerte sur Telegram et réplique en PAPER TRADING (argent virtuel)
- Publie tout dans docs/data.json pour le cockpit
- Optionnel : suit ton propre wallet (adresse publique, lecture seule)
  via la variable d'environnement MY_WALLET

⚠️ Aucune exécution réelle : portefeuille 100% virtuel (10 000 $ fictifs).
   Ce n'est pas un conseil financier.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------- Configuration -------------------------

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_URL = "https://api.hyperliquid.xyz/info"

TOP_N = 5                    # nombre de traders suivis
MIN_ACCOUNT_VALUE = 100_000  # $ minimum sur le compte (filtre anti-chanceux)
PAPER_START = 10_000.0       # capital virtuel de départ
ALLOC_PCT = 0.05             # 5% du capital par position répliquée
MAX_POSITIONS = 10

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MY_WALLET = os.environ.get("MY_WALLET", "").strip()

STATE_FILE = Path("state_copy.json")
DATA_FILE = Path("docs/data.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ------------------------- API Hyperliquid -------------------------

def post_info(payload: dict) -> dict | list:
    r = requests.post(INFO_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_leaderboard() -> list[dict]:
    """Top traders sur 30 jours, filtrés par taille de compte."""
    r = requests.get(LEADERBOARD_URL, timeout=30)
    r.raise_for_status()
    rows = r.json().get("leaderboardRows", [])
    out = []
    for row in rows:
        perf = dict(row.get("windowPerformances", []))
        month = perf.get("month") or {}
        acct = float(row.get("accountValue", 0) or 0)
        roi = float(month.get("roi", 0) or 0)
        pnl = float(month.get("pnl", 0) or 0)
        if acct >= MIN_ACCOUNT_VALUE and pnl > 0:
            out.append({
                "address": row.get("ethAddress", ""),
                "name": row.get("displayName") or row.get("ethAddress", "")[:8],
                "account_value": acct,
                "roi_30d": roi,
                "pnl_30d": pnl,
            })
    out.sort(key=lambda t: t["roi_30d"], reverse=True)
    return out[:TOP_N]


def get_positions(address: str) -> dict[str, dict]:
    """Positions ouvertes d'un wallet -> {coin: {side, size, entry, pnl}}."""
    data = post_info({"type": "clearinghouseState", "user": address})
    positions = {}
    for ap in data.get("assetPositions", []):
        p = ap.get("position", {})
        szi = float(p.get("szi", 0) or 0)
        if szi == 0:
            continue
        positions[p["coin"]] = {
            "side": "LONG" if szi > 0 else "SHORT",
            "size": abs(szi),
            "entry": float(p.get("entryPx", 0) or 0),
            "pnl": float(p.get("unrealizedPnl", 0) or 0),
        }
    return positions


def get_mids() -> dict[str, float]:
    """Prix mid actuels de tous les actifs Hyperliquid."""
    data = post_info({"type": "allMids"})
    return {k: float(v) for k, v in data.items() if not k.startswith("@")}

# ------------------------- Paper trading -------------------------

def paper_open(paper: dict, coin: str, side: str, price: float, trader: str) -> str | None:
    if price <= 0 or len(paper["positions"]) >= MAX_POSITIONS:
        return None
    if any(p["coin"] == coin and p["side"] == side for p in paper["positions"]):
        return None  # déjà répliquée
    amount = paper["equity"] * ALLOC_PCT
    if paper["cash"] < amount:
        return None
    paper["cash"] -= amount
    paper["positions"].append({
        "coin": coin, "side": side, "entry": price,
        "amount": amount, "trader": trader, "opened": now_iso(),
    })
    return f"ouverture {side} {coin} à {price:,.2f} $ ({amount:,.0f} $ virtuels)"


def paper_close(paper: dict, coin: str, side: str, price: float) -> str | None:
    for i, p in enumerate(paper["positions"]):
        if p["coin"] == coin and p["side"] == side:
            direction = 1 if side == "LONG" else -1
            pnl = p["amount"] * direction * (price - p["entry"]) / p["entry"]
            paper["cash"] += p["amount"] + pnl
            paper["positions"].pop(i)
            return f"clôture {side} {coin} à {price:,.2f} $ (P&L {pnl:+,.0f} $)"
    return None


def paper_mark_to_market(paper: dict, mids: dict) -> None:
    equity = paper["cash"]
    for p in paper["positions"]:
        price = mids.get(p["coin"], p["entry"])
        direction = 1 if p["side"] == "LONG" else -1
        p["price"] = price
        p["pnl"] = p["amount"] * direction * (price - p["entry"]) / p["entry"]
        equity += p["amount"] + p["pnl"]
    paper["equity"] = round(equity, 2)
    paper["equity_curve"].append([now_iso(), paper["equity"]])
    paper["equity_curve"] = paper["equity_curve"][-500:]

# ------------------------- Persistance -------------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("Telegram non configuré, alerte ignorée :\n" + text)
        return
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"},
        timeout=30,
    ).raise_for_status()

# ------------------------- Programme principal -------------------------

def main() -> int:
    state = load_json(STATE_FILE, {
        "tracked": {},  # {address: {coin: side}}
        "paper": {
            "start": PAPER_START, "cash": PAPER_START, "equity": PAPER_START,
            "positions": [], "history": [], "equity_curve": [],
        },
    })
    paper = state["paper"]
    alerts = []

    try:
        traders = get_leaderboard()
        mids = get_mids()
    except Exception as e:
        print(f"Erreur API Hyperliquid : {e}")
        return 0  # on n'échoue pas le workflow pour une API indisponible

    for t in traders:
        addr = t["address"]
        try:
            positions = get_positions(addr)
        except Exception as e:
            print(f"[{t['name']}] erreur positions : {e}")
            continue

        t["positions"] = [
            {"coin": c, **p} for c, p in sorted(positions.items())
        ]
        old = state["tracked"].get(addr, {})
        new = {c: p["side"] for c, p in positions.items()}

        # Nouvelles positions ou retournements
        for coin, side in new.items():
            if old.get(coin) != side:
                price = mids.get(coin, positions[coin]["entry"])
                if old.get(coin):  # retournement : on ferme l'ancienne
                    done = paper_close(paper, coin, old[coin], price)
                    if done:
                        paper["history"].append({"time": now_iso(), "note": done, "trader": t["name"]})
                emoji = "🟢" if side == "LONG" else "🔴"
                alerts.append(
                    f"{emoji} <b>{t['name']}</b> (ROI 30j {t['roi_30d']*100:+.0f}%)\n"
                    f"Ouvre un <b>{side} {coin}</b> vers {price:,.2f} $"
                )
                done = paper_open(paper, coin, side, price, t["name"])
                if done:
                    paper["history"].append({"time": now_iso(), "note": done, "trader": t["name"]})

        # Positions fermées
        for coin, side in old.items():
            if coin not in new:
                price = mids.get(coin, 0)
                alerts.append(
                    f"⚪ <b>{t['name']}</b> ferme son <b>{side} {coin}</b>"
                    + (f" vers {price:,.2f} $" if price else "")
                )
                done = paper_close(paper, coin, side, price or 1)
                if done:
                    paper["history"].append({"time": now_iso(), "note": done, "trader": t["name"]})

        state["tracked"][addr] = new

    paper_mark_to_market(paper, mids)
    paper["history"] = paper["history"][-100:]

    # Suivi lecture seule de ton wallet (optionnel)
    wallet = None
    if MY_WALLET:
        try:
            wpos = get_positions(MY_WALLET)
            wallet = {
                "address": MY_WALLET,
                "positions": [{"coin": c, **p} for c, p in sorted(wpos.items())],
            }
        except Exception as e:
            print(f"Erreur wallet : {e}")

    # Publication pour le cockpit
    data = load_json(DATA_FILE, {})
    data.update({
        "updated": now_iso(),
        "traders": traders,
        "paper": paper,
        "wallet": wallet,
    })
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=1))

    if alerts:
        msg = "👥 <b>Copytrading (paper)</b>\n\n" + "\n\n".join(alerts[:15])
        msg += (f"\n\n💼 Portefeuille virtuel : <b>{paper['equity']:,.0f} $</b> "
                f"({(paper['equity']/paper['start']-1)*100:+.1f}%)"
                "\n<i>Simulation — pas un conseil financier.</i>")
        send_telegram(msg)
        print(f"{len(alerts)} alerte(s) copytrading envoyée(s).")
    else:
        print("Copytrading : aucun mouvement chez les top traders.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
