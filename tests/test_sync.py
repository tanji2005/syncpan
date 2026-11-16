from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import posixpath
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

from sync_openlist import OpenList, SyncError, Syncer, child, validate_config


CONFIG = json.loads(Path("sync_config.json").read_text())


class MemoryAPI:
    def __init__(self, tree):
        self.tree = deepcopy(tree)
        self.writes = []
        self.reads = []

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

    def mkdir(self, path):
        self.writes.append(("mkdir", path))
        self.node(posixpath.dirname(path))[posixpath.basename(path)] = {}

    def copy_file(self, source, destination, entry):
        name = entry["name"]
        self.writes.append(("copy", source, destination, name))
        assert name not in self.node(destination), "Existing file must never be copied"
        self.node(destination)[name] = self.node(source)[name]


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
        for target in ("gongyun", "local"):
            self.assertEqual(api.tree[target]["2026"]["11"]["new.txt"], b"new")
            self.assertEqual(api.tree[target]["2027"]["1"]["next.txt"], b"next")
            self.assertNotIn("2025", api.tree[target])
            self.assertNotIn("Attachments", api.tree[target])
        self.assertNotIn("only-target.txt", api.tree["local"])
        self.assertEqual(api.tree["gongyun"]["only-target.txt"], b"preserve")
        self.assertTrue(all(write[1].startswith("/wangyuheng/")
                            for write in api.writes if write[0] == "copy"))

    def test_recurse_only_new_files_and_second_run_is_idempotent(self):
        api = MemoryAPI({
            "wangyuheng": {"2026": {"10": {"a.txt": b"changed", "b.txt": b"new"}}},
            "gongyun": {"2026": {"10": {"a.txt": b"keep"}}},
            "local": {"2026": {"10": {"a.txt": b"keep", "extra.txt": b"keep"}}},
        })
        Syncer(api, self.config()).run()
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
        self.assertEqual(syncer.counts["files_planned"], 2)

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
        self.assertEqual(api.tree["local"]["2026"]["x"], b"x")


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

    def test_copy_waits_and_uses_non_overwrite_flags(self):
        entry = {"name": "x", "is_dir": False, "size": 3}
        self.api.api = Mock(return_value={"tasks": [{"id": "task-1"}]})
        self.api.wait_task = Mock()
        self.api.list_dir = Mock(return_value={"x": entry})
        self.api.copy_file("/wangyuheng/2026", "/local/2026", entry)
        request = self.api.api.call_args.args[1]
        self.assertIs(request["overwrite"], False)
        self.assertIs(request["skip_existing"], True)
        self.assertIs(request["merge"], False)
        self.api.wait_task.assert_called_once_with("task-1")

    def test_task_retry_states_are_not_treated_as_success(self):
        self.api.api = Mock(side_effect=[{"state": 5}, {"state": 8}, {"state": 2}])
        with patch("sync_openlist.time.sleep"):
            self.api.wait_task("task-1")
        self.assertEqual(self.api.api.call_count, 3)

    def test_failed_and_canceled_task_fail(self):
        for state in (4, 7):
            self.api.api = Mock(return_value={"state": state, "error": "failed"})
            with self.assertRaises(SyncError):
                self.api.wait_task("task-1")

    def test_missing_or_wrong_size_copy_is_not_success(self):
        entry = {"name": "x", "is_dir": False, "size": 3}
        self.api.api = Mock(return_value={"tasks": [{"id": "task-1"}]})
        self.api.wait_task = Mock()
        for target in ({}, {"x": {"name": "x", "is_dir": False, "size": 2}}):
            self.api.list_dir = Mock(return_value=target)
            with self.assertRaises(SyncError):
                self.api.copy_file("/source", "/target", entry)

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
