"""Track replay changes by date so unrelated uploads keep their match cache."""

from datetime import datetime
from hashlib import sha256
import json

from dateutil.relativedelta import relativedelta


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS match_day_revisions (
            day TEXT PRIMARY KEY, revision INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS video_match_cache (
            video_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
            replay_hash TEXT NOT NULL, result_json TEXT NOT NULL
        )
    """)
    # AFTER triggers fire only for actual changes, not INSERT OR IGNORE conflicts.
    # They also cover manual corrections, deleted replays, and roster edits.
    def mark(timestamp):
        day = (f"CASE WHEN date({timestamp}, '+0 days') IS NULL "
               f"OR date({timestamp}, '+0 days') != substr({timestamp}, 1, 10) THEN '*' "
               f"ELSE substr({timestamp}, 1, 10) END")
        return (f"INSERT INTO match_day_revisions VALUES ({day}, 1) "
                "ON CONFLICT(day) DO UPDATE SET revision = revision + 1;")

    for table in ("battles", "battle_participants"):
        for event, rows in (("INSERT", ["NEW"]), ("DELETE", ["OLD"]),
                            ("UPDATE", ["OLD", "NEW"])):
            statements = []
            for row in rows:
                timestamp = (f"{row}.timestamp" if table == "battles" else
                             f"(SELECT timestamp FROM battles WHERE battle_id = {row}.battle_id)")
                statements.append(mark(timestamp))
            conn.execute(f"""
                CREATE TRIGGER IF NOT EXISTS match_revision_{table}_{event.lower()}
                AFTER {event} ON {table} BEGIN {' '.join(statements)} END
            """)
    # REPLACE does not always fire delete triggers. Record the old date before
    # a replacement moves a replay into a different upload window.
    previous_date = "(SELECT timestamp FROM battles WHERE battle_id = NEW.battle_id)"
    conn.execute(f"""
        CREATE TRIGGER IF NOT EXISTS match_revision_battle_replace
        BEFORE INSERT ON battles
        WHEN EXISTS (SELECT 1 FROM battles WHERE battle_id = NEW.battle_id
                     AND timestamp IS NOT NEW.timestamp)
        BEGIN {mark(previous_date)} END
    """)
    conn.commit()


def replay_scope_hash(revisions, upload_date, months):
    try:
        end = datetime.strptime(upload_date, "%Y%m%d").date()
        start = end - relativedelta(months=months)
        start, end = start.isoformat(), end.isoformat()
        selected = [(day, revision) for day, revision in revisions
                    if day == "*" or start <= day <= end]
    except (ValueError, TypeError):
        selected = revisions  # Invalid dates make the matcher consider every battle.
    return digest(selected)
