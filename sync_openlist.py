#!/usr/bin/env python3
"""Add-only, server-side OpenList replication. Python standard library only."""

import argparse
from collections import Counter
import json
import logging
import os
from pathlib import Path
import posixpath
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


LOG = logging.getLogger("sync")


class SyncError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    # Never forward the Authorization header to a redirected host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def child(parent, name):
    if not isinstance(name, str) or name in ("", ".", "..") or any(
        c in name for c in ("/", "\\", "\x00", "\r", "\n")
    ):
        raise SyncError(f"Unsafe entry name: {name!r}")
    return posixpath.join(parent, name)


def validate_config(config):
    url = urlsplit(config["base_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password:
        raise SyncError("base_url must be an HTTPS URL without credentials")
    if url.query or url.fragment:
        raise SyncError("base_url must not contain a query or fragment")
    paths = [config["source_root"], *config["destination_roots"]]
    if len(paths) < 2 or len(set(paths)) != len(paths):
        raise SyncError("Supply distinct source and destination roots")
    for path in paths:
        if not path.startswith("/") or path == "/" or posixpath.normpath(path) != path:
            raise SyncError(f"Invalid root path: {path!r}")
        if any(c in path for c in ("\\", "\x00", "\r", "\n")):
            raise SyncError(f"Invalid root path: {path!r}")
    for a in paths:
        for b in paths:
            if a != b and b.startswith(a + "/"):
                raise SyncError("Source and destination roots must not be nested")
    if config["existing_directories"] not in ("skip", "recurse"):
        raise SyncError("existing_directories must be skip or recurse")
    if not 1000 <= config["start_year"] <= 9999:
        raise SyncError("Invalid start_year")
    for key in ("request_timeout_seconds", "run_timeout_seconds", "poll_interval_seconds"):
        if config[key] <= 0:
            raise SyncError(f"{key} must be positive")


class OpenList:
    def __init__(self, config, token):
        self.base_url = config["base_url"].rstrip("/")
        self.token = token
        self.timeout = config["request_timeout_seconds"]
        self.interval = config["poll_interval_seconds"]
        self.deadline = time.monotonic() + config["run_timeout_seconds"]
        self.opener = build_opener(NoRedirect())

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SyncError("Run timeout; OpenList tasks may still be running. Check its task panel.")
        return remaining

    def api(self, route, body, *, retry=False):
        # Only retry reads. A timed-out copy/mkdir may already have been accepted.
        for attempt in range(3 if retry else 1):
            request = Request(
                self.base_url + route,
                data=json.dumps(body).encode(),
                headers={"Authorization": self.token, "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with self.opener.open(request, timeout=min(self.timeout, self.remaining())) as r:
                    payload = json.load(r)
            except HTTPError as exc:
                if retry and exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(min(2 ** attempt, self.remaining()))
                    continue
                raise SyncError(f"{route}: HTTP {exc.code}") from None
            except (URLError, TimeoutError, OSError) as exc:
                if retry and attempt < 2:
                    time.sleep(min(2 ** attempt, self.remaining()))
                    continue
                raise SyncError(f"{route}: network error ({type(exc).__name__}); writes not retried") from None
            except (ValueError, UnicodeError):
                raise SyncError(f"{route}: invalid JSON response") from None
            if not isinstance(payload, dict) or payload.get("code") != 200:
                raise SyncError(f"{route}: {payload.get('message', 'API error') if isinstance(payload, dict) else 'invalid response'}")
            return payload.get("data")

    def list_dir(self, path):
        entries = {}
        page = 1
        while True:
            data = self.api("/api/fs/list", {
                "path": path, "password": "", "page": page,
                "per_page": 1000, "refresh": page == 1,
            }, retry=True)
            if not isinstance(data, dict) or not isinstance(data.get("total"), int):
                raise SyncError(f"Invalid directory listing: {path}")
            content = data.get("content") or []
            if not isinstance(content, list):
                raise SyncError(f"Invalid directory content: {path}")
            for entry in content:
                name = entry["name"]
                child(path, name)
                if name in entries:
                    raise SyncError(f"Duplicate or unstable pagination: {path}/{name}")
                if not isinstance(entry.get("is_dir"), bool):
                    raise SyncError(f"Invalid entry type: {path}/{name}")
                entries[name] = entry
            if len(entries) >= data["total"]:
                return entries
            if not content:
                raise SyncError(f"Incomplete directory listing: {path}")
            page += 1

    def mkdir(self, path):
        self.api("/api/fs/mkdir", {"path": path})

    def copy_file(self, source, destination, entry):
        name = entry["name"]
        data = self.api("/api/fs/copy", {
            "src_dir": source, "dst_dir": destination, "names": [name],
            "overwrite": False, "skip_existing": True, "merge": False,
        })
        # OpenList v4 returns tasks; a successful submission is NOT a completed copy.
        if data is not None and not isinstance(data, dict):
            raise SyncError("Unrecognized copy response")
        tasks = (data or {}).get("tasks", [])
        if not isinstance(tasks, list):
            raise SyncError("Invalid copy task list")
        for task in tasks:
            self.wait_task(task["id"])
        # Also covers synchronous completion and a race where skip_existing applied.
        target = self.list_dir(destination).get(name)
        if not target or target["is_dir"]:
            raise SyncError(f"Copy not verified: {destination}/{name}")
        # A concurrent writer's same-name file must be left alone, even if its size differs.
        if tasks and target.get("size") != entry.get("size"):
            raise SyncError(f"Copy size mismatch: {destination}/{name}")

    def wait_task(self, task_id):
        while True:
            info = self.api("/api/task/copy/info?" + urlencode({"tid": task_id}), {}, retry=True)
            if not isinstance(info, dict):
                raise SyncError(f"Invalid task response: {task_id}")
            state = info.get("state")
            if state == 2:  # tache.StateSucceeded
                return
            if state in (4, 7):  # StateCanceled, StateFailed
                raise SyncError(f"Copy task {task_id} failed: {info.get('error', '')}")
            if state not in (0, 1, 3, 5, 6, 8, 9):
                raise SyncError(f"Unknown task state: {state!r}")
            time.sleep(min(self.interval, self.remaining()))


class Syncer:
    def __init__(self, api, config, dry_run=False):
        self.api = api
        self.config = config
        self.dry_run = dry_run
        self.counts = Counter()
        self.source_cache = {}

    def source_entries(self, path):
        if path not in self.source_cache:
            self.source_cache[path] = self.api.list_dir(path)
        return self.source_cache[path]

    def make_directory(self, path):
        LOG.info("%s mkdir %s", "PLAN" if self.dry_run else "CREATE", path)
        self.counts["directories_planned" if self.dry_run else "directories_created"] += 1
        if not self.dry_run:
            self.api.mkdir(path)

    def tree(self, source, destination, *, new=False):
        stack = [(source, destination, new)]
        while stack:
            src, dst, virtual = stack.pop()
            # Only a just-planned directory is virtual. Real directories are always refreshed.
            target = {} if self.dry_run and virtual else self.api.list_dir(dst)
            self.counts["directories_scanned"] += 1
            for name, entry in sorted(self.source_entries(src).items()):
                src_path, dst_path = child(src, name), child(dst, name)
                existing = target.get(name)
                if existing is not None:
                    if entry["is_dir"] and existing["is_dir"] and self.config["existing_directories"] == "recurse":
                        stack.append((src_path, dst_path, False))
                    else:
                        self.counts["directories_skipped" if entry["is_dir"] else "files_skipped"] += 1
                        LOG.info("SKIP existing %s", dst_path)
                    continue
                if entry["is_dir"]:
                    self.make_directory(dst_path)
                    stack.append((src_path, dst_path, True))
                else:
                    LOG.info("%s %s -> %s", "PLAN copy" if self.dry_run else "COPY", src_path, dst_path)
                    if not self.dry_run:
                        self.api.copy_file(src, dst, entry)
                    self.counts["files_planned" if self.dry_run else "files_copied"] += 1

    def run(self):
        source = self.config["source_root"]
        years = sorted(name for name, entry in self.source_entries(source).items()
                       if entry["is_dir"] and re.fullmatch(r"[0-9]{4}", name)
                       and int(name) >= self.config["start_year"])
        if not years:
            raise SyncError("No matching year directories found in source")
        failures = []
        for root in self.config["destination_roots"]:
            try:
                # Mount roots must exist. Never mistake a permission/driver error for an empty directory.
                targets = self.api.list_dir(root)
                for year in years:
                    src, dst = child(source, year), child(root, year)
                    entry = targets.get(year)
                    if entry and not entry["is_dir"]:
                        LOG.warning("SKIP year path occupied by a file: %s", dst)
                        self.counts["files_skipped"] += 1
                        continue
                    if entry is None:
                        self.make_directory(dst)
                    # Year directories are routing containers, not items subject to directory skipping.
                    self.tree(src, dst, new=entry is None)
            except SyncError as exc:
                LOG.error("Destination %s failed: %s", root, exc)
                failures.append(root)
        if failures:
            raise SyncError("Failed destination(s): " + ", ".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="sync_config.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("OPENLIST_TOKEN", "").strip()

    class Redact(logging.Filter):
        def filter(self, record):
            record.msg = record.getMessage().replace(token, "[REDACTED]") if token else record.getMessage()
            record.args = ()
            return True

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    LOG.addFilter(Redact())
    syncer = None
    success = False
    try:
        if not token:
            raise SyncError("Set the OPENLIST_TOKEN environment variable / GitHub Actions secret")
        config = json.loads(Path(args.config).read_text())
        override = os.environ.get("EXISTING_DIRECTORIES", "config")
        if override != "config":
            config["existing_directories"] = override
        validate_config(config)
        dry_run = args.dry_run or os.environ.get("DRY_RUN", "false").lower() == "true"
        syncer = Syncer(OpenList(config, token), config, dry_run)
        LOG.info("dry_run=%s existing_directories=%s", dry_run, config["existing_directories"])
        syncer.run()
        success = True
    except (SyncError, OSError, ValueError, KeyError, TypeError) as exc:
        LOG.error("%s", exc)
    finally:
        counts = dict(syncer.counts) if syncer else {}
        LOG.info("Result=%s counts=%s", "success" if success else "failed", counts)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as file:
                file.write("## OpenList sync\n\n")
                file.write(f"Result: **{'success' if success else 'failed'}**\n\n")
                if syncer:
                    file.write(f"Dry run: `{syncer.dry_run}`; existing directories: `{syncer.config['existing_directories']}`\n\n")
                for key, value in sorted(counts.items()):
                    file.write(f"- {key}: {value}\n")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
