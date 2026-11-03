"""End-to-end tests for etesync-cli against a real Etebase server that allows signups
(e.g. the etesync-server test-server image).

There is deliberately no default server URL: set ETESYNC_TEST_API_URL explicitly to a
*disposable* test server, so the suite can't accidentally create accounts on a personal one.

Start a test server, then run from the repository root with the project virtualenv:

    podman run -d -p 127.0.0.1:3737:3735 test-server
    ETESYNC_TEST_API_URL=http://127.0.0.1:3737/ .venv/bin/python -m unittest -v tests.test_etesync_cli

Without ETESYNC_TEST_API_URL (or when the server is unreachable) the suite skips itself; set
ETESYNC_TEST_REQUIRE_SERVER=1 (e.g. in CI) to make that an error instead.

Every test gets its own temporary HOME, so the real ~/.etesync-cli is never touched. The CLI is
run through a subprocess with stdin piped and a detached session, so getpass() can't read from
the developer's terminal.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import uuid

from etebase import Account, Client, User

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(REPO_ROOT, "etesync-cli")
SERVER_URL = os.environ.get("ETESYNC_TEST_API_URL")
REQUIRE_SERVER = os.environ.get("ETESYNC_TEST_REQUIRE_SERVER") == "1"

EVENT_MARKER = "Grocery shopping"
CONTACT_MARKER = "Jane Doe"
EVENT1_UID = "event-one@test.example.com"
EVENT2_UID = "event-two@test.example.com"
CONTACT_UID = "contact-one@test.example.com"
TASK_UID = "task-one@test.example.com"

EVENT_TEMPLATE = "\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//etesync-cli test//EN",
    "BEGIN:VEVENT",
    "UID:event-one@test.example.com",
    "DTSTART:2026-11-05T10:00:00Z",
    "DTEND:2026-11-05T11:00:00Z",
    "SUMMARY:Grocery shopping",
    "END:VEVENT",
    "END:VCALENDAR",
    "",
])

VCARD = "\n".join([
    "BEGIN:VCARD",
    "VERSION:3.0",
    "N:Doe;Jane;;;",
    "FN:Jane Doe",
    "UID:contact-one@test.example.com",
    "EMAIL:jane@example.com",
    "END:VCARD",
    "",
])

VTODO = "\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//etesync-cli test//EN",
    "BEGIN:VTODO",
    "UID:task-one@test.example.com",
    "SUMMARY:Write report",
    "STATUS:NEEDS-ACTION",
    "END:VTODO",
    "END:VCALENDAR",
    "",
])


def millis():
    return int(round(time.time() * 1000))


def seed(account):
    """Create a calendar (with a revision and a deletion), an address book and a task list.

    Returns the collection UIDs as (calendar, address_book, tasks).
    """
    col_mgr = account.get_collection_manager()

    cal = col_mgr.create("etebase.vevent", {"name": "Test Calendar", "mtime": millis()}, b"")
    col_mgr.upload(cal)
    cal_mgr = col_mgr.get_item_manager(cal)

    item = cal_mgr.create({"name": EVENT1_UID, "mtime": millis()}, EVENT_TEMPLATE.encode())
    cal_mgr.batch([item], None, None)
    item.content = EVENT_TEMPLATE.replace(EVENT_MARKER, EVENT_MARKER + " (updated)").encode()
    item.meta = {"name": EVENT1_UID, "mtime": millis()}
    cal_mgr.batch([item], None, None)

    event2 = EVENT_TEMPLATE.replace("event-one", "event-two").replace(EVENT_MARKER,
                                                                      "Canceled event")
    item2 = cal_mgr.create({"name": EVENT2_UID, "mtime": millis()}, event2.encode())
    cal_mgr.batch([item2], None, None)
    item2.delete()
    cal_mgr.batch([item2], None, None)

    ab = col_mgr.create("etebase.vcard", {"name": "Test Contacts", "mtime": millis()}, b"")
    col_mgr.upload(ab)
    ab_mgr = col_mgr.get_item_manager(ab)
    item = ab_mgr.create({"name": CONTACT_UID, "mtime": millis()}, VCARD.encode())
    ab_mgr.batch([item], None, None)

    tasks = col_mgr.create("etebase.vtodo", {"name": "Test Tasks", "mtime": millis()}, b"")
    col_mgr.upload(tasks)
    tasks_mgr = col_mgr.get_item_manager(tasks)
    item = tasks_mgr.create({"name": TASK_UID, "mtime": millis()}, VTODO.encode())
    tasks_mgr.batch([item], None, None)

    return cal.uid, ab.uid, tasks.uid


def run_cli(args, home, stdin="", expect=0):
    """Run etesync-cli in an isolated HOME; returns the completed process."""
    env = dict(os.environ)
    env["HOME"] = home
    env["PYTHONWARNINGS"] = "ignore"
    proc = subprocess.run([sys.executable, CLI_PATH] + list(args), input=stdin,
                          capture_output=True, text=True, env=env, timeout=300,
                          start_new_session=True)
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            "etesync-cli {} exited {} (expected {})\nstdout:\n{}\nstderr:\n{}".format(
                " ".join(args), proc.returncode, expect, proc.stdout, proc.stderr))
    return proc


def output(proc):
    return proc.stdout + proc.stderr


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def iter_files(root):
    for dirpath, _, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def new_account(server):
    """Sign up a throwaway account; returns (username, service_password, account)."""
    username = "cli" + uuid.uuid4().hex[:10]
    service_password = "service-pass-" + uuid.uuid4().hex
    account = Account.signup(Client("etesync-cli-test", server), User(username, "test@example.com"),
                             service_password)
    return username, service_password, account


class EndToEndTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        skip = RuntimeError if REQUIRE_SERVER else unittest.SkipTest
        if not SERVER_URL:
            raise skip(
                "ETESYNC_TEST_API_URL is not set; point it at a disposable test server "
                "(see tests/test_etesync_cli.py docstring)")
        try:
            probe = SERVER_URL.rstrip("/") + "/api/v1/authentication/is_etebase/"
            with urllib.request.urlopen(probe, timeout=5):
                pass
        except Exception as e:
            raise skip("test server not reachable at {}: {}".format(SERVER_URL, e))

        cls.server = SERVER_URL
        cls.username, cls.service_password, account = new_account(cls.server)
        cls.encryption_password = "local-pass-" + uuid.uuid4().hex
        cls.cal_uid, cls.ab_uid, cls.tasks_uid = seed(account)

    def setUp(self):
        home = tempfile.mkdtemp(prefix="etesync-cli-test-")
        self.addCleanup(shutil.rmtree, home, True)
        self.home = home
        self.cache_dir = os.path.join(home, ".etesync-cli")

    def add_user(self, expect=0, username=None, service_password=None):
        username = username or self.username
        password = service_password or self.service_password
        return run_cli(["user", "add", "--server-url", self.server, username], self.home,
                       stdin="\n".join([password, self.encryption_password,
                                        self.encryption_password, ""]),
                       expect=expect)

    def with_password(self, args, expect=0, password=None):
        return run_cli(args, self.home, stdin=(password or self.encryption_password) + "\n",
                       expect=expect)

    def sync(self, username=None):
        return self.with_password(["sync", username or self.username])

    def user_cache_dir(self, username=None):
        key = (username or self.username).lower()
        return os.path.join(self.cache_dir, "cache", hashlib.sha256(key.encode()).hexdigest())

    # --- user management ---------------------------------------------------------------

    def test_add_user_and_list(self):
        self.add_user()
        listing = output(run_cli(["user", "list"], self.home))
        self.assertIn(self.username, listing)

        users_file = os.path.join(self.cache_dir, "users_cache")
        if os.name == "posix":
            self.assertEqual(0o700, mode(self.cache_dir))
            self.assertEqual(0o600, mode(users_file))

        # The stored material is ciphertext only; the server URL lives inside the session.
        with open(users_file, "rb") as f:
            blob = f.read()
        self.assertNotIn(self.service_password.encode(), blob)
        self.assertNotIn(self.encryption_password.encode(), blob)
        self.assertNotIn(self.server.encode(), blob)
        entry = json.loads(blob)["users"][self.username]
        self.assertEqual("scrypt", entry["kdf"]["name"])

        # Adding the same user twice is refused, whatever the case of the username.
        proc = self.add_user(expect=1, username=self.username.upper())
        self.assertIn("User already exists", output(proc))

    def test_add_user_rejects_bad_passphrases(self):
        cases = [
            ("short", "short", "at least 8 characters"),
            (self.service_password, self.service_password, "must differ"),
            ("local-pass-one", "local-pass-two", "don't match"),
        ]
        for passphrase, repeat, message in cases:
            with self.subTest(message=message):
                proc = run_cli(["user", "add", "--server-url", self.server, self.username],
                               self.home, stdin="\n".join([self.service_password, passphrase,
                                                           repeat, ""]), expect=1)
                self.assertIn(message, output(proc))
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, "users_cache")))

    def test_add_user_rejects_unsafe_username(self):
        for bad in ["../evil", "has/slash", "has space"]:
            proc = run_cli(["user", "add", "--server-url", self.server, bad], self.home,
                           expect=1)
            self.assertIn("Invalid username", output(proc))

    def test_add_user_refuses_plain_http_to_remote_server(self):
        proc = run_cli(["user", "add", "--server-url", "http://etebase.example.com/", "someone"],
                       self.home, expect=1)
        self.assertIn("unencrypted http://", output(proc))

    def test_unknown_user(self):
        proc = run_cli(["sync", "ghost"], self.home, expect=1)
        self.assertIn("User doesn't exist", output(proc))

    def test_user_del_logs_out_and_purges_cache(self):
        self.add_user()
        self.sync()
        self.assertTrue(os.path.isdir(self.user_cache_dir()))
        users_file = os.path.join(self.cache_dir, "users_cache")
        with open(users_file, "rb") as f:
            saved_session = f.read()

        proc = self.with_password(["user", "del", self.username])
        self.assertIn("logged out", output(proc))
        self.assertFalse(os.path.exists(self.user_cache_dir()))
        listing = output(run_cli(["user", "list"], self.home))
        self.assertNotIn(self.username, listing)

        proc = run_cli(["user", "del", self.username], self.home, expect=1)
        self.assertIn("User doesn't exist", output(proc))

        # The deleted session's auth token no longer works on the server.
        with open(users_file, "wb") as f:
            f.write(saved_session)
        proc = self.with_password(["sync", self.username], expect=1)
        self.assertIn("Invalid token", output(proc))

    def test_user_del_local_only_needs_no_passphrase(self):
        self.add_user()
        proc = self.with_password(["user", "del", self.username], expect=1, password="wrong-one")
        self.assertIn("--local-only", output(proc))
        proc = run_cli(["user", "del", "--local-only", self.username], self.home)
        self.assertIn("not logged out", output(proc))
        self.assertNotIn(self.username, output(run_cli(["user", "list"], self.home)))

    def test_legacy_caches_are_removed(self):
        legacy_caches = [
            {"users": {"olduser": "some-old-fernet-blob"}},  # etesync based version
            {"version": 2, "salt": "c2FsdA==", "users": {"olduser": {"session": "x"}}},
        ]
        users_file = os.path.join(self.cache_dir, "users_cache")
        old_cache = os.path.join(self.cache_dir, "cache", "olduser")
        for legacy in legacy_caches:
            with self.subTest(legacy=legacy):
                os.makedirs(old_cache, exist_ok=True)
                with open(users_file, "w", encoding="utf-8") as f:
                    json.dump(legacy, f)
                with open(users_file + ".legacy", "w", encoding="utf-8") as f:
                    f.write("older backup")

                proc = run_cli(["user", "list"], self.home)
                self.assertIn("older etesync-cli version", output(proc))
                self.assertNotIn("olduser", proc.stdout)
                for path in [users_file, users_file + ".legacy", old_cache]:
                    self.assertFalse(os.path.exists(path), path)

    # --- sync --------------------------------------------------------------------------

    def test_sync_and_list_collections(self):
        self.add_user()
        proc = self.sync()
        self.assertIn("Collections updated: 3, removed: 0", output(proc))
        self.assertIn("Items synced: 3", output(proc))

        listing = output(self.with_password(["list", self.username]))
        for header in ["# Address Books", "# Calendars", "# Tasks"]:
            self.assertIn(header, listing)
        for name in ["Test Contacts", "Test Calendar", "Test Tasks"]:
            self.assertIn(name, listing)

        if os.name == "posix":
            state_file = os.path.join(self.user_cache_dir(), "state.json")
            self.assertEqual(0o600, mode(state_file))
            self.assertEqual(0o700, mode(os.path.dirname(state_file)))

        # The local cache must only contain encrypted etebase blobs.
        for path in iter_files(os.path.join(self.cache_dir, "cache")):
            with open(path, "rb") as f:
                blob = f.read()
            self.assertNotIn(EVENT_MARKER.encode(), blob, path)
            self.assertNotIn(CONTACT_MARKER.encode(), blob, path)
            self.assertNotIn(EVENT1_UID.encode(), blob, path)

    def test_incremental_sync_is_a_noop(self):
        self.add_user()
        first = self.sync()
        self.assertIn("Collections updated: 3", output(first))
        second = self.sync()
        self.assertIn("Collections updated: 0, removed: 0", output(second))
        self.assertIn("Items synced: 0, removed: 0", output(second))

    def test_items_are_tracked_by_server_uid(self):
        # Two items sharing a vObject UID, and an item whose name changes after the first sync:
        # neither may lose or duplicate an item in the cache.
        username, service_password, account = new_account(self.server)
        col_mgr = account.get_collection_manager()
        cal = col_mgr.create("etebase.vevent", {"name": "Clashes", "mtime": millis()}, b"")
        col_mgr.upload(cal)
        item_mgr = col_mgr.get_item_manager(cal)

        def event(summary):
            return EVENT_TEMPLATE.replace(EVENT_MARKER, summary).encode()

        first = item_mgr.create({"name": "same@x", "mtime": millis()}, event("First twin"))
        second = item_mgr.create({"name": "same@x", "mtime": millis()}, event("Second twin"))
        renamed = item_mgr.create({"name": "old@x", "mtime": millis()}, event("Renamed one"))
        item_mgr.batch([first, second, renamed], None, None)

        self.add_user(username=username, service_password=service_password)
        self.assertIn("Items synced: 3", output(self.sync(username)))
        renamed.meta = {"name": "new@x", "mtime": millis()}
        item_mgr.batch([renamed], None, None)
        self.sync(username)
        second.delete()
        item_mgr.batch([second], None, None)
        self.assertIn("Items synced: 0, removed: 1", output(self.sync(username)))

        out = output(self.with_password(["list", username, cal.uid[:8]]))
        self.assertIn("Collection items: 2", out)
        self.assertEqual(1, out.count("First twin"))
        self.assertEqual(1, out.count("Renamed one"))
        self.assertNotIn("Second twin", out)

    def test_output_strips_terminal_control_sequences(self):
        username, service_password, account = new_account(self.server)
        col_mgr = account.get_collection_manager()
        evil = "Evil\x1b]0;title\x07\x1b[31m\u202eName"
        cal = col_mgr.create("etebase.vevent", {"name": evil, "mtime": millis()}, b"")
        col_mgr.upload(cal)
        item_mgr = col_mgr.get_item_manager(cal)
        content = EVENT_TEMPLATE.replace(EVENT_MARKER, "Party\x1b[2K\rtime").encode()
        item = item_mgr.create({"name": "evil\x1b[1m@x", "mtime": millis()}, content)
        item_mgr.batch([item], None, None)

        self.add_user(username=username, service_password=service_password)
        self.sync(username)
        outputs = [
            self.with_password(["list", username]),
            self.with_password(["list", username, cal.uid[:8]]),
            self.with_password(["journal", username, cal.uid[:8]]),
        ]
        for proc in outputs:
            self.assertNotIn("\x1b", output(proc))
            self.assertNotIn("\r", output(proc))
            self.assertNotIn("\u202e", output(proc))
        self.assertIn("Evil]0;title[31mName", output(outputs[0]))
        self.assertIn("Party[2Ktime", output(outputs[1]))

    # --- listing -----------------------------------------------------------------------

    def test_list_collection_contents(self):
        self.add_user()
        self.sync()
        proc = self.with_password(["list", self.username, self.cal_uid[:8]])
        out = output(proc)
        self.assertIn("Collection items: 1", out)
        self.assertIn(EVENT_MARKER + " (updated)", out)

    def test_list_requires_sync(self):
        self.add_user()
        for args in [["list", self.username], ["list", self.username, self.cal_uid[:8]]]:
            proc = self.with_password(args, expect=1)
            self.assertIn("run sync first", output(proc))

    def test_list_unknown_prefix(self):
        self.add_user()
        self.sync()
        proc = self.with_password(["list", self.username, "zzzzzzzz"], expect=1)
        self.assertIn("No collection matching", output(proc))

    # --- journal -----------------------------------------------------------------------

    def test_journal(self):
        self.add_user()
        proc = self.with_password(["journal", self.username, self.cal_uid[:8]])
        out = output(proc)
        self.assertIn("Journal items: 4", out)
        self.assertIn("Collection items: 1", out)
        # Two revisions of event-one, oldest first.
        self.assertGreaterEqual(out.count(EVENT1_UID), 2)
        self.assertLess(out.index(EVENT_MARKER), out.index("(updated)"))
        # event-two's deletion shows up as a deleted revision.
        self.assertIn(", deleted)", out)
        self.assertIn(EVENT2_UID, out)

    def test_journal_unknown_prefix(self):
        self.add_user()
        proc = self.with_password(["journal", self.username, "zzzzzzzz"], expect=1)
        self.assertIn("No collection matching", output(proc))

    # --- authentication failures -------------------------------------------------------

    def test_wrong_passphrase(self):
        self.add_user()
        proc = self.with_password(["sync", self.username], expect=1, password="wrong-password")
        self.assertIn("Wrong passphrase", output(proc))


if __name__ == "__main__":
    unittest.main()
