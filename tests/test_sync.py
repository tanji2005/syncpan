from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.error import URLError

from sync_openlist import OpenList, SyncError, Syncer, child, validate_config


CONFIG = json.loads(Path("sync_config.json").read_text())


class MemoryAPI:
    def __init__(self, tree):
        self.tree = deepcopy(tree)
        self.writes = []
        self.reads = []
        self.pending = []

    def node(self, path):
        value = self.tree
        for part in path.strip("/").split("/"):
            value = value[part]
        return value

    def list_dir(self, path):
        self.reads.append(path)
        return {
            name: {"name": name, "is_dir": isinstance(value, dict),
                   "size": 0 if isinstance(value, dict) else len(value)}
            for name, value in self.node(path).items()
        }

    def submit_copy(self, source, destination, names):
        self.writes.append((source, destination, names))
        self.pending.append((source, destination, names))
        return [{"id": f"task-{len(self.writes)}", "state": 0}]

    def finish_pending(self):
        # Simulate work performed by OpenList AFTER the submitting process exits.
        for source, destination, names in self.pending:
            for name in names:
                if name not in self.node(destination):
                    self.node(destination)[name] = deepcopy(self.node(source)[name])
        self.pending.clear()


class SyncTests(unittest.TestCase):
    def config(self, policy="recurse"):
        config = deepcopy(CONFIG)
        config["existing_directories"] = policy
        return config

    def test_one_source_two_targets_and_new_years(self):
        api = MemoryAPI({
            "wangyuheng": {
                "2026": {"11": {"new.txt": b"new"}},
                "2027": {"1": {"next.txt": b"next"}},
                "2025": {"ignore.txt": b"old"},
                "Attachments": {"ignore.txt": b"other"},
            },
            "gongyun": {"only-target.txt": b"preserve"}, "local": {},
        })
        syncer = Syncer(api, self.config())
        syncer.run()
        self.assertEqual(len(api.writes), 4)
        self.assertNotIn("2026", api.tree["local"])  # Accepted, not completed.
        self.assertEqual(syncer.counts["directories_submitted"], 4)
        self.assertNotIn("/wangyuheng/2026/11", api.reads)
        api.finish_pending()
        for target in ("gongyun", "local"):
            self.assertEqual(api.tree[target]["2026"]["11"]["new.txt"], b"new")
            self.assertEqual(api.tree[target]["2027"]["1"]["next.txt"], b"next")
            self.assertNotIn("2025", api.tree[target])
            self.assertNotIn("Attachments", api.tree[target])
        self.assertNotIn("only-target.txt", api.tree["local"])
        self.assertEqual(api.tree["gongyun"]["only-target.txt"], b"preserve")
        self.assertTrue(all(write[0] == "/wangyuheng" for write in api.writes))

    def test_recurse_only_new_files_and_second_run_is_idempotent(self):
        api = MemoryAPI({
            "wangyuheng": {"2026": {"10": {"a.txt": b"changed", "b.txt": b"new"}}},
            "gongyun": {"2026": {"10": {"a.txt": b"keep"}}},
            "local": {"2026": {"10": {"a.txt": b"keep", "extra.txt": b"keep"}}},
        })
        Syncer(api, self.config()).run()
        api.finish_pending()
        for root in ("gongyun", "local"):
            self.assertEqual(api.tree[root]["2026"]["10"]["a.txt"], b"keep")
            self.assertEqual(api.tree[root]["2026"]["10"]["b.txt"], b"new")
        self.assertEqual(len(api.writes), 2)
        api.writes.clear()
        Syncer(api, self.config()).run()
        self.assertEqual(api.writes, [])

    def test_strict_skip_existing_month_not_year_container(self):
        api = MemoryAPI({
            "wangyuheng": {"2026": {"10": {"new.txt": b"x"}, "11": {"new.txt": b"y"}}},
            "gongyun": {"2026": {"10": {}}}, "local": {"2026": {"10": {}}},
        })
        Syncer(api, self.config("skip")).run()
        self.assertNotIn("/wangyuheng/2026/11", api.reads)
        api.finish_pending()
        for root in ("gongyun", "local"):
            self.assertEqual(api.tree[root]["2026"]["10"], {})
            self.assertEqual(api.tree[root]["2026"]["11"], {"new.txt": b"y"})
        self.assertNotIn("/wangyuheng/2026/10", api.reads)

    def test_dry_run_has_no_writes_even_for_missing_years(self):
        api = MemoryAPI({"wangyuheng": {"2026": {"11": {"x": b"x"}}}, "gongyun": {}, "local": {}})
        before = deepcopy(api.tree)
        syncer = Syncer(api, self.config(), dry_run=True)
        syncer.run()
        self.assertEqual(api.tree, before)
        self.assertEqual(api.writes, [])
        self.assertEqual(syncer.counts["directories_planned"], 2)

    def test_file_directory_name_collision_is_skipped(self):
        api = MemoryAPI({
            "wangyuheng": {"2026": {"dir": {"x": b"x"}, "file": b"x"}},
            "gongyun": {"2026": {"dir": b"keep", "file": {}}},
            "local": {"2026": {"dir": b"keep", "file": {}}},
        })
        Syncer(api, self.config()).run()
        self.assertEqual(api.writes, [])

    def test_failure_of_one_target_does_not_block_other_target(self):
        api = MemoryAPI({"wangyuheng": {"2026": {"x": b"x"}}, "local": {}})
        original = api.list_dir
        api.list_dir = lambda path: (_ for _ in ()).throw(SyncError("denied")) if path == "/gongyun" else original(path)
        with self.assertRaisesRegex(SyncError, "/gongyun"):
            Syncer(api, self.config()).run()
        api.finish_pending()
        self.assertEqual(api.tree["local"]["2026"]["x"], b"x")

    def test_batch_new_files_and_folders_without_listing_their_contents(self):
        api = MemoryAPI({
            "wangyuheng": {"2026": {"a": b"a", "b": b"b", "11": {"nested": {"big": b"x"}}}},
            "gongyun": {"2026": {}}, "local": {"2026": {}},
        })
        syncer = Syncer(api, self.config("skip"))
        syncer.run()
        self.assertEqual(len(api.writes), 2)  # One HTTP request per target year.
        self.assertEqual(api.writes[0], ("/wangyuheng/2026", "/gongyun/2026", ["11", "a", "b"]))
        self.assertNotIn("/wangyuheng/2026/11", api.reads)
        self.assertNotIn("/gongyun/2026/11", api.reads)
        self.assertEqual(api.tree["gongyun"]["2026"], {})
        self.assertNotIn("files_copied", syncer.counts)

    def test_large_batch_is_split_into_bounded_requests(self):
        files = {f"file-{i:03}": b"x" for i in range(205)}
        api = MemoryAPI({"wangyuheng": {"2026": files}, "gongyun": {"2026": {}}, "local": {"2026": {}}})
        Syncer(api, self.config()).run()
        self.assertEqual([len(names) for _, _, names in api.writes], [100, 100, 5, 100, 100, 5])


