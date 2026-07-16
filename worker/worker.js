/**
 * Bot Telegram interactif — Cloudflare Worker (gratuit)
 * ------------------------------------------------------
 * Répond instantanément aux commandes en lisant le data.json publié
 * par GitHub Actions sur ton cockpit GitHub Pages.
 *
 * Commandes : /start /prix /portefeuille /traders /signaux /aide
 *
 * Déploiement (résumé — voir GUIDE) :
 *   1. wrangler deploy   (ou copier-coller dans le dashboard Cloudflare)
 *   2. wrangler secret put TELEGRAM_TOKEN
 *   3. Enregistrer le webhook :
 *      https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://<ton-worker>.workers.dev
 */

const DATA_URL = "https://amine265.github.io/crypto-gold-bot/data.json";
const COCKPIT_URL = "https://amine265.github.io/crypto-gold-bot/";

// --- Trading spot manuel (commande /spot) ---
const ENVELOPPE = 100;     // $ — capital total dédié au spot
const RISQUE_MAX = 0.02;   // 2% de l'enveloppe risqués par trade au maximum
const FRAIS = 0.0025;      // taux de frais par ordre (0,25% ≈ Kraken palier 1)

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("Bot en ligne ✅");

    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }
    const msg = update.message;
    if (!msg || !msg.text) return new Response("ok");

    const chatId = msg.chat.id;
    const cmd = msg.text.trim().split(/[\s@]/)[0].toLowerCase();

    let data = null;
    try {
      data = await (await fetch(DATA_URL + "?t=" + Date.now(), { cf: { cacheTtl: 0 } })).json();
    } catch {}

    const reply = buildReply(cmd, data);
    await sendMessage(env.TELEGRAM_TOKEN, chatId, reply.text, reply.keyboard);
    return new Response("ok");
  },
};

/* ------------------------- Réponses ------------------------- */

const fmt = (n, d = 0) =>
  Number(n).toLocaleString("fr-FR", { maximumFractionDigits: d });
const heure = (iso) => {
  try {
    return new Date(iso).toLocaleString("fr-FR", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
      timeZone: "Europe/Paris",
    });
  } catch { return ""; }
};

