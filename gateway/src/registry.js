// Keeps one grammY Bot per team, plus the master bot, and connects each to Telegram.
import { Bot, webhookCallback } from 'grammy';
import { setTeamDefaultCommands, syncAdminCommands } from './commands.js';
import { botOptions, cfg } from './config.js';
import { installTeamHandlers } from './team.js';

const ALLOWED_UPDATES = ['message', 'callback_query', 'my_chat_member'];
const byBotId = new Map();
const byTeamId = new Map();

export const getEntry = (botId) => byBotId.get(String(botId));
export const entryByTeam = (teamId) => byTeamId.get(Number(teamId));

/** Connect a bot to Telegram: a webhook to PUBLIC_URL, or long polling. */
export async function connect(bot, path, secret) {
  if (cfg.mode === 'webhook') {
    await bot.api.setWebhook(`${cfg.publicUrl}${path}`, { secret_token: secret, allowed_updates: ALLOWED_UPDATES });
    return webhookCallback(bot, 'express', { secretToken: secret, onTimeout: 'return', timeoutMilliseconds: 25_000 });
  }
  bot.start({ allowed_updates: ALLOWED_UPDATES, onStart: (me) => console.log(`polling @${me.username}`) })
    .catch((e) => console.error(`polling stopped for ${path}:`, e.message));
  return null;
}

export async function registerTeam(team) {
  const old = byBotId.get(String(team.bot_id));
  if (old && cfg.mode === 'polling') await old.bot.stop().catch(() => {});

  const bot = new Bot(team.token, botOptions());
  await bot.init();
  const entry = { bot, team: { ...team } };
  delete entry.team.token;
  bot.use((ctx, next) => {
    ctx.entry = entry;
    return next();
  });
  installTeamHandlers(bot);
  entry.handler = await connect(bot, `/tg/${team.bot_id}`, team.webhook_secret);
  byBotId.set(String(team.bot_id), entry);
  byTeamId.set(Number(team.team_id), entry);

  await setTeamDefaultCommands(bot.api).catch((e) => console.warn('default commands:', e.message));
  await syncAdminCommands(entry).catch((e) => console.warn('admin commands:', e.message));
  console.log(`team ${team.team_id} ready: @${bot.botInfo.username}`);
  return entry;
}
