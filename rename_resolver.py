"""Resolve historical BAR player names to stable account IDs.

Replay lists expose only ``player_name``, but names are not identities: accounts
rename, and names can be reused. Gex tracks BAR accounts across renames, so this
module caches its lookups and writes unambiguous name -> account mappings for the
frontend export. Only names in battles that are actually linked to a video are
resolved, and every result is cached so a run can be interrupted and resumed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import datetime, timezone
import json
import sqlite3
import threading
import time
import unicodedata
from urllib.parse import quote

import requests

import console as ui


DB_NAME = "data/game_battles.db"
GEX_SEARCH_URL = "https://gex.honu.pw/api/user/search/{name}"
GEX_USER_AGENT = "youtube-finder (discord: cubicleannihilator)"
BAR_USERS_URL = "https://api.bar-rts.com/cached-users"
# The documented limit is 300 lookups per five minutes.
REQUESTS_PER_WINDOW = 300
WINDOW_SECONDS = 300.0
REQUEST_TIMEOUT = (10, 45)
MAX_RETRIES = 4

logger = ui.get("renames")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_accounts (
            player_name TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            current_name TEXT,
            source TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS player_rename_cache (
            player_name TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            matches_json TEXT NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS player_accounts_user "
                 "ON player_accounts(user_id)")
    conn.commit()


def matched_names(conn):
    """Only names that can appear in the exported catalog need an identity."""
    return [row[0] for row in conn.execute("""
        SELECT DISTINCT p.player_name
        FROM battle_participants p
        JOIN (SELECT battle_id FROM battle_videos
              UNION SELECT battle_id FROM twitch_battle_videos) matched
          ON matched.battle_id = p.battle_id
        WHERE p.player_name IS NOT NULL AND p.player_name != ''
        ORDER BY p.player_name
    """)]


def _fold(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def matching_accounts(name, rows):
    """Keep only exact name matches; Gex also returns prefix-like results."""
    if not isinstance(rows, list):
        return []
    exact = [
        row for row in rows
        if isinstance(row, dict)
        and (row.get("previousName") == name or row.get("username") == name)
    ]
    if not exact:
        folded = _fold(name)
        exact = [
            row for row in rows
            if isinstance(row, dict)
            and folded in (_fold(str(row.get("previousName") or "")),
                           _fold(str(row.get("username") or "")))
        ]
    accounts = {}
    for row in exact:
        user_id = row.get("userID")
        if isinstance(user_id, int) and not isinstance(user_id, bool):
            accounts.setdefault(user_id, row.get("username") or None)
    return [{"user_id": user_id, "current_name": current_name}
            for user_id, current_name in sorted(accounts.items())]


def fetch_current_users(session=None):
    """Return unambiguous current username -> account ID from BAR's user list."""
    response = (session or requests).get(
        BAR_USERS_URL, headers={"User-Agent": GEX_USER_AGENT},
        timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("BAR user list must be a list")
    by_name = {}
    for user in payload:
        if not isinstance(user, dict):
            continue
        name, user_id = user.get("username"), user.get("id")
        if not name or not isinstance(user_id, int) or isinstance(user_id, bool):
            continue
        by_name.setdefault(name, set()).add(user_id)
    # A name used by more than one live account is not an identity.
    return {name: next(iter(ids)) for name, ids in by_name.items() if len(ids) == 1}


class RateLimiter:
    """Conservative spacing for the documented 300 requests / 5 minutes."""

    def __init__(self, interval=None, *, clock=time.monotonic, sleep=time.sleep):
        default = WINDOW_SECONDS / REQUESTS_PER_WINDOW
        self.interval = default if interval is None else max(0.0, float(interval))
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            now = self._clock()
            wait = self._next - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
            self._next = max(now, self._next) + self.interval

    def defer(self, seconds):
        with self._lock:
            self._next = max(self._next, self._clock() + max(0.0, seconds))


def _retry_after(response):
    try:
        return max(1.0, float(response.headers.get("Retry-After", 1)))
    except (AttributeError, TypeError, ValueError):
        return 1.0


class GexClient:
    def __init__(self, session=None, limiter=None):
        self._session = session
        self._local = threading.local()
        self.limiter = limiter or RateLimiter()
        self.requests_made = 0
        if session is not None:
            self._configure(session)

    @staticmethod
    def _configure(session):
        # requests supplies its own default User-Agent; this must overwrite it.
        session.headers["User-Agent"] = GEX_USER_AGENT
        session.headers["Accept"] = "application/json"

    def _get_session(self):
        if self._session is not None:
            return self._session
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._configure(session)
            self._local.session = session
        return session

    def search(self, name):
        url = GEX_SEARCH_URL.format(name=quote(name, safe=""))
        params = {"includeSkill": "false", "searchPreviousNames": "true"}
        for attempt in range(MAX_RETRIES):
            self.limiter.acquire()
            self.requests_made += 1
            response = self._get_session().get(
                url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                wait = _retry_after(response)
                logger.warning("Gex rate limited; pausing %.0fs", wait)
                self.limiter.defer(wait)
                continue
            if 500 <= response.status_code < 600:
                self.limiter.defer(min(2 ** attempt, 30))
                continue
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Gex search response must be a list")
            return payload
        raise RuntimeError(f"Gex lookup failed after {MAX_RETRIES} attempts")


def _write_cache(conn, name, status, matches, checked_at):
    conn.execute("""
        INSERT INTO player_rename_cache (player_name, status, matches_json, checked_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(player_name) DO UPDATE SET status=excluded.status,
            matches_json=excluded.matches_json, checked_at=excluded.checked_at
    """, (name, status, json.dumps(matches, ensure_ascii=False,
                                   separators=(",", ":")), checked_at))


def _store_mapping(conn, name, user_id, current_name, source, checked_at):
    conn.execute("""
        INSERT INTO player_accounts
            (player_name, user_id, current_name, source, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(player_name) DO UPDATE SET user_id=excluded.user_id,
            current_name=excluded.current_name, source=excluded.source,
            checked_at=excluded.checked_at
    """, (name, user_id, current_name, source, checked_at))


def resolve(*, db_name=DB_NAME, max_requests=None, force=False, client=None,
            users=None, limiter=None, workers=4):
    """Resolve names in matched battles, bounded by ``max_requests`` lookups."""
    if workers < 1:
        raise ValueError("Worker count must be positive")
    started = time.perf_counter()
    stats = {"names": 0, "cached": 0, "current": 0, "looked_up": 0,
             "resolved": 0, "ambiguous": 0, "not_found": 0, "errors": 0,
             "remaining": 0}
    with closing(sqlite3.connect(db_name, timeout=60)) as conn:
        ensure_schema(conn)
        names = matched_names(conn)
        stats["names"] = len(names)
        mapped = {name: user_id for name, user_id in conn.execute(
            "SELECT player_name, user_id FROM player_accounts")}
        cached = {row[0] for row in conn.execute(
            "SELECT player_name FROM player_rename_cache")}
        pending = [name for name in names
                   if force or (name not in mapped and name not in cached)]
        stats["cached"] = len(names) - len(pending)
        if not pending:
            logger.info("Rename lookup · %d name(s) already cached", len(names))
            return stats
        if max_requests == 0:
            stats["remaining"] = len(pending)
            logger.info("Rename lookup disabled · %d name(s) pending", len(pending))
            return stats

        if users is None:
            logger.info("Rename lookup · %d pending name(s); loading current BAR users",
                        len(pending))
            try:
                users = fetch_current_users()
            except Exception as error:
                stats["errors"] += 1
                stats["remaining"] = len(pending)
                logger.error("Could not load current BAR users; rename lookup "
                             "skipped: %s", ui.escape(str(error)))
                return stats

        processed = set()
        checked_at = _now()
        try:
            for name in pending:
                user_id = users.get(name)
                if user_id is None:
                    continue
                _store_mapping(conn, name, user_id, name, "bar-current", checked_at)
                _write_cache(conn, name, "ok",
                             [{"user_id": user_id, "current_name": name}], checked_at)
                processed.add(name)
                stats["current"] += 1
                stats["resolved"] += 1
            conn.commit()

            unresolved = [name for name in pending if name not in processed]
            if client is None:
                client = GexClient(limiter=limiter)
            queue = unresolved if max_requests is None else unresolved[:max(0, max_requests)]
            if queue:
                logger.info("Rename lookup · %d current · %d Gex lookup(s) queued · "
                            "%d worker(s)", stats["current"], len(queue), workers)
            used = 0
            processed_count = 0

            def lookup(name):
                if len(name) < 3:
                    return name, [], 0, None
                try:
                    rows = client.search(name)
                    count = 1
                    matches = matching_accounts(name, rows)
                    if not matches and len(name) >= 6:
                        folded = _fold(name)
                        if folded != name:
                            rows = client.search(folded)
                            count += 1
                            matches = matching_accounts(name, rows)
                    return name, matches, count, None
                except Exception as error:
                    # Do not cache failures; the next run retries them.
                    return name, [], 0, error

            # Requests are latency-bound (~4 s); overlap them while the limiter
            # still starts at most one request per interval. Only this thread
            # writes to SQLite.
            with ThreadPoolExecutor(max_workers=workers) as executor:
                for start in range(0, len(queue), 100):
                    batch = queue[start:start + 100]
                    futures = {executor.submit(lookup, name): name for name in batch}
                    for future in as_completed(futures):
                        name, matches, count, error = future.result()
                        used += count
                        processed_count += 1
                        if error is not None:
                            stats["errors"] += 1
                            logger.warning("Gex lookup failed for %s: %s",
                                           ui.escape(name),
                                           ui.escape(str(error)))
                            continue
                        processed.add(name)
                        if len(matches) == 1:
                            match = matches[0]
                            existing = mapped.get(name)
                            if existing is not None and existing != match["user_id"]:
                                conn.execute("DELETE FROM player_accounts "
                                             "WHERE player_name = ?", (name,))
                                mapped.pop(name, None)
                                _write_cache(conn, name, "ambiguous", matches,
                                             checked_at)
                                stats["ambiguous"] += 1
                            else:
                                _store_mapping(conn, name, match["user_id"],
                                               match["current_name"], "gex",
                                               checked_at)
                                _write_cache(conn, name, "ok", matches, checked_at)
                                mapped[name] = match["user_id"]
                                stats["resolved"] += 1
                        elif matches:
                            if name in mapped:
                                conn.execute("DELETE FROM player_accounts "
                                             "WHERE player_name = ?", (name,))
                                mapped.pop(name, None)
                            _write_cache(conn, name, "ambiguous", matches,
                                         checked_at)
                            stats["ambiguous"] += 1
                        else:
                            _write_cache(conn, name, "not_found", [], checked_at)
                            stats["not_found"] += 1
                        if processed_count % 25 == 0:
                            conn.commit()
                            elapsed = time.perf_counter() - started
                            rate = processed_count / elapsed if elapsed else 0
                            remaining = len(queue) - processed_count
                            eta = remaining / rate if rate else 0
                            logger.info(
                                "Rename lookup · %d/%d · %d resolved · "
                                "%d ambiguous · %d not found · %d error(s) · "
                                "~%s left", processed_count, len(queue),
                                stats["resolved"] - stats["current"],
                                stats["ambiguous"], stats["not_found"],
                                stats["errors"], ui.duration(eta))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        stats["looked_up"] = used
        stats["remaining"] = len([name for name in pending
                                  if name not in processed])
    logger.info(
        "Rename lookup · %d name(s) · %d cached · %d current · %d looked up · "
        "%d resolved · %d ambiguous · %d not found · %d error(s) · %s",
        stats["names"], stats["cached"], stats["current"], stats["looked_up"],
        stats["resolved"], stats["ambiguous"], stats["not_found"],
        stats["errors"], ui.duration(time.perf_counter() - started))
    if stats["remaining"]:
        logger.info("Rename lookup · %d name(s) left for the next run",
                    stats["remaining"])
    return stats


def main(*, db_name=DB_NAME, max_requests=None, force=False, interval=None,
         workers=4):
    return resolve(db_name=db_name, max_requests=max_requests, force=force,
                   limiter=RateLimiter(interval=interval), workers=workers)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Resolve historical BAR player names to account IDs")
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--max-requests", type=int, default=None,
                        help="maximum Gex lookups this run (default: no limit)")
    parser.add_argument("--force", action="store_true",
                        help="recheck names already in the cache")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between Gex requests (default: 1.0)")
    parser.add_argument("--workers", type=int, default=4,
                        help="concurrent Gex requests (default: 4)")
    main(**vars(parser.parse_args()))
