"""Offline tests for etesync-cli's local storage and input/output hardening.

The script has no .py extension, so it is loaded through SourceFileLoader. Each test points the
module's paths at a temporary directory.

    .venv/bin/python -m unittest -v tests.test_unit
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cli():
    loader = importlib.machinery.SourceFileLoader("etesync_cli", os.path.join(REPO_ROOT, "etesync-cli"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


cli = load_cli()


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="etesync-cli-unit-")
        self.addCleanup(shutil.rmtree, self.root, True)
        home_dir = os.path.join(self.root, ".etesync-cli")
        paths = {
            "HOME_DIR": home_dir,
            "USERS_FILE": os.path.join(home_dir, "users_cache"),
            "LEGACY_USERS_FILE": os.path.join(home_dir, "users_cache.legacy"),
            "CACHE_DIR": os.path.join(home_dir, "cache"),
            "LOCK_FILE": os.path.join(home_dir, "lock"),
        }
        for name, value in paths.items():
            self.addCleanup(setattr, cli, name, getattr(cli, name))
            setattr(cli, name, value)
        os.makedirs(home_dir)

    def write_users(self, data):
        with open(cli.USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)


class SanitizeTest(unittest.TestCase):
    def test_strips_terminal_controls_keeps_text(self):
        text = "a\tb\nc\r\nd\x1b]0;t\x07e\x1b[2Kf\rg\x9bh\x7fi\u202ej\u2067k Ünïcødé"
        self.assertEqual("a\tb\nc\nd]0;te[2Kfghijk Ünïcødé", cli.sanitize(text))

    def test_decoded_vobject_values_are_sanitized(self):
        # Quoted-printable and base64 values only contain escapes after vobject decodes them.
        vcard = ("BEGIN:VCARD\r\nVERSION:2.1\r\nN:Doe;Jane\r\n"
                 "FN;ENCODING=QUOTED-PRINTABLE:Jane=1B[31mRED\r\n"
                 "NOTE;ENCODING=BASE64:G1szMW1oaQ==\r\nEND:VCARD\r\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.print_content(vcard.encode())
        self.assertIn("Jane[31mRED", buf.getvalue())
        self.assertNotIn("\x1b", buf.getvalue())

    def test_malformed_content_is_printed_sanitized(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.print_content(b"not a vobject \x1b[2J")
        self.assertEqual("not a vobject [2J\n\n", buf.getvalue())


class ValidationTest(unittest.TestCase):
    def test_usernames_follow_server_rules_and_fold_case(self):
        self.assertEqual("me@example.com", cli.user_key("Me@Example.com"))
        self.assertEqual("ünï_c.o-d+e", cli.user_key("Ünï_c.o-d+e"))
        for bad in ["", "a/b", "../x", "a b", "a\x1bb", "x" * 151]:
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "Invalid username"):
                cli.user_key(bad)

    def test_server_url(self):
        for ok in ["https://etebase.example.com/", "http://localhost:3735/", "http://127.0.0.1/",
                   "http://[::1]:8000/"]:
            self.assertEqual(ok, cli.validate_server_url(ok))
        self.assertEqual("http://lan.example/", cli.validate_server_url("http://lan.example/", True))
        with self.assertRaisesRegex(RuntimeError, "unencrypted"):
            cli.validate_server_url("http://lan.example/")
        for bad in ["ftp://example.com/", "example.com", "https://"]:
            with self.subTest(bad=bad), self.assertRaisesRegex(RuntimeError, "Invalid server URL"):
                cli.validate_server_url(bad)


class LegacyStateTest(CliTestCase):
    def test_older_formats_are_deleted_with_their_cache(self):
        for legacy in [{"users": {"old": "fernet-blob"}},
                       {"version": 2, "salt": "c2FsdA==", "users": {"old": {"session": "x"}}}]:
            with self.subTest(legacy=legacy):
                self.write_users(legacy)
                os.makedirs(os.path.join(cli.CACHE_DIR, "old"))
                with open(cli.LEGACY_USERS_FILE, "w") as f:
                    f.write("backup")
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    cli.remove_legacy_state()
                self.assertIn("older etesync-cli version", err.getvalue())
                for path in [cli.USERS_FILE, cli.LEGACY_USERS_FILE, cli.CACHE_DIR]:
                    self.assertFalse(os.path.exists(path), path)
                self.assertEqual({}, cli.UsersCache().users)

    def test_current_and_newer_formats_are_kept(self):
        for version in [cli.USERS_VERSION, cli.USERS_VERSION + 1]:
            with self.subTest(version=version):
                self.write_users({"version": version, "users": {"me": {}}})
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    cli.remove_legacy_state()
                self.assertEqual("", err.getvalue())
                self.assertTrue(os.path.exists(cli.USERS_FILE))
        with self.assertRaisesRegex(RuntimeError, "newer etesync-cli"):
            cli.UsersCache()

    def test_corrupt_users_file_is_reported_not_deleted(self):
        for content in ["", "{not json", "[]"]:
            with self.subTest(content=content):
                with open(cli.USERS_FILE, "w") as f:
                    f.write(content)
                cli.remove_legacy_state()
                with self.assertRaisesRegex(RuntimeError, "Corrupt users cache"):
                    cli.UsersCache()
                self.assertTrue(os.path.exists(cli.USERS_FILE))


class StorageTest(CliTestCase):
    def test_users_cache_round_trip_is_private(self):
        users = cli.UsersCache()
        users.set("me@example.com", {"session": "s"})
        users.save()
        self.assertEqual({"me@example.com": {"session": "s"}}, cli.UsersCache().users)
        self.assertEqual([], [n for n in os.listdir(cli.HOME_DIR) if n.endswith(".tmp")])
        if os.name == "posix":
            self.assertEqual(0o600, stat.S_IMODE(os.stat(cli.USERS_FILE).st_mode))

    def test_session_key_depends_on_passphrase_and_salt(self):
        entry = {"salt": "c2FsdHNhbHRzYWx0c2FsdA==",
                 "kdf": {"name": "scrypt", "n": 2 ** 10, "r": 8, "p": 1}}
        key = cli.entry_key(entry, "passphrase")
        self.assertEqual(cli.KEY_SIZE, len(key))
        self.assertEqual(key, cli.entry_key(dict(entry), "passphrase"))
        self.assertNotEqual(key, cli.entry_key(entry, "passphrasf"))
        self.assertNotEqual(key, cli.entry_key(dict(entry, salt="b3RoZXJzYWx0"), "passphrase"))
        with self.assertRaisesRegex(RuntimeError, "Corrupt session cache entry"):
            cli.entry_key({"salt": entry["salt"], "kdf": {"name": "pbkdf2"}}, "passphrase")

    def test_cache_paths_hide_username_and_reject_unsafe_uids(self):
        cache = cli.LocalCache("me@example.com")
        self.assertNotIn("me@example.com", cache.dir)
        self.assertEqual(cli.CACHE_DIR, os.path.dirname(cache.dir))
        for bad in ["../escape", "a/b", ".hidden", ""]:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(RuntimeError, "Invalid UID"):
                    cache.save_collection(bad, b"x")
                with self.assertRaisesRegex(RuntimeError, "Invalid UID"):
                    cache.save_item("col", bad, b"x")
        self.assertEqual([], os.listdir(cache.cols_dir))

    def test_items_keyed_by_uid_and_stale_layouts_reset(self):
        cache = cli.LocalCache("me@example.com")
        cache.save_state(cache.state())
        cache.save_item("col", "item1", b"one")
        cache.save_item("col", "item2", b"two")
        self.assertEqual(["item1", "item2"], [os.path.basename(p) for p in cache.item_paths("col")])
        self.assertTrue(cache.remove_item("col", "item1"))
        self.assertFalse(cache.remove_item("col", "item1"))

        with open(cache.state_file, "w") as f:
            json.dump({"collections_stoken": "old", "items": {}}, f)  # No version: old layout.
        self.assertIsNone(cache.state()["collections_stoken"])
        self.assertFalse(os.path.exists(cache.dir))


if __name__ == "__main__":
    unittest.main()
