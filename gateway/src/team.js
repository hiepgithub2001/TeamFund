// Handlers for every team bot. Each bot instance has ctx.entry = { bot, team } set by the registry.
import { InlineKeyboard, InputFile } from 'grammy';
import { clearAdminCommands, setAdminCommands } from './commands.js';
import { core } from './core.js';
import { amountAndTitle, esc, fullName, money, parseAmount } from './format.js';
import { t } from './i18n.js';
import { langFor, langOf, setLang } from './lang.js';
import { HTML, billLabel, edit, isPrivate, owesLines, payKeyboard, safe, statusText } from './render.js';
import { delState, getState, setState } from './state.js';

const tp = (ctx, path = '') => `/teams/${ctx.entry.team.team_id}${path}`;
const api = (ctx, path, opts = {}) => core(tp(ctx, path), { actor: ctx.from?.id, ...opts });
const groupLang = (ctx) => ctx.entry.team.lang;
const pickKey = (ctx, msgId) => `k:${ctx.me.id}:${ctx.chat.id}:${msgId}`;
const inputKey = (ctx) => `in:${ctx.me.id}:${ctx.chat.id}`;

async function role(ctx) {
  return (await api(ctx, '/role')).role;
}

/** Returns true if allowed; otherwise tells the user and returns false. */
async function allow(ctx, level, { privateOnly = true } = {}) {
  if (privateOnly && !isPrivate(ctx)) {
    await ctx.reply(t(ctx.lang, 'err.private_only'), HTML);
    return false;
  }
  const r = await role(ctx);
  const ok = level === 'owner' ? r === 'owner' : r === 'owner' || r === 'manager';
  if (!ok) {
    const text = t(ctx.lang, level === 'owner' ? 'err.not_owner' : 'err.not_manager');
    if (ctx.callbackQuery) await ctx.answerCallbackQuery({ text, show_alert: true });
    else await ctx.reply(text, HTML);
  }
  return ok;
}

async function postToGroup(ctx, text, extra = {}) {
  const chatId = ctx.entry.team.group_chat_id;
  if (!chatId) return;
  try {
    if (extra.photo) await ctx.api.sendPhoto(chatId, extra.photo, { caption: text, ...HTML, reply_markup: extra.reply_markup });
    else await ctx.api.sendMessage(chatId, text, { ...HTML, reply_markup: extra.reply_markup });
  } catch (e) {
    console.warn(`group post failed for team ${ctx.entry.team.team_id}: ${e.description || e.message}`);
  }
}

// ---- multi-select picker (pay, charge, split, cash, assign) -----------------------------------

function pickerText(lang, st) {
  return st.title;
}

function pickerKeyboard(lang, st) {
  const kb = new InlineKeyboard();
  const perRow = st.kind === 'pay' || st.kind === 'markpaid' || st.kind === 'assign' ? 1 : 2;
  st.options.forEach((o, i) => {
    kb.text(`${st.selected.includes(o.id) ? '✅' : '☐'} ${o.label}`, `k:t:${i}`);
    if ((i + 1) % perRow === 0) kb.row();
  });
  if (st.options.length % perRow) kb.row();
  if (st.kind === 'pay') {
    const total = st.options.filter((o) => st.selected.includes(o.id)).reduce((s, o) => s + o.amount, 0);
    kb.text(t(lang, 'pay.button', { count: st.selected.length, amount: money(total, lang) }), 'k:ok').row()
      .text('🔄', 'k:r');
  } else {
    kb.text(t(lang, 'btn.all'), 'k:a').text(t(lang, 'btn.confirm', { n: st.selected.length }), 'k:ok').row()
      .text(t(lang, 'btn.cancel'), 'k:x');
  }
  return kb;
}

async function openPicker(ctx, st) {
  const msg = await ctx.reply(pickerText(ctx.lang, st), { ...HTML, reply_markup: pickerKeyboard(ctx.lang, st) });
  setState(pickKey(ctx, msg.message_id), st);
}

const unpaidOptions = (lang, unpaid) => unpaid.map((u) => ({
  id: u.member_id, label: `${u.name} · ${money(u.total, lang)}`, amount: u.total, name: u.name,
}));

async function openPayMenu(ctx) {
  const unpaid = await api(ctx, '/unpaid');
  if (!unpaid.length) return ctx.reply(t(ctx.lang, 'pay.none'), HTML);
  return openPicker(ctx, {
    kind: 'pay',
    title: t(ctx.lang, 'pay.title', { team: esc(ctx.entry.team.name) }),
    options: unpaidOptions(ctx.lang, unpaid),
    selected: [],
  });
}

