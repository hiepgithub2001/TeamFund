"""Database engine, schema and the SQLAlchemy unit of work."""
from datetime import date, datetime

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..application.ports import Conflict
from ..domain import entities as e
from . import repositories as r
from .orm import metadata, start_mappers


def make_session_factory(database_url: str):
    start_mappers()
    args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=args)
    if database_url.startswith("sqlite"):  # enforce foreign keys like PostgreSQL does (tests, local runs)
        event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
    metadata.create_all(engine)
    _add_missing_columns(engine)
    _copy_old_plan_members(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _copy_old_plan_members(engine) -> None:
    """Members used to hold one plan (members.plan_id/plan_from/plan_until). Move those into plan_members and
    clear the old columns, so this happens once (old undo records that touch them are then refused)."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO plan_members (team_id, plan_id, member_id, start, until, created_at) "
            "SELECT m.team_id, m.plan_id, m.id, m.plan_from, m.plan_until, m.created_at FROM members m "
            "JOIN fee_plans p ON p.id = m.plan_id AND p.team_id = m.team_id WHERE m.plan_id IS NOT NULL"))
        conn.execute(text("UPDATE members SET plan_id = NULL, plan_from = NULL, plan_until = NULL "
                          "WHERE plan_id IS NOT NULL OR plan_from IS NOT NULL OR plan_until IS NOT NULL"))


def _add_missing_columns(engine) -> None:
    """create_all makes new tables but never changes existing ones. New columns are always nullable,
    so adding them in place is enough; anything bigger would need a real migration."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have:
                    assert col.nullable, f"{table.name}.{col.name} must be nullable to add in place"
                    conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} '
                                      f'{col.type.compile(engine.dialect)}'))
                    if col.unique:  # ADD COLUMN can't carry the constraint; a unique index does the same job
                        conn.execute(text(f'CREATE UNIQUE INDEX IF NOT EXISTS uq_{table.name}_{col.name} '
                                          f'ON {table.name} ({col.name})'))
            # indexes declared later (index=True on a column, or Index(...)) that the table doesn't have yet
            existing = {i["name"] for i in insp.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in existing:
                    index.create(conn, checkfirst=True)


# what /undo can put back; the log, job marks, users and followers are never undone
_TRACKED = {c.__name__: c for c in (e.Team, e.TeamAdmin, e.Member, e.FeePlan, e.PlanMember, e.Bill, e.Payment, e.PaymentIntent,
                                    e.Expense, e.Match, e.MatchSchedule, e.Contact)}


def _plain(v):
    if isinstance(v, datetime):
        return {"$dt": v.isoformat()}
    if isinstance(v, date):
        return {"$d": v.isoformat()}
    return v


def _value(v):
    if isinstance(v, dict) and "$dt" in v:
        return datetime.fromisoformat(v["$dt"])
    if isinstance(v, dict) and "$d" in v:
        return date.fromisoformat(v["$d"])
    return v


def _row(obj) -> dict:
    return {a.key: _plain(getattr(obj, a.key)) for a in inspect(obj).mapper.column_attrs}


class SqlUnitOfWork:
    """Implements the UnitOfWork port with one SQLAlchemy session per business transaction."""

    def __init__(self, session_factory):
        self._factory = session_factory

    def __enter__(self):
        self.session = s = self._factory()
        self.users, self.team_requests, self.teams = r.SqlUsers(s), r.SqlTeamRequests(s), r.SqlTeams(s)
        self.admins, self.manager_requests, self.members = r.SqlAdmins(s), r.SqlManagerRequests(s), r.SqlMembers(s)
        self.bills, self.intents, self.payments = r.SqlBills(s), r.SqlIntents(s), r.SqlPayments(s)
        self.plans, self.followers, self.memberships = r.SqlFeePlans(s), r.SqlFollowers(s), r.SqlPlanMembers(s)
        self.expenses, self.audit, self.job_runs = r.SqlExpenses(s), r.SqlAudit(s), r.SqlJobRuns(s)
        self.matches, self.schedules, self.contacts = r.SqlMatches(s), r.SqlMatchSchedules(s), r.SqlContacts(s)
        self._changes: list = []
        event.listen(s, "after_flush", self._record)
        return self

    def _record(self, session, _ctx) -> None:
        """After each flush: what was inserted, changed (old and new values) or deleted."""
        for obj in session.new:
            if type(obj).__name__ in _TRACKED:
                self._changes.append({"op": "insert", "type": type(obj).__name__, "id": obj.id, "after": _row(obj)})
        for obj in session.dirty:
            if type(obj).__name__ not in _TRACKED or not session.is_modified(obj):
                continue
            fields = {}
            for a in inspect(obj).mapper.column_attrs:
                h = inspect(obj).attrs[a.key].history
                if h.added or h.deleted:
                    fields[a.key] = [_plain(h.deleted[0] if h.deleted else None), _plain(h.added[0] if h.added else None)]
            if fields:
                self._changes.append({"op": "update", "type": type(obj).__name__, "id": obj.id, "fields": fields})
        for obj in session.deleted:
            if type(obj).__name__ in _TRACKED:
                self._changes.append({"op": "delete", "type": type(obj).__name__, "id": obj.id, "before": _row(obj)})

    def changes(self) -> list:
        return list(self._changes)

    def revert(self, changes: list) -> None:
        """Undo recorded changes, newest first, but only if every row is still as the action left it."""
        s = self.session
        for c in reversed(changes):
            cls = _TRACKED[c["type"]]
            obj = s.get(cls, c["id"])
            if c["op"] == "insert":
                if obj is None or _row(obj) != c["after"]:
                    raise Conflict(f"{c['type']} {c['id']} changed")
                s.delete(obj)
            elif c["op"] == "update":
                if obj is None or any(_row(obj)[k] != new for k, (old, new) in c["fields"].items()):
                    raise Conflict(f"{c['type']} {c['id']} changed")
                for k, (old, _new) in c["fields"].items():
                    setattr(obj, k, _value(old))
            else:
                if obj is not None:
                    raise Conflict(f"{c['type']} {c['id']} exists again")
                s.add(cls(**{k: _value(v) for k, v in c["before"].items()}))
            self.flush()

    def __exit__(self, *exc):
        self.session.rollback()  # no-op after a commit
        self.session.close()

    def flush(self) -> None:
        try:
            self.session.flush()
        except IntegrityError as e:
            raise Conflict(str(e.orig)) from e

    def commit(self) -> None:
        try:
            self.session.commit()
            self._changes = []
        except IntegrityError as e:
            self.session.rollback()
            raise Conflict(str(e.orig)) from e

    def rollback(self) -> None:
        self.session.rollback()
