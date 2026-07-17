"""
Agent d'exécution Kraken — spot, achat uniquement, sans levier
---------------------------------------------------------------
Exécute les signaux d'ACHAT au verdict ✅ (mêmes règles que /spot) sur Kraken,
selon le protocole : deux moitiés, TP1/TP2, SL systématique, SL remonté à
l'entrée après TP1.

MODES (pilotés depuis Telegram via le worker, stockés dans Cloudflare KV) :
  blanc  — construit et VALIDE les ordres via l'API Kraken (validate=true),
           n'exécute rien. Palier d'entraînement.
  bouton — propose chaque trade sur Telegram avec un bouton ✅ ; exécute les
           trades approuvés au passage suivant (délai ≤ 15 min).
  auto   — exécute directement les signaux ✅.

GARDE-FOUS (codés en dur, volontairement) :
  - Achats spot uniquement : jamais de vente à découvert, jamais de marge
  - 50 $ max par position, 3 trades max par jour
  - Coupe-circuit : 3 SL consécutifs -> pause automatique + alerte
  - /pause depuis Telegram à tout moment
  - Types de signaux suspendus automatiquement si stats défavorables
    (>= 5 occurrences et >= 60 % d'échecs)
  - La clé API ne doit PAS avoir les droits de retrait (vérifié à chaque run)

Répartition de la protection (assumée et documentée) :
  - Moitié A : TP1 posé sur Kraken (temps réel) ; SL surveillé par l'agent (15 min)
  - Moitié B : SL posé sur Kraken (temps réel) ; TP2 surveillé par l'agent (15 min)
  -> le stop "dur" est toujours actif sur la moitié la plus exposée, et
     l'agent complète aux quarts d'heure.

⚠️ Capital d'apprentissage uniquement. Pas un conseil financier.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

# ------------------------- Configuration -------------------------

KRAKEN_KEY = os.environ.get("KRAKEN_KEY", "")
KRAKEN_SECRET = os.environ.get("KRAKEN_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FLAGS_URL = os.environ.get("FLAGS_URL", "")        # https://cockpit-bot....workers.dev/flags
AGENT_SECRET = os.environ.get("AGENT_SECRET", "")  # partagé avec le worker

MAX_PAR_POSITION = 50.0    # $ par trade (les deux moitiés incluses)
MAX_TRADES_JOUR = 3
MAX_SL_CONSECUTIFS = 3
FRAIS = 0.0025             # par ordre, estimation (palier 1 Kraken)
RISQUE_MAX = 0.02          # part de l'enveloppe risquée par trade
ENVELOPPE = 100.0

# Paires Kraken négociables par l'agent (PAXG reste manuel : pas de paire USDC)
PAIRES = {"bitcoin": "XBTUSDC", "ethereum": "ETHUSDC"}

API = "https://api.kraken.com"
STATE_FILE = Path("agent_state.json")
DATA_FILE = Path("docs/data.json")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ------------------------- Client Kraken -------------------------

def kraken_private(path: str, payload: dict) -> dict:
    payload = {**payload, "nonce": str(int(time.time() * 1000))}
    post = urllib.parse.urlencode(payload)
    digest = hashlib.sha256((payload["nonce"] + post).encode()).digest()
    mac = hmac.new(base64.b64decode(KRAKEN_SECRET), path.encode() + digest, hashlib.sha512)
    r = requests.post(API + path, data=payload, timeout=30, headers={
        "API-Key": KRAKEN_KEY, "API-Sign": base64.b64encode(mac.digest()).decode()})
    r.raise_for_status()
    out = r.json()
    if out.get("error"):
        raise RuntimeError(f"Kraken {path}: {out['error']}")
    return out.get("result", {})


def kraken_public(path: str, params: dict | None = None) -> dict:
    r = requests.get(API + path, params=params or {}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if out.get("error"):
        raise RuntimeError(f"Kraken {path}: {out['error']}")
    return out.get("result", {})


_PAIR_INFO: dict = {}

def pair_info(pair: str) -> dict:
    if not _PAIR_INFO:
        res = kraken_public("/0/public/AssetPairs", {"pair": ",".join(PAIRES.values())})
        for v in res.values():
            _PAIR_INFO[v["altname"]] = v
    return _PAIR_INFO[pair]


def fmt_price(pair: str, p: float) -> str:
    return f"{p:.{pair_info(pair)['pair_decimals']}f}"


def fmt_vol(pair: str, v: float) -> str:
    return f"{v:.{pair_info(pair)['lot_decimals']}f}"


def ticker_price(pair: str) -> float:
    res = kraken_public("/0/public/Ticker", {"pair": pair})
    return float(next(iter(res.values()))["c"][0])


def solde_usdc() -> float:
    bal = kraken_private("/0/private/Balance", {})
    return float(bal.get("USDC", 0) or 0)


def verifier_cle_sans_retrait() -> None:
    """Garde-fou : refuse de tourner si la clé a des droits de retrait."""
    try:
        kraken_private("/0/private/WithdrawMethods", {"asset": "USDC"})
    except RuntimeError as e:
        if "Permission denied" in str(e):
            return  # parfait : la clé ne peut pas retirer
        raise
    raise SystemExit("⛔ La clé API a des droits de RETRAIT. Agent refusé. "
                     "Recrée une clé sans Withdraw Funds.")


def add_order(pair: str, price: float, volume: float, validate: bool,
              close_type: str | None = None, close_price: float | None = None) -> dict:
    payload = {"pair": pair, "type": "buy", "ordertype": "limit",
               "price": fmt_price(pair, price), "volume": fmt_vol(pair, volume)}
    if close_type:
        payload["close[ordertype]"] = close_type
        payload["close[price]"] = fmt_price(pair, close_price)
        if close_type.endswith("-limit"):
            # les types *-limit exigent aussi le prix limite (close[price2]) ;
            # limite = déclencheur : vente à TP1 exactement, pas de slippage
            payload["close[price2]"] = fmt_price(pair, close_price)
    if validate:
        payload["validate"] = "true"
    return kraken_private("/0/private/AddOrder", payload)


def sell_market(pair: str, volume: float) -> dict:
    return kraken_private("/0/private/AddOrder", {
        "pair": pair, "type": "sell", "ordertype": "market",
        "volume": fmt_vol(pair, volume)})


def open_orders() -> dict:
    return kraken_private("/0/private/OpenOrders", {}).get("open", {})


def cancel(txid: str) -> None:
    kraken_private("/0/private/CancelOrder", {"txid": txid})

# ------------------------- Utilitaires -------------------------

def send_telegram(text: str, buttons: list | None = None) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TG absent :", text)
        return
    body = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons:
        body["reply_markup"] = {"inline_keyboard": buttons}
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json=body, timeout=30).raise_for_status()


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def get_flags() -> dict:
    """Mode, pause et approbations depuis le worker (KV Cloudflare)."""
    try:
        return requests.get(FLAGS_URL, timeout=15).json()
    except Exception:
        return {"mode": "blanc", "pause": True, "approvals": []}  # sûr par défaut


def consume_approvals(ids: list[str]) -> None:
    if ids:
        requests.post(FLAGS_URL.replace("/flags", "/consume"),
                      json={"ids": ids},
                      headers={"X-Agent-Secret": AGENT_SECRET}, timeout=15)


def type_signal(reason: str) -> str:
    r = (reason or "").lower()
    if "macd" in r: return "MACD"
    if "rsi" in r: return "RSI"
    if "sma" in r: return "SMA"
    return "autre"


def verdict_ok(plan: dict) -> bool:
    """Réplique du filtre /spot : frais vs gains, verdict ✅ exigé."""
    taille = min(ENVELOPPE, (ENVELOPPE * RISQUE_MAX) / (plan["risk_pct"] / 100))
    taille = min(taille, MAX_PAR_POSITION)
    gain_tp2 = taille * abs(plan["tp2"] - plan["entry"]) / plan["entry"]
    gain_tp1 = taille * abs(plan["tp1"] - plan["entry"]) / plan["entry"]
    frais_ar = taille * FRAIS * 2
    return gain_tp2 - frais_ar > 0 and gain_tp1 - frais_ar > 0

# ------------------------- Statistiques adaptatives -------------------------

def maj_stats(state: dict, data: dict) -> list[str]:
    """Met à jour les stats par type de signal ; suspend les types défavorables."""
    notes = []
    vus = set(state.setdefault("resultats_vus", []))
    stats = state.setdefault("stats_types", {})
    for r in data.get("signaux_resultats", []):
        key = f"{r['signal_time']}|{r['asset']}|{r['type']}"
        if key in vus or r["type"] != "achat":
            continue
        vus.add(key)
        t = type_signal(r.get("reason", ""))
        s = stats.setdefault(t, {"n": 0, "tp2": 0, "sl": 0})
        s["n"] += 1
        if r["resultat"] == "TP2":
            s["tp2"] += 1
        elif r["resultat"] == "SL" and not r.get("tp1_franchi"):
            s["sl"] += 1
    state["resultats_vus"] = list(vus)[-200:]
    suspendus = state.setdefault("types_suspendus", [])
    for t, s in stats.items():
        if s["n"] >= 5 and s["sl"] / s["n"] >= 0.6 and t not in suspendus:
            suspendus.append(t)
            notes.append(f"📉 Type <b>{t}</b> suspendu : {s['sl']}/{s['n']} échecs. "
                         f"L'agent n'exécutera plus ces signaux (les alertes continuent).")
        if t in suspendus and s["n"] >= 8 and s["sl"] / s["n"] < 0.5:
            suspendus.remove(t)
            notes.append(f"📈 Type <b>{t}</b> réactivé : les stats se sont redressées.")
    return notes

# ------------------------- Exécution d'un trade -------------------------

def executer(plan: dict, validate: bool) -> dict | None:
    """Place l'entrée en deux moitiés. Retourne l'enregistrement du trade."""
    pair = PAIRES[plan["coin"]]
    prix = ticker_price(pair)
    # Signal périmé ? (règle du protocole)
    if prix >= (plan["entry"] + plan["tp1"]) / 2 or prix <= plan["sl"]:
        send_telegram(f"⏭️ <b>Agent</b> — signal {plan['asset']} ignoré : "
                      f"prix actuel {prix:,.2f} $ hors zone d'entrée du plan.")
        return None
    taille = min(MAX_PAR_POSITION,
                 (ENVELOPPE * RISQUE_MAX) / (plan["risk_pct"] / 100), ENVELOPPE)
    if not validate and solde_usdc() < taille:
        send_telegram(f"⏭️ <b>Agent</b> — solde USDC insuffisant pour {plan['asset']} "
                      f"({taille:.0f} $ requis).")
        return None
    entry = min(plan["entry"], prix)  # jamais au-dessus du plan
    vol_a = (taille / 2) / entry
    vol_b = (taille / 2) / entry
    # Moitié A : TP1 posé sur Kraken ; moitié B : SL posé sur Kraken
    ra = add_order(pair, entry, vol_a, validate,
                   close_type="take-profit-limit", close_price=plan["tp1"])
    rb = add_order(pair, entry, vol_b, validate,
                   close_type="stop-loss", close_price=plan["sl"])
    return {"id": f"{plan['time']}|{plan['coin']}", "coin": plan["coin"], "pair": pair,
            "asset": plan["asset"], "entry": entry, "sl": plan["sl"],
            "tp1": plan["tp1"], "tp2": plan["tp2"], "vol_a": vol_a, "vol_b": vol_b,
            "txid_a": (ra.get("txid") or ["validate"])[0],
            "txid_b": (rb.get("txid") or ["validate"])[0],
            "statut": "valide" if validate else "ouvert",
            "tp1_fait": False, "ouvert_le": now_iso()}

# ------------------------- Gestion des positions ouvertes -------------------------

def gerer_positions(state: dict) -> list[str]:
    """Aux quarts d'heure : TP1 -> vendre A + remonter le SL de B à l'entrée ;
    TP2 -> vendre B ; filet SL sur A si le stop de B est parti."""
    notes = []
    ouverts = open_orders()
    for tr in state.get("trades", []):
        if tr["statut"] not in ("ouvert", "tp1_fait"):
            continue
        prix = ticker_price(tr["pair"])
        d_entree_execute = tr["txid_a"] not in ouverts  # l'entrée A n'attend plus
        if tr["statut"] == "ouvert" and prix >= tr["tp1"] and d_entree_execute:
            # TP1 : Kraken a normalement vendu A tout seul (take-profit posé).
            # On remonte le SL de B à l'entrée : annuler l'ancien stop, en reposer un.
            for txid, o in ouverts.items():
                if (o["descr"]["pair"].replace("/", "") in (tr["pair"], tr["pair"].replace("XBT", "BTC"))
                        and o["descr"]["ordertype"] == "stop-loss"
                        and o["descr"]["type"] == "sell"):
                    cancel(txid)
            kraken_private("/0/private/AddOrder", {
                "pair": tr["pair"], "type": "sell", "ordertype": "stop-loss",
                "price": fmt_price(tr["pair"], tr["entry"]),
                "volume": fmt_vol(tr["pair"], tr["vol_b"])})
            tr["statut"] = "tp1_fait"
            state["sl_consecutifs"] = 0
            notes.append(f"🎯 <b>Agent</b> — TP1 exécuté sur {tr['asset']} : moitié A vendue, "
                         f"SL de la moitié B remonté à l'entrée ({tr['entry']:,.2f} $). "
                         f"Le trade ne peut plus perdre.")
        elif tr["statut"] == "tp1_fait" and prix >= tr["tp2"]:
            sell_market(tr["pair"], tr["vol_b"])
            tr["statut"] = "clos_tp2"
            notes.append(f"🎯🎯 <b>Agent</b> — TP2 atteint sur {tr['asset']} : moitié B vendue "
                         f"à ~{prix:,.2f} $. Trade complet gagnant.")
        elif prix <= tr["sl"] * 0.999 and d_entree_execute and tr["statut"] == "ouvert":
            # Le stop de B s'est déclenché côté Kraken ; filet : vendre A si encore détenu
            sell_market(tr["pair"], tr["vol_a"])
            tr["statut"] = "clos_sl"
            state["sl_consecutifs"] = state.get("sl_consecutifs", 0) + 1
            notes.append(f"🛑 <b>Agent</b> — SL sur {tr['asset']} : position soldée "
                         f"({state['sl_consecutifs']} SL consécutif(s)).")
    state["trades"] = [t for t in state.get("trades", []) if t["statut"] != "valide"][-30:]
    return notes

# ------------------------- Programme principal -------------------------

def test_blanc() -> int:
    """Test forcé : signal fictif déroulé jusqu'à la validation Kraken (rien placé)."""
    verifier_cle_sans_retrait()
    prix = ticker_price("XBTUSDC")
    plan = {"time": now_iso(), "coin": "bitcoin", "asset": "BTC (TEST)", "type": "achat",
            "entry": prix * 0.999, "sl": prix * 0.985, "tp1": prix * 1.013,
            "tp2": prix * 1.027, "risk_pct": 1.4, "reason": "test forcé"}
    trade = executer(plan, validate=True)
    if trade:
        send_telegram(f"🧪 <b>Test à blanc réussi</b> — Kraken a validé les 2 ordres "
                      f"fictifs BTC @ {trade['entry']:,.2f} $ (TP1 et SL attachés, "
                      f"volumes et arrondis corrects). Rien n'a été placé.")
        print("Test à blanc : OK")
    return 0