async function onPickerConfirm(ctx, st, key) {
  const L = ctx.lang;
  const ids = st.selected;
  if (!ids.length) return ctx.answerCallbackQuery({ text: t(L, 'pay.select_first') });

  if (st.kind === 'pay') {
    const intent = await api(ctx, '/intents', { method: 'POST', body: { member_ids: ids } });
    delState(key);
    await ctx.answerCallbackQuery();
    await ctx.editMessageReplyMarkup({ reply_markup: undefined }).catch(() => {});
    const caption = t(L, 'pay.qr_caption', {
      members: esc(intent.members.join(', ')), amount: money(intent.amount, L), bank: esc(intent.bank_name),
      account: esc(intent.bank_account), holder: esc(intent.bank_holder || ''), code: intent.code,
      expires: intent.expires_at,
    });
    try {
      await ctx.replyWithPhoto(intent.qr_url, { caption, ...HTML });
    } catch (e) {
      console.warn('QR photo failed:', e.description || e.message);
      await ctx.reply(`${caption}\n\n${t(L, 'pay.qr_failed')}`, HTML);
    }
    return;
  }

  if (!(await allow(ctx, 'manager'))) return;
  if (st.kind === 'charge' || st.kind === 'split') {
    const r = await api(ctx, '/charges', {
      method: 'POST', body: { title: st.data.title, amount: st.data.amount, kind: st.kind, member_ids: ids },
    });
    delState(key);
    await ctx.answerCallbackQuery();
    await edit(ctx, t(L, 'charge.done', { count: r.count, title: esc(r.title), each: money(r.each, L) }));
    const G = groupLang(ctx);
    await postToGroup(ctx, t(G, 'group.charge', { title: esc(r.title), each: money(r.each, G), count: r.count }),
      { reply_markup: payKeyboard(G, ctx.me.username) });
  } else if (st.kind === 'markpaid') {
    const r = await api(ctx, '/markpaid', { method: 'POST', body: { member_ids: ids } });
    delState(key);
    await ctx.answerCallbackQuery();
    await edit(ctx, t(L, 'markpaid.done', { members: esc(r.members.join(', ')), amount: money(r.amount, L) }));
    const G = groupLang(ctx);
    await postToGroup(ctx, t(G, 'group.paid', { members: esc(r.members.join(', ')), amount: money(r.amount, G) }));
  } else if (st.kind === 'assign') {
    const r = await api(ctx, `/payments/${st.data.payment_id}/assign`, { method: 'POST', body: { member_ids: ids } });
    delState(key);
    await ctx.answerCallbackQuery();
    await edit(ctx, t(L, 'assign.done', { members: esc(r.members.join(', ')) }));
    const G = groupLang(ctx);
    await postToGroup(ctx, t(G, 'group.paid', { members: esc(r.members.join(', ')), amount: money(r.amount, G) }));
  }
}

async function onPicker(ctx) {
  const [, action, arg] = ctx.match;
  const key = pickKey(ctx, ctx.callbackQuery.message.message_id);
  const st = getState(key);
  if (!st) return ctx.answerCallbackQuery({ text: t(ctx.lang, 'err.menu_expired'), show_alert: true });
  if (action === 't') {
    const o = st.options[Number(arg)];
    if (o) st.selected = st.selected.includes(o.id) ? st.selected.filter((x) => x !== o.id) : [...st.selected, o.id];
  } else if (action === 'a') {
    st.selected = st.selected.length === st.options.length ? [] : st.options.map((o) => o.id);
  } else if (action === 'r') {
    const unpaid = await api(ctx, '/unpaid');
    if (!unpaid.length) {
      delState(key);
      await ctx.answerCallbackQuery();
      return edit(ctx, t(ctx.lang, 'pay.none'));
    }
    st.options = unpaidOptions(ctx.lang, unpaid);
    st.selected = st.selected.filter((id) => st.options.some((o) => o.id === id));
  } else if (action === 'x') {
    delState(key);
    await ctx.answerCallbackQuery();
    return edit(ctx, t(ctx.lang, 'cancelled'));
  } else if (action === 'ok') {
    return onPickerConfirm(ctx, st, key);
  }
  setState(key, st);
  await ctx.answerCallbackQuery();
  await edit(ctx, pickerText(ctx.lang, st), pickerKeyboard(ctx.lang, st));
}

// ---- members ---------------------------------------------------------------------------------

