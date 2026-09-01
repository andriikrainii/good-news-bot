/**
 * Миттєва частина бота: живе на Netlify і відповідає одразу.
 *
 * Telegram надсилає сюди кожне натискання кнопки, і ця функція
 * тут-таки перемальовує лічильник. Новини, як і раніше, шле GitHub.
 *
 * Два входи:
 *   /telegram — сюди стукає Telegram (захищено таємним заголовком)
 *   /sync     — сюди стукає GitHub Actions (захищено ключем)
 */

import { getStore } from "@netlify/blobs";

const STORE = "goodnews";
const POST_PREFIX = "post:";
const POST_MEMORY_DAYS = 30;

const COUNT_CHOICES = [1, 2, 3, 4, 5, 7, 10];
const SCHEDULE_PRESETS = {
  1: ["10:00"],
  2: ["09:00", "19:00"],
  3: ["09:00", "14:00", "20:00"],
  4: ["08:00", "12:00", "16:00", "20:00"],
};

const COMMAND_MENUS = {
  menu: "main", start: "main", help: "main",
  temy: "topics", topics: "topics",
  chastota: "freq", freq: "freq",
  stat: "stats", stats: "stats",
};

// ------------------------------------------------------------ Telegram

async function callTelegram(method, payload) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  try {
    const resp = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    return await resp.json();
  } catch (err) {
    console.error(`${method} не спрацював:`, err.message);
    return { ok: false };
  }
}

