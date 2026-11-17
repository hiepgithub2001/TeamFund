import { cfg } from './config.js';

export class CoreError extends Error {
  constructor(status, detail) {
    const code = detail && typeof detail === 'object' && !Array.isArray(detail) && detail.code
      ? detail.code
      : status === 404 ? 'not_found' : status === 422 ? 'invalid' : 'generic';
    super(code);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

/** Call the Python core API. `actor` is the Telegram user the action is done for. */
export async function core(path, { method = 'GET', body, actor, raw = false } = {}) {
  const headers = { 'X-Internal-Key': cfg.internalKey };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (actor) headers['X-Actor-Id'] = String(actor);
  const res = await fetch(`${cfg.coreUrl}/api${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(30_000),
  });
  if (!res.ok) {
    let detail = null;
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    throw new CoreError(res.status, detail);
  }
  if (raw) return Buffer.from(await res.arrayBuffer());
  return res.json();
}