async function membersList(ctx, editInPlace = false) {
  const L = ctx.lang;
  const members = await api(ctx, '/members');
  if (!members.length) return editInPlace ? edit(ctx, t(L, 'members.empty')) : ctx.reply(t(L, 'members.empty'), HTML);
  const kb = new InlineKeyboard();
  members.forEach((m, i) => {
    const icon = m.status === 'paused' ? '⏸ ' : '';
    kb.text(`${icon}${m.name}${m.shirt_number ? ` #${m.shirt_number}` : ''}`, `m:v:${m.id}`);
    if (i % 2 === 1) kb.row();
  });
  const text = t(L, 'members.title', { n: members.length });
  return editInPlace ? edit(ctx, text, kb) : ctx.reply(text, { ...HTML, reply_markup: kb });
}

function memberView(L, m) {
  const dash = (v) => (v ? esc(v) : '—');
  const text = t(L, 'member.detail', {
    name: esc(m.name), nickname: dash(m.nickname), shirt: dash(m.shirt_number), phone: dash(m.phone), note: dash(m.note),
    fee: money(m.fee, L), custom: m.fee_override != null ? t(L, 'member.custom') : '',
    status: t(L, `status.${m.status}`), owes: money(m.owes, L),
  });
  const kb = new InlineKeyboard()
    .text(t(L, 'btn.name'), `m:e:${m.id}:name`).text(t(L, 'btn.nickname'), `m:e:${m.id}:nickname`).row()
    .text(t(L, 'btn.shirt_number'), `m:e:${m.id}:shirt_number`).text(t(L, 'btn.phone'), `m:e:${m.id}:phone`).row()
    .text(t(L, 'btn.note'), `m:e:${m.id}:note`).text(t(L, 'btn.fee'), `m:e:${m.id}:fee`).row()
    .text(m.status === 'paused' ? t(L, 'btn.activate') : t(L, 'btn.pause'),
      `m:s:${m.id}:${m.status === 'paused' ? 'active' : 'paused'}`)
    .text(t(L, 'btn.bills'), `m:b:${m.id}`).row()
    .text(t(L, 'btn.remove'), `m:rm:${m.id}`).text(t(L, 'btn.back'), 'm:l');
  return [text, kb];
}

async function billsView(ctx, memberId) {
  const L = ctx.lang;
  const { member, bills } = await api(ctx, `/members/${memberId}/bills`);
  const kb = new InlineKeyboard();
  for (const b of bills) kb.text(`✖️ ${billLabel(L, b)} · ${money(b.amount, L)}`, `b:w:${b.id}:${memberId}`).row();
  kb.text(t(L, 'btn.back'), `m:v:${memberId}`);
  const key = bills.length ? 'bills.title' : 'bills.none';
  return edit(ctx, t(L, key, { name: esc(member.name) }), kb);
}

async function onMemberCallback(ctx) {
  if (!(await allow(ctx, 'manager'))) return;
  const [, action, id, arg] = ctx.match;
  const L = ctx.lang;
  if (action === 'l') {
    await ctx.answerCallbackQuery();
    return membersList(ctx, true);
  }
  if (action === 'v') {
    const [text, kb] = memberView(L, await api(ctx, `/members/${id}`));
    await ctx.answerCallbackQuery();
    return edit(ctx, text, kb);
  }
  if (action === 'e') {
    const m = await api(ctx, `/members/${id}`);
    setState(inputKey(ctx), { type: 'edit', memberId: Number(id), field: arg }, 10 * 60_000);
    await ctx.answerCallbackQuery();
    const prompt = arg === 'fee'
      ? t(L, 'member.ask_fee', { name: esc(m.name) })
      : t(L, 'member.ask', { field: t(L, `field.${arg}`), name: esc(m.name) });
    return ctx.reply(prompt, { ...HTML, reply_markup: { force_reply: true } });
  }
  if (action === 's') {
    const m = await api(ctx, `/members/${id}`, { method: 'PATCH', body: { status: arg } });
    const [text, kb] = memberView(L, m);
    await ctx.answerCallbackQuery({ text: t(L, 'member.updated') });
    return edit(ctx, text, kb);
  }
  if (action === 'b') {
    await ctx.answerCallbackQuery();
    return billsView(ctx, id);
  }
  if (action === 'rm') {
    const m = await api(ctx, `/members/${id}`);
    await ctx.answerCallbackQuery();
    return edit(ctx, t(L, 'member.confirm_remove', { name: esc(m.name) }),
      new InlineKeyboard().text(t(L, 'btn.yes_remove'), `m:rmy:${id}`).text(t(L, 'btn.cancel'), `m:v:${id}`));
  }
  if (action === 'rmy') {
    const m = await api(ctx, `/members/${id}`, { method: 'DELETE' });
    await ctx.answerCallbackQuery();
    return edit(ctx, t(L, 'member.removed', { name: esc(m.name) }),
      new InlineKeyboard().text(t(L, 'btn.back'), 'm:l'));
  }
  return ctx.answerCallbackQuery();
}

function parseMemberLines(text) {
  return text.split('\n').map((line) => line.split('|').map((s) => s.trim())).filter((p) => p[0])
    .map(([name, shirt, phone]) => ({ name: name.slice(0, 100), shirt_number: shirt || null, phone: phone || null }));
}

async function addMembers(ctx, text) {
  const members = parseMemberLines(text);
  if (!members.length) return ctx.reply(t(ctx.lang, 'add.prompt'), HTML);
  const created = await api(ctx, '/members', { method: 'POST', body: { members } });
  return ctx.reply(t(ctx.lang, 'add.done', { n: created.length, names: esc(created.map((m) => m.name).join(', ')) }), HTML);
}

async function onTextInput(ctx, next) {
  if (!isPrivate(ctx) || ctx.msg.text.startsWith('/')) return next();
  const key = inputKey(ctx);
  const st = getState(key);
  if (!st) return next();
  delState(key);
  if (!(await allow(ctx, 'manager'))) return;
  const text = ctx.msg.text.trim();
  if (st.type === 'add') return addMembers(ctx, text);
  if (st.type === 'edit') {
    let body;
    if (st.field === 'fee') {
      const fee = text === '-' ? null : text === '0' ? 0 : parseAmount(text);
      if (text !== '-' && fee == null) return ctx.reply(t(ctx.lang, 'err.invalid'), HTML);
      body = { fee_override: fee };
    } else {
      body = { [st.field]: text === '-' ? null : text };
    }
    const m = await api(ctx, `/members/${st.memberId}`, { method: 'PATCH', body });
    const [view, kb] = memberView(ctx.lang, m);
    return ctx.reply(`${t(ctx.lang, 'member.updated')}\n\n${view}`, { ...HTML, reply_markup: kb });
  }
  return next();
}

// ---- manager requests --------------------------------------------------------------------------

function requestCard(L, r) {
  const text = t(L, 'mr.card', {
    name: esc(r.name), username: r.username ? ` (@${esc(r.username)})` : '', id: r.tg_id, at: r.created_at,
  });
  const kb = new InlineKeyboard().text(t(L, 'btn.approve'), `mr:a:${r.id}`).text(t(L, 'btn.reject'), `mr:r:${r.id}`);
  return [text, kb];
}

async function onRequestManager(ctx) {
  if (!isPrivate(ctx)) return ctx.reply(t(ctx.lang, 'err.private_only'), HTML);
  const r = await api(ctx, '/manager-requests', { method: 'POST' });
  await ctx.reply(t(ctx.lang, 'mr.sent'), HTML);
  const ownerLang = await langFor(r.owner_tg_id, groupLang(ctx));
  const [text, kb] = requestCard(ownerLang, r);
  await ctx.api.sendMessage(r.owner_tg_id, text, { ...HTML, reply_markup: kb })
    .catch((e) => console.warn('owner notify failed:', e.description || e.message));
}

async function onDecideManager(ctx) {
  const [, action, id] = ctx.match;
  const r = await api(ctx, `/manager-requests/${id}/decide`, { method: 'POST', body: { approve: action === 'a' } });
  const L = ctx.lang;
  await ctx.answerCallbackQuery();
  const approved = r.status === 'approved';
  await edit(ctx, t(L, approved ? 'mr.approved_card' : 'mr.rejected_card', { name: esc(r.name) }));
  const userLang = await langFor(r.tg_id, groupLang(ctx));
  await ctx.api.sendMessage(r.tg_id, t(userLang, approved ? 'mr.approved_user' : 'mr.rejected_user',
    { team: esc(ctx.entry.team.name) }), HTML).catch(() => {});
  if (approved) {
    await setAdminCommands(ctx.api, r.tg_id, 'manager');
    await postToGroup(ctx, t(groupLang(ctx), 'group.new_manager', { name: esc(r.name) }));
  }
}

// ---- owner tools ---------------------------------------------------------------------------

async function managersView(ctx, editInPlace = false) {
  const L = ctx.lang;
  const admins = await api(ctx, '/admins');
  const lines = [t(L, 'managers.title')];
  const kb = new InlineKeyboard();
  for (const a of admins) {
    lines.push(t(L, a.role === 'owner' ? 'managers.owner' : 'managers.manager', { name: esc(a.name) }));
    if (a.role === 'manager') kb.text(t(L, 'managers.remove', { name: a.name }), `a:rm:${a.tg_id}`).row();
  }
  const text = lines.join('\n');
  return editInPlace ? edit(ctx, text, kb) : ctx.reply(text, { ...HTML, reply_markup: kb });
}

async function onAdminCallback(ctx) {
  if (!(await allow(ctx, 'owner'))) return;
  const [, action, tgId] = ctx.match;
  const L = ctx.lang;
  if (action === 'rm') {
    const r = await api(ctx, `/admins/${tgId}`, { method: 'DELETE' });
    await clearAdminCommands(ctx.api, Number(tgId));
    await ctx.answerCallbackQuery({ text: t(L, 'managers.removed', { name: r.name }).replace(/<[^>]+>/g, '') });
    const userLang = await langFor(Number(tgId), groupLang(ctx));
    await ctx.api.sendMessage(tgId, t(userLang, 'managers.removed_user', { team: esc(ctx.entry.team.name) }), HTML).catch(() => {});
    return managersView(ctx, true);
  }
  if (action === 'to') {
    const admins = await api(ctx, '/admins');
    const target = admins.find((a) => String(a.tg_id) === tgId);
    await ctx.answerCallbackQuery();
    return edit(ctx, t(L, 'to.confirm', { name: esc(target?.name || tgId) }),
      new InlineKeyboard().text(t(L, 'to.yes'), `a:toy:${tgId}`).text(t(L, 'btn.cancel'), 'a:x:0'));
  }
  if (action === 'toy') {
    const r = await api(ctx, '/transfer-owner', { method: 'POST', body: { tg_id: Number(tgId) } });
    await setAdminCommands(ctx.api, r.tg_id, 'owner');
    await setAdminCommands(ctx.api, r.previous_owner, 'manager');
    await ctx.answerCallbackQuery();
    await edit(ctx, t(L, 'to.done', { name: esc(r.name) }));
    const userLang = await langFor(r.tg_id, groupLang(ctx));
    await ctx.api.sendMessage(r.tg_id, t(userLang, 'to.done_user', { team: esc(ctx.entry.team.name) }), HTML).catch(() => {});
    return;
  }
  await ctx.answerCallbackQuery();
  return edit(ctx, t(L, 'cancelled'));
}

// ---- unmatched payments ---------------------------------------------------------------------

export function unmatchedCard(L, p) {
  const reason = t(L, `reason.${p.reason || 'no_code'}`, { needed: money(p.needed || 0, L) });
  const text = t(L, 'unmatched.card', {
    amount: money(p.amount, L), content: esc(p.content || '—'), at: p.at, reason,
  });
  const kb = new InlineKeyboard().text(t(L, 'btn.assign'), `u:a:${p.payment_id}`).row()
    .text(t(L, 'btn.dismiss'), `u:d:${p.payment_id}`);
  return [text, kb];
}

async function onUnmatchedCallback(ctx) {
  if (!(await allow(ctx, 'manager'))) return;
  const [, action, id] = ctx.match;
  const L = ctx.lang;
  if (action === 'd') {
    await api(ctx, `/payments/${id}/dismiss`, { method: 'POST' });
    await ctx.answerCallbackQuery();
    return edit(ctx, `${ctx.callbackQuery.message.text ? esc(ctx.callbackQuery.message.text) : ''}\n\n${t(L, 'dismiss.done')}`);
  }
  const list = await api(ctx, '/unmatched');
  const p = list.find((x) => String(x.payment_id) === id);
  if (!p) return ctx.answerCallbackQuery({ text: t(L, 'err.already_resolved'), show_alert: true });
  const unpaid = await api(ctx, '/unpaid');
  if (!unpaid.length) return ctx.answerCallbackQuery({ text: t(L, 'pick.markpaid_none'), show_alert: true });
  await ctx.answerCallbackQuery();
  return openPicker(ctx, {
    kind: 'assign', title: t(L, 'pick.assign', { amount: money(p.amount, L) }),
    options: unpaidOptions(L, unpaid), selected: [], data: { payment_id: p.payment_id },
  });
}

// ---- install ---------------------------------------------------------------------------------

export function installTeamHandlers(bot) {
  bot.use(async (ctx, next) => {
    ctx.lang = isPrivate(ctx) ? await langOf(ctx.from, ctx.entry.team.lang) : ctx.entry.team.lang;
    if (!isPrivate(ctx) && ctx.from) langOf(ctx.from, ctx.entry.team.lang).catch(() => {}); // keep names fresh
    return next();
  });

  // everyone
  const start = safe(async (ctx) => {
    if (!isPrivate(ctx)) {
      return ctx.reply(t(ctx.lang, 'btn.pay_now'), { reply_markup: payKeyboard(ctx.lang, ctx.me.username) });
    }
    return openPayMenu(ctx);
  });
  bot.command(['start', 'pay'], start);

  bot.command('status', safe(async (ctx) => {
    const s = await api(ctx, '/status');
    await ctx.reply(statusText(ctx.lang, s), { ...HTML, reply_markup: s.unpaid.length ? payKeyboard(ctx.lang, ctx.me.username) : undefined });
  }));

  bot.command('fund', safe(async (ctx) => {
    const f = await api(ctx, '/fund');
    const L = ctx.lang;
    let text = t(L, 'fund.text', { income: money(f.income, L), expense: money(f.expense, L), balance: money(f.balance, L) });
    if (f.unmatched && isPrivate(ctx) && ['owner', 'manager'].includes(await role(ctx))) {
      text += `\n\n${t(L, 'fund.unmatched', { n: f.unmatched })}`;
    }
    await ctx.reply(text, HTML);
  }));

  bot.command('history', safe(async (ctx) => {
    const L = ctx.lang;
    const items = await api(ctx, '/history');
    if (!items.length) return ctx.reply(t(L, 'history.empty'), HTML);
    const lines = items.map((i) => `${i.type === 'in' ? '➕' : '➖'} ${i.at} · <b>${money(i.amount, L)}</b> · ${esc(i.title)}${i.source === 'cash' ? ` (${t(L, 'history.cash')})` : ''}`);
    await ctx.reply(`${t(L, 'history.title')}\n${lines.join('\n')}`, HTML);
  }));

  bot.command('requestmanager', safe(onRequestManager));
  bot.callbackQuery(/^mr:(a|r):(\d+)$/, safe(onDecideManager));

  bot.command('lang', safe(async (ctx) => {
    await ctx.reply(t(ctx.lang, 'lang.choose'), {
      reply_markup: new InlineKeyboard().text('English', 'l:en').text('Tiếng Việt', 'l:vi'),
    });
  }));
  bot.callbackQuery(/^l:(en|vi)$/, safe(async (ctx) => {
    await setLang(ctx.from.id, ctx.match[1]);
    ctx.lang = ctx.match[1];
    await ctx.answerCallbackQuery();
    await edit(ctx, t(ctx.lang, 'lang.done'));
  }));

  bot.command('help', safe(async (ctx) => {
    const r = await role(ctx);
    let text = t(ctx.lang, 'help.everyone');
    if (r) text += t(ctx.lang, 'help.manager');
    if (r === 'owner') text += t(ctx.lang, 'help.owner');
    await ctx.reply(text, HTML);
  }));

  bot.callbackQuery(/^k:(t|a|ok|x|r)(?::(\d+))?$/, safe(onPicker));

  // managers
  bot.command('members', safe(async (ctx) => {
    if (await allow(ctx, 'manager')) await membersList(ctx);
  }));
  bot.callbackQuery(/^m:(l|v|e|s|b|rm|rmy)(?::(\d+))?(?::(\w+))?$/, safe(onMemberCallback));
  bot.callbackQuery(/^b:w:(\d+):(\d+)$/, safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    await api(ctx, `/bills/${ctx.match[1]}/waive`, { method: 'POST' });
    await ctx.answerCallbackQuery({ text: t(ctx.lang, 'bills.waived') });
    await billsView(ctx, ctx.match[2]);
  }));

  bot.command('add', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    if (ctx.match.trim()) return addMembers(ctx, ctx.match);
    setState(inputKey(ctx), { type: 'add' }, 10 * 60_000);
    await ctx.reply(t(ctx.lang, 'add.prompt'), { ...HTML, reply_markup: { force_reply: true } });
  }));

  bot.command('setfee', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const raw = ctx.match.trim();
    const fee = raw === '0' ? 0 : parseAmount(raw);
    if (fee == null) return ctx.reply(t(ctx.lang, 'usage.setfee'), HTML);
    const r = await api(ctx, '/settings', { method: 'PATCH', body: { fee } });
    await ctx.reply(t(ctx.lang, 'setfee.done', { fee: money(r.fee, ctx.lang) }), HTML);
  }));

  bot.command('setdue', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const day = Number(ctx.match.trim());
    if (!Number.isInteger(day) || day < 1 || day > 28) return ctx.reply(t(ctx.lang, 'usage.setdue'), HTML);
    const r = await api(ctx, '/settings', { method: 'PATCH', body: { due_day: day } });
    await ctx.reply(t(ctx.lang, 'setdue.done', { day: r.due_day }), HTML);
  }));

  const chargeCommand = (kind) => safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const [amount, title] = amountAndTitle(ctx.match);
    if (!amount || !title) return ctx.reply(t(ctx.lang, `usage.${kind}`), HTML);
    const members = (await api(ctx, '/members')).filter((m) => m.status === 'active');
    if (!members.length) return ctx.reply(t(ctx.lang, 'err.no_members'), HTML);
    await openPicker(ctx, {
      kind, title: t(ctx.lang, `pick.${kind}`, { title: esc(title), amount: money(amount, ctx.lang) }),
      options: members.map((m) => ({ id: m.id, label: m.name })), selected: members.map((m) => m.id),
      data: { title, amount },
    });
  });
  bot.command('charge', chargeCommand('charge'));
  bot.command('split', chargeCommand('split'));

  bot.command('markpaid', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const unpaid = await api(ctx, '/unpaid');
    if (!unpaid.length) return ctx.reply(t(ctx.lang, 'pick.markpaid_none'), HTML);
    await openPicker(ctx, { kind: 'markpaid', title: t(ctx.lang, 'pick.markpaid'), options: unpaidOptions(ctx.lang, unpaid), selected: [] });
  }));

  const expense = safe(async (ctx, args) => {
    if (!(await allow(ctx, 'manager'))) return;
    const [amount, title] = amountAndTitle(args);
    if (!amount || !title) return ctx.reply(t(ctx.lang, 'usage.expense'), HTML);
    const photo = ctx.msg.photo?.at(-1)?.file_id ?? null;
    const r = await api(ctx, '/expenses', { method: 'POST', body: { amount, title, photo_file_id: photo } });
    await ctx.reply(t(ctx.lang, 'expense.done', { balance: money(r.balance, ctx.lang) }), HTML);
    const G = groupLang(ctx);
    await postToGroup(ctx, t(G, 'group.expense', {
      amount: money(r.amount, G), title: esc(r.title), by: esc(r.by), balance: money(r.balance, G),
    }), { photo });
  });
  bot.command('expense', (ctx) => expense(ctx, ctx.match));
  // a receipt photo with "/expense 500000 Pitch" as its caption (grammY doesn't match commands in captions)
  bot.on('message:photo', (ctx, next) => {
    const m = (ctx.msg.caption || '').match(/^\/expense(?:@\w+)?(?:\s+([\s\S]*))?$/);
    return m ? expense(ctx, m[1] || '') : next();
  });

  bot.command('remind', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager', { privateOnly: false }))) return;
    const G = groupLang(ctx);
    const s = await api(ctx, '/status');
    const text = statusText(G, s);
    const extra = { reply_markup: payKeyboard(G, ctx.me.username) };
    if (ctx.entry.team.group_chat_id && ctx.chat.id !== ctx.entry.team.group_chat_id) {
      await postToGroup(ctx, text, extra);
      await ctx.reply('✅', HTML);
    } else {
      await ctx.reply(text, { ...HTML, ...extra });
    }
  }));

  bot.command('unmatched', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const list = await api(ctx, '/unmatched');
    if (!list.length) return ctx.reply(t(ctx.lang, 'unmatched.none'), HTML);
    for (const p of list) {
      const [text, kb] = unmatchedCard(ctx.lang, p);
      await ctx.reply(text, { ...HTML, reply_markup: kb });
    }
  }));
  bot.callbackQuery(/^u:(a|d):(\d+)$/, safe(onUnmatchedCallback));

  bot.command('export', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const file = await api(ctx, '/export', { raw: true });
    const name = `${ctx.entry.team.name.replace(/[^\p{L}\p{N}]+/gu, '_')}_${new Date().toISOString().slice(0, 10)}.xlsx`;
    await ctx.replyWithDocument(new InputFile(file, name), { caption: t(ctx.lang, 'export.caption', { team: esc(ctx.entry.team.name) }), ...HTML });
  }));

  bot.command('settings', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const L = ctx.lang;
    const s = await api(ctx, '');
    const none = t(L, 'settings.not_set');
    await ctx.reply(t(L, 'settings.text', {
      team: esc(s.name), fee: s.fee ? money(s.fee, L) : none, due: s.due_day,
      bank: s.bank_account ? `${esc(s.bank_name)} · ${esc(s.bank_account)} · ${esc(s.bank_holder)}` : none,
      sepay: s.sepay_connected ? '✅' : none, group: s.group_chat_id ? t(L, 'settings.linked') : none,
      lang: s.lang === 'vi' ? 'Tiếng Việt' : 'English',
    }), HTML);
  }));

  bot.command('teamlang', safe(async (ctx) => {
    if (!(await allow(ctx, 'manager'))) return;
    const lang = ctx.match.trim().toLowerCase();
    if (!['en', 'vi'].includes(lang)) return ctx.reply(t(ctx.lang, 'usage.teamlang'), HTML);
    await api(ctx, '/settings', { method: 'PATCH', body: { lang } });
    ctx.entry.team.lang = lang;
    await ctx.reply(t(lang, 'teamlang.done'), HTML);
  }));

  bot.command('linkgroup', safe(async (ctx) => {
    if (isPrivate(ctx)) return ctx.reply(t(ctx.lang, 'err.group_only'), HTML);
    if (!(await allow(ctx, 'manager', { privateOnly: false }))) return;
    await api(ctx, '/group', { method: 'PUT', body: { chat_id: ctx.chat.id } });
    ctx.entry.team.group_chat_id = ctx.chat.id;
    await ctx.reply(t(groupLang(ctx), 'group.linked', { team: esc(ctx.entry.team.name) }), HTML);
  }));

  // owner
  bot.command('setbank', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const m = ctx.match.trim().match(/^(\S+)\s+(\S+)\s+(.+)$/);
    if (!m) return ctx.reply(t(ctx.lang, 'usage.setbank'), HTML);
    const r = await api(ctx, '/bank', { method: 'PUT', body: { bank_name: m[1], bank_account: m[2], bank_holder: m[3] } });
    await ctx.reply(t(ctx.lang, 'setbank.done'), HTML);
    const G = groupLang(ctx);
    await postToGroup(ctx, t(G, 'group.bank_changed', {
      by: esc(r.by), bank: esc(r.bank_name), account: esc(r.bank_account), holder: esc(r.bank_holder),
    }));
  }));

  bot.command('sepay', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const s = await api(ctx, '/sepay');
    await ctx.reply(t(ctx.lang, 'sepay.info', {
      url: esc(s.webhook_url), status: t(ctx.lang, s.connected ? 'sepay.connected' : 'sepay.not_connected'),
    }), { ...HTML, reply_markup: new InlineKeyboard().text(t(ctx.lang, 'btn.gen_key'), 's:gen') });
  }));
  bot.callbackQuery('s:gen', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const r = await api(ctx, '/sepay-key', { method: 'POST' });
    await ctx.answerCallbackQuery();
    await ctx.reply(t(ctx.lang, 'sepay.key', { key: esc(r.api_key) }), HTML);
  }));

  bot.command('requests', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const list = await api(ctx, '/manager-requests');
    if (!list.length) return ctx.reply(t(ctx.lang, 'mr.none'), HTML);
    for (const r of list) {
      const [text, kb] = requestCard(ctx.lang, r);
      await ctx.reply(text, { ...HTML, reply_markup: kb });
    }
  }));

  bot.command('managers', safe(async (ctx) => {
    if (await allow(ctx, 'owner')) await managersView(ctx);
  }));
  bot.command('transferowner', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const managers = (await api(ctx, '/admins')).filter((a) => a.role === 'manager');
    if (!managers.length) return ctx.reply(t(ctx.lang, 'to.none'), HTML);
    const kb = new InlineKeyboard();
    for (const a of managers) kb.text(a.name, `a:to:${a.tg_id}`).row();
    await ctx.reply(t(ctx.lang, 'to.choose'), { ...HTML, reply_markup: kb });
  }));
  bot.callbackQuery(/^a:(rm|to|toy|x):(\d+)$/, safe(onAdminCallback));

  bot.command('log', safe(async (ctx) => {
    if (!(await allow(ctx, 'owner'))) return;
    const rows = await api(ctx, '/audit');
    if (!rows.length) return ctx.reply(t(ctx.lang, 'log.empty'), HTML);
    const lines = rows.map((r) => `${r.at} · ${esc(r.actor)} · <code>${esc(r.action)}</code>${r.target ? ` ${esc(r.target)}` : ''}`);
    await ctx.reply(`${t(ctx.lang, 'log.title')}\n${lines.join('\n')}`, HTML);
  }));

  // text replies to "send me ..." prompts
  bot.on('message:text', safe(onTextInput));

  // the bot was added to a group
  bot.on('my_chat_member', safe(async (ctx) => {
    const { chat, new_chat_member: m } = ctx.myChatMember;
    if (chat.type !== 'private' && ['member', 'administrator'].includes(m.status)) {
      await ctx.api.sendMessage(chat.id, t(groupLang(ctx), 'group.added'), HTML);
    }
  }));

  bot.callbackQuery(/.*/, (ctx) => ctx.answerCallbackQuery()); // stale buttons
  bot.catch((err) => console.error(`team bot ${err.ctx?.me?.username}:`, err.error));
}

export const _test = { parseMemberLines, fullName };
