"""
Agent B0 — croisement de la SMA200 journalière, spot Kraken, sans levier
------------------------------------------------------------------------
La seule stratégie validée par les campagnes de backtest. Une décision par
jour, juste après la clôture journalière UTC :

  - clôture qui passe AU-DESSUS de sa SMA200 (veille en dessous) et aucune
    position B0 sur l'actif  -> achat spot au marché de ALLOC_USDC ;
  - clôture SOUS la SMA200 avec position B0 détenue -> vente spot TOTALE
    (couvre le croisement baissier ET le rattrapage d'un run manqué).

Aucun TP, aucun SL, aucun autre ordre. C'est toute la stratégie.

GARDE-FOUS (leçons de l'ancien agent, conservées) :
  - la clé API ne doit PAS avoir les droits de retrait (vérifié à chaque run)
  - l'état Kraken fait foi : volumes réellement exécutés relevés via les
    fills (exécutions partielles), solde réel vérifié avant tout ordre
  - maximum UN ordre par actif et par jour
  - drapeau pause du worker (/pause et /reprise s'appliquent à B0)

TEST_B0=1 : déroule tout (données, SMA200, décision), valide un ordre
fictif via validate=true, n'exécute RIEN, et envoie le rapport sur Telegram.

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

ALLOC_USDC = 45.0   # $ investis par actif à chaque croisement haussier
                    # (plafonnés à 50 % du solde USDC disponible)
SMA = 200           # période de la moyenne mobile, clôtures JOURNALIÈRES

# ohlc : paires essayées dans l'ordre pour l'historique (cohérence avec
# l'exécution d'abord, USD en secours si l'historique USDC est trop court)
ACTIFS = {
    "BTC": {"pair": "XBTUSDC", "ohlc": ["XBTUSDC", "XBTUSD"], "gecko": "bitcoin"},
    "ETH": {"pair": "ETHUSDC", "ohlc": ["ETHUSDC", "ETHUSD"], "gecko": "ethereum"},
}

KRAKEN_KEY = os.environ.get("KRAKEN_KEY", "")
KRAKEN_SECRET = os.environ.get("KRAKEN_SECRET", "")
TG_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
FLAGS_URL = os.environ.get("FLAGS_URL", "")   # /flags du worker (pause)

API = "https://api.kraken.com"
STATE_FILE = Path("b0_state.json")
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
        res = kraken_public("/0/public/AssetPairs",
                            {"pair": ",".join(a["pair"] for a in ACTIFS.values())})
        for v in res.values():
            _PAIR_INFO[v["altname"]] = v
    return _PAIR_INFO[pair]


def fmt_vol(pair: str, v: float) -> str:
    dec = pair_info(pair)["lot_decimals"]
    v = int(v * 10 ** dec) / 10 ** dec  # tronqué, jamais arrondi au-dessus
    return f"{v:.{dec}f}"


def ticker_price(pair: str) -> float:
    res = kraken_public("/0/public/Ticker", {"pair": pair})
    return float(next(iter(res.values()))["c"][0])


_ASSETS: dict = {}

def solde_actif(asset: str) -> float:
    """Solde d'un actif, tolérant aux deux nomenclatures Kraken : Balance peut
    indexer sous le code interne (« XETH ») ou l'altname (« ETH »)."""
    bal = kraken_private("/0/private/Balance", {})
    if asset in bal:
        return float(bal[asset] or 0)
    if not _ASSETS:
        for k, v in kraken_public("/0/public/Assets").items():
            _ASSETS[k] = v.get("altname", k)
    for cle in (_ASSETS.get(asset, asset), "X" + asset, asset.lstrip("X")):
        if cle in bal:
            return float(bal[cle] or 0)
    return 0.0


def verifier_cle_sans_retrait() -> None:
    """Garde-fou : refuse de tourner si la clé a des droits de retrait."""
    try:
        kraken_private("/0/private/WithdrawMethods", {"asset": "USDC"})
    except RuntimeError as e:
        if "Permission denied" in str(e):
            return  # parfait : la clé ne peut pas retirer
        raise
    raise SystemExit("⛔ La clé API a des droits de RETRAIT. Agent B0 refusé. "
                     "Recrée une clé sans Withdraw Funds.")


def ordre_marche(pair: str, sens: str, volume: float, validate: bool) -> dict:
    payload = {"pair": pair, "type": sens, "ordertype": "market",
               "volume": fmt_vol(pair, volume)}
    if validate:
        payload["validate"] = "true"
    return kraken_private("/0/private/AddOrder", payload)


