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
          "/signaux — derniers signaux\n" +
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
      const lignes = (data.signals || []).slice(0, 6).map(
        (s) =>
          `${s.type === "achat" ? "🟢" : "🔴"} <b>${s.asset}</b> — ${s.reason}\n<i>${heure(s.time)}</i>`,
      );
      return {
        text: lignes.length
          ? "🚨 <b>Derniers signaux</b>\n\n" + lignes.join("\n\n")
          : "Aucun signal enregistré pour l'instant.",
        keyboard: kbCockpit,
      };
    }

    case "/aide":
      return {
        text:
          "📌 /prix — marchés + RSI\n/portefeuille — profils simulés\n" +
          "/traders — top traders\n/signaux — derniers signaux",
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
