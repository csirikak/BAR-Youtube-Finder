from contextlib import closing, redirect_stdout
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import rename_resolver
from updateBattleDB import setup_database
from updateSchema import add_new_tables


class FakeClient:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error
        self.calls = []

    def search(self, name):
        self.calls.append(name)
        if self.error is not None:
            raise self.error
        return self.responses.get(name, [])


def account(user_id, current, previous):
    return {"userID": user_id, "username": current, "previousName": previous}


class RenameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Path(self.temporary.name) / "battles.db"
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)
        setup_database(self.db).close()
        add_new_tables(self.db)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT INTO battles VALUES "
                         "('battle', '2026-09-01T12:00:00Z', 'Map')")
            conn.execute("INSERT INTO videos VALUES "
                         "('video', '20260902', 'Title', 'Channel')")
            conn.execute("INSERT INTO battle_videos "
                         "(battle_id, video_id, video_timestamp_sec) "
                         "VALUES ('battle', 'video', 90)")
            conn.commit()
        self.set_names(["Ambiguous", "Current", "Nope", "OldName"])

    def set_names(self, names):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM battle_participants")
            conn.executemany("INSERT INTO battle_participants VALUES ('battle', ?)",
                             [(name,) for name in names])
            conn.commit()

    def resolve(self, **kwargs):
        kwargs.setdefault("db_name", self.db)
        kwargs.setdefault("users", {"Current": 1})
        kwargs.setdefault("workers", 1)
        return rename_resolver.resolve(**kwargs)

    def mapping(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return dict(conn.execute(
                "SELECT player_name, user_id FROM player_accounts"))

    def cache_statuses(self):
        with closing(sqlite3.connect(self.db)) as conn:
            return dict(conn.execute(
                "SELECT player_name, status FROM player_rename_cache"))

    def test_current_and_previous_names_share_an_account(self):
        client = FakeClient({
            "OldName": [account(2, "NewName", "OldName")],
            "Ambiguous": [account(3, "Three", "Ambiguous"),
                          account(4, "Four", "Ambiguous")],
            "Nope": [],
        })
        stats = self.resolve(client=client)
        self.assertEqual(stats["current"], 1)
        self.assertEqual(stats["resolved"], 2)
        self.assertEqual(stats["ambiguous"], 1)
        self.assertEqual(stats["not_found"], 1)
        self.assertEqual(self.mapping(), {"Current": 1, "OldName": 2})
        self.assertEqual(self.cache_statuses(), {
            "Current": "ok", "OldName": "ok",
            "Ambiguous": "ambiguous", "Nope": "not_found",
        })
        self.assertEqual(sorted(client.calls), ["Ambiguous", "Nope", "OldName"])

    def test_prefix_results_are_ignored_without_an_exact_name(self):
        client = FakeClient({
            "OldName": [account(9, "Other", "OldNameExtra"),
                        account(2, "NewName", "OldName")],
            "Ambiguous": [],
            "Nope": [],
        })
        self.resolve(client=client)
        self.assertEqual(self.mapping(), {"Current": 1, "OldName": 2})

    def test_cache_prevents_repeat_lookups(self):
        self.resolve(client=FakeClient({
            "OldName": [account(2, "NewName", "OldName")],
            "Ambiguous": [account(3, "Three", "Ambiguous"),
                          account(4, "Four", "Ambiguous")],
            "Nope": [],
        }))
        second = FakeClient(error=AssertionError("network used"))
        stats = self.resolve(client=second)
        self.assertEqual(second.calls, [])
        self.assertEqual(stats["cached"], 4)
        self.assertEqual(self.mapping(), {"Current": 1, "OldName": 2})

    def test_lookup_budget_bounds_work_and_resumes(self):
        self.set_names(["OldOne", "OldThree", "OldTwo"])
        responses = {
            "OldOne": [account(11, "NewOne", "OldOne")],
            "OldThree": [account(13, "NewThree", "OldThree")],
            "OldTwo": [account(12, "NewTwo", "OldTwo")],
        }
        first = FakeClient(responses)
        stats = self.resolve(users={}, client=first, max_requests=1)
        self.assertEqual(first.calls, ["OldOne"])
        self.assertEqual(stats["remaining"], 2)
        second = FakeClient(responses)
        self.resolve(users={}, client=second, max_requests=2)
        self.assertEqual(second.calls, ["OldThree", "OldTwo"])
        self.assertEqual(self.mapping(), {
            "OldOne": 11, "OldThree": 13, "OldTwo": 12,
        })

    def test_concurrent_lookups_still_resolve_every_name(self):
        self.set_names(["OldOne", "OldThree", "OldTwo"])
        client = FakeClient({
            "OldOne": [account(11, "NewOne", "OldOne")],
            "OldThree": [account(13, "NewThree", "OldThree")],
            "OldTwo": [account(12, "NewTwo", "OldTwo")],
        })
        self.resolve(users={}, client=client, workers=4)
        self.assertEqual(sorted(client.calls), ["OldOne", "OldThree", "OldTwo"])
        self.assertEqual(self.mapping(), {
            "OldOne": 11, "OldThree": 13, "OldTwo": 12,
        })

    def test_casefold_retry_resolves_an_uppercase_alias(self):
        self.set_names(["CCCCCENTIPEDE"])
        client = FakeClient({
            "CCCCCENTIPEDE": [],
            "cccccentipede": [account(60841, "Hazred", "CCCCCENTIPEDE")],
        })
        stats = self.resolve(users={}, client=client)
        self.assertEqual(client.calls,
                         ["CCCCCENTIPEDE", "cccccentipede"])
        self.assertEqual(stats["looked_up"], 2)
        self.assertEqual(self.mapping(), {"CCCCCENTIPEDE": 60841})

    def test_network_error_is_not_cached_and_is_retried(self):
        self.set_names(["OldName"])
        failed = FakeClient(error=RuntimeError("gex down"))
        stats = self.resolve(users={}, client=failed)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["remaining"], 1)
        self.assertEqual(self.mapping(), {})
        self.assertEqual(self.cache_statuses(), {})
        recovered = FakeClient({
            "OldName": [account(2, "NewName", "OldName")],
        })
        self.resolve(users={}, client=recovered)
        self.assertEqual(self.mapping(), {"OldName": 2})

    def test_gex_client_sends_the_required_user_agent(self):
        response = Mock(status_code=200)
        response.json.return_value = [account(2, "NewName", "OldName")]
        session = requests.Session()
        session.get = Mock(return_value=response)
        client = rename_resolver.GexClient(
            session=session, limiter=rename_resolver.RateLimiter(interval=0))
        client.search("[FFS]Hazred")
        self.assertEqual(session.headers["User-Agent"],
                         "youtube-finder (discord: cubicleannihilator)")
        url = session.get.call_args.args[0]
        self.assertTrue(url.endswith(
            "/api/user/search/%5BFFS%5DHazred"), url)
        self.assertEqual(session.get.call_args.kwargs["params"], {
            "includeSkill": "false", "searchPreviousNames": "true",
        })
        response.raise_for_status.assert_called_once()

    def test_current_user_list_skips_names_shared_by_two_accounts(self):
        response = Mock()
        response.json.return_value = [
            {"id": 1, "username": "One"},
            {"id": 2, "username": "Shared"},
            {"id": 3, "username": "Shared"},
            {"username": "No ID"},
        ]
        with patch.object(rename_resolver.requests, "get",
                          return_value=response) as get:
            users = rename_resolver.fetch_current_users()
        self.assertEqual(users, {"One": 1})
        get.assert_called_once()

    def test_rate_limiter_spaces_requests(self):
        now = [0.0]
        sleeps = []
        limiter = rename_resolver.RateLimiter(
            interval=1.0, clock=lambda: now[0],
            sleep=lambda seconds: (sleeps.append(seconds),
                                   now.__setitem__(0, now[0] + seconds)))
        limiter.acquire()
        limiter.acquire()
        limiter.acquire()
        self.assertEqual(sleeps, [1.0, 1.0])


if __name__ == "__main__":
    unittest.main()