def attendre_fills(txid: str) -> dict:
    """Volumes/coûts RÉELLEMENT exécutés (leçon des exécutions partielles).
    Un ordre au marché se remplit vite ; on interroge jusqu'à ~30 s."""
    for _ in range(10):
        o = kraken_private("/0/private/QueryOrders", {"txid": txid}).get(txid, {})
        if o.get("status") in ("closed", "canceled"):
            return {"vol": float(o.get("vol_exec", 0) or 0),
                    "cout": float(o.get("cost", 0) or 0),
                    "frais": float(o.get("fee", 0) or 0),
                    "prix": float(o.get("price", 0) or 0),
                    "statut": o.get("status")}
        time.sleep(3)
    o = kraken_private("/0/private/QueryOrders", {"txid": txid}).get(txid, {})
    return {"vol": float(o.get("vol_exec", 0) or 0),
            "cout": float(o.get("cost", 0) or 0),
            "frais": float(o.get("fee", 0) or 0),
            "prix": float(o.get("price", 0) or 0),
            "statut": o.get("status", "inconnu")}

# ------------------------- Données & SMA200 -------------------------

def clotures_kraken(pair: str) -> list[tuple[str, float]] | None:
    """Clôtures journalières complètes [(date, close)] via l'OHLC public.
    La bougie du jour (en cours) est écartée : seule la clôture faite compte."""
    debut_jour = int(datetime.now(timezone.utc)
                     .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    since = debut_jour - 290 * 86400
    try:
        res = kraken_public("/0/public/OHLC",
                            {"pair": pair, "interval": 1440, "since": since})
    except Exception as e:
        print(f"OHLC {pair} : {e}")
        return None
    bougies = next((v for k, v in res.items() if k != "last"), [])
    out = [(datetime.fromtimestamp(int(b[0]), timezone.utc).date().isoformat(),
            float(b[4])) for b in bougies if int(b[0]) < debut_jour]
    return out if len(out) >= SMA + 1 else None


def clotures_gecko(coin_id: str) -> list[tuple[str, float]] | None:
    """Repli CoinGecko (prix USD quotidiens ~00:00 UTC, point du jour écarté)."""
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": 290, "interval": "daily"},
            timeout=30)
        r.raise_for_status()
        pts = r.json()["prices"]
    except Exception as e:
        print(f"CoinGecko {coin_id} : {e}")
        return None
    aujourdhui = datetime.now(timezone.utc).date().isoformat()
    out = []
    for ts, px in pts:
        d = datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat()
        if d >= aujourdhui:
            continue
        if out and out[-1][0] == d:   # doublon éventuel sur la même date
            out[-1] = (d, float(px))
        else:
            out.append((d, float(px)))
    return out if len(out) >= SMA + 1 else None


def analyser(nom: str) -> dict:
    """Clôtures + SMA200 + position par rapport à la moyenne (et depuis quand)."""
    cfg = ACTIFS[nom]
    serie, source = None, None
    for p in cfg["ohlc"]:
        serie = clotures_kraken(p)
        if serie:
            source = f"Kraken {p}"
            break
    if not serie:
        serie = clotures_gecko(cfg["gecko"])
        source = "CoinGecko (repli)"
    if not serie:
        raise RuntimeError(f"{nom} : impossible d'obtenir {SMA + 1} clôtures "
                           f"journalières (Kraken et CoinGecko).")
    dates = [d for d, _ in serie]
    closes = [c for _, c in serie]
    # SMA200 pour chaque clôture depuis qu'elle est calculable
    smas = [sum(closes[i - SMA + 1:i + 1]) / SMA for i in range(SMA - 1, len(closes))]
    dessus = [closes[SMA - 1 + i] > smas[i] for i in range(len(smas))]
    # depuis quand le côté actuel est-il tenu ?
    i = len(dessus) - 1
    while i > 0 and dessus[i - 1] == dessus[-1]:
        i -= 1
    return {
        "source": source,
        "date": dates[-1], "cloture": closes[-1], "sma": smas[-1],
        "au_dessus": dessus[-1],
        "veille_au_dessus": dessus[-2] if len(dessus) >= 2 else dessus[-1],
        "depuis": dates[SMA - 1 + i],
        "croise_haussier": dessus[-1] and len(dessus) >= 2 and not dessus[-2],
        "croise_baissier": not dessus[-1] and len(dessus) >= 2 and dessus[-2],
    }

# ------------------------- Utilitaires -------------------------

def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        print("TG absent :", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      json={"chat_id": TG_CHAT_ID, "text": text,
                            "parse_mode": "HTML"}, timeout=30).raise_for_status()
    except Exception as e:
        print(f"TG échec ({e}) :", text)


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            pass
    return default


def get_flags() -> dict:
    try:
        return requests.get(FLAGS_URL, timeout=15).json()
    except Exception:
        return {"pause": True}   # sûr par défaut : pas d'ordre si worker muet


def evenement(state: dict, nom: str, texte: str) -> None:
    state.setdefault("dernier_evenement", {})[nom] = {"time": now_iso(), "texte": texte}
    state.setdefault("historique", [])
    state["historique"].append({"time": now_iso(), "actif": nom, "texte": texte})
    state["historique"] = state["historique"][-100:]


