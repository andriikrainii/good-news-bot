/**
 * Будильник. Живе на Netlify і працює цілодобово.
 *
 * GitHub на безкоштовному тарифі будить бота коли заманеться — раз на
 * 2-5 годин замість щогодини. Через це розсилки приходили невчасно.
 * Тому час стежить Netlify: коли настав час розсилки, він сам просить
 * GitHub запустити бота.
 *
 * Будильник не шле новини — це й далі робить GitHub. Він лише стукає.
 */

import { getStore } from "@netlify/blobs";

const STORE = "goodnews";
const TIMEZONE = "Europe/Kyiv";

// Скільки годин після часу розсилки ще має сенс наздоганяти.
const CATCH_UP_HOURS = 6;
// Скільки днів пам'ятати, що будильник уже дзвонив.
const MEMORY_DAYS = 7;

const REPO = process.env.GITHUB_REPO || "andriikrainii/good-news-bot";
const WORKFLOW = process.env.GITHUB_WORKFLOW_FILE || "goodnews.yml";
const BRANCH = process.env.GITHUB_BRANCH || "main";

/** Котра зараз година в Києві. */
function kyivNow() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: TIMEZONE,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date()).map((p) => [p.type, p.value])
  );
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

function toMinutes(time) {
  const match = /^(\d{2}):(\d{2})$/.exec(String(time).trim());
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  if (hour > 23 || minute > 59) return null;
  return hour * 60 + minute;
}

async function askGithubToRun() {
  const token = process.env.GITHUB_TOKEN;
  if (!token) {
    console.error("немає GITHUB_TOKEN — будити нема чим");
    return false;
  }
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        authorization: `Bearer ${token}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "good-news-bot-alarm",
      },
      body: JSON.stringify({ ref: BRANCH, inputs: { mode: "schedule" } }),
    });
    if (resp.status === 204) return true;
    console.error(`GitHub відповів ${resp.status}: ${(await resp.text()).slice(0, 300)}`);
    return false;
  } catch (err) {
    console.error("не достукався до GitHub:", err.message);
    return false;
  }
}

/** Тримати функцію кнопок не сплячою: розбудити сплячу коштує кількох секунд. */
async function keepButtonsAwake() {
  const site = process.env.URL || process.env.DEPLOY_URL;
  if (!site) return;
  try {
    await fetch(`${site}/warm`, { method: "GET" });
  } catch {
    // Прогрів — річ необов'язкова, мовчки пропускаємо.
  }
}

export default async () => {
  const store = getStore(STORE);
  const warming = keepButtonsAwake();
  const config = await store.get("config", { type: "json" });
  const times = config?.send_times || [];
  if (!times.length) {
    console.log("розклад ще не відомий — GitHub не присилав знімка налаштувань");
    await warming;
    return new Response("no schedule");
  }

  const now = kyivNow();
  const done = new Set((await store.get("alarm_done", { type: "json" })) || []);

  // Найсвіжіша розсилка, час якої минув, а будильник по ній ще не дзвонив.
  let target = null;
  for (const time of times) {
    const at = toMinutes(time);
    if (at === null) continue;
    const late = now.minutes - at;
    if (late < 0 || late > CATCH_UP_HOURS * 60) continue;
    const key = `${now.date} ${time}`;
    if (done.has(key)) continue;
    if (!target || at > target.at) target = { at, key, time, late };
  }

  if (!target) {
    await warming;
    return new Response("no slot due");
  }

  console.log(`час розсилки ${target.time} (запізнення ${target.late} хв) — бужу GitHub`);
  if (!(await askGithubToRun())) {
    // Не записуємо в пам'ять — спробуємо ще раз наступного разу.
    return new Response("github unreachable", { status: 200 });
  }

  await warming;
  done.add(target.key);
  const cutoff = new Date(Date.now() - MEMORY_DAYS * 86400_000)
    .toISOString().slice(0, 10);
  await store.setJSON(
    "alarm_done",
    [...done].filter((key) => key.slice(0, 10) >= cutoff).sort()
  );
  return new Response(`asked github to run for ${target.key}`);
};

// Кожні 10 хвилин. Дзвонить лише тоді, коли справді час розсилки.
export const config = { schedule: "*/10 * * * *" };
