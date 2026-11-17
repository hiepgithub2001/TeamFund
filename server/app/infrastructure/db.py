"""Database engine, schema and the SQLAlchemy unit of work."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..application.ports import Conflict
from . import repositories as r
from .orm import metadata, start_mappers


def make_session_factory(database_url: str):
    start_mappers()
    args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, pool_pre_ping=True, connect_args=args)
    metadata.create_all(engine)
    _add_missing_columns(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


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


class SqlUnitOfWork:
    """Implements the UnitOfWork port with one SQLAlchemy session per business transaction."""

    def __init__(self, session_factory):
        self._factory = session_factory

    def __enter__(self):
        self.session = s = self._factory()
        self.users, self.team_requests, self.teams = r.SqlUsers(s), r.SqlTeamRequests(s), r.SqlTeams(s)
        self.admins, self.manager_requests, self.members = r.SqlAdmins(s), r.SqlManagerRequests(s), r.SqlMembers(s)
        self.bills, self.intents, self.payments = r.SqlBills(s), r.SqlIntents(s), r.SqlPayments(s)
        self.plans = r.SqlFeePlans(s)
        self.expenses, self.audit, self.job_runs = r.SqlExpenses(s), r.SqlAudit(s), r.SqlJobRuns(s)
        return self

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
        except IntegrityError as e:
            self.session.rollback()
            raise Conflict(str(e.orig)) from e

    def rollback(self) -> None:
        self.session.rollback()
