# Team Fund

Telegram bots that collect a football team's monthly fee and confirm payments automatically through SePay.

- A **master bot** registers teams. Anyone can ask with `/requestteam`; a system admin approves.
- Each team gets its **own bot**. Members open it, tap the names they're paying for, and get a SePay QR code with a unique transfer code. SePay's webhook confirms the payment, the payer gets a receipt and the team group gets a post.
- Members **without Telegram** use the team's **pay page** (a small web app at a secret link a manager gets with `/weblink`): pay by QR like `/start`, and read this month's status, the fund and its history, and the roster (no phone numbers). Read-only otherwise.
- The **owner** sets the bank account and SePay key and approves managers. **Managers** handle members, fees, charges, cash payments, expenses and reports.

## Architecture

Diagrams and the full design are in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (open [docs/architecture.html](docs/architecture.html) in a browser for the interactive page). Rebuild them after changing the page with `python3 scripts/build-docs.py docs/architecture.html`.

One Python server, organised as Clean Architecture. Every source-code dependency points inward, and `server/tests/test_architecture.py` fails if one doesn't.

```
server/app/
  domain/          entities (plain dataclasses) and pure business rules      imports nothing
  application/     use cases + ports (interfaces they need)                  imports domain
  infrastructure/  adapters out: SQLAlchemy repos and unit of work,          implements ports
                   Fernet, SePay QR URL, openpyxl, clock
  interfaces/      adapters in: Telegram bots, HTTP webhooks, scheduler      calls use cases
  main.py          composition root: builds the adapters and wires them in
```

- The domain dataclasses are mapped onto tables from `infrastructure/orm.py` (SQLAlchemy imperative mapping), so the domain never imports SQLAlchemy.
- Use cases get everything through `Deps` (unit of work, clock, cipher, QR, spreadsheet, notifier, config) and run in worker threads; bots and webhooks call them via `interfaces/deps.py`.
- SePay's JSON is translated to an `IncomingTransfer` at the edge (`interfaces/http/sepay.py`); the use case knows nothing about SePay's format.

| Container | Image | Job |
|---|---|---|
| `app` | `./server` (Python 3.12, FastAPI, aiogram) | Everything above. Runs with one worker, because the bots and jobs live in this process. |
| `db` | `postgres:16-alpine` | Storage |
| `caddy` | `caddy:2.8.4` | HTTPS on 443; `/tg/*`, `/sepay/*`, `/p/*` (pay pages), `/health` → app. Config in `caddy/Caddyfile`; Caddy reloads it by itself when it changes. |

## Run

```bash
scripts/init-env.sh <MASTER_BOT_TOKEN> <YOUR_TELEGRAM_ID>   # writes .env with fresh secrets
docker compose up -d --build
docker compose logs -f app
```

To update later: `scripts/deploy.sh` (pulls `main`, rebuilds, restarts, checks `/health`).
To roll back: `scripts/deploy.sh <commit-sha>`.

Port 443 must be reachable from the internet; Telegram and SePay both call it. Port 80 isn't needed (nginx keeps it).

## Setting up a team

1. Send `/requestteam` to the master bot and wait for approval.
2. In @BotFather: `/newbot`, copy the token.
3. Send `/newteam` to the master bot and paste the token.
4. In my.sepay.vn: link the team's bank account (and, for VA-only banks such as BIDV personal, create a VA), then create an API token (Cấu hình công ty → API Access).
5. In the new team bot: `/sepaytoken <token>`, then tap the team's account in the list. The bot saves it and creates the SePay webhook. The account can't be typed in; `/setbank` shows the list again to change it.
6. Add the bot to the team group and send `/linkgroup` there.
7. `/add` members (one per line, `Name | shirt | phone`), `/setfee 200000`, `/setdue 5`. Members on another amount or cycle (e.g. 300k per quarter): `/plans`. Money that isn't a fee (prizes, the old balance): `/income 2tr Thưởng 15-10-2026`.

## Schedule (Asia/Ho_Chi_Minh)

- Every hour: create this month's bills; announce them once in the group.
- 09:00: reminder in the group 3 days before the due day, on the due day, then every 3 days while someone owes.
- 20:00 on the last day of the month: report (collected, spent, balance, who still owes).

## Tests

```bash
pip install -r server/requirements.txt pytest
(cd server && python -m pytest -q tests)   # rules, use cases, dependency rule, bot helpers
python tests/e2e.py                         # the whole server against a fake Telegram API
```

Bot texts live in `server/app/interfaces/bot/locales/en.json` and `vi.json`; both files must have the same keys.

## Backups

`scripts/backup.sh` dumps the database to `~/backups` and keeps 14 days. Run it daily from cron:

```
0 3 * * * /home/lehiepvps/projects/teamfund/scripts/backup.sh >> /home/lehiepvps/backups/backup.log 2>&1
```
Also keep `.env` safe: `TOKEN_ENC_KEY` decrypts the stored team bot tokens.
