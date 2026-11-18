"""Operations for the system admin, in the master bot: /ops shows the whole system on one screen, and a
daily check at 08:00 messages the admins only when something needs them.

The database side comes from the ops use cases; this module reads what only the host and Telegram know:
disk, memory, the newest backup file, each bot's webhook, and the errors this process logged."""
import logging
import os
import shutil
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from ...config import settings
from ..deps import S
from . import registry
from .fmt import esc, money
from .i18n import t
from .lang import lang_for
from .run import call

log = logging.getLogger("ops")
STARTED = time.time()
DISK_LIMIT = 80          # % used
BACKUP_MAX_AGE = timedelta(hours=26)
WEBHOOK_BACKLOG = 20     # updates waiting at Telegram


class ErrorLog(logging.Handler):
    """Remembers the time and first line of every ERROR this process logs (the last 500)."""

    def __init__(self):
        super().__init__(logging.ERROR)
        self.items: deque = deque(maxlen=500)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.items.append((record.created, f"{record.name}: {record.getMessage()}".splitlines()[0][:200]))
        except Exception:
            pass

    def since(self, seconds: float) -> list[tuple[float, str]]:
        cut = time.time() - seconds
        return [x for x in self.items if x[0] >= cut]


errors = ErrorLog()


def install() -> None:
    """Count errors from every logger (called once at startup)."""
    if errors not in logging.getLogger().handlers:
        logging.getLogger().addHandler(errors)


# ---- what the host knows -------------------------------------------------------------------------

def disk() -> dict:
    u = shutil.disk_usage("/")
    return {"pct": round(u.used * 100 / u.total), "used_gb": u.used / 1e9, "total_gb": u.total / 1e9}


def memory() -> dict | None:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        return {"used_gb": (info["MemTotal"] - info["MemAvailable"]) / 1e9, "total_gb": info["MemTotal"] / 1e9}
    except Exception:
        return None


def newest_backup() -> dict | None:
    """The newest teamfund-*.sql.gz in BACKUP_DIR. None: not checked (no BACKUP_DIR). {"missing": True}: none."""
    folder = os.getenv("BACKUP_DIR")
    if not folder:
        return None
    try:
        files = [e for e in os.scandir(folder) if e.name.startswith("teamfund-") and e.name.endswith(".sql.gz")]
    except OSError:
        return {"missing": True}
    if not files:
        return {"missing": True}
    f = max(files, key=lambda e: e.stat().st_mtime)
    st = f.stat()
    return {"name": f.name, "age": timedelta(seconds=time.time() - st.st_mtime), "size_kb": st.st_size // 1024,
            "at": datetime.fromtimestamp(st.st_mtime, timezone.utc)}


async def webhooks() -> list[dict]:
    """Each bot as Telegram sees it: updates waiting, and the last delivery error (webhook mode only)."""
    bots = [(f"@{e.username}", e.bot) for e in registry.by_bot.values()]
    if registry.master["bot"] is not None:
        bots.insert(0, ("master", registry.master["bot"]))
    out = []
    for name, bot in bots:
        row = {"name": name, "ok": True, "pending": 0, "error": None, "error_at": None}
        if settings.telegram_mode == "webhook":
            try:
                info = await bot.get_webhook_info()
                row["pending"] = info.pending_update_count or 0
                if info.last_error_date and datetime.now(timezone.utc) - info.last_error_date < timedelta(hours=24):
                    row["error"], row["error_at"] = info.last_error_message or "?", info.last_error_date
                row["ok"] = bool(info.url) and row["pending"] <= WEBHOOK_BACKLOG and row["error"] is None
                if not info.url:
                    row["error"] = "no webhook"
            except Exception as e:
                row["ok"], row["error"] = False, str(e)[:120]
        out.append(row)
    return out


# ---- the report -----------------------------------------------------------------------------------

