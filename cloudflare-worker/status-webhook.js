/**
 * Cloudflare Worker: принимает webhook от Telegram, и по команде /status
 * запускает GitHub Actions workflow "VPS status (on demand)" через API.
 *
 * Требуемые секреты (Settings -> Variables and Secrets в Cloudflare Worker):
 *   TELEGRAM_BOT_TOKEN  — токен бота (для мгновенного "⏳ Запускаю проверку...")
 *   TELEGRAM_CHAT_ID    — ваш chat_id, единственный, кому разрешено дёргать /status
 *   WEBHOOK_SECRET       — произвольная случайная строка, подтверждающая, что
 *                          запрос действительно пришёл от Telegram
 *   GITHUB_TOKEN         — fine-grained PAT с правом Actions: Read and write
 *                          только на репозиторий mba_monitor
 *   GITHUB_OWNER          — marscreator77-alt
 *   GITHUB_REPO           — mba_monitor
 */

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("ok", { status: 200 });
    }

    const secretHeader = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (secretHeader !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 401 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const message = update.message;
    if (!message || !message.text) {
      return new Response("ok", { status: 200 });
    }

    const chatId = String(message.chat.id);
    if (chatId !== env.TELEGRAM_CHAT_ID) {
      // не отвечаем чужим чатам, но и не палим лишнего
      return new Response("ok", { status: 200 });
    }

    const text = message.text.trim();
    const isStatusCommand = text === "/status" || text.startsWith("/status@");
    if (!isStatusCommand) {
      return new Response("ok", { status: 200 });
    }

    await sendTelegramMessage(env, "⏳ Запускаю проверку VPS...");

    const dispatchResp = await fetch(
      `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/status.yml/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "vps-monitor-status-webhook",
        },
        body: JSON.stringify({ ref: "main" }),
      }
    );

    if (!dispatchResp.ok) {
      const errText = await dispatchResp.text();
      await sendTelegramMessage(
        env,
        `⚠️ Не удалось запустить проверку (GitHub API ${dispatchResp.status}). Попробуйте позже.`
      );
      console.error("GitHub dispatch failed", dispatchResp.status, errText);
    }

    return new Response("ok", { status: 200 });
  },
};

async function sendTelegramMessage(env, text) {
  await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: env.TELEGRAM_CHAT_ID, text }),
  });
}
