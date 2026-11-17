// Short-lived conversation state (open menus, "send me the name" prompts). Lost on restart, which is fine:
// the user just sends the command again.
const store = new Map();

export function setState(key, value, ttlMs = 30 * 60_000) {
  store.set(key, { value, exp: Date.now() + ttlMs });
}

export function getState(key) {
  const e = store.get(key);
  if (!e) return null;
  if (e.exp < Date.now()) {
    store.delete(key);
    return null;
  }
  return e.value;
}

export function delState(key) {
  store.delete(key);
}

setInterval(() => {
  const now = Date.now();
  for (const [k, e] of store) if (e.exp < now) store.delete(k);
}, 60_000).unref();