def _ago(lang: str, delta: timedelta) -> str:
    s = int(delta.total_seconds())
    if s < 3600:
        return t(lang, "ops.min", n=max(1, s // 60))
    if s < 2 * 86400:  # "30 h", never "1 days"
        return t(lang, "ops.hours", n=s // 3600)
    return t(lang, "ops.days", n=s // 86400)


def _local(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(settings.timezone)).strftime("%H:%M %d/%m")


async def collect(actor: int | None) -> dict:
    return {"db": await call(S.ops.db_ok), "teams": await call(S.ops.teams, actor), "disk": disk(),
            "mem": memory(), "backup": newest_backup(), "bots": await webhooks(),
            "errors": errors.since(86400), "uptime": timedelta(seconds=time.time() - STARTED)}


def problems(lang: str, r: dict) -> list[str]:
    """Everything that needs the admin, one line each. Empty: all fine."""
    out = []
    if not r["db"]:
        out.append(t(lang, "ops.p_db"))
    if r["disk"]["pct"] >= DISK_LIMIT:
        out.append(t(lang, "ops.p_disk", pct=r["disk"]["pct"]))
    b = r["backup"]
    if b is not None and b.get("missing"):
        out.append(t(lang, "ops.p_backup_none"))
    elif b is not None and b["age"] > BACKUP_MAX_AGE:
        out.append(t(lang, "ops.p_backup_old", ago=_ago(lang, b["age"])))
    for bot in r["bots"]:
        if not bot["ok"]:
            out.append(t(lang, "ops.p_bot", name=esc(bot["name"]), pending=bot["pending"],
                         error=esc(bot["error"] or "—")))
    if r["errors"]:
        out.append(t(lang, "ops.p_errors", n=len(r["errors"]), last=esc(r["errors"][-1][1])))
    for x in r["teams"]:
        if x["unmatched_stale"]:
            out.append(t(lang, "ops.p_unmatched", team=esc(x["name"]), n=x["unmatched"],
                         amount=money(x["unmatched_amount"], lang), days=x["unmatched_oldest_days"]))
        if x["sepay_quiet"]:
            out.append(t(lang, "ops.p_quiet", team=esc(x["name"]), n=x["qr_made_7d"]))
    return out


def report(lang: str, r: dict) -> str:
    issues = problems(lang, r)
    lines = [t(lang, "ops.title"),
             t(lang, "ops.ok") if not issues else t(lang, "ops.issues", n=len(issues)),
             *[f"⚠️ {p}" for p in issues], "",
             t(lang, "ops.server")]
    lines.append(t(lang, "ops.db_ok" if r["db"] else "ops.db_down"))
    lines.append(t(lang, "ops.uptime", ago=_ago(lang, r["uptime"])))
    d = r["disk"]
    lines.append(t(lang, "ops.disk", pct=d["pct"], used=f"{d['used_gb']:.0f}", total=f"{d['total_gb']:.0f}"))
    if r["mem"]:
        lines.append(t(lang, "ops.mem", used=f"{r['mem']['used_gb']:.1f}", total=f"{r['mem']['total_gb']:.1f}"))
    b = r["backup"]
    if b is None:
        lines.append(t(lang, "ops.backup_unchecked"))
    elif b.get("missing"):
        lines.append(t(lang, "ops.backup_none"))
    else:
        lines.append(t(lang, "ops.backup", at=_local(b["at"]), ago=_ago(lang, b["age"]), kb=b["size_kb"]))
    lines.append(t(lang, "ops.errors", n=len(r["errors"])))
    lines += ["", t(lang, "ops.bots")]
    for bot in r["bots"]:
        mark = "✅" if bot["ok"] else "⚠️"
        extra = t(lang, "ops.bot_pending", n=bot["pending"]) if bot["pending"] else ""
        if bot["error"]:
            extra += f" · {esc(bot['error'])}"
        lines.append(f"{mark} {esc(bot['name'])}{extra}")
    lines += ["", t(lang, "ops.teams")]
    for x in r["teams"]:
        last = (t(lang, "ops.last_transfer", at=x["last_transfer"], days=x["last_transfer_days"])
                if x["last_transfer"] else t(lang, "ops.no_transfer"))
        unmatched = (t(lang, "ops.unmatched", n=x["unmatched"], amount=money(x["unmatched_amount"], lang))
                     if x["unmatched"] else t(lang, "ops.unmatched_none"))
        bank = "" if x["has_bank"] else " · " + t(lang, "ops.no_bank")
        lines.append(t(lang, "ops.team", team=esc(x["name"]), bank=bank, last=last, unmatched=unmatched,
                       made=x["qr_made_7d"], paid=x["qr_paid_7d"], open=x["qr_open"]))
    return "\n".join(lines)


async def daily_check() -> None:
    """08:00 every day: tell the system admins what needs them. Silent when all is well."""
    try:
        r = await collect(None)
        for admin_id in settings.master_admin_ids:
            lang = await lang_for(admin_id, settings.default_lang)
            issues = problems(lang, r)
            if not issues or registry.master["bot"] is None:
                continue
            text = "\n".join([t(lang, "ops.daily", n=len(issues)), *[f"⚠️ {p}" for p in issues], "",
                              t(lang, "ops.daily_more")])
            try:
                await registry.master["bot"].send_message(admin_id, text)
            except Exception as e:
                log.warning("daily check to %s: %s", admin_id, e)
    except Exception:
        log.exception("daily check failed")
