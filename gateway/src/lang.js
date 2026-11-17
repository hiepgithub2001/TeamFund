import { core } from './core.js';

// tg_id -> 'en' | 'vi' | null (null = no explicit choice yet)
const cache = new Map();
const seen = new Map(); // tg_id -> last upsert time, so names stay fresh without a call per message
const UPSERT_EVERY_MS = 6 * 60 * 60_000;

/** Language for someone who just sent us an update. Also keeps their name in the core up to date. */
export async function langOf(from, fallback) {
  if (!from) return fallback;
  const last = seen.get(from.id) || 0;
  if (Date.now() - last > UPSERT_EVERY_MS) {
    try {
      const u = await core('/users/upsert', {
        method: 'POST',
        body: { tg_id: from.id, first_name: from.first_name ?? null, last_name: from.last_name ?? null, username: from.username ?? null },
      });
      cache.set(from.id, u.lang);
      seen.set(from.id, Date.now());
    } catch {
      return cache.get(from.id) || fallback;
    }
  }
  return cache.get(from.id) || fallback;
}

/** Language for someone we are messaging first (an owner, a requester). */
export async function langFor(tgId, fallback) {
  if (cache.has(tgId)) return cache.get(tgId) || fallback;
  try {
    const u = await core(`/users/${tgId}`);
    cache.set(tgId, u.lang);
    return u.lang || fallback;
  } catch {
    return fallback;
  }
}

export async function setLang(tgId, lang) {
  await core(`/users/${tgId}/lang`, { method: 'PUT', body: { lang } });
  cache.set(tgId, lang);
}
