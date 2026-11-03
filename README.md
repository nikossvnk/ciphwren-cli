A command line tool for accessing the EteSync journal and collections (e.g. Calendars)

# Intro

This will at some point become a fully fledge tool, though at the moment it's quite raw.

The client has been rewritten on top of the Etebase API using the maintained
[`etebase-py`](https://github.com/nikossvnk/etebase-py) fork, which replaces the old and
unmaintained `etesync` Python package.

Security properties:

* The cached session (auth token, keys and server URL) is encrypted at rest with a key derived
  from a *local passphrase* you choose when adding the user (scrypt, random per-user salt). EteSync
  2.0 has no separate encryption password, so this passphrase only protects the local copy; it
  must differ from your EteSync password.
* The local item cache is encrypted too: it stores etebase's encrypted blobs, so it is not
  readable without your passphrase. (The old cache at ```~/.etesync/data.db``` was stored in
  plaintext.)
* All files are created with restrictive permissions (```0700``` directories, ```0600``` files)
  and written atomically; concurrent runs are serialized with a lock file.
* Passwords are always prompted for on the terminal. The old ```ETESYNC_ENC_PASS``` environment
  variable was removed: environment variables are visible to child processes and via ```/proc```.
* Everything printed from the server (collection names, item contents, errors) is stripped of
  terminal control characters, so a shared collection can't inject escape sequences.
* ```user del``` logs the session out on the server, so its auth token stops working.
* Plain ```http://``` server URLs are refused for non-local servers unless ```--allow-http``` is
  given.

The cache lives at ```~/.etesync-cli/``` (```users_cache``` for the sessions and ```cache/```
for the synced collections/items, one directory per user named by a hash of the username).

# Installation

Python 3.10-3.14 is required. (etebase-py 0.32+ builds against Python's stable ABI via PyO3, so
Python 3.14 works; older versions of the fork crashed on it.)

There's currently no installation process, just get the ```etesync-cli``` script from this repo,
create a virtualenv and install the dependencies:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or: uv pip install --python .venv/bin/python -r requirements.txt
```

Building `etebase` from source requires a Rust toolchain ([rustup](https://rustup.rs/)) and Perl
modules for the vendored OpenSSL build. On Fedora, if the OpenSSL build fails:

```
dnf install perl-IPC-Cmd perl-ExtUtils-MakeMaker perl-Time-Piece
```

The package builds with [maturin](https://www.maturin.rs/) (PEP 517); ```pip``` fetches it
automatically while building.

# Usage

Help:

```etesync-cli -h```

Add user. You'll be asked for your EteSync login password and to choose a local passphrase
(at least 8 characters); use ```--server-url``` or the ```ETESYNC_SERVER_URL``` environment
variable for a self-hosted server:

```
etesync-cli user add me@etesync.com
etesync-cli user add --server-url https://etebase.example.com/ me@example.com
```

Viewing your data:

Sync (sync is not automatic!)

```
$ etesync-cli sync me@etesync.com
Collections updated: 3, removed: 0
Items synced: 4, removed: 0
```

View the list of synced collections (requires a sync first):

```
$ etesync-cli list me@etesync.com
# Address Books
        Default (0530a37fd91426293759cea8aa8d330f9945f323254236d1472e94aeef6a6e8f)

# Calendars
        Default (b71504d4fef88e685472bb7a77f10c338d7e6bef9041994e3809e7622727f5a3)
        Reminders (666d05611f46b820831ef75d6e6f58098272c12b793e9b33a8476362b3704e3f)

# Tasks
        My Tasks (26597d22d0da9e1b1d7d0c3b4a02a75f4a65e09536f8f4eeb9203a6d8e0b7f31)
```

View the contents of a collection (requires a sync first; the argument is the collection UID or
any prefix of it):

```
$ etesync-cli list me@etesync.com 666d0561
Collection items: 1

# Items
 VCALENDAR
    VERSION: 2.0
    ...
```

View the change journal of a collection (fetched live from the server, oldest revision first by
modification time). Etebase has no collection-wide history, so this makes one request per item
and is slow for large collections:

```
$ etesync-cli journal me@etesync.com 666d0561
Journal items: 4
Collection items: 1

# Items
4d7be429-e311-4da9-bc34-87c4a01dc37f (mtime: 2017-03-24 16:50:21+00:00)
 VCALENDAR
    ...
```

Manage cached users (```user del``` asks for the passphrase to log the session out on the server;
```--local-only``` skips that, leaving the session valid on the server):

```
$ etesync-cli user list
$ etesync-cli user del me@etesync.com
$ etesync-cli user del --local-only me@etesync.com
```

# Upgrading from older versions

Session caches written by older versions (the etesync-based one, and the first etebase-based
one) are incompatible and are deleted on first use, together with their item cache and any
```users_cache.legacy``` backup. The plaintext cache at ```~/.etesync/data.db``` is no longer
used (you can delete it). Re-add your users with ```user add``` and run ```sync``` again.

# Development

Style check:

```
.venv/bin/python -m flake8 etesync-cli tests
```

## Testing

Offline tests of the local storage and the input/output hardening (no server needed):

```
.venv/bin/python -m unittest -v tests.test_unit
```

The suite in ```tests/test_etesync_cli.py``` is end-to-end: it creates throwaway accounts on a
real Etebase server and drives the CLI through a subprocess. There is deliberately no default
server URL, so it can never create accounts on a personal server — you must point it at a
*disposable* test server explicitly:

```
podman run -d -p 127.0.0.1:3737:3735 test-server
ETESYNC_TEST_API_URL=http://127.0.0.1:3737/ .venv/bin/python -m unittest -v tests.test_etesync_cli
```

(The ```test-server``` image is built from the ```docker/test-server/Dockerfile``` in
etesync-server; it allows signups.) Without ```ETESYNC_TEST_API_URL``` — or if the server is
unreachable — the suite skips itself; set ```ETESYNC_TEST_REQUIRE_SERVER=1``` (e.g. in CI) to make
that a failure instead. Every test gets a temporary ```HOME```, so your real ```~/.etesync-cli```
is never touched.