class APITests(unittest.TestCase):
    def setUp(self):
        self.api = OpenList(CONFIG, "test-token")

    def test_paginate_and_only_first_page_refreshes(self):
        entry = lambda name: {"name": name, "is_dir": False, "size": 1}
        self.api.api = Mock(side_effect=[
            {"total": 2, "content": [entry("a")]},
            {"total": 2, "content": [entry("b")]},
        ])
        self.assertEqual(set(self.api.list_dir("/test")), {"a", "b"})
        self.assertTrue(self.api.api.call_args_list[0].args[1]["refresh"])
        self.assertFalse(self.api.api.call_args_list[1].args[1]["refresh"])

    def test_incomplete_and_duplicate_pagination_fail_closed(self):
        for second in ([], [{"name": "a", "is_dir": False}]):
            with self.subTest(second=second):
                self.api.api = Mock(side_effect=[
                    {"total": 2, "content": [{"name": "a", "is_dir": False}]},
                    {"total": 2, "content": second},
                ])
                with self.assertRaises(SyncError):
                    self.api.list_dir("/test")

    def test_copy_returns_pending_tasks_immediately_without_polling_or_size_check(self):
        tasks = [{"id": "task-1", "state": 0}]
        self.api.api = Mock(return_value={"tasks": tasks})
        self.api.list_dir = Mock(side_effect=AssertionError("Must not verify transfer"))
        returned = self.api.submit_copy("/wangyuheng/2026", "/local/2026", ["11", "x"])
        request = self.api.api.call_args.args[1]
        self.assertEqual(request["names"], ["11", "x"])
        self.assertIs(request["overwrite"], False)
        self.assertIs(request["skip_existing"], True)
        self.assertIs(request["merge"], False)
        self.assertEqual(returned, tasks)
        self.api.api.assert_called_once()
        self.api.list_dir.assert_not_called()

    def test_invalid_task_response_is_failure(self):
        for data in ([], {"tasks": {}}, {"tasks": [{"id": ""}]}, {"tasks": [None]}):
            self.api.api = Mock(return_value=data)
            with self.assertRaises(SyncError):
                self.api.submit_copy("/source", "/target", ["x"])

    def test_empty_batch_does_not_submit_anything(self):
        self.api.api = Mock()
        self.assertEqual(self.api.submit_copy("/source", "/target", []), [])
        self.api.api.assert_not_called()

    def test_synchronous_or_race_skipped_response_needs_no_polling(self):
        for data in (None, {"message": "Copy operations completed immediately"}, {"tasks": []}):
            self.api.api = Mock(return_value=data)
            self.assertEqual(self.api.submit_copy("/source", "/target", ["x"]), [])
            self.api.api.assert_called_once()

    def test_ambiguous_write_is_not_retried(self):
        self.api.opener.open = Mock(side_effect=URLError("timeout"))
        with self.assertRaises(SyncError):
            self.api.api("/api/fs/copy", {})
        self.assertEqual(self.api.opener.open.call_count, 1)

    def test_http_200_api_error_is_failure(self):
        response = BytesIO(json.dumps({"code": 403, "message": "denied"}).encode())
        self.api.opener.open = Mock(return_value=response)
        with self.assertRaisesRegex(SyncError, "denied"):
            self.api.list_dir("/source")

    def test_run_timeout_fails(self):
        self.api.deadline = 0
        with self.assertRaisesRegex(SyncError, "timeout"):
            self.api.remaining()


class ConfigTests(unittest.TestCase):
    def test_config_and_nested_paths(self):
        validate_config(CONFIG)
        for target in ("/wangyuheng/backup", "/wangyuheng", "/", "../local"):
            config = deepcopy(CONFIG)
            config["destination_roots"] = [target]
            with self.assertRaises(SyncError):
                validate_config(config)

    def test_unsafe_remote_entry_names(self):
        for name in ("", "..", ".", "../x", "a/b", "a\\b", "a\x00b", "a\nb"):
            with self.assertRaises(SyncError):
                child("/source", name)


if __name__ == "__main__":
    unittest.main()