def publier_etat(state: dict, analyses: dict, pause: bool) -> None:
    """Publie l'état B0 pour le cockpit et la commande /b0 du worker."""
    data = load_json(DATA_FILE, {})
    data["b0"] = {
        "maj": now_iso(), "pause": pause, "alloc": ALLOC_USDC,
        "actifs": {
            nom: {
                "au_dessus": a["au_dessus"], "depuis": a["depuis"],
                "cloture": round(a["cloture"], 2), "sma200": round(a["sma"], 2),
                "source": a["source"], "date_cloture": a["date"],
                "position": state.get("positions", {}).get(nom),
                "dernier_evenement": state.get("dernier_evenement", {}).get(nom),
            } for nom, a in analyses.items()
        },
    }
    DATA_FILE.write_text(json.dumps(data, indent=1))

# ------------------------- Décision & exécution -------------------------

def decision(nom: str, a: dict, position: dict | None) -> str | None:
    """'achat', 'vente' ou None. La vente couvre le croisement baissier ET le
    rattrapage (position détenue sous la SMA après un run manqué) ; l'achat ne
    se fait QUE sur croisement — jamais en entrant en cours de tendance."""
    if a["croise_haussier"] and not position:
        return "achat"
    if position and not a["au_dessus"]:
        return "vente"
    return None


def executer_achat(nom: str, a: dict, state: dict, validate: bool) -> str | None:
    pair = ACTIFS[nom]["pair"]
    usdc = solde_actif("USDC")            # le solde réel fait foi
    alloc = min(ALLOC_USDC, usdc * 0.5)
    prix = ticker_price(pair)
    vol = alloc / prix
    if alloc < 5 or float(pair_info(pair).get("ordermin", 0)) > vol:
        send_telegram(f"⚠️ <b>B0</b> — croisement haussier {nom} mais solde USDC "
                      f"insuffisant ({usdc:.2f} $ dispo → allocation {alloc:.2f} $ "
                      f"sous le minimum). Aucun ordre passé.")
        return None
    r = ordre_marche(pair, "buy", vol, validate)
    if validate:
        return f"[TEST] achat {nom} validé par Kraken : {fmt_vol(pair, vol)} @ ~{prix:,.2f} $"
    txid = (r.get("txid") or [None])[0]
    f = attendre_fills(txid)
    if f["vol"] <= 0:
        send_telegram(f"❌ <b>B0</b> — achat {nom} : ordre {txid} sans exécution "
                      f"détectée (statut {f['statut']}). À vérifier sur Kraken.")
        return None
    state.setdefault("positions", {})[nom] = {
        "vol": f["vol"], "cout_usd": round(f["cout"] + f["frais"], 2),
        "prix_achat": f["prix"], "ouvert_le": now_iso(), "txid": txid}
    texte = (f"achat {f['cout']:.2f} $ exécuté à {f['prix']:,.2f} $ "
             f"({f['vol']:.8g} {nom}, frais {f['frais']:.2f} $)")
    evenement(state, nom, texte)
    send_telegram(f"🔔 <b>B0</b> : {nom} a clôturé au-dessus de sa SMA200 "
                  f"({a['cloture']:,.2f} $ vs {a['sma']:,.2f} $) → {texte}.")
    return texte


def executer_vente(nom: str, a: dict, state: dict, validate: bool) -> str | None:
    pair = ACTIFS[nom]["pair"]
    position = state["positions"][nom]
    if validate:
        return (f"[TEST] vente {nom} : position de {position['vol']:.8g} {nom} "
                f"serait soldée au marché")
    base = pair_info(pair)["base"]
    solde = solde_actif(base)             # le solde réel fait foi
    vol = min(position["vol"], solde)
    if solde < position["vol"] * 0.99:
        if float(fmt_vol(pair, vol)) <= 0:
            del state["positions"][nom]
            evenement(state, nom, "position marquée close (solde Kraken nul)")
            send_telegram(f"⚠️ <b>B0</b> — vente {nom} : solde Kraken quasi nul "
                          f"({solde:.8g} vs {position['vol']:.8g} attendu). Position "
                          f"marquée close sans ordre — à vérifier sur Kraken.")
            return None
        send_telegram(f"⚠️ <b>B0</b> — {nom} : solde réel {solde:.8g} inférieur à la "
                      f"position enregistrée {position['vol']:.8g} ; vente du solde réel.")
    r = ordre_marche(pair, "sell", vol, validate=False)
    txid = (r.get("txid") or [None])[0]
    f = attendre_fills(txid)
    if f["vol"] <= 0:
        send_telegram(f"❌ <b>B0</b> — vente {nom} : ordre {txid} sans exécution "
                      f"détectée (statut {f['statut']}). Position conservée dans "
                      f"l'état, nouvel essai au prochain run.")
        return None
    produit = f["cout"] - f["frais"]
    pnl = produit - position["cout_usd"]
    del state["positions"][nom]
    texte = (f"vente exécutée à {f['prix']:,.2f} $ ({f['vol']:.8g} {nom}) → "
             f"P&L {pnl:+.2f} $")
    evenement(state, nom, texte)
    send_telegram(f"🔔 <b>B0</b> : {nom} a clôturé sous sa SMA200 "
                  f"({a['cloture']:,.2f} $ vs {a['sma']:,.2f} $) → {texte} "
                  f"(acheté {position['cout_usd']:.2f} $, récupéré {produit:.2f} $ "
                  f"frais déduits).")
    return texte

