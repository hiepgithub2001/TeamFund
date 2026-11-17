import crypto from 'node:crypto';
import express from 'express';
import { setMasterCommands } from './commands.js';
import { cfg } from './config.js';
import { core } from './core.js';
import { handleEvent } from './events.js';
import { buildMasterBot } from './master.js';
import { connect, getEntry, registerTeam } from './registry.js';

const app = express();
app.use(express.json({ limit: '1mb' }));

let masterHandler = null;

app.get('/health', (_req, res) => res.json({ ok: true }));

app.post('/tg/master', (req, res, next) => (masterHandler ? masterHandler(req, res, next) : res.sendStatus(503)));

app.post('/tg/:botId', (req, res, next) => {
  const entry = getEntry(req.params.botId);
  if (!entry?.handler) return res.sendStatus(404);
  return entry.handler(req, res, next);
});

const sameKey = (a, b) => a.length === b.length && crypto.timingSafeEqual(Buffer.from(a), Buffer.from(b));

app.post('/internal/events', async (req, res) => {
  if (!sameKey(String(req.get('X-Internal-Key') || ''), cfg.internalKey)) return res.sendStatus(401);
  try {
    await handleEvent(req.body);
    res.json({ ok: true });
  } catch (e) {
    console.error('event failed:', e.message);
    res.status(503).json({ ok: false });
  }
});

async function waitForCore() {
  for (let i = 0; ; i++) {
    try {
      return await core('/internal/bots');
    } catch (e) {
      if (i % 10 === 0) console.log(`waiting for core… (${e.message})`);
      await new Promise((r) => setTimeout(r, 2000));
    }
  }
}

async function boot() {
  if (cfg.mode === 'webhook' && !cfg.publicUrl.startsWith('https://')) {
    throw new Error('PUBLIC_URL must be an https:// URL in webhook mode (or set TELEGRAM_MODE=polling)');
  }
  const teams = await waitForCore();
  for (const team of teams) {
    try {
      await registerTeam(team);
    } catch (e) {
      console.error(`could not start bot for team ${team.team_id} (@${team.bot_username}): ${e.message}`);
    }
  }
  if (!cfg.masterToken) {
    console.warn('MASTER_BOT_TOKEN is not set; the master bot is off');
    return;
  }
  const master = buildMasterBot(cfg.masterToken);
  await master.init();
  masterHandler = await connect(master, '/tg/master', cfg.masterSecret);
  await setMasterCommands(master.api, cfg.adminIds).catch((e) => console.warn('master commands:', e.message));
  console.log(`master bot ready: @${master.botInfo.username} (${cfg.mode})`);
}

app.listen(cfg.port, () => {
  console.log(`gateway listening on :${cfg.port}`);
  boot().catch((e) => {
    console.error('boot failed:', e);
    process.exit(1);
  });
});