const escapeHtml = (text) =>
  String(text ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

// ------------------------------------------------------------ налаштування

/** Знімок config.yaml плюс зміни, які ще не доїхали до GitHub. */
async function effectiveConfig(store) {
  const snapshot = (await store.get("config", { type: "json" })) || {
    posts_per_day: 0, send_times: [], topics: [],
  };
  const pending = (await store.get("pending", { type: "json" })) || [];
  const config = JSON.parse(JSON.stringify(snapshot));
  for (const change of pending) {
    if (change.kind === "topic" && config.topics?.[change.index]) {
      config.topics[change.index].enabled = change.value;
    } else if (change.kind === "count") {
      config.posts_per_day = change.value;
    } else if (change.kind === "schedule") {
      config.send_times = SCHEDULE_PRESETS[change.key] || config.send_times;
    }
  }
  return config;
}

/** Записати зміну в чергу — GitHub забере її й акуратно впише в config.yaml. */
async function queueChange(store, change) {
  const pending = (await store.get("pending", { type: "json" })) || [];
  const nextId = pending.reduce((max, item) => Math.max(max, item.id || 0), 0) + 1;
  pending.push({ ...change, id: nextId });
  await store.setJSON("pending", pending);
}

// ------------------------------------------------------------ голоси

async function allPosts(store) {
  const { blobs } = await store.list({ prefix: POST_PREFIX });
  const posts = {};
  await Promise.all(
    blobs.map(async ({ key }) => {
      const post = await store.get(key, { type: "json" });
      if (post) posts[key.slice(POST_PREFIX.length)] = post;
    })
  );
  return posts;
}

function tallyVotes(posts) {
  const topics = {}, sources = {};
  for (const post of Object.values(posts)) {
    const up = (post.up || []).length;
    const down = (post.down || []).length;
    if (!up && !down) continue;
    for (const name of post.topics || []) {
      const entry = (topics[name] ??= { up: 0, down: 0 });
      entry.up += up;
      entry.down += down;
    }
    if (post.source) {
      const entry = (sources[post.source] ??= { up: 0, down: 0 });
      entry.up += up;
      entry.down += down;
    }
  }
  return { topics, sources };
}

const netScore = (votes, name) => {
  const entry = votes?.[name] || {};
  return (entry.up || 0) - (entry.down || 0);
};

// ------------------------------------------------------------ кнопки

function voteKeyboard(post) {
  const up = (post.up || []).length;
  const down = (post.down || []).length;
  return [[
    { text: up ? `👍 ${up}` : "👍", callback_data: "v:u" },
    { text: down ? `👎 ${down}` : "👎", callback_data: "v:d" },
  ]];
}

function menuFor(kind, config, votes) {
  const times = (config.send_times || []).join(", ");
  const count = config.posts_per_day || 0;

  if (kind === "topics") {
    const keyboard = (config.topics || []).map((topic, index) => {
      const net = netScore(votes.topics, topic.name);
      const tail = net ? `  (${net > 0 ? "+" : ""}${net})` : "";
      return [{
        text: `${topic.enabled ? "✅" : "❌"} ${topic.name}${tail}`,
        callback_data: `t:${index}`,
      }];
    });
    keyboard.push([{ text: "⬅️ Назад", callback_data: "m:main" }]);
    return {
      text: "<b>📋 Теми новин</b>\n\nНатисніть на тему, щоб увімкнути або вимкнути її.\n" +
            "✅ — новини на цю тему приходять, ❌ — ні.",
      keyboard,
    };
  }

  if (kind === "freq") {
    const row = COUNT_CHOICES.map((n) => ({
      text: (n === count ? "• " : "") + n, callback_data: `n:${n}`,
    }));
    const scheduleRow = Object.keys(SCHEDULE_PRESETS).map((key) => {
      const same = JSON.stringify(SCHEDULE_PRESETS[key]) === JSON.stringify(config.send_times || []);
      return {
        text: `${same ? "• " : ""}${key} ${key === "1" ? "раз" : "рази"}`,
        callback_data: `s:${key}`,
      };
    });
    return {
      text: `<b>⏰ Скільки і коли</b>\n\nЗараз: <b>${count}</b> новин на добу, о ${escapeHtml(times)}\n\n` +
            "Новини діляться між часами розсилки якомога рівніше.",
      keyboard: [
        row.slice(0, 4), row.slice(4),
        [{ text: "— коли надсилати —", callback_data: "m:freq" }],
        scheduleRow,
        [{ text: "⬅️ Назад", callback_data: "m:main" }],
      ],
    };
  }

  if (kind === "stats") {
    const rows = (config.topics || [])
      .map((topic) => {
        const entry = votes.topics?.[topic.name] || {};
        return { name: topic.name, up: entry.up || 0, down: entry.down || 0 };
      })
      .filter((row) => row.up || row.down)
      .sort((a, b) => (b.up - b.down) - (a.up - a.down));
    const lines = ["<b>📊 Що подобається групі</b>", ""];
    if (rows.length) {
      for (const row of rows) lines.push(`${escapeHtml(row.name)}: 👍 ${row.up}  👎 ${row.down}`);
      lines.push("", "Теми, які подобаються більше, бот показує частіше.");
    } else {
      lines.push("Поки ніхто не голосував.",
                 "Тисніть 👍 або 👎 під новинами — і бот підлаштується.");
    }
    return { text: lines.join("\n"), keyboard: [[{ text: "⬅️ Назад", callback_data: "m:main" }]] };
  }

  const enabled = (config.topics || []).filter((t) => t.enabled).map((t) => t.name);
  return {
    text: `<b>⚙️ Налаштування бота</b>\n\nЗараз: <b>${count}</b> новин на добу, о ${escapeHtml(times)}\n` +
          `Увімкнені теми: ${escapeHtml(enabled.join(", ") || "— жодної —")}\n\n` +
          "Змінювати може будь-хто з групи.",
    keyboard: [
      [{ text: "📋 Теми новин", callback_data: "m:topics" }],
      [{ text: "⏰ Скільки і коли", callback_data: "m:freq" }],
      [{ text: "📊 Що подобається групі", callback_data: "m:stats" }],
    ],
  };
}

// ------------------------------------------------------------ Telegram-вхід

async function handleTelegram(update, store) {
  const chatId = String(process.env.TELEGRAM_CHAT_ID || "");

  const message = update.message;
  if (message) {
    if (String(message.chat?.id) !== chatId) return;
    const word = (message.text || "").trim().split(/\s+/)[0] || "";
    if (!word.startsWith("/")) return;
    const kind = COMMAND_MENUS[word.slice(1).split("@")[0].toLowerCase()];
    if (!kind) return;
    const config = await effectiveConfig(store);
    const votes = tallyVotes(await allPosts(store));
    const { text, keyboard } = menuFor(kind, config, votes);
    await callTelegram("sendMessage", {
      chat_id: chatId, text, parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: { inline_keyboard: keyboard },
    });
    return;
  }

  const callback = update.callback_query;
  if (!callback) return;

  const data = callback.data || "";
  const messageId = callback.message?.message_id;
  const answer = (text) =>
    callTelegram("answerCallbackQuery", { callback_query_id: callback.id, text: text || "" });

  if (String(callback.message?.chat?.id) !== chatId) {
    await answer("Цей бот працює лише у своїй групі.");
    return;
  }

  // --- голос: найшвидший шлях, одразу перемальовуємо кнопки ---
  if (data.startsWith("v:")) {
    const key = POST_PREFIX + messageId;
    const post = await store.get(key, { type: "json" });
    if (!post) {
      await answer("Ця новина вже застара для голосування.");
      return;
    }
    const userId = String(callback.from?.id);
    post.up ||= [];
    post.down ||= [];
    const liked = data === "v:u";
    const mine = liked ? post.up : post.down;
    const other = liked ? post.down : post.up;
    let note;
    const at = mine.indexOf(userId);
    if (at !== -1) {
      mine.splice(at, 1);
      note = "Голос скасовано.";
    } else {
      mine.push(userId);
      const was = other.indexOf(userId);
      if (was !== -1) other.splice(was, 1);
      note = liked ? "Дякую! Врахую 👍" : "Зрозумів, таких менше 👎";
    }
    await store.setJSON(key, post);
    await Promise.all([
      answer(note),
      callTelegram("editMessageReplyMarkup", {
        chat_id: chatId, message_id: messageId,
        reply_markup: { inline_keyboard: voteKeyboard(post) },
      }),
    ]);
    return;
  }

  // --- меню й налаштування ---
  let kind = "main";
  let note = "";

  if (data.startsWith("m:")) {
    kind = data.slice(2);
  } else if (data.startsWith("t:")) {
    kind = "topics";
    const index = Number(data.slice(2));
    const config = await effectiveConfig(store);
    const topic = config.topics?.[index];
    if (topic) {
      const value = !topic.enabled;
      await queueChange(store, { kind: "topic", index, value });
      note = `${topic.name}: ${value ? "увімкнено" : "вимкнено"}`;
    }
  } else if (data.startsWith("n:")) {
    kind = "freq";
    const value = Number(data.slice(2));
    if (COUNT_CHOICES.includes(value)) {
      await queueChange(store, { kind: "count", value });
      note = `Тепер ${value} новин на добу`;
    }
  } else if (data.startsWith("s:")) {
    kind = "freq";
    const key = data.slice(2);
    if (SCHEDULE_PRESETS[key]) {
      await queueChange(store, { kind: "schedule", key });
      note = "Новий розклад: " + SCHEDULE_PRESETS[key].join(", ");
    }
  } else {
    return;
  }

  const config = await effectiveConfig(store);
  const votes = tallyVotes(await allPosts(store));
  const { text, keyboard } = menuFor(kind, config, votes);
  await Promise.all([
    answer(note),
    callTelegram("editMessageText", {
      chat_id: chatId, message_id: messageId, text, parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: { inline_keyboard: keyboard },
    }),
  ]);
}

// ------------------------------------------------------------ вхід із GitHub

async function handleSync(body, store) {
  if (body.config) await store.setJSON("config", body.config);

  for (const [messageId, post] of Object.entries(body.new_posts || {})) {
    await store.setJSON(POST_PREFIX + messageId, { up: [], down: [], ...post });
  }

  if (body.ack_pending) {
    const pending = (await store.get("pending", { type: "json" })) || [];
    await store.setJSON("pending", pending.filter((item) => item.id > body.ack_pending));
  }

  const posts = await allPosts(store);
  const cutoff = Date.now() - POST_MEMORY_DAYS * 86400_000;
  for (const [messageId, post] of Object.entries(posts)) {
    if (Date.parse(post.at || "") < cutoff) {
      await store.delete(POST_PREFIX + messageId);
      delete posts[messageId];
    }
  }

  return {
    votes: tallyVotes(posts),
    pending: (await store.get("pending", { type: "json" })) || [],
    posts_count: Object.keys(posts).length,
  };
}

// ------------------------------------------------------------ маршрути

export default async (req) => {
  const store = getStore(STORE);
  const path = new URL(req.url).pathname;

  if (path.endsWith("/telegram")) {
    const secret = process.env.TELEGRAM_WEBHOOK_SECRET || "";
    if (secret && req.headers.get("x-telegram-bot-api-secret-token") !== secret) {
      return new Response("no", { status: 401 });
    }
    let update;
    try {
      update = await req.json();
    } catch {
      return new Response("ok");
    }
    try {
      await handleTelegram(update, store);
    } catch (err) {
      // Telegram повторює те, на що не відповіли — тому завжди кажемо "ok".
      console.error("помилка обробки:", err);
    }
    return new Response("ok");
  }

  if (path.endsWith("/sync")) {
    const expected = process.env.SYNC_SECRET || "";
    if (!expected || req.headers.get("authorization") !== `Bearer ${expected}`) {
      return new Response("no", { status: 401 });
    }
    const body = await req.json().catch(() => ({}));
    const result = await handleSync(body, store);
    return Response.json(result);
  }

  return new Response("good-news-bot");
};

export const config = { path: ["/telegram", "/sync"] };
