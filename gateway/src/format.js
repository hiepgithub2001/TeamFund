export function esc(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

export function money(n, lang = 'vi') {
  return `${new Intl.NumberFormat(lang === 'vi' ? 'vi-VN' : 'en-US').format(n || 0)}đ`;
}

/** "200000", "200.000", "200,000", "200k", "1.5tr", "2m" -> integer VND, or null. */
export function parseAmount(raw) {
  if (!raw) return null;
  let s = String(raw).trim().toLowerCase().replace(/\s+/g, '').replace(/đ|vnd$/g, '');
  let mult = 1;
  const suffix = s.match(/(k|m|tr|trieu)$/);
  if (suffix) {
    mult = suffix[1] === 'k' ? 1_000 : 1_000_000;
    s = s.slice(0, -suffix[1].length).replace(',', '.');
    const n = Number(s);
    return Number.isFinite(n) && n > 0 ? Math.round(n * mult) : null;
  }
  s = s.replace(/[.,]/g, '');
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n > 0 ? n : null;
}

/** Split "500000 Pitch rental" into [amount, title]. */
export function amountAndTitle(text) {
  const m = String(text || '').trim().match(/^(\S+)\s+(.+)$/s);
  if (!m) return [null, null];
  return [parseAmount(m[1]), m[2].trim()];
}

export function fullName(from) {
  return [from?.first_name, from?.last_name].filter(Boolean).join(' ') || from?.username || String(from?.id ?? '');
}
