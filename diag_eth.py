"""Diagnostic temporaire ETH — lecture seule, aucune commande passée.
N'imprime que des informations déjà publiées dans agent_state.json
(volumes du trade ETH, txids, prix publics)."""
from datetime import datetime, timezone

from agent import kraken_private

# Soldes liés à l'ETH uniquement
bal = kraken_private("/0/private/Balance", {})
eth = {k: v for k, v in bal.items() if "ETH" in k.upper()}
print("Balance (clés ETH) :", eth or "aucune clé ETH")

# Ordres ouverts sur ETHUSDC
oo = kraken_private("/0/private/OpenOrders", {}).get("open", {})
for txid, o in oo.items():
    d = o.get("descr", {})
    if "ETH" in d.get("pair", ""):
        print(f"Ordre ouvert {txid}: {d.get('type')} {d.get('ordertype')} "
              f"vol={o.get('vol')} exec={o.get('vol_exec')} prix={d.get('price')} "
              f"statut={o.get('status')}")

# Entrées du trade (volumes exécutés réels)
q = kraken_private("/0/private/QueryOrders",
                   {"txid": "OPMCCE-UZZ6X-2IR2W7,OJTKOF-N5AE5-6T62FR"})
for txid, o in q.items():
    print(f"Entrée {txid}: status={o.get('status')} vol={o.get('vol')} "
          f"vol_exec={o.get('vol_exec')} fee={o.get('fee')} cost={o.get('cost')}")

# Historique des trades ETHUSDC depuis l'ouverture du 22/07
start = datetime(2026, 7, 22, 15, 0, tzinfo=timezone.utc).timestamp()
th = kraken_private("/0/private/TradesHistory", {"start": start})
print("TradesHistory count :", th.get("count"))
for tid, t in th.get("trades", {}).items():
    if "ETH" in t.get("pair", ""):
        print(f"Trade {tid}: {t.get('type')} vol={t.get('vol')} prix={t.get('price')} "
              f"fee={t.get('fee')} time={t.get('time')} ordertxid={t.get('ordertxid')}")
