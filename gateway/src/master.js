// The master bot: team registration requests, approval by the system admin, and /newteam.
import { Api, Bot, InlineKeyboard } from 'grammy';
import { botOptions, cfg, isSuper } from './config.js';
import { core } from './core.js';
import { esc } from './format.js';
import { t } from './i18n.js';
import { langFor, langOf, setLang } from './lang.js';
import { registerTeam } from './registry.js';
import { HTML, edit, isPrivate, safe } from './render.js';
import { delState, getState, setState } from './state.js';

const TOKEN_RE = /^\d{5,}:[A-Za-z0-9_-]{30,}$/;
const inputKey = (ctx) => `master:${ctx.chat.id}`;

function requestCard(L, r) {
  const text = t(L, 'm.request_card', {
    team: esc(r.team_name), name: esc(r.name), username: r.username ? ` (@${esc(r.username)})` : '',
    id: r.tg_id, at: r.created_at,
  });
  const kb = new InlineKeyboard().text(t(L, 'btn.approve'), `tr:a:${r.id}`).text(t(L, 'btn.reject'), `tr:r:${r.id}`);
  return [text, kb];
}

export function buildMasterBot(token) {
  const bot = new Bot(token, botOptions());

  bot.use(async (ctx, next) => {
    const fallback = ['en', 'vi'].includes(ctx.from?.language_code) ? ctx.from.language_code : cfg.defaultLang;
    ctx.lang = await langOf(ctx.from, fallback);
    return next();
  });
  // the master bot only works in private chats
  bot.use((ctx, next) => (ctx.chat && !isPrivate(ctx) ? undefined : next()));

  const welcome = safe(async (ctx) => {
    let text = t(ctx.lang, 'm.welcome');
    if (isSuper(ctx.from.id)) text += t(ctx.lang, 'm.admin');
    await ctx.reply(text, HTML);
  });
  bot.command(['start', 'help'], welcome);

  bot.command('requestteam', safe(async (ctx) => {
    setState(inputKey(ctx), { type: 'team_name' }, 10 * 60_000);
    await ctx.reply(t(ctx.lang, 'm.ask_team_name'), { ...HTML, reply_markup: { force_reply: true } });
  }));

  bot.command('newteam', safe(async (ctx) => {
    const r = await core('/team-requests/approved', { actor: ctx.from.id });
    if (!r.request && !r.is_super) return ctx.reply(t(ctx.lang, 'err.not_approved'), HTML);
    setState(inputKey(ctx), { type: 'token' }, 10 * 60_000);
    await ctx.reply(t(ctx.lang, 'm.ask_token'), HTML);
  }));

  bot.callbackQuery(/^tr:(a|r):(\d+)$/, safe(async (ctx) => {
    const r = await core(`/team-requests/${ctx.match[2]}/decide`, {
      method: 'POST', actor: ctx.from.id, body: { approve: ctx.match[1] === 'a' },
    });
    const approved = r.status === 'approved';
    await ctx.answerCallbackQuery();
    await edit(ctx, t(ctx.lang, approved ? 'm.approved_card' : 'm.rejected_card', { team: esc(r.team_name), name: esc(r.name) }));
    const L = await langFor(r.tg_id, cfg.defaultLang);
    await ctx.api.sendMessage(r.tg_id, t(L, approved ? 'm.approved_user' : 'm.rejected_user', { team: esc(r.team_name) }), HTML)
      .catch((e) => console.warn('requester notify failed:', e.description || e.message));
  }));

  bot.command('requests', safe(async (ctx) => {
    if (!isSuper(ctx.from.id)) return ctx.reply(t(ctx.lang, 'err.not_super'), HTML);
    const list = await core('/team-requests', { actor: ctx.from.id });
    if (!list.length) return ctx.reply(t(ctx.lang, 'm.requests_none'), HTML);
    for (const r of list) {
      const [text, kb] = requestCard(ctx.lang, r);
      await ctx.reply(text, { ...HTML, reply_markup: kb });
    }
  }));

  bot.command('teams', safe(async (ctx) => {
    if (!isSuper(ctx.from.id)) return ctx.reply(t(ctx.lang, 'err.not_super'), HTML);
    const teams = await core('/teams', { actor: ctx.from.id });
    const lines = teams.map((x) => t(ctx.lang, 'm.team_line', {
      name: esc(x.name), bot: x.bot_username, owner: esc(x.owner), members: x.members,
    }));
    await ctx.reply([t(ctx.lang, 'm.teams_title', { n: teams.length }), ...lines].join('\n'), HTML);
  }));

  bot.command('myteams', safe(async (ctx) => {
    const teams = await core('/teams/mine', { actor: ctx.from.id });
    if (!teams.length) return ctx.reply(t(ctx.lang, 'm.myteams_empty'), HTML);
    const lines = teams.map((x) => `• <b>${esc(x.name)}</b>: @${x.bot_username} (${t(ctx.lang, `role.${x.role}`)})`);
    await ctx.reply([t(ctx.lang, 'm.myteams_title'), ...lines].join('\n'), HTML);
  }));

  bot.command('lang', safe(async (ctx) => {
    await ctx.reply(t(ctx.lang, 'lang.choose'), {
      reply_markup: new InlineKeyboard().text('English', 'l:en').text('Tiếng Việt', 'l:vi'),
    });
  }));
  bot.callbackQuery(/^l:(en|vi)$/, safe(async (ctx) => {
    await setLang(ctx.from.id, ctx.match[1]);
    await ctx.answerCallbackQuery();
    await edit(ctx, t(ctx.lang = ctx.match[1], 'lang.done'));
  }));

  bot.on('message:text', safe(async (ctx, next) => {
    const text = ctx.msg.text.trim();
    // A pasted token is accepted even without a prompt, so it never sits in the chat.
    const st = getState(inputKey(ctx)) || (TOKEN_RE.test(text) ? { type: 'token' } : null);
    if (!st || text.startsWith('/')) return next();
    delState(inputKey(ctx));

    if (st.type === 'team_name') {
      const r = await core('/team-requests', { method: 'POST', actor: ctx.from.id, body: { team_name: text.slice(0, 100) } });
      await ctx.reply(t(ctx.lang, 'm.request_sent', { team: esc(r.team_name) }), HTML);
      for (const adminId of r.admin_ids) {
        const L = await langFor(adminId, cfg.defaultLang);
        const [card, kb] = requestCard(L, r);
        await ctx.api.sendMessage(adminId, card, { ...HTML, reply_markup: kb })
          .catch((e) => console.warn(`admin ${adminId} notify failed:`, e.description || e.message));
      }
      return;
    }

    if (st.type === 'token') {
      await ctx.deleteMessage().then(() => ctx.reply(t(ctx.lang, 'm.token_deleted'), HTML)).catch(() => {});
      if (!TOKEN_RE.test(text)) return ctx.reply(t(ctx.lang, 'm.bad_token'), HTML);
      let me;
      try {
        me = await new Api(text, botOptions().client).getMe();
      } catch {
        return ctx.reply(t(ctx.lang, 'm.token_rejected'), HTML);
      }
      const team = await core('/teams', {
        method: 'POST', actor: ctx.from.id,
        body: { token: text, bot_id: me.id, bot_username: me.username, name: me.first_name },
      });
      await registerTeam(team);
      await ctx.reply(t(ctx.lang, 'm.team_created', { team: esc(team.name), bot: team.bot_username }), HTML);
    }
  }));

  bot.callbackQuery(/.*/, (ctx) => ctx.answerCallbackQuery());
  bot.catch((err) => console.error('master bot:', err.error));
  return bot;
}