function buildReply(cmd, data) {
  const kbCockpit = {
    inline_keyboard: [[{ text: "📊 Ouvrir le cockpit", web_app: { url: COCKPIT_URL } }]],
  };

  if (!data && cmd !== "/start" && cmd !== "/aide")
    return { text: "⏳ Données indisponibles pour l'instant. Réessaie dans une minute." };

  switch (cmd) {
    case "/start":
      return {
        text:
          "👋 <b>Cockpit Crypto & Or</b>\n\n" +
          "Je surveille les marchés toutes les heures et je t'alerte sur les signaux " +
          "et les mouvements des top traders (en simulation).\n\n" +
          "📌 <b>Commandes</b>\n" +
          "/prix — BTC, ETH, Or + RSI\n" +
          "/portefeuille — les 3 profils simulés\n" +
          "/traders — top traders suivis\n" +
          "/signaux — derniers signaux\n/spot — signaux d'achat dimensionnés pour mon enveloppe\n" +
          "/aide — rappel des commandes\n\n" +
          "<i>Outil informatif — pas un conseil financier.</i>",
        keyboard: kbCockpit,
      };

    case "/prix": {
      const m = data.market || {};
      const lignes = Object.values(m).map((a) => {
        const zone = a.rsi < 30 ? "🟢 survente" : a.rsi > 70 ? "🔴 surachat" : "⚪ neutre";
        return `<b>${a.label}</b> — ${fmt(a.price, 2)} $\nRSI ${a.rsi} · ${zone}`;
      });
      return {
        text: lignes.length
          ? `📈 <b>Marchés</b> (maj ${heure(data.market_updated)})\n\n` + lignes.join("\n\n")
          : "Pas encore de données marché.",
        keyboard: kbCockpit,
      };
    }

    case "/portefeuille": {
      const profs = data.profiles || {};
      const blocs = Object.values(profs).map((p) => {
        const perf = ((p.equity / p.start - 1) * 100).toFixed(1);
        const fleche = perf >= 0 ? "▲" : "▼";
        const pos = (p.positions || [])
          .map((x) => `  · ${x.side} ${x.coin} @ ${fmt(x.entry, 2)} $ (${x.pnl >= 0 ? "+" : ""}${fmt(x.pnl)} $)`)
          .join("\n");
        return (
          `<b>${p.label}</b> ${fleche} ${fmt(p.equity)} $ (${perf >= 0 ? "+" : ""}${perf}%)` +
          (pos ? "\n" + pos : "\n  · aucune position")
        );
      });
      return {
        text: blocs.length
          ? "💼 <b>Portefeuilles simulés</b> (départ 10 000 $ chacun)\n\n" +
            blocs.join("\n\n") +
            "\n\n<i>Paper trading — aucun fonds réel.</i>"
          : "Les profils démarrent au prochain passage du bot.",
        keyboard: kbCockpit,
      };
    }

    case "/traders": {
      const lignes = (data.traders || []).slice(0, 8).map(
        (t) =>
          `<b>${t.name}</b> — ROI 30j ${(t.roi_30d * 100).toFixed(0)}% · ` +
          `P&L ${fmt(t.pnl_30d)} $ · compte ${fmt(t.account_value)} $` +
          ((t.positions || []).length
            ? "\n" + t.positions.map((p) => `  · ${p.side} ${p.coin}`).join("\n")
            : ""),
      );
      return {
        text: lignes.length
          ? "👥 <b>Top traders suivis</b>\n\n" + lignes.join("\n\n")
          : "Pas encore de traders chargés.",
        keyboard: kbCockpit,
      };
    }

    case "/signaux": {
      const lignes = (data.signals || []).slice(0, 6).map((s) => {
        const plan = s.plan
          ? `\n🎯 Entrée ${fmt(s.plan.entry, 2)} $ · SL ${fmt(s.plan.sl, 2)} $ (−${s.plan.risk_pct}%) · TP1 ${fmt(s.plan.tp1, 2)} $ · TP2 ${fmt(s.plan.tp2, 2)} $`
          : "";
        return `${s.type === "achat" ? "🟢" : "🔴"} <b>${s.asset}</b> — ${s.reason}${plan}\n<i>${heure(s.time)}</i>`;
      });
      return {
        text: lignes.length
          ? "🚨 <b>Derniers signaux</b>\n\n" + lignes.join("\n\n")
          : "Aucun signal enregistré pour l'instant.",
        keyboard: kbCockpit,
      };
    }

    case "/spot": {
      // Signaux d'ACHAT uniquement (jouables en spot sans levier), avec plan
      const achats = (data.signals || [])
        .filter((s) => s.type === "achat" && s.plan)
        .slice(0, 5);
      if (!achats.length)
        return {
          text:
            "🛒 <b>Spot</b> — aucun signal d'achat récent avec plan.\n" +
            "Les signaux de vente/short ne sont pas jouables en spot : patience, " +
            "le prochain 🟢 apparaîtra ici.",
          keyboard: kbCockpit,
        };
      const blocs = achats.map((s) => {
        const p = s.plan;
        // Taille : risque max 2% de l'enveloppe, plafonnée à l'enveloppe (pas de levier)
        const tailleIdeale = (ENVELOPPE * RISQUE_MAX) / (p.risk_pct / 100);
        const taille = Math.min(ENVELOPPE, tailleIdeale);
        const perteSL = (taille * p.risk_pct) / 100;
        const gainTP1 = taille * Math.abs(p.tp1 - p.entry) / p.entry;
        const gainTP2 = taille * Math.abs(p.tp2 - p.entry) / p.entry;
        const fraisAR = taille * FRAIS * 2;
        const netTP2 = gainTP2 - fraisAR;
        const verdict =
          netTP2 <= 0
            ? "⛔ frais ≥ gain : à laisser passer"
            : gainTP1 - fraisAR <= 0
              ? "⚠️ rentable seulement si TP2 atteint"
              : "✅ exploitable";
        return (
          `🟢 <b>${s.asset}</b> — ${s.reason}\n<i>${heure(s.time)}</i>\n` +
          `Position : <b>${taille.toFixed(0)} $</b> (sans levier)\n` +
          `Entrée ${fmt(p.entry, 2)} $ · SL ${fmt(p.sl, 2)} $ · TP1 ${fmt(p.tp1, 2)} $ · TP2 ${fmt(p.tp2, 2)} $\n` +
          `Perte au SL ≈ ${perteSL.toFixed(2)} $ · Gain TP1 ≈ +${gainTP1.toFixed(2)} $ · TP2 ≈ +${gainTP2.toFixed(2)} $\n` +
          `Frais A/R ≈ ${fraisAR.toFixed(2)} $ → net TP2 ≈ ${netTP2 >= 0 ? "+" : ""}${netTP2.toFixed(2)} $\n${verdict}`
        );
      });
      return {
        text:
          `🛒 <b>Plan spot</b> — enveloppe ${ENVELOPPE} $ · risque max ${RISQUE_MAX * 100}%/trade\n\n` +
          blocs.join("\n\n") +
          "\n\n<i>Niveaux indicatifs (frais estimés à 0,25%/ordre) — pas un conseil financier. " +
          "Vérifie le prix actuel avant d'entrer : un signal ancien peut être périmé.</i>",
        keyboard: kbCockpit,
      };
    }

    case "/aide":
      return {
        text:
          "📌 /prix — marchés + RSI\n/portefeuille — profils simulés\n" +
          "/traders — top traders\n/signaux — derniers signaux\n/spot — plan spot (enveloppe 100 $)",
        keyboard: kbCockpit,
      };

    default:
      return { text: "Commande inconnue. Tape /aide pour la liste." };
  }
}

async function sendMessage(token, chatId, text, keyboard) {
  const body = { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true };
  if (keyboard) body.reply_markup = keyboard;
  await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
