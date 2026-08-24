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
    definitions = {
        "evaluation_mode": "VARCHAR(24) NOT NULL DEFAULT 'strict_prospective'",
        "unavailable_reason": "VARCHAR(64) NULL",
        "required_observation_date": "DATE NULL",
        "calendar_id": "VARCHAR(16) NULL",
        "calendar_provider": "VARCHAR(32) NULL",
        "calendar_version": "VARCHAR(32) NULL",
    }
    changed = []
    with engine.begin() as connection:
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(text(
                    f"ALTER TABLE lockup_prospective_signals ADD COLUMN {name} {definition}"))
                changed.append(f"lockup_prospective_signals.{name}")
        # The old spelling denoted the same strict prospective population.
        # Migrate only when it cannot collide with an already locked strict
        # row; compatibility reads continue to cover such pre-existing edge
        # cases until their database is manually reconciled.
        migrated = connection.execute(text("""
            UPDATE lockup_prospective_signals AS legacy
               SET evaluation_mode = 'strict_prospective'
             WHERE evaluation_mode = 'prospective'
               AND NOT EXISTS (
                   SELECT 1 FROM lockup_prospective_signals AS strict_row
                    WHERE strict_row.hypothesis_id = legacy.hypothesis_id
                      AND strict_row.hypothesis_version = legacy.hypothesis_version
                      AND strict_row.lockup_id = legacy.lockup_id
                      AND strict_row.evaluation_mode = 'strict_prospective')
        """))
        if migrated.rowcount and migrated.rowcount > 0:
            changed.append("lockup_prospective_signals.evaluation_mode_values")
        if engine.dialect.name == "postgresql":
            connection.execute(text(
                "ALTER TABLE lockup_prospective_signals ALTER COLUMN evaluation_mode "
                "SET DEFAULT 'strict_prospective'"))
        constraint_names = {item.get("name") for item in
                            inspect(engine).get_unique_constraints(table)}
        if (engine.dialect.name == "postgresql" and
                "uq_prospective_hypothesis_lockup_mode" not in constraint_names):
            # Mode is part of evidence identity, allowing strict and shadow to
            # coexist without changing either locked record.
            connection.execute(text(
                "ALTER TABLE lockup_prospective_signals DROP CONSTRAINT IF EXISTS "
                "uq_prospective_hypothesis_lockup"))
            connection.execute(text(
                "ALTER TABLE lockup_prospective_signals ADD CONSTRAINT "
                "uq_prospective_hypothesis_lockup_mode UNIQUE "
                "(hypothesis_id, hypothesis_version, lockup_id, evaluation_mode)"))
    return changed


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
