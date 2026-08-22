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
    return upgrade_milestone_2(engine) + upgrade_milestone_3(engine)
