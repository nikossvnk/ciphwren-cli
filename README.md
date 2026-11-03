A command line tool for accessing the EteSync journal and collections (e.g. Calendars)

# Intro

This will at some point become a fully fledge tool, though at the moment it's quite raw.

The client has been rewritten on top of the Etebase API using the maintained
[`etebase-py`](https://github.com/nikossvnk/etebase-py) fork, which replaces the old and
unmaintained `etesync` Python package.

Security properties:

* The cached session (auth token and keys) is encrypted at rest with a key derived from your
  encryption password (PBKDF2-HMAC-SHA256, 600k iterations, random salt stored next to the data).
* The local item cache is encrypted too: it stores etebase's encrypted blobs, so it is not
  readable without your encryption password. (The old cache at ```~/.etesync/data.db``` was
  stored in plaintext.)
* All files are created with restrictive permissions (```0700``` directories, ```0600``` files).
* Passwords are always prompted for on the terminal. The old ```ETESYNC_ENC_PASS``` environment
  variable was removed: environment variables are visible to child processes and via ```/proc```.

The cache lives at ```~/.etesync-cli/``` (```users_cache``` for the sessions and ```cache/```
for the synced collections/items).

# Installation

Python 3.10-3.13 is required (the etebase-py fork doesn't support Python 3.14 yet).

There's currently no installation process, just get the ```etesync-cli``` script from this repo,
create a virtualenv and install the dependencies:

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # or: uv pip install --python .venv/bin/python -r requirements.txt
```

Building `etebase` from source requires a Rust toolchain ([rustup](https://rustup.rs/)) and Perl
modules for the vendored OpenSSL build. On Fedora, if the OpenSSL build fails:

```
dnf install perl-IPC-Cmd perl-ExtUtils-MakeMaker
```

# Usage

Help:

```etesync-cli -h```

Add user (you'll be asked for your EteSync login password and your encryption password; use
```--server-url``` or the ```ETESYNC_SERVER_URL``` environment variable for a self-hosted server):

```etesync-cli user add me@etesync.com```

Viewing your data:

Sync (sync is not automatic!)

```
$ etesync-cli sync me@etesync.com
Collections updated: 3, removed: 0
Items synced: 4
```

View the list of collections:

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

View the change journal of a collection (fetched live from the server, oldest revision first):

```
$ etesync-cli journal me@etesync.com 666d0561
Journal items: 4
Collection items: 1

# Items
4d7be429-e311-4da9-bc34-87c4a01dc37f (mtime: 2017-03-24 16:50:21+00:00)
 VCALENDAR
    ...
```

Manage cached users:

```
$ etesync-cli user list
$ etesync-cli user del me@etesync.com
```

# Upgrading from the old (etesync-based) version

Sessions cached by the old version are incompatible with etebase. The old cache file is renamed
to ```~/.etesync-cli/users_cache.legacy``` on first use, and the plaintext cache at
```~/.etesync/data.db``` is no longer used (you can delete it). Re-add your users with
```user add``` and run ```sync``` again.

# Development

Style check:

```
.venv/bin/python -m flake8 etesync-cli tests
```

## Testing

The suite in ```tests/test_etesync_cli.py``` is end-to-end: it creates a throwaway account on a
real Etebase server and drives the CLI through a subprocess. There is deliberately no default
server URL, so it can never create accounts on a personal server — you must point it at a
*disposable* test server explicitly:

```
podman run -d -p 127.0.0.1:3737:3735 test-server
ETESYNC_TEST_API_URL=http://127.0.0.1:3737/ .venv/bin/python -m unittest -v tests.test_etesync_cli
```

(The ```test-server``` image is built from the ```docker/test-server/Dockerfile``` in
etesync-server; it allows signups.) Without ```ETESYNC_TEST_API_URL``` — or if the server is
unreachable — the suite skips itself. Every test gets a temporary ```HOME```, so your real
```~/.etesync-cli``` is never touched.
