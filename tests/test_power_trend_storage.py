import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from db.models import AlliancePowerSnapshot, AlliancePowerTracking


def test_power_storage_migration_round_trip_and_daily_uniqueness(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "alembic/versions/20260905120000_add_alliance_power_tracking.py"
    spec = importlib.util.spec_from_file_location("power_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    with create_engine("sqlite://").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE existing_data (id INTEGER PRIMARY KEY)")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()

        tracking = AlliancePowerTracking.__table__
        snapshots = AlliancePowerSnapshot.__table__
        connection.execute(tracking.insert(), {"guild_id": 123, "kingdom_id": 830, "alliance_id": 83900004})
        connection.execute(tracking.insert(), {"guild_id": 456, "kingdom_id": 830, "alliance_id": 83900004})
        members = {"28584855": {"name": "어신짱", "fid": "117248174", "power": 686765911}}
        snapshot = {
            "kingdom_id": 830,
            "alliance_id": 83900004,
            "snapshot_date": date(2026, 9, 5),
            "captured_at": datetime(2026, 9, 5, 12, tzinfo=timezone.utc),
            "alliance_name": "Serendipity",
            "alliance_tag": "KOR",
            "total_power": 2**40,
            "members": members,
        }
        connection.execute(snapshots.insert(), snapshot)
        stored = connection.execute(select(snapshots)).mappings().one()
        assert stored["members"] == members
        assert stored["total_power"] == 2**40
        assert stored["snapshot_date"] == date(2026, 9, 5)
        assert connection.execute(select(tracking.c.created_at).where(tracking.c.guild_id == 123)).scalar_one()

        with pytest.raises(IntegrityError):
            connection.execute(snapshots.insert(), snapshot)
        with pytest.raises(IntegrityError):
            connection.execute(tracking.insert(), {"guild_id": 123, "kingdom_id": 900, "alliance_id": 1})
        connection.execute(snapshots.insert(), {**snapshot, "snapshot_date": date(2026, 9, 6)})

        migration.downgrade()
        assert inspect(connection).get_table_names() == ["existing_data"]
