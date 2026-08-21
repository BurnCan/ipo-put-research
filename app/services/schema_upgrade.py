"""Narrow, idempotent prototype schema upgrade for Milestone 2."""
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
