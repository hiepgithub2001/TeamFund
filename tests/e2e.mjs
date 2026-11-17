// End-to-end test: real core + real gateway, with a fake Telegram Bot API.
// Usage: PYTHON=/path/to/venv/bin/python node tests/e2e.mjs
import { spawn } from 'node:child_process';
import http from 'node:http';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PY = process.env.PYTHON || 'python3';
const TG_PORT = 18081, CORE_PORT = 18000, GW_PORT = 18002;
const MASTER_TOKEN = '999999:' + 'A'.repeat(35);
const TEAM_TOKEN = '555555:' + 'B'.repeat(35);
const SUPER = 1, OWNER = 100, MEMBER = 300, GROUP = -1001;

// ---- fake Telegram -------------------------------------------------------------------------
const calls = [];
let msgId = 1000;
const fakeTg = http.createServer((req, res) => {
  let body = '';
  req.on('data', (c) => (body += c));
  req.on('end', () => {
    const [, token, method] = req.url.match(/^\/bot([^/]+)\/(\w+)/) || [];
    let params = {};
    try { params = JSON.parse(body || '{}'); } catch { params = { raw: body.length }; }
    calls.push({ token, method, params });
    const botId = Number(token.split(':')[0]);
    let result = true;
    if (method === 'getMe') {
      result = { id: botId, is_bot: true, first_name: botId === 555555 ? 'FC Test Bot' : 'Master',
        username: botId === 555555 ? 'fctest_bot' : 'master_bot', can_join_groups: true,
        can_read_all_group_messages: false, supports_inline_queries: false };
    } else if (['sendMessage', 'sendPhoto', 'sendDocument', 'editMessageText'].includes(method)) {
      result = { message_id: ++msgId, date: 0, chat: { id: Number(params.chat_id) || 0, type: 'private' }, text: params.text };
    }
    res.setHeader('Content-Type', 'application/json');
    res.end(JSON.stringify({ ok: true, result }));
  });
});

// ---- helpers -------------------------------------------------------------------------------
const procs = [];
function start(cmd, args, opts) {
  const p = spawn(cmd, args, { ...opts, stdio: ['ignore', 'pipe', 'pipe'] });
  const name = path.basename(opts.cwd);
  p.stdout.on('data', (d) => process.env.VERBOSE && process.stdout.write(`[${name}] ${d}`));
  p.stderr.on('data', (d) => process.env.VERBOSE && process.stdout.write(`[${name}] ${d}`));
  procs.push(p);
  return p;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(fn, label, ms = 15000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = await fn().catch(() => null);
    if (v) return v;
    await sleep(100);
  }
  throw new Error(`timed out: ${label}`);
}

let updateId = 1;
const user = (id) => ({ id, is_bot: false, first_name: `User${id}`, language_code: 'en' });
const chatOf = (chatId) => ({ id: chatId, type: chatId < 0 ? 'supergroup' : 'private', title: chatId < 0 ? 'Team' : undefined });

