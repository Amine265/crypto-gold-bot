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
const ENVELOPPE = 50;      // $ — capital total dédié au spot
const RISQUE_MAX = 0.02;   // 2% de l'enveloppe risqués par trade au maximum
const FRAIS = 0.0025;      // taux de frais par ordre (0,25% ≈ Kraken palier 1)

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- Endpoints pour l'agent (GitHub Actions) ---
    if (url.pathname === "/flags" && request.method === "GET") {
      const flags = await lireFlags(env);
      return new Response(JSON.stringify(flags), {
        headers: { "Content-Type": "application/json" },
      });
    }
    if (url.pathname === "/consume" && request.method === "POST") {
      if (request.headers.get("X-Agent-Secret") !== env.AGENT_SECRET)
        return new Response("nope", { status: 403 });
      const { ids = [] } = await request.json().catch(() => ({}));
      const flags = await lireFlags(env);
      flags.approvals = flags.approvals.filter((a) => !ids.includes(a));
      await env.AGENT_KV.put("flags", JSON.stringify(flags));
      return new Response("ok");
    }

    if (request.method !== "POST") return new Response("Bot en ligne ✅");

    let update;
    try { update = await request.json(); } catch { return new Response("ok"); }

    // --- Bouton ✅ d'approbation d'un trade proposé ---
    if (update.callback_query) {
      const cq = update.callback_query;
      if ((cq.data || "").startsWith("ap:")) {
        const id = cq.data.slice(3);
        const flags = await lireFlags(env);
        if (!flags.approvals.includes(id)) flags.approvals.push(id);
        await env.AGENT_KV.put("flags", JSON.stringify(flags));
        await tg(env, "answerCallbackQuery", {
          callback_query_id: cq.id,
          text: "Approuvé — exécution au prochain passage (≤ 15 min).",
        });
        await tg(env, "editMessageText", {
          chat_id: cq.message.chat.id, message_id: cq.message.message_id,
          text: cq.message.text + "\n\n✅ Approuvé — exécution ≤ 15 min.",
        });
      }
      return new Response("ok");
    }

    const msg = update.message;
    if (!msg || !msg.text) return new Response("ok");

    const chatId = msg.chat.id;
    const cmd = msg.text.trim().split(/[\s@]/)[0].toLowerCase();

    // --- Commandes de pilotage de l'agent (écrivent dans KV) ---
    const pilotage = {
      "/pause": { pause: true, texte: "⏸️ Agent en pause. Les positions ouvertes restent gérées ; aucune nouvelle entrée." },
      "/reprise": { pause: false, texte: "▶️ Agent réarmé." },
      "/mode_blanc": { mode: "blanc", texte: "🧪 Mode À BLANC : validation Kraken sans exécution." },
      "/mode_bouton": { mode: "bouton", texte: "🔘 Mode BOUTON : chaque trade attend ton ✅." },
      "/mode_auto": { mode: "auto", texte: "🤖 Mode AUTO : exécution directe des signaux ✅. Garde-fous actifs." },
    };
    if (pilotage[cmd]) {
      const flags = await lireFlags(env);
      if ("pause" in pilotage[cmd]) flags.pause = pilotage[cmd].pause;
      if (pilotage[cmd].mode) flags.mode = pilotage[cmd].mode;
      if (cmd === "/reprise") flags.pause = false;
      await env.AGENT_KV.put("flags", JSON.stringify(flags));
      await sendMessage(env.TELEGRAM_TOKEN, chatId, pilotage[cmd].texte);
      return new Response("ok");
    }

    let data = null;
    try {
      data = await (await fetch(DATA_URL + "?t=" + Date.now(), { cf: { cacheTtl: 0 } })).json();
    } catch {}

    if (cmd === "/agent") {
      const flags = await lireFlags(env);
      const a = (data && data.agent) || {};
      const pos = (a.positions || [])
        .map((p) => `  · ${p.asset} @ ${fmt(p.entry, 2)} $ — ${p.statut === "tp1_fait" ? "TP1 fait, SL à l'entrée" : "ouvert"}`)
        .join("\n");
      await sendMessage(
        env.TELEGRAM_TOKEN, chatId,
        `🤖 <b>Agent</b>\nMode : <b>${flags.mode}</b>${flags.pause ? " · ⏸️ EN PAUSE" : ""}\n` +
          `Trades aujourd'hui : ${a.trades_jour ?? 0}/5 · SL consécutifs : ${a.sl_consecutifs ?? 0}/3\n` +
          `Validations à blanc : ${a.validations_blanc ?? 0}\n` +
          (a.types_suspendus?.length ? `Types suspendus : ${a.types_suspendus.join(", ")}\n` : "") +
          (pos ? `Positions :\n${pos}` : "Aucune position agent ouverte.") +
          `\n\n/pause /reprise /mode_blanc /mode_bouton /mode_auto`,
        kbFromCockpit(),
      );
      return new Response("ok");
    }

    if (cmd === "/gains") {
      const pnl = (data && data.agent && data.agent.pnl) || [];
      const nowMs = Date.now();
      const somme = (jours) =>
        pnl
          .filter((p) => nowMs - new Date(p.time).getTime() <= jours * 86400e3)
          .reduce((s, p) => s + p.usd, 0);
      const ligne = (label, jours) => {
        const s = somme(jours);
        const pct = (s / ENVELOPPE) * 100;
        const e = s > 0 ? "🟢" : s < 0 ? "🔴" : "⚪";
        return `${e} ${label} : <b>${s >= 0 ? "+" : ""}${s.toFixed(2)} $</b> (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}% de l'enveloppe)`;
      };
      const dernieres = pnl.slice(-5).reverse()
        .map((p) => `  · ${p.usd >= 0 ? "+" : ""}${p.usd.toFixed(2)} $ — ${p.asset} ${p.quoi} <i>${heure(p.time)}</i>`)
        .join("\n");
      await sendMessage(
        env.TELEGRAM_TOKEN, chatId,
        pnl.length
          ? `💰 <b>Gains réalisés (agent)</b>\n\n${ligne("Aujourd'hui", 1)}\n${ligne("7 jours", 7)}\n${ligne("30 jours", 30)}\n\nDernières sorties :\n${dernieres}\n\n<i>P&L estimés (frais ~0,25%/ordre inclus). Ne couvre pas tes trades manuels — ceux-là vivent dans ton journal.</i>`
          : "💰 <b>Gains réalisés (agent)</b> — aucune sortie enregistrée pour l'instant. Le registre démarre au premier TP ou SL exécuté par l'agent (modes bouton/auto).",
        kbFromCockpit(),
      );
      return new Response("ok");
    }

    const reply = buildReply(cmd, data);
    await sendMessage(env.TELEGRAM_TOKEN, chatId, reply.text, reply.keyboard);
    return new Response("ok");
  },
};

