import sqlite3
from pathlib import Path

import pytest

from kalshibot.persistence.backup import backup_database


def test_backup_database_is_consistent(tmp_path):
    source = tmp_path / "source.db"
    destination = tmp_path / "backups" / "copy.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE sample(value TEXT)")
        db.execute("INSERT INTO sample VALUES ('ok')")
        db.commit()

    backup_database(source, destination)

    with sqlite3.connect(destination) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT value FROM sample").fetchone()[0] == "ok"


def test_failed_backup_removes_temporary_file_and_preserves_destination(tmp_path):
    source = tmp_path / "not-a-database.db"
    source.write_text("invalid sqlite")
    destination = tmp_path / "backups" / "copy.db"
    destination.parent.mkdir()
    destination.write_bytes(b"previous verified backup")
    temp = destination.with_suffix(".tmp")
    Path(f"{temp}-wal").write_bytes(b"stale wal")
    Path(f"{temp}-shm").write_bytes(b"stale shm")

    with pytest.raises(sqlite3.DatabaseError):
        backup_database(source, destination)

    assert destination.read_bytes() == b"previous verified backup"
    assert not temp.exists()
    assert not Path(f"{temp}-wal").exists()
    assert not Path(f"{temp}-shm").exists()