async function send(botPath, secret, update) {
  const r = await fetch(`http://127.0.0.1:${GW_PORT}${botPath}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Telegram-Bot-Api-Secret-Token': secret },
    body: JSON.stringify({ update_id: updateId++, ...update }),
  });
  assert.equal(r.status, 200, `webhook ${botPath} -> ${r.status}`);
}
function textUpdate(from, text, chatId = from) {
  const cmd = text.match(/^\/\w+/);
  return { message: { message_id: ++msgId, date: 0, from: user(from), chat: chatOf(chatId), text,
    entities: cmd ? [{ type: 'bot_command', offset: 0, length: cmd[0].length }] : undefined } };
}
function cbUpdate(from, data, messageId, chatId = from) {
  return { callback_query: { id: String(++msgId), from: user(from), chat_instance: 'x', data,
    message: { message_id: messageId, date: 0, chat: chatOf(chatId), text: '' } } };
}
const sent = (pred) => calls.filter((c) => ['sendMessage', 'sendPhoto', 'editMessageText'].includes(c.method) && pred(c));
const lastTo = (chatId, token) => sent((c) => String(c.params.chat_id) === String(chatId) && (!token || c.token === token)).at(-1);
const markupData = (c) => (c.params.reply_markup?.inline_keyboard || []).flat().map((b) => b.callback_data).filter(Boolean);
async function waitFor(pred, label) {
  return until(async () => sent(pred).at(-1), label);
}

// ---- run -----------------------------------------------------------------------------------
async function main() {
  await new Promise((r) => fakeTg.listen(TG_PORT, r));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'tf-e2e-'));
  const shared = { INTERNAL_KEY: 'ik', MASTER_ADMIN_IDS: String(SUPER), DEFAULT_LANG: 'en', PATH: process.env.PATH };
  const fernet = await new Promise((resolve, reject) => {
    const p = spawn(PY, ['-c', 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())']);
    let out = ''; p.stdout.on('data', (d) => (out += d)); p.on('close', (c) => (c ? reject(new Error('fernet')) : resolve(out.trim())));
  });
  start(PY, ['-m', 'uvicorn', 'app.main:app', '--port', String(CORE_PORT)], {
    cwd: path.join(root, 'core'),
    env: { ...shared, DATABASE_URL: `sqlite:///${tmp}/e2e.db`, TOKEN_ENC_KEY: fernet,
      GATEWAY_URL: `http://127.0.0.1:${GW_PORT}`, PUBLIC_URL: 'https://teamfund.test' },
  });
  await until(() => fetch(`http://127.0.0.1:${CORE_PORT}/health`).then((r) => r.ok), 'core up');
  start(process.execPath, ['src/index.js'], {
    cwd: path.join(root, 'gateway'),
    env: { ...shared, PORT: String(GW_PORT), CORE_URL: `http://127.0.0.1:${CORE_PORT}`, PUBLIC_URL: 'https://teamfund.test',
      MASTER_BOT_TOKEN: MASTER_TOKEN, MASTER_WEBHOOK_SECRET: 'msecret', TELEGRAM_API_ROOT: `http://127.0.0.1:${TG_PORT}` },
  });
  await until(async () => calls.some((c) => c.method === 'setWebhook' && c.params.url?.endsWith('/tg/master')), 'master webhook');
  const M = (u) => send('/tg/master', 'msecret', u);

  // 1. team request -> admin approves -> /newteam with token
  await M(textUpdate(OWNER, '/start'));
  await M(textUpdate(OWNER, '/requestteam'));
  await M(textUpdate(OWNER, 'FC Test'));
  const card = await waitFor((c) => String(c.params.chat_id) === String(SUPER) && /Team request/.test(c.params.text), 'admin card');
  const approve = markupData(card).find((d) => d.startsWith('tr:a:'));
  await M(cbUpdate(SUPER, approve, 1, SUPER));
  await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /approved/.test(c.params.text || ''), 'approved msg');
  await M(textUpdate(OWNER, '/newteam'));
  await M(textUpdate(OWNER, TEAM_TOKEN));
  await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /is ready/.test(c.params.text || ''), 'team created');
  const hook = calls.find((c) => c.method === 'setWebhook' && c.params.url?.endsWith('/tg/555555'));
  assert.ok(hook, 'team webhook set');
  assert.ok(calls.some((c) => c.method === 'deleteMessage'), 'token message deleted');
  const T = (u) => send('/tg/555555', hook.params.secret_token, u);
  const TT = TEAM_TOKEN;

  // wrong secret is refused
  const bad = await fetch(`http://127.0.0.1:${GW_PORT}/tg/555555`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Telegram-Bot-Api-Secret-Token': 'nope' }, body: '{"update_id":1}' });
  assert.equal(bad.status, 401);

  // 2. setup in the team bot
  await T(textUpdate(OWNER, '/setbank MBBank 0123456789 nguyen van a'));
  await waitFor((c) => c.token === TT && /Bank account saved/.test(c.params.text || ''), 'bank saved');
  await T(textUpdate(OWNER, '/setfee 200k'));
  await waitFor((c) => c.token === TT && /Monthly fee set to 200,000đ/.test(c.params.text || ''), 'fee set');
  await T(textUpdate(OWNER, '/add An | 10\nBình\nCường | 7 | 0901234567'));
  await waitFor((c) => c.token === TT && /Added 3/.test(c.params.text || ''), 'members added');
  await T(textUpdate(OWNER, '/linkgroup', GROUP));
  await waitFor((c) => String(c.params.chat_id) === String(GROUP) && /linked/.test(c.params.text || ''), 'group linked');
  await T(textUpdate(OWNER, '/sepay'));
  await T(cbUpdate(OWNER, 's:gen', 1));
  const keyMsg = await waitFor((c) => c.token === TT && /New SePay API key/.test(c.params.text || ''), 'sepay key');
  const apiKey = keyMsg.params.text.match(/<code>([^<]+)<\/code>/)[1];

  // non-manager can't use manager commands
  await T(textUpdate(MEMBER, '/setfee 1'));
  await waitFor((c) => String(c.params.chat_id) === String(MEMBER) && /Only team managers/.test(c.params.text || ''), 'role check');

  // 3. member pays for An and Bình
  await T(textUpdate(MEMBER, '/start'));
  const menu = await waitFor((c) => String(c.params.chat_id) === String(MEMBER) && /Choose who you are paying for/.test(c.params.text || ''), 'pay menu');
  const menuId = msgId; // id the fake server gave that message
  const menuMsg = calls.filter((c) => c.method === 'sendMessage').indexOf(menu);
  assert.ok(menuMsg >= 0);
  await T(cbUpdate(MEMBER, 'k:t:0', menuId));
  await T(cbUpdate(MEMBER, 'k:t:1', menuId));
  await T(cbUpdate(MEMBER, 'k:ok', menuId));
  const qr = await until(async () => calls.find((c) => c.method === 'sendPhoto' && String(c.params.chat_id) === String(MEMBER)), 'qr photo');
  assert.match(qr.params.photo, /^https:\/\/qr\.sepay\.vn\/img\?acc=0123456789&bank=MBBank&amount=400000&des=TF/);
  const code = qr.params.caption.match(/<code>(TF[A-Z0-9]{8})<\/code>/)[1];
  assert.match(qr.params.caption, /An, Bình/);

  // 4. SePay confirms
  const hookRes = await fetch(`http://127.0.0.1:${CORE_PORT}/sepay/1`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Apikey ${apiKey}` },
    body: JSON.stringify({ id: 1, gateway: 'MBBank', transferType: 'in', transferAmount: 400000, content: `CT ${code} FT2611` }),
  });
  assert.deepEqual(await hookRes.json(), { success: true });
  await waitFor((c) => String(c.params.chat_id) === String(MEMBER) && /Payment received: <b>400,000đ<\/b>/.test(c.params.text || ''), 'receipt');
  await waitFor((c) => String(c.params.chat_id) === String(GROUP) && /An, Bình<\/b> paid 400,000đ/.test(c.params.text || ''), 'group paid post');

  // 5. status, unmatched transfer, assign
  await T(textUpdate(MEMBER, '/status'));
  await waitFor((c) => String(c.params.chat_id) === String(MEMBER) && /Paid \(2\)/.test(c.params.text || '') && /Cường: 200,000đ/.test(c.params.text || ''), 'status');
  await fetch(`http://127.0.0.1:${CORE_PORT}/sepay/1`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Apikey ${apiKey}` },
    body: JSON.stringify({ id: 2, transferType: 'in', transferAmount: 200000, content: 'cuong dong quy' }),
  });
  const um = await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /needs checking/.test(c.params.text || ''), 'unmatched card');
  const assignBtn = markupData(um).find((d) => d.startsWith('u:a:'));
  await T(cbUpdate(OWNER, assignBtn, 5));
  await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /Which members is this/.test(c.params.text || ''), 'assign picker');
  const pickId = msgId;
  await T(cbUpdate(OWNER, 'k:t:0', pickId));
  await T(cbUpdate(OWNER, 'k:ok', pickId));
  await waitFor((c) => c.method === 'editMessageText' && /Assigned to Cường/.test(c.params.text || ''), 'assigned');

  // 6. manager request flow
  await T(textUpdate(MEMBER, '/requestmanager'));
  const mr = await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /Manager request/.test(c.params.text || ''), 'manager card');
  await T(cbUpdate(OWNER, markupData(mr).find((d) => d.startsWith('mr:a:')), 9));
  await waitFor((c) => String(c.params.chat_id) === String(MEMBER) && /You are now a manager/.test(c.params.text || ''), 'manager approved');
  assert.ok(calls.some((c) => c.method === 'setMyCommands' && c.params.scope?.chat_id === MEMBER), 'manager menu set');
  await T(textUpdate(MEMBER, '/expense 300000 Pitch rental'));
  await waitFor((c) => String(c.params.chat_id) === String(GROUP) && /Expense/.test(c.params.text || ''), 'expense posted');

  // 7. fund, members UI, lang, export
  await T(textUpdate(OWNER, '/fund'));
  await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /Balance: 300,000đ/.test(c.params.text || ''), 'fund');
  await T(textUpdate(OWNER, '/members'));
  const list = await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /Members<\/b> \(3\)/.test(c.params.text || ''), 'members list');
  await T(cbUpdate(OWNER, markupData(list)[0], 77));
  await waitFor((c) => c.method === 'editMessageText' && /Shirt number: 10/.test(c.params.text || ''), 'member detail');
  await T(cbUpdate(OWNER, 'l:vi', 78));
  await T(textUpdate(OWNER, '/fund'));
  await waitFor((c) => String(c.params.chat_id) === String(OWNER) && /Quỹ đội/.test(c.params.text || ''), 'vietnamese');
  await T(textUpdate(OWNER, '/export'));
  await until(async () => calls.find((c) => c.method === 'sendDocument'), 'export sent');

  console.log(`e2e passed (${calls.length} Telegram API calls)`);
}

main().then(() => { cleanup(); process.exit(0); }, (e) => { console.error(e); cleanup(); process.exit(1); });
function cleanup() { for (const p of procs) p.kill(); fakeTg.close(); }
