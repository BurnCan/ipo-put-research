"""Narrow, idempotent prototype schema upgrades."""
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def upgrade_milestone_2(engine: Engine) -> list[str]:
    """Add IPO classification columns without deleting existing data."""
    existing = {column["name"] for column in inspect(engine).get_columns("ipos")}
    definitions = {
        "candidate_type": "VARCHAR(32) NOT NULL DEFAULT 'unknown'",
        "classification_status": "VARCHAR(32) NOT NULL DEFAULT 'unclassified'",
        "offering_status": "VARCHAR(32) NOT NULL DEFAULT 'filed'",
        "classification_reason": "TEXT NULL",
        "final_prospectus_filing_id": "INTEGER NULL REFERENCES filings(id) ON DELETE SET NULL",
    }
    changed = []
    with engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE ipos ADD COLUMN {name} {definition}"))
                changed.append(name)
        if engine.dialect.name == "postgresql":
            connection.execute(text("CREATE INDEX IF NOT EXISTS ix_ipos_final_prospectus_filing_id ON ipos (final_prospectus_filing_id)"))
    return changed


def upgrade_milestone_3(engine: Engine) -> list[str]:
    """Add offering columns and provenance tables without replacing existing data."""
    from app.models import FilingDocument, IPOFact
    inspector = inspect(engine)
    changed: list[str] = []
    existing = {column["name"] for column in inspector.get_columns("ipos")}
    definitions = {
        "primary_shares": "NUMERIC(18, 2) NULL",
        "secondary_shares": "NUMERIC(18, 2) NULL",
        "shares_outstanding_post_ipo": "NUMERIC(18, 2) NULL",
    }
    with engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE ipos ADD COLUMN {name} {definition}")); changed.append(name)
    table_names = set(inspect(engine).get_table_names())
    for table in (FilingDocument.__table__, IPOFact.__table__):
        if table.name not in table_names:
            table.create(engine, checkfirst=True); changed.append(table.name)
    return changed


def upgrade_schema(engine: Engine) -> list[str]:
    return (upgrade_milestone_2(engine) + upgrade_milestone_3(engine) + upgrade_milestone_4(engine)
            + upgrade_milestone_5(engine) + upgrade_milestone_6(engine) + upgrade_milestone_8(engine))


def upgrade_milestone_8(engine: Engine) -> list[str]:
    """Create or narrowly extend the append-only prospective validation table."""
    from app.models import LockupProspectiveSignal
    table = LockupProspectiveSignal.__tablename__
    if table not in set(inspect(engine).get_table_names()):
        LockupProspectiveSignal.__table__.create(engine, checkfirst=True)
        return [table]
    existing = {column["name"] for column in inspect(engine).get_columns(table)}
    if "unavailable_reason" in existing:
        return []
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE lockup_prospective_signals "
            "ADD COLUMN unavailable_reason VARCHAR(64) NULL"))
    return ["lockup_prospective_signals.unavailable_reason"]


def upgrade_milestone_6(engine: Engine) -> list[str]:
    """Create recomputable lockup snapshot and outcome tables safely."""
    from app.models import LockupEventAnalysis, LockupSignalSnapshot
    changed = []
    existing = set(inspect(engine).get_table_names())
    for table in (LockupSignalSnapshot.__table__, LockupEventAnalysis.__table__):
        if table.name not in existing:
            table.create(engine, checkfirst=True)
            changed.append(table.name)
    return changed


def upgrade_milestone_5(engine: Engine) -> list[str]:
    """Create normalized market identity, observations, and derived summaries."""
    from app.models import DailyPrice, IPOMarketSummary, Security
    changed = []
    existing = set(inspect(engine).get_table_names())
    for table in (Security.__table__, DailyPrice.__table__, IPOMarketSummary.__table__):
        if table.name not in existing:
            table.create(engine, checkfirst=True)
            changed.append(table.name)
    return changed


def upgrade_milestone_4(engine: Engine) -> list[str]:
    """Create agreement provenance and add nullable canonical pointers safely."""
    from app.models import IPOLockup
    changed: list[str] = []
    if "ipo_lockups" not in set(inspect(engine).get_table_names()):
        IPOLockup.__table__.create(engine, checkfirst=True)
        changed.append("ipo_lockups")
    existing = {column["name"] for column in inspect(engine).get_columns("ipos")}
    definitions = {
        "primary_lockup_id": "INTEGER NULL REFERENCES ipo_lockups(id) ON DELETE SET NULL",
        "primary_lockup_expiration_date": "DATE NULL",
    }
    with engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE ipos ADD COLUMN {name} {definition}"))
                changed.append(name)
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_ipos_primary_lockup_id ON ipos (primary_lockup_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_ipos_primary_lockup_expiration_date ON ipos (primary_lockup_expiration_date)"))
    return changed
