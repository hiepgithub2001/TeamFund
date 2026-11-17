// Message builders shared by commands and core events.
import { InlineKeyboard } from 'grammy';
import { CoreError } from './core.js';
import { esc, money } from './format.js';
import { dicts, t } from './i18n.js';

export const HTML = { parse_mode: 'HTML', link_preview_options: { is_disabled: true } };

export function payKeyboard(lang, botUsername) {
  return new InlineKeyboard().url(t(lang, 'btn.pay_now'), `https://t.me/${botUsername}?start=pay`);
}

export function owesLines(lang, rows) {
  return rows.map((u) => t(lang, 'line.owes', { name: esc(u.name), amount: money(u.total, lang) })).join('\n');
}

export function statusText(lang, s) {
  if (!s.fee) return t(lang, 'status.no_fee');
  const parts = [t(lang, 'status.title', { period: s.period_label, fee: money(s.fee, lang), due: s.due_day })];
  if (s.paid.length) parts.push(t(lang, 'status.paid', { n: s.paid.length, names: s.paid.map(esc).join(', ') }));
  if (s.unpaid.length) {
    parts.push(`${t(lang, 'status.unpaid', { n: s.unpaid.length, total: money(s.total_unpaid, lang) })}\n${owesLines(lang, s.unpaid)}`);
  } else {
    parts.push(t(lang, 'status.all_paid'));
  }
  return parts.join('\n\n');
}

export function billLabel(lang, b) {
  return b.kind === 'monthly' ? t(lang, 'bill.monthly', { period: b.period_label }) : `${b.title} (${b.period_label})`;
}

export function errorText(lang, err) {
  if (err instanceof CoreError) {
    const key = `err.${err.code}`;
    if (dicts.en[key]) return t(lang, key, typeof err.detail === 'object' && err.detail ? err.detail : {});
  }
  return t(lang, 'err.generic');
}

/** Send an error back to whoever triggered the update. */
export async function replyError(ctx, err) {
  if (!(err instanceof CoreError)) console.error(err);
  const text = errorText(ctx.lang, err);
  try {
    if (ctx.callbackQuery) await ctx.answerCallbackQuery({ text: text.replace(/<[^>]+>/g, ''), show_alert: true });
    else await ctx.reply(text, HTML);
  } catch (e) {
    console.error('could not report error', e.description || e.message);
  }
}

/** Run a handler and report failures in the user's language. */
export const safe = (fn) => async (ctx, ...rest) => {
  try {
    await fn(ctx, ...rest);
  } catch (err) {
    await replyError(ctx, err);
  }
};

/** Edit a message, ignoring Telegram's "message is not modified". */
export async function edit(ctx, text, reply_markup) {
  try {
    await ctx.editMessageText(text, { ...HTML, reply_markup });
  } catch (e) {
    if (!String(e.description || '').includes('not modified')) throw e;
  }
}

export const isPrivate = (ctx) => ctx.chat?.type === 'private';
