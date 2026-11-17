# Team Fund

Telegram bots that collect a football team's monthly fee and confirm payments automatically through SePay.

- A **master bot** registers teams. Anyone can ask with `/requestteam`; a system admin approves.
- Each team gets its **own bot**. Members open it, tap the names they're paying for, and get a SePay QR code with a unique transfer code. SePay's webhook confirms the payment, the payer gets a receipt and the team group gets a post.
- The **owner** sets the bank account and SePay key and approves managers. **Managers** handle members, fees, charges, cash payments, expenses and reports.

## Services

| Service | Stack | Job |
|---|---|---|
| `gateway` | Node.js, grammY | Every Telegram bot (master + teams), menus, EN/VI text |
| `core` | Python, FastAPI | Data, roles, billing, SePay webhook, Excel export |
| `scheduler` | Python, APScheduler | Monthly bills, group reminders, month-end report |
| `db` | PostgreSQL 16 | Storage (only `core` writes to it) |
| `caddy` | Caddy 2 | HTTPS on 443; `/tg/*` → gateway, `/sepay/*` → core |

## Run

```bash
scripts/init-env.sh <MASTER_BOT_TOKEN> <YOUR_TELEGRAM_ID>   # writes .env with fresh secrets
docker compose up -d --build
docker compose logs -f gateway core
```

To update later: `scripts/deploy.sh` (pulls `main`, rebuilds, restarts, checks `/health`).
To roll back: `scripts/deploy.sh <commit-sha>`.

Port 443 must be reachable from the internet; Telegram and SePay both call it. Port 80 isn't needed (nginx keeps it).

## Setting up a team

1. Send `/requestteam` to the master bot and wait for approval.
2. In @BotFather: `/newbot`, copy the token.
3. Send `/newteam` to the master bot and paste the token.
4. In the new team bot: `/setbank MBBank 0123456789 NGUYEN VAN A`, then `/sepay`, and paste the webhook URL and API key into SePay (event: incoming money).
5. Add the bot to the team group and send `/linkgroup` there.
6. `/add` members (one per line, `Name | shirt | phone`), `/setfee 200000`, `/setdue 5`.

## Schedule (Asia/Ho_Chi_Minh)

- Every hour: create this month's bills; announce them once in the group.
- 09:00: reminder in the group 3 days before the due day, on the due day, then every 3 days while someone owes.
- 20:00 on the last day of the month: report (collected, spent, balance, who still owes).

## Tests

```bash
cd core && pip install -r requirements.txt pytest && python -m pytest -q tests
cd gateway && npm ci
PYTHON=python3 node tests/e2e.mjs     # core + gateway against a fake Telegram API
```

## Backups

`scripts/backup.sh` dumps the database to `~/backups` and keeps 14 days. Run it daily from cron:

```
0 3 * * * /home/lehiepvps/projects/teamfund/scripts/backup.sh >> /home/lehiepvps/backups/backup.log 2>&1
```
Also keep `.env` safe: `TOKEN_ENC_KEY` decrypts the stored team bot tokens.
