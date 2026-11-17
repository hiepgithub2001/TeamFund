const ids = (raw) => (raw || '').split(',').map((s) => s.trim()).filter(Boolean).map(Number);

export const cfg = {
  port: Number(process.env.PORT || 3000),
  coreUrl: (process.env.CORE_URL || 'http://core:8000').replace(/\/$/, ''),
  internalKey: process.env.INTERNAL_KEY || 'dev-internal-key',
  publicUrl: (process.env.PUBLIC_URL || '').replace(/\/$/, ''),
  masterToken: process.env.MASTER_BOT_TOKEN || '',
  masterSecret: process.env.MASTER_WEBHOOK_SECRET || 'change-me',
  adminIds: ids(process.env.MASTER_ADMIN_IDS),
  // webhook: Telegram pushes updates to PUBLIC_URL. polling: the gateway pulls them (no HTTPS needed).
  mode: process.env.TELEGRAM_MODE === 'polling' ? 'polling' : 'webhook',
  // Only for tests: point grammY at a fake Telegram server.
  apiRoot: process.env.TELEGRAM_API_ROOT || undefined,
  defaultLang: process.env.DEFAULT_LANG === 'en' ? 'en' : 'vi',
};

export const isSuper = (id) => cfg.adminIds.includes(Number(id));

export const botOptions = () => (cfg.apiRoot ? { client: { apiRoot: cfg.apiRoot } } : {});
