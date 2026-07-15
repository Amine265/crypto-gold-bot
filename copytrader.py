"""
Copytrading (mode PAPER) — 3 profils d'investissement en parallèle
-------------------------------------------------------------------
Chaque profil simule une stratégie différente sur les top traders Hyperliquid,
avec son propre portefeuille virtuel de 10 000 $ :

  PRUDENT   — 3 traders max, gros comptes (>500k$), ROI 30j plafonné à 300%
              (écarte les profils "trop beaux pour durer"), pas de short,
              3% du capital par position, 5 positions max
  ÉQUILIBRÉ — 5 traders, comptes >100k$, long + short, 5% par position
  AGRESSIF  — 8 traders, tri ROI sans plafond, long + short, 10% par position

Après quelques semaines, comparer les courbes d'équité permet de choisir
une stratégie en connaissance de cause. Aucun ordre réel n'est passé.

⚠️ Simulation à but informatif. Ce n'est pas un conseil financier.
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

PAPER_START = 10_000.0

PROFILES = {
    "prudent": {
        "label": "Prudent", "top_n": 3, "min_account": 500_000,
        "roi_cap": 3.0, "shorts": False, "alloc": 0.03, "max_pos": 5,
    },
    "equilibre": {
        "label": "Équilibré", "top_n": 5, "min_account": 100_000,
        "roi_cap": None, "shorts": True, "alloc": 0.05, "max_pos": 8,
    },
    "agressif": {
        "label": "Agressif", "top_n": 8, "min_account": 100_000,
        "roi_cap": None, "shorts": True, "alloc": 0.10, "max_pos": 12,
    },
}

TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MY_WALLET = os.environ.get("MY_WALLET", "").strip()

STATE_FILE = Path("state_copy.json")
DATA_FILE = Path("docs/data.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ------------------------- API Hyperliquid -------------------------

def post_info(payload: dict):
    r = requests.post(INFO_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def get_leaderboard() -> list[dict]:
    """Traders rentables sur 30 jours, triés par ROI décroissant."""
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
        if acct >= 100_000 and pnl > 0 and roi > 0:
            out.append({
                "address": row.get("ethAddress", ""),
                "name": row.get("displayName") or row.get("ethAddress", "")[:8],
                "account_value": acct,
                "roi_30d": roi,
                "pnl_30d": pnl,
            })
    out.sort(key=lambda t: t["roi_30d"], reverse=True)
    return out


def select_traders(all_traders: list[dict], cfg: dict) -> list[dict]:
    pool = [
        t for t in all_traders
        if t["account_value"] >= cfg["min_account"]
        and (cfg["roi_cap"] is None or t["roi_30d"] <= cfg["roi_cap"])
    ]
    return pool[: cfg["top_n"]]


def get_positions(address: str) -> dict[str, dict]:
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
    data = post_info({"type": "allMids"})
    return {k: float(v) for k, v in data.items() if not k.startswith("@")}

# ------------------------- Paper trading -------------------------

def new_paper() -> dict:
    return {"start": PAPER_START, "cash": PAPER_START, "equity": PAPER_START,
            "positions": [], "history": [], "equity_curve": []}


def paper_open(paper, cfg, coin, side, price, trader):
    if price <= 0 or len(paper["positions"]) >= cfg["max_pos"]:
        return None
    if side == "SHORT" and not cfg["shorts"]:
        return None
    if any(p["coin"] == coin and p["side"] == side for p in paper["positions"]):
        return None
    amount = paper["equity"] * cfg["alloc"]
    if paper["cash"] < amount:
        return None
    paper["cash"] -= amount
    paper["positions"].append({"coin": coin, "side": side, "entry": price,
                               "amount": amount, "trader": trader, "opened": now_iso()})
    return f"ouverture {side} {coin} à {price:,.2f} $ ({amount:,.0f} $ virtuels)"


def paper_close(paper, coin, side, price):
    for i, p in enumerate(paper["positions"]):
        if p["coin"] == coin and p["side"] == side:
            direction = 1 if side == "LONG" else -1
            pnl = p["amount"] * direction * (price - p["entry"]) / p["entry"]
            paper["cash"] += p["amount"] + pnl
            paper["positions"].pop(i)
            return f"clôture {side} {coin} à {price:,.2f} $ (P&L {pnl:+,.0f} $)"
    return None


def mark_to_market(paper, mids):
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
    paper["history"] = paper["history"][-100:]

# ------------------------- Utilitaires -------------------------

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
    state = load_json(STATE_FILE, {})
    state.setdefault("tracked", {})
    # Migration depuis la v2 : l'ancien portefeuille unique devient "équilibré"
    old_paper = state.pop("paper", None)
    state.setdefault("profiles", {})
    for key in PROFILES:
        if key not in state["profiles"]:
            state["profiles"][key] = (
                old_paper if key == "equilibre" and old_paper else new_paper()
            )

    try:
        all_traders = get_leaderboard()
        mids = get_mids()
    except Exception as e:
        print(f"Erreur API Hyperliquid : {e}")
        return 0

    # Traders par profil + union des adresses à surveiller
    profile_traders = {k: select_traders(all_traders, cfg) for k, cfg in PROFILES.items()}
    watched = {}
    for lst in profile_traders.values():
        for t in lst:
            watched[t["address"]] = t

    # Détection des mouvements (une seule fois, partagée entre profils)
    events, alerts = [], []
    for addr, t in watched.items():
        try:
            positions = get_positions(addr)
        except Exception as e:
            print(f"[{t['name']}] erreur positions : {e}")
            continue
        t["positions"] = [{"coin": c, **p} for c, p in sorted(positions.items())]
        old = state["tracked"].get(addr, {})
        new = {c: p["side"] for c, p in positions.items()}

        for coin, side in new.items():
            if old.get(coin) != side:
                price = mids.get(coin, positions[coin]["entry"])
                if old.get(coin):
                    events.append((addr, t["name"], "close", coin, old[coin], price))
                events.append((addr, t["name"], "open", coin, side, price))
                emoji = "🟢" if side == "LONG" else "🔴"
                alerts.append(f"{emoji} <b>{t['name']}</b> (ROI 30j {t['roi_30d']*100:+.0f}%)\n"
                              f"Ouvre un <b>{side} {coin}</b> vers {price:,.2f} $")
        for coin, side in old.items():
            if coin not in new:
                price = mids.get(coin, 0)
                events.append((addr, t["name"], "close", coin, side, price or 1))
                alerts.append(f"⚪ <b>{t['name']}</b> ferme son <b>{side} {coin}</b>"
                              + (f" vers {price:,.2f} $" if price else ""))
        state["tracked"][addr] = new

    # Application des événements à chaque profil selon ses règles
    for key, cfg in PROFILES.items():
        paper = state["profiles"][key]
        addrs = {t["address"] for t in profile_traders[key]}
        for addr, name, action, coin, side, price in events:
            if addr not in addrs:
                continue
            done = (paper_open(paper, cfg, coin, side, price, name) if action == "open"
                    else paper_close(paper, coin, side, price))
            if done:
                paper["history"].append({"time": now_iso(), "note": done,
                                         "trader": name, "profil": cfg["label"]})
        mark_to_market(paper, mids)

    # Suivi lecture seule du wallet (optionnel)
    wallet = None
    if MY_WALLET:
        try:
            wpos = get_positions(MY_WALLET)
            wallet = {"address": MY_WALLET,
                      "positions": [{"coin": c, **p} for c, p in sorted(wpos.items())]}
        except Exception as e:
            print(f"Erreur wallet : {e}")

    # Publication pour le cockpit et le bot Telegram
    data = load_json(DATA_FILE, {})
    profiles_pub = {}
    for key, cfg in PROFILES.items():
        p = state["profiles"][key]
        profiles_pub[key] = {"label": cfg["label"], **p,
                             "traders": [t["name"] for t in profile_traders[key]]}
    data.update({
        "updated": now_iso(),
        "traders": [watched[a] for a in watched],
        "profiles": profiles_pub,
        "paper": profiles_pub["equilibre"],  # compatibilité v2
        "wallet": wallet,
    })
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=1))

    if alerts:
        recap = " · ".join(
            f"{PROFILES[k]['label']} {state['profiles'][k]['equity']:,.0f} $"
            f" ({(state['profiles'][k]['equity']/PAPER_START-1)*100:+.1f}%)"
            for k in PROFILES)
        msg = ("👥 <b>Copytrading (paper)</b>\n\n" + "\n\n".join(alerts[:15])
               + f"\n\n💼 {recap}\n<i>Simulation — pas un conseil financier.</i>")
        send_telegram(msg)
        print(f"{len(alerts)} alerte(s) copytrading envoyée(s).")
    else:
        print("Copytrading : aucun mouvement chez les top traders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
