// Command menus. Telegram shows each person the menu for their scope, so managers see manager commands
// and everyone else only sees the payment commands. The server still checks roles on every action.
import { core } from './core.js';
import { t } from './i18n.js';

const EVERYONE = ['start', 'status', 'fund', 'history', 'requestmanager', 'lang', 'help'];
const MANAGER = ['start', 'status', 'fund', 'history', 'members', 'add', 'setfee', 'setdue', 'charge', 'split',
  'markpaid', 'expense', 'remind', 'unmatched', 'export', 'settings', 'teamlang', 'lang', 'help'];
const OWNER = [...MANAGER, 'setbank', 'sepay', 'requests', 'managers', 'transferowner', 'log'];
const GROUP = ['status', 'fund', 'history', 'linkgroup', 'remind'];
const MASTER = ['start', 'requestteam', 'newteam', 'myteams', 'lang', 'help'];
const MASTER_ADMIN = [...MASTER, 'requests', 'teams'];

const LANGS = [['en', undefined], ['vi', 'vi']];
const list = (names, lang) => names.map((c) => ({ command: c, description: t(lang, `cmd.${c}`) }));

async function setFor(api, names, scope) {
  for (const [lang, code] of LANGS) {
    await api.setMyCommands(list(names, lang), { scope, ...(code ? { language_code: code } : {}) });
  }
}

export async function setTeamDefaultCommands(api) {
  await setFor(api, EVERYONE, { type: 'all_private_chats' });
  await setFor(api, GROUP, { type: 'all_group_chats' });
}

export async function setAdminCommands(api, chatId, role) {
  try {
    await setFor(api, role === 'owner' ? OWNER : MANAGER, { type: 'chat', chat_id: chatId });
  } catch (e) {
    // "chat not found" until the person has opened the bot; they get the menu on the next sync
    console.warn(`commands for ${chatId}: ${e.description || e.message}`);
  }
}

export async function clearAdminCommands(api, chatId) {
  for (const [, code] of LANGS) {
    try {
      await api.deleteMyCommands({ scope: { type: 'chat', chat_id: chatId }, ...(code ? { language_code: code } : {}) });
    } catch { /* nothing to clear */ }
  }
}

export async function syncAdminCommands(entry) {
  const admins = await core(`/teams/${entry.team.team_id}/admins`);
  for (const a of admins) await setAdminCommands(entry.bot.api, a.tg_id, a.role);
}

export async function setMasterCommands(api, adminIds) {
  await setFor(api, MASTER, { type: 'all_private_chats' });
  for (const id of adminIds) {
    try {
      await setFor(api, MASTER_ADMIN, { type: 'chat', chat_id: id });
    } catch (e) {
      console.warn(`master admin commands for ${id}: ${e.description || e.message}`);
    }
  }
}
