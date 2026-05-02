# Authoring Pages for a NomadPortal-Hosted Site

This guide covers what site authors need to know when writing pages that get served by NomadPortal's bundled NomadNet node.

The audience is people who want their `.mu` page to do something dynamic — render a list of items from a database, show the requesting user's fingerprint, react to form submissions, etc.

## File layout

```
site/
├── pages/
│   ├── index.mu          ← served at  /page/index.mu  (and /)
│   ├── about.mu          ← served at  /page/about.mu
│   └── games/
│       └── score.mu      ← served at  /page/games/score.mu
├── files/
│   └── logo.png          ← served at  /file/logo.png
├── lib/                  ← pip-installed packages live here  (managed by NomadPortal)
├── data/                 ← put your SQLite files / state here
└── requirements.txt      ← optional, see "Adding dependencies" below
```

The `pages/` and `files/` directories are scanned every 5 minutes so a new file becomes reachable without restarting the container.

## Static vs executable pages

A page is **static** unless it has the executable bit set. Static pages are sent as-is — fast, cached server-side.

A page is **executable** when:

```bash
chmod +x site/pages/myscript.mu
```

The first line should be a shebang:

```python
#!/usr/bin/env python3
```

NomadPortal runs the script as a subprocess; **stdout becomes the page body**, exactly as if the bytes had been read from a static file. Stderr is discarded. Exit codes other than zero still return whatever was printed.

There is no preprocessor — your script can output any Micron source, including the page-level directives `#!bg=…` and `#!fg=…` if you choose.

## Environment variables

NomadPortal sets these env vars before invoking your script:

| Variable | When set | Value |
|---|---|---|
| `node_destination` | always | This node's destination hex hash |
| `link_id` | when served over a Reticulum link | RNS link ID, hex |
| `remote_identity` | when the requester identified | requester's identity hex hash |
| `field_<name>` | per submitted field | form/var data attached to the request |
| `var_<name>` | per submitted var | URL- or link-attached vars |
| `PYTHONPATH` | always | `/site/lib` (so packages from `requirements.txt` import) |
| `PATH` | always | inherited from the container |

Read them with `os.environ.get("name", default)`.

A starter pattern for "show the requesting user":

```python
#!/usr/bin/env python3
import os

remote = os.environ.get("remote_identity", "")
if remote:
    print(f"`F0c6Welcome back, `f`F0ef{remote}`f")
else:
    print("`F888Anonymous browsing.`f Click the `★` button to identify.")
```

## Form submissions

When a user clicks a Micron link with field references — `[Save`/page/foo.mu`action=save|reg_name]` — NomadPortal's frontend collects the value of each named input and submits them all together. Your script reads them back as `var_<name>`:

```python
#!/usr/bin/env python3
import os, sys

action = os.environ.get("var_action", "")

if action == "save":
    name = os.environ.get("var_reg_name", "").strip()
    if not name:
        sys.exit_msg = "Name required"
        # ... handle error
    # ... store name somewhere
    print(f"`F0c6Saved as: `f`F0ef{name}`f")
else:
    # No action — show the form
    print("`<reg_name`>")
    print("`[Save`/page/register.mu`action=save|reg_name]")
```

> **Trailing `=` gotcha:** NomadNet's TUI accepts `<name=`>` as an alternative input syntax that NomadPortal and MeshChat *don't* parse correctly — the `=` ends up inside the input name. Use the `<name`>` form (no `=`) for portability.

## Adding Python packages

Drop a `site/requirements.txt`:

```
requests>=2.32
psycopg2-binary>=2.9
redis>=5.0
```

On the next container start, the entrypoint runs `pip install --target /site/lib -r site/requirements.txt`. First start is slower (download), subsequent starts are near-instant (pip skips already-satisfied entries). All packages are importable from your scripts via the `PYTHONPATH=/site/lib` env var that's already set up.

**The `requirements.txt` is fully trusted.** Anyone who can write to it can install any package and run any code. Treat it like the rest of `site/pages/` — only put trusted packages there.

## Persistent state

For local persistence: `site/data/` is just a writable directory in the volume. Use Python stdlib `sqlite3` — no install needed:

```python
#!/usr/bin/env python3
import sqlite3

conn = sqlite3.connect("/site/data/scores.sqlite")
conn.execute("CREATE TABLE IF NOT EXISTS scores (name TEXT PRIMARY KEY, points INT)")
for name, pts in conn.execute("SELECT * FROM scores ORDER BY points DESC LIMIT 10"):
    print(f"{name}: {pts}")
conn.close()
```

For external databases (Postgres, MySQL, Redis), add the driver to `requirements.txt` and connect via host networking from inside the script. The container can reach `host.docker.internal` on most platforms, or whatever IP your DB is bound to.

## Linking and field references

Standard Micron link forms:

```
`[Label`url]                          → simple link
`[Label`url`field=value]              → submit field=value on click
`[Label`url`field=value|input1|input2] → submit field=value AND each named input's value
`[Label`url`*]                         → submit all inputs on the page
```

The `|`-separated input names refer to the `name` attribute of `<...>` inputs above the link. NomadPortal tries to find each by exact match against `<input name="...">`.

## Debugging

- **stdout** becomes the page body — what you `print()` is what users see.
- **stderr** is discarded by the site server. Use `print(..., file=sys.stderr)` only if you're running the script outside NomadPortal.
- For detailed errors **don't** rely on the page body — log to a file:

  ```python
  import logging
  logging.basicConfig(filename="/site/data/myscript.log", level=logging.INFO)
  log = logging.getLogger(__name__)
  log.info("Got action: %s", action)
  ```

- The script runs as the same user as the NomadPortal process. It can read/write anywhere that user has access.

## Caching

NomadPortal caches the output of executable pages, keyed by `(node_hash, path, requester_identity)`. Two consequences:

- The same logged-in user hitting the same page within `CACHE_TTL` (default 300 s) gets the cached output. **Don't rely on side effects firing every request.**
- Different users see independently cached output, so you can render per-user content without leaking.

If you need to bust the cache during development: `CACHE_TTL: 1` in `docker-compose.yml`, restart.

## Security boundary

Anything in `site/pages/` runs as the NomadPortal process. There's no sandboxing — your scripts can read every file the process can, open any port, install anything. **The model is "the operator writes the scripts."** Don't accept user-uploaded `.mu` pages.

Field values from network requests **are** untrusted — sanitise them before using in shell commands, SQL queries, or filesystem paths. Standard practice.
