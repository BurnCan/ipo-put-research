from sqlalchemy import create_engine, inspect

from app.db import Base
from app.services.schema_upgrade import upgrade_market_data_backfill_attempts


def test_backfill_attempt_schema_upgrade_is_additive_and_idempotent():
    engine = create_engine('sqlite://')
    # Dependencies represent an already-populated pre-feature schema.
    Base.metadata.tables['companies'].create(engine)
    Base.metadata.tables['securities'].create(engine)
    assert upgrade_market_data_backfill_attempts(engine) == [
        'market_data_backfill_attempts']
    assert upgrade_market_data_backfill_attempts(engine) == []
    assert 'market_data_backfill_attempts' in inspect(engine).get_table_names()
