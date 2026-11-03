"""End-to-end tests for etesync-cli against a real Etebase server that allows signups
(e.g. the etesync-server test-server image).

There is deliberately no default server URL: set ETESYNC_TEST_API_URL explicitly to a
*disposable* test server, so the suite can't accidentally create accounts on a personal one.

Start a test server, then run from the repository root with the project virtualenv:

    podman run -d -p 127.0.0.1:3737:3735 test-server
    ETESYNC_TEST_API_URL=http://127.0.0.1:3737/ .venv/bin/python -m unittest -v tests.test_etesync_cli

Without ETESYNC_TEST_API_URL (or when the server is unreachable) the suite skips itself.

Every test gets its own temporary HOME, so the real ~/.etesync-cli is never touched. The CLI is
run through a subprocess with stdin piped and a detached session, so getpass() can't read from
the developer's terminal.
"""

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


class EndToEndTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SERVER_URL:
            raise unittest.SkipTest(
                "ETESYNC_TEST_API_URL is not set; point it at a disposable test server "
                "(see tests/test_etesync_cli.py docstring)")
        try:
            probe = SERVER_URL.rstrip("/") + "/api/v1/authentication/is_etebase/"
            with urllib.request.urlopen(probe, timeout=5):
                pass
        except Exception as e:
            raise unittest.SkipTest("test server not reachable at {}: {}".format(SERVER_URL, e))

        cls.server = SERVER_URL
        cls.username = "cli" + uuid.uuid4().hex[:10]
        cls.service_password = "service-pass-" + uuid.uuid4().hex
        cls.encryption_password = "enc-pass-" + uuid.uuid4().hex
        client = Client("etesync-cli-test", cls.server)
        account = Account.signup(client, User(cls.username, "test@example.com"),
                                 cls.service_password)
        cls.cal_uid, cls.ab_uid, cls.tasks_uid = seed(account)

    def setUp(self):
        home = tempfile.mkdtemp(prefix="etesync-cli-test-")
        self.addCleanup(shutil.rmtree, home, True)
        self.home = home
        self.cache_dir = os.path.join(home, ".etesync-cli")

    def add_user(self, expect=0, extra_args=()):
        return run_cli(["--server-url", self.server] + list(extra_args) +
                       ["user", "add", self.username], self.home,
                       stdin=self.service_password + "\n" + self.encryption_password + "\n",
                       expect=expect)

    def with_password(self, args, expect=0, password=None):
        return run_cli(args, self.home, stdin=(password or self.encryption_password) + "\n",
                       expect=expect)

    def sync(self):
        return self.with_password(["sync", self.username])

    # --- user management ---------------------------------------------------------------

    def test_add_user_and_list(self):
        self.add_user()
        listing = output(run_cli(["user", "list"], self.home))
        self.assertIn(self.username, listing)

        users_file = os.path.join(self.cache_dir, "users_cache")
        if os.name == "posix":
            self.assertEqual(0o700, mode(self.cache_dir))
            self.assertEqual(0o600, mode(users_file))

        # The stored material is ciphertext only.
        with open(users_file, "rb") as f:
            blob = f.read()
        self.assertNotIn(self.service_password.encode(), blob)
        self.assertNotIn(self.encryption_password.encode(), blob)
        self.assertIn(b'"salt"', blob)
        self.assertIn(b'"session"', blob)

        # Adding the same user twice is refused.
        proc = self.add_user(expect=1)
        self.assertIn("User already exists", output(proc))

    def test_add_user_rejects_unsafe_username(self):
        for bad in ["../evil", ".hidden", "has/slash"]:
            proc = run_cli(["--server-url", self.server, "user", "add", bad], self.home,
                           expect=1)
            self.assertIn("Invalid username", output(proc))

    def test_unknown_user(self):
        proc = run_cli(["sync", "ghost"], self.home, expect=1)
        self.assertIn("User doesn't exist", output(proc))

    def test_user_del_purges_cache(self):
        self.add_user()
        self.sync()
        self.assertTrue(os.path.isdir(os.path.join(self.cache_dir, "cache", self.username)))

        run_cli(["user", "del", self.username], self.home)
        self.assertFalse(os.path.exists(os.path.join(self.cache_dir, "cache", self.username)))
        listing = output(run_cli(["user", "list"], self.home))
        self.assertNotIn(self.username, listing)

        proc = run_cli(["user", "del", self.username], self.home, expect=1)
        self.assertIn("User doesn't exist", output(proc))

    def test_legacy_cache_migration(self):
        os.makedirs(self.cache_dir, exist_ok=True)
        legacy_file = os.path.join(self.cache_dir, "users_cache")
        with open(legacy_file, "w", encoding="utf-8") as f:
            json.dump({"users": {"olduser": "some-old-fernet-blob"}}, f)

        proc = run_cli(["user", "list"], self.home)
        self.assertIn("old format cache", output(proc))
        self.assertTrue(os.path.exists(legacy_file + ".legacy"))
        self.assertFalse(os.path.exists(legacy_file))

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
            state_file = os.path.join(self.cache_dir, "cache", self.username, "state.json")
            self.assertEqual(0o600, mode(state_file))
            self.assertEqual(0o700, mode(os.path.dirname(state_file)))

        # The local cache must only contain encrypted etebase blobs.
        for path in iter_files(os.path.join(self.cache_dir, "cache")):
            with open(path, "rb") as f:
                blob = f.read()
            self.assertNotIn(EVENT_MARKER.encode(), blob, path)
            self.assertNotIn(CONTACT_MARKER.encode(), blob, path)

    def test_incremental_sync_is_a_noop(self):
        self.add_user()
        first = self.sync()
        self.assertIn("Collections updated: 3", output(first))
        second = self.sync()
        self.assertIn("Collections updated: 0, removed: 0", output(second))
        self.assertIn("Items synced: 0", output(second))

    # --- listing -----------------------------------------------------------------------

    def test_list_collection_contents(self):
        self.add_user()
        self.sync()
        proc = self.with_password(["list", self.username, self.cal_uid[:8]])
        out = output(proc)
        self.assertIn("Collection items: 1", out)
        self.assertIn(EVENT_MARKER + " (updated)", out)

    def test_list_prefix_requires_sync(self):
        self.add_user()
        proc = self.with_password(["list", self.username, self.cal_uid[:8]], expect=1)
        self.assertIn("run sync first", output(proc))

        # The plain listing is live and needs no local cache.
        listing = output(self.with_password(["list", self.username]))
        self.assertIn("# Calendars", listing)
        self.assertIn("Test Calendar", listing)

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

    def test_wrong_encryption_password(self):
        self.add_user()
        proc = self.with_password(["sync", self.username], expect=1, password="wrong-password")
        self.assertIn("Wrong encryption password", output(proc))


if __name__ == "__main__":
    unittest.main()
