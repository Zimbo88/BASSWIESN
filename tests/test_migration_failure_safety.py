"""Failed additive migrations must never claim the complete baseline exists."""

import pytest
from sqlalchemy import create_engine, text

from basswiesn.app.db import migrations

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("step", ["_ensure_device_state_columns", "_ensure_indexes",
                                  "_normalize_preset_source_aliases"])
def test_interrupted_migration_has_no_success_marker_and_can_resume(monkeypatch, step):
    engine = create_engine("sqlite:///:memory:")
    original = getattr(migrations, step)

    def fail(_bind):
        raise RuntimeError("injected migration failure")

    try:
        monkeypatch.setattr(migrations, step, fail)
        with pytest.raises(RuntimeError, match="injected"):
            migrations.ensure_schema_baseline(engine)
        with engine.connect() as connection:
            applied = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        assert "1.0.x-baseline" not in applied
        assert "1.1.0" not in applied
        monkeypatch.setattr(migrations, step, original)
        assert migrations.ensure_schema_baseline(engine).ready
        assert migrations.ensure_schema_baseline(engine).ready
    finally:
        engine.dispose()
