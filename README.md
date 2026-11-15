# Ciphwren CLI

A command line tool for accessing the collections (e.g. Calendars) of a Ciphwren account, on any
Etebase server, and their change journal.

> [!NOTE]
> **This is an independent fork** of [cli-client](https://github.com/etesync/cli-client), the
> original project of the EteSync authors. This fork is not affiliated with, endorsed by or
> supported by EteSync or Etebase in any way. The history of the original project is kept as it
> is, with its authors, and the changes of this fork follow it. Like the original, it's licensed
> under the [GPL-3.0](./LICENSE).

# Intro

This will at some point become a fully fledge tool, though at the moment it's quite raw.

The client has been rewritten on top of the Etebase API using the maintained
[`etebase-py`](https://github.com/nikossvnk/etebase-py) fork, which replaces the old and
unmaintained `etesync` Python package.

Security properties:

* The cached session (auth token, keys and server URL) is encrypted at rest with a key derived
  from a *local passphrase* you choose when adding the user (scrypt, random per-user salt). Etebase
  has no separate encryption password, so this passphrase only protects the local copy; it
  must differ from your login password.
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

The cache lives at ```~/.ciphwren-cli/``` (```users_cache``` for the sessions and ```cache/```
for the synced collections/items, one directory per user named by a hash of the username).

# Installation

Python 3.10-3.14 is required. (etebase-py 0.32+ builds against Python's stable ABI via PyO3, so
Python 3.14 works; older versions of the fork crashed on it.)

There's currently no installation process, just get the ```ciphwren-cli``` script from this repo,
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

```ciphwren-cli -h```

Add user. You'll be asked for your login password and to choose a local passphrase (at least 8
characters). There is no default server: give your server's URL with ```--server-url``` or the
```CIPHWREN_SERVER_URL``` environment variable:

```
ciphwren-cli user add --server-url https://etebase.example.com/ me@example.com
CIPHWREN_SERVER_URL=https://etebase.example.com/ ciphwren-cli user add me@example.com
```

Viewing your data:

Sync (sync is not automatic!)

```
$ ciphwren-cli sync me@example.com
Collections updated: 3, removed: 0
Items synced: 4, removed: 0
```

View the list of synced collections (requires a sync first):

```
$ ciphwren-cli list me@example.com
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
$ ciphwren-cli list me@example.com 666d0561
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
$ ciphwren-cli journal me@example.com 666d0561
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
$ ciphwren-cli user list
$ ciphwren-cli user del me@example.com
$ ciphwren-cli user del --local-only me@example.com
```

# Upgrading from older versions

This tool was called ```etesync-cli``` before. The first run of ```ciphwren-cli``` moves
```~/.etesync-cli/``` to ```~/.ciphwren-cli/``` (unless that already exists), and the old
```ETESYNC_SERVER_URL``` environment variable is still read. The old default server, EteSync's
hosted service, was removed: users you already added keep the server they were added with, and
new ones need ```--server-url``` or ```CIPHWREN_SERVER_URL```.

Session caches written by older versions (the etesync-based one, and the first etebase-based
one) are incompatible and are deleted on first use, together with their item cache and any
```users_cache.legacy``` backup. The plaintext cache at ```~/.etesync/data.db``` is no longer
used (you can delete it). Re-add your users with ```user add``` and run ```sync``` again.

# Development

Style check:

```
.venv/bin/python -m flake8 ciphwren-cli tests
```

## Testing

Offline tests of the local storage and the input/output hardening (no server needed):

```
.venv/bin/python -m unittest -v tests.test_unit
```

The suite in ```tests/test_ciphwren_cli.py``` is end-to-end: it creates throwaway accounts on a
real Etebase server and drives the CLI through a subprocess. There is deliberately no default
server URL, so it can never create accounts on a personal server — you must point it at a
*disposable* test server explicitly:

```
podman run -d -p 127.0.0.1:3737:3735 ghcr.io/nikossvnk/ciphwren-server/test-server
CIPHWREN_TEST_API_URL=http://127.0.0.1:3737/ .venv/bin/python -m unittest -v tests.test_ciphwren_cli
```

(The ```test-server``` image, ```ghcr.io/nikossvnk/ciphwren-server/test-server```, is built from
the ```docker/test-server/Dockerfile``` in
[ciphwren-server](https://github.com/nikossvnk/ciphwren-server); it allows signups.) Without
```CIPHWREN_TEST_API_URL``` — or if the server is unreachable — the suite skips itself; set
```CIPHWREN_TEST_REQUIRE_SERVER=1``` (e.g. in CI) to make that a failure instead. Every test gets a
temporary ```HOME```, so your real ```~/.ciphwren-cli``` is never touched.
