"""SQLite storage: timestamped, append-only snapshots. Never overwrite history."""

from .schema import connect, init_db, record_snapshot

__all__ = ["connect", "init_db", "record_snapshot"]