def main() -> int:
    if not KRAKEN_KEY or not KRAKEN_SECRET:
        print("Clés Kraken absentes — agent inactif.")
        return 0

    flags = get_flags()
    mode = flags.get("mode", "blanc")
    state = load_json(STATE_FILE, {})
    data = load_json(DATA_FILE, {})
    notes = []

    verifier_cle_sans_retrait()

    # Coupe-circuit et pause
    if state.get("sl_consecutifs", 0) >= MAX_SL_CONSECUTIFS and mode != "blanc":
        if not state.get("pause_annoncee"):
            send_telegram(f"⛔ <b>Agent en pause automatique</b> : {MAX_SL_CONSECUTIFS} SL "
                          f"consécutifs. Tape /reprise pour réarmer après analyse.")
            state["pause_annoncee"] = True
        STATE_FILE.write_text(json.dumps(state, indent=1))
        return 0
    if flags.get("pause"):
        print("Agent en pause (drapeau).")
        # on gère quand même les positions déjà ouvertes, par sécurité
        if mode != "blanc":
            notes += gerer_positions(state)
        for n in notes:
            send_telegram(n)
        STATE_FILE.write_text(json.dumps(state, indent=1))
        return 0

    # Stats adaptatives
    notes += maj_stats(state, data)

    # Gestion des positions en cours (modes réels)
    if mode != "blanc":
        notes += gerer_positions(state)

    # Limite journalière
    jour = now_iso()[:10]
    daily = state.setdefault("daily", {"date": jour, "n": 0})
    if daily["date"] != jour:
        state["daily"] = daily = {"date": jour, "n": 0}

    # Candidats : signaux d'achat récents, ✅, paire négociable, type actif, non traités
    executes = set(state.setdefault("signaux_traites", []))
    candidats = []
    for pl in data.get("plans_actifs", []):
        sid = f"{pl['time']}|{pl['coin']}"
        if (pl["type"] == "achat" and pl["coin"] in PAIRES and sid not in executes
                and type_signal(pl.get("reason", "")) not in state.get("types_suspendus", [])
                and verdict_ok(pl)):
            candidats.append(pl)

    approbations = flags.get("approvals", [])
    consommees = []
    for pl in candidats:
        sid = f"{pl['time']}|{pl['coin']}"
        if daily["n"] >= MAX_TRADES_JOUR:
            break
        if mode == "bouton" and sid not in approbations:
            if sid not in state.setdefault("proposes", []):
                state["proposes"].append(sid)
                send_telegram(
                    f"🤖 <b>Agent — proposition</b>\n{pl['asset']} ACHAT · "
                    f"entrée {pl['entry']:,.2f} $ · SL {pl['sl']:,.2f} $ · "
                    f"TP1 {pl['tp1']:,.2f} $ · TP2 {pl['tp2']:,.2f} $\n"
                    f"<i>{pl.get('reason','')}</i>",
                    buttons=[[{"text": "✅ Exécuter", "callback_data": f"ap:{sid}"}]])
            continue
        trade = executer(pl, validate=(mode == "blanc"))
        executes.add(sid)
        if sid in approbations:
            consommees.append(sid)
        if trade:
            state.setdefault("trades", []).append(trade)
            if mode == "blanc":
                notes.append(f"🧪 <b>Agent (à blanc)</b> — ordres VALIDÉS par Kraken pour "
                             f"{trade['asset']} : 2×{fmt_vol(trade['pair'], trade['vol_a'])} "
                             f"@ {trade['entry']:,.2f} $, TP1/SL attachés. Rien n'a été placé.")
                state.setdefault("validations_blanc", 0)
                state["validations_blanc"] += 1
            else:
                daily["n"] += 1
                notes.append(f"🤖 <b>Agent</b> — position ouverte sur {trade['asset']} : "
                             f"2 moitiés @ {trade['entry']:,.2f} $, TP1 {trade['tp1']:,.2f} $ "
                             f"(posé), SL {trade['sl']:,.2f} $ (posé), TP2 géré aux 15 min.")
    consume_approvals(consommees)
    state["signaux_traites"] = list(executes)[-100:]

    # Publication de l'état pour le cockpit et /agent
    data["agent"] = {
        "mode": mode, "pause": bool(flags.get("pause")),
        "maj": now_iso(),
        "trades_jour": daily["n"], "sl_consecutifs": state.get("sl_consecutifs", 0),
        "validations_blanc": state.get("validations_blanc", 0),
        "types_suspendus": state.get("types_suspendus", []),
        "stats_types": state.get("stats_types", {}),
        "positions": [{k: t[k] for k in ("asset", "entry", "sl", "tp1", "tp2", "statut")}
                      for t in state.get("trades", []) if t["statut"] in ("ouvert", "tp1_fait")],
    }
    DATA_FILE.write_text(json.dumps(data, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=1))

    for n in notes:
        send_telegram(n)
    print(f"Agent [{mode}] : {len(candidats)} candidat(s), {len(notes)} note(s).")
    return 0


if __name__ == "__main__":
    if os.environ.get("TEST_BLANC"):
        sys.exit(test_blanc())
    sys.exit(main())