async function lireFlags(env) {
  const raw = await env.AGENT_KV.get("flags");
  const flags = raw ? JSON.parse(raw) : {};
  return { mode: flags.mode || "blanc", pause: flags.pause !== false, approvals: flags.approvals || [] };
}

async function tg(env, method, body) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

function kbFromCockpit() {
  return { inline_keyboard: [[{ text: "📊 Ouvrir le cockpit", web_app: { url: COCKPIT_URL } }]] };
}

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
          "/signaux — derniers signaux\n/spot — signaux d'achat dimensionnés pour mon enveloppe\n/bilan — historique et taux de réussite des signaux\n/agent — état et pilotage de l'agent Kraken\n/gains — P&L réalisés : jour, 7j, 30j\n" +
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
        const v = s.plan && s.plan.verdict ? s.plan.verdict + " " : "";
        const plan = s.plan
          ? `\n🎯 Entrée ${fmt(s.plan.entry, 2)} $ · SL ${fmt(s.plan.sl, 2)} $ (−${s.plan.risk_pct}%) · TP1 ${fmt(s.plan.tp1, 2)} $ · TP2 ${fmt(s.plan.tp2, 2)} $`
          : "";
        return `${s.type === "achat" ? "🟢" : "🔴"} ${v}<b>${s.asset}</b> — ${s.reason}${plan}\n<i>${heure(s.time)}</i>`;
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

    case "/bilan": {
      const res = data.signaux_resultats || [];
      if (!res.length)
        return {
          text:
            "📒 <b>Bilan des signaux</b> — aucun signal résolu pour l'instant.\n" +
            "Chaque signal est suivi jusqu'à son SL, son TP2 ou 7 jours ; " +
            "les issues s'accumuleront ici, que tu aies pris le trade ou non.",
          keyboard: kbCockpit,
        };
      const stats = (arr) => {
        const n = arr.length;
        if (!n) return "aucun signal résolu.";
        const c = (f) => arr.filter(f).length;
        const pc = (x) => ((x / n) * 100).toFixed(0);
        const tp2 = c((r) => r.resultat === "TP2");
        const sl = c((r) => r.resultat === "SL");
        const exp = c((r) => r.resultat === "expiré");
        const tp1 = c((r) => r.tp1_franchi);
        return (
          `🎯 TP2 : <b>${tp2}</b> (${pc(tp2)}%) · 🛑 SL : <b>${sl}</b> (${pc(sl)}%) · ⏳ expirés : ${exp}\n` +
          `TP1 franchi au moins : <b>${tp1}</b> (${pc(tp1)}%)`
        );
      };
      const achats = res.filter((r) => r.type === "achat");
      const ventes = res.filter((r) => r.type === "vente");
      const parActif = {};
      for (const r of achats) {
        const a = (parActif[r.asset] = parActif[r.asset] || { tp2: 0, sl: 0 });
        if (r.resultat === "TP2") a.tp2 += 1;
        if (r.resultat === "SL") a.sl += 1;
      }
      const tableau = Object.entries(parActif)
        .map(([asset, c]) => `${asset} ${c.tp2}🎯/${c.sl}🛑`)
        .join(" · ");
      let texte =
        `📒 <b>Bilan des signaux</b> (${res.length} résolus)\n\n` +
        `🟢 <b>Achats (jouables en spot)</b> — ${achats.length}\n${stats(achats)}\n` +
        (tableau ? `Par actif : ${tableau}\n` : "") +
        `\n🔴 <b>Ventes (indicatif)</b> — ${ventes.length}\n${stats(ventes)}\n\n` +
        `Dernières issues :\n`;
      const pied =
        "\n<i>Niveaux de prix contrôlés toutes les 15 minutes — indicatif, " +
        "pris ou non par toi.</i>";
      // Limite Telegram 4096 caractères : on tronque la liste, jamais les stats
      for (const r of res.slice(0, 10)) {
        const e = r.resultat === "TP2" ? "🎯" : r.resultat === "SL" ? "🛑" : "⏳";
        const ligne = `${e} ${r.type} <b>${r.asset}</b> → ${r.resultat}${r.tp1_franchi && r.resultat === "SL" ? " (après TP1)" : ""} · <i>${heure(r.signal_time)}</i>\n`;
        if (texte.length + ligne.length + pied.length > 3900) break;
        texte += ligne;
      }
      return { text: texte + pied, keyboard: kbCockpit };
    }

    case "/aide":
      return {
        text:
          "📌 /prix — marchés + RSI\n/portefeuille — profils simulés\n" +
          "/traders — top traders\n/signaux — derniers signaux\n/spot — plan spot\n/bilan — historique des signaux",
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