# ------------------------- Programme principal -------------------------

def ligne_etat(nom: str, a: dict, position: dict | None) -> str:
    cote = "au-dessus" if a["au_dessus"] else "en dessous"
    pos = (f"position {position['vol']:.8g} {nom} ({position['cout_usd']:.2f} $)"
           if position else "aucune position")
    return (f"{nom} : clôture {a['cloture']:,.2f} $ {cote} de la SMA200 "
            f"({a['sma']:,.2f} $) depuis le {a['depuis']} · {pos} · {a['source']}")


def main() -> int:
    test = bool(os.environ.get("TEST_B0"))
    if not KRAKEN_KEY or not KRAKEN_SECRET:
        print("Clés Kraken absentes — B0 inactif.")
        return 0

    verifier_cle_sans_retrait()
    state = load_json(STATE_FILE, {})
    flags = get_flags()
    pause = bool(flags.get("pause"))

    analyses = {nom: analyser(nom) for nom in ACTIFS}
    for nom, a in analyses.items():
        print(ligne_etat(nom, a, state.get("positions", {}).get(nom)))

    # maximum UN ordre par actif et par jour
    jour = now_iso()[:10]
    oj = state.setdefault("ordres_jour", {"date": jour, "actifs": []})
    if oj["date"] != jour:
        state["ordres_jour"] = oj = {"date": jour, "actifs": []}

    if test:
        rapports = []
        for nom, a in analyses.items():
            position = state.get("positions", {}).get(nom)
            d = decision(nom, a, position)
            if d == "achat":
                rapports.append(executer_achat(nom, a, state, validate=True)
                                or f"[TEST] achat {nom} : refusé (solde)")
            elif d == "vente":
                rapports.append(executer_vente(nom, a, state, validate=True))
            else:
                rapports.append(f"[TEST] {nom} : aucune action "
                                f"({'déjà' if a['au_dessus'] else 'toujours'} "
                                f"{'au-dessus' if a['au_dessus'] else 'sous'} la SMA200, "
                                f"pas de croisement)")
        # ordre fictif systématique : prouve que volumes/arrondis/permissions
        # passent la validation Kraken même les jours sans croisement
        pair = ACTIFS["BTC"]["pair"]
        prix = ticker_price(pair)
        ordre_marche(pair, "buy", ALLOC_USDC / prix, validate=True)
        usdc = solde_actif("USDC")
        etat = "\n".join("· " + ligne_etat(n, a, state.get("positions", {}).get(n))
                         for n, a in analyses.items())
        send_telegram(
            "🧪 <b>Test B0 réussi</b> — rien n'a été placé.\n\n" + etat +
            "\n\nDécisions du jour :\n" + "\n".join("· " + r for r in rapports) +
            f"\n\nOrdre fictif validé par Kraken (marché, {ALLOC_USDC:.0f} $ BTC)." +
            f"\nSolde USDC : {usdc:.2f} $ · pause : {'oui' if pause else 'non'}.")
        print("Test B0 : OK (aucun ordre réel, aucun état modifié)")
        return 0

    for nom, a in analyses.items():
        position = state.get("positions", {}).get(nom)
        d = decision(nom, a, position)
        if not d:
            continue
        if pause:
            send_telegram(f"⏸️ <b>B0</b> — signal <b>{d}</b> sur {nom} ignoré : "
                          f"agent en pause (/reprise pour réarmer).")
            continue
        if nom in oj["actifs"]:
            print(f"{nom} : ordre déjà passé aujourd'hui, signal {d} ignoré.")
            continue
        try:
            fait = (executer_achat if d == "achat" else executer_vente)(
                nom, a, state, validate=False)
        except Exception as e:
            send_telegram(f"❌ <b>B0</b> — échec {d} {nom} : {e}")
            continue
        if fait:
            oj["actifs"].append(nom)

    publier_etat(state, analyses, pause)
    STATE_FILE.write_text(json.dumps(state, indent=1))
    print(f"B0 : terminé ({len(state.get('positions', {}))} position(s) en cours).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
