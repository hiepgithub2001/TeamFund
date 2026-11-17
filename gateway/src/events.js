// Turns events from the core (payments, scheduler jobs) into Telegram messages.
import { esc, money } from './format.js';
import { t } from './i18n.js';
import { langFor } from './lang.js';
import { entryByTeam } from './registry.js';
import { HTML, owesLines, payKeyboard } from './render.js';
import { unmatchedCard } from './team.js';

async function send(api, chatId, text, extra = {}) {
  try {
    await api.sendMessage(chatId, text, { ...HTML, ...extra });
  } catch (e) {
    console.warn(`send to ${chatId} failed: ${e.description || e.message}`);
  }
}

export async function handleEvent(ev) {
  const entry = entryByTeam(ev.team_id);
  if (!entry) throw new Error(`team ${ev.team_id} is not loaded`);
  const { api } = entry.bot;
  const G = entry.team.lang;
  const group = ev.group_chat_id ?? entry.team.group_chat_id;
  const bot = entry.bot.botInfo.username;

  switch (ev.type) {
    case 'payment_confirmed': {
      const members = esc(ev.members.join(', '));
      if (ev.payer_tg_id) {
        const L = await langFor(ev.payer_tg_id, G);
        await send(api, ev.payer_tg_id, t(L, 'pay.receipt', { amount: money(ev.amount, L), members }));
      }
      if (group) await send(api, group, t(G, 'group.paid', { members, amount: money(ev.amount, G) }));
      break;
    }
    case 'unmatched_payment':
      for (const adminId of ev.admins) {
        const L = await langFor(adminId, G);
        const [text, kb] = unmatchedCard(L, ev);
        await send(api, adminId, text, { reply_markup: kb });
      }
      break;
    case 'bills_created':
      if (group) {
        await send(api, group, t(G, 'group.bills_created', { period: ev.period_label, fee: money(ev.fee, G), due: ev.due_day }),
          { reply_markup: payKeyboard(G, bot) });
      }
      break;
    case 'reminder':
      if (group) {
        const head = t(G, ev.overdue ? 'group.overdue' : 'group.reminder', { period: ev.period_label, due: ev.due_day });
        await send(api, group, `${head}\n${owesLines(G, ev.unpaid)}`, { reply_markup: payKeyboard(G, bot) });
      }
      break;
    case 'month_report':
      if (group) {
        const head = t(G, 'group.report', {
          period: ev.period_label, collected: money(ev.collected, G), spent: money(ev.spent, G),
          balance: money(ev.balance, G), paid: ev.paid_count,
        });
        const tail = ev.unpaid.length ? `${t(G, 'group.report_unpaid')}\n${owesLines(G, ev.unpaid)}` : t(G, 'group.report_all_paid');
        await send(api, group, `${head}\n\n${tail}`, ev.unpaid.length ? { reply_markup: payKeyboard(G, bot) } : {});
      }
      break;
    default:
      console.warn('unknown event', ev.type);
  }
}
