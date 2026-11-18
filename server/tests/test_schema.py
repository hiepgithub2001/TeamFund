"""The schema update at startup: new nullable columns and new indexes reach an existing database."""
import tempfile

from sqlalchemy import create_engine, inspect, text

from app.infrastructure.db import make_session_factory


def test_missing_column_and_index_are_added():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    make_session_factory(f"sqlite:///{db}")
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:  # an older database: bills without match_id or its index
        conn.execute(text("DROP INDEX ix_bills_match_id"))
        conn.execute(text("ALTER TABLE bills DROP COLUMN match_id"))
    assert "match_id" not in {c["name"] for c in inspect(engine).get_columns("bills")}
    make_session_factory(f"sqlite:///{db}")  # the app starts again
    insp = inspect(create_engine(f"sqlite:///{db}"))
    assert "match_id" in {c["name"] for c in insp.get_columns("bills")}
    assert "ix_bills_match_id" in {i["name"] for i in insp.get_indexes("bills")}
