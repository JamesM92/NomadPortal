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

### The separator rule — this is the bug you will hit

If your script needs to append its own parameter to a link that already has a field spec (a session token is the common case — NomadNet requests carry no cookies or headers you control, so a token has to travel in the link itself if you're building login/sessions on top of `remote_identity`), the separator you use depends on whether a field-spec section already exists on that link:

```
`[Label`url`tkn=abc123]              ← no existing field spec: backtick introduces it
`[Label`url`action=save`tkn=abc123]  ← WRONG: a second backtick starts a new field-spec
                                         section instead of extending the first — most
                                         renderers only honor the first one
`[Label`url`action=save|tkn=abc123]  ← RIGHT: pipe-separate within the existing section
```

Two failure modes, both real, both silent in the sense that nothing errors — the link just doesn't do what you wanted:

- A `|` that appears **before** any backtick has introduced a field-spec section gets parsed as part of the *filename* — NomadNet answers "Page not found" for the whole link.
- A link with **4 or more backtick-separated parts** breaks MeshChat's parser specifically (NomadNet itself tolerates it). If a link already carries a field spec and you need to add another parameter, join it with `|`, never with another backtick.

The practical rule: count backticks before deciding. If the link body (everything before the closing `]`) already contains two backticks (label, url, and one field-spec section), your next parameter joins with `|`. Only reach for a third backtick if the link genuinely has no field spec yet.

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

### Telling readers how long a page stays current: `#!c=N`

Start a page with `#!c=<seconds>` to say how long a copy stays fresh. This is the same directive NomadNet reads.

```
#!c=3600
>Opening hours
...
```

- **`#!c=3600`**: for one hour after a copy is saved, NomadPortal shows it as it is and does not ask the network. The reader sees a green dot.
- **No directive**: every visit shows the saved copy at once and checks for a newer one in the background.
- **`#!c=0`**: every visit checks for a newer copy. A saved copy is still kept, so the page can show if the network is down.
- The Refresh button and the auto-refresh timer always fetch a new copy.
- The in-memory page cache above uses the same number as its lifetime for that page. The longest value used is 30 days.

Put the line at the top of the page, in the first 20 lines, with nothing else on it.

## Restricting a page: `.allowed` files

To limit a page to certain people, put a file with the same name plus `.allowed` next to it. This is the NomadNet format.

```
site/pages/members.mu
site/pages/members.mu.allowed
```

`members.mu.allowed` lists one identity hash per line. An identity hash is 32 hexadecimal characters.

```
a1b2c3d4e5f60718293a4b5c6d7e8f90
0f1e2d3c4b5a69788796a5b4c3d2e1f0
```

- Only a requester who has **identified** and whose hash is in the list gets the page. Everyone else sees "Request Not Allowed". The page is not run or read for them.
- The same works for a file in `site/files/`: `report.pdf.allowed` restricts `report.pdf`.
- If the `.allowed` file is executable, it is run and its output is the list. Use this to build the list from your own data. The script has 10 seconds to finish.
- **If the list cannot be read, nobody gets in.** This covers a missing read permission, a script that fails or hangs, and a list with no valid hash.
- The access list itself is never served, and restricted pages are left out of the sitemap and out of the text shown to search engines.
- On the web interface, a signed-in reader counts as identified only when they have turned on **Identify** (the fingerprint button) for this site. Readers who are not signed in are always refused a restricted page.
- Lines that are not a valid hash are ignored. Hashes may be upper or lower case.

## Images

Show an image with an image line. The line must start with `` `( `` and have nothing else on it.

```
`(The site logo`w=40`a=c`:/media/logo.webp)
```

The parts, separated by backticks: the **alt text** (shown while the image loads, and if it cannot load), optional settings, and the **address** of the image last.

| Setting | Meaning |
|---------|---------|
| `w=40` | Width in characters. `w=50%` is a share of the page width. `w=n` is the image's own size. |
| `h=10` | Height in rows. |
| `a=l`, `a=c`, `a=r` | Align left, centre or right. |

- Put the image file in the **same folder as your pages** (`site/pages/logo.webp`). The address `:/media/logo.webp` points to it. A folder works too: `:/media/art/logo.webp`.
- Use **WebP** files. Over the Reticulum network only WebP is sent, and other NomadNet programs ignore everything else. Convert a picture with, for example, `cwebp photo.jpg -o photo.webp`.
- To use an image from another node, write its address before the colon: `` `(Logo`<node hash>:/media/logo.webp) ``.
- An image can be restricted like a page: put a `.allowed` file beside it (see *Restricting a page*).
- Images are limited to 2 MB. Larger images are not shown.
- Only `/media/` paths work. A web address (`https://...`) is not loaded, and the alt text is shown instead. Visitors who are locked to your site also see only the alt text for images on other nodes.
- Search engines and link previews see the alt text, not the picture.
- A line inside a literal block (`` `= ``) or a table is not an image, the same as in NomadNet.

## Partials

A partial shows part of another page inside your page, without leaving it. Use it for a news box, a status line or a counter.

```
`{/page/news.mu`30`pid=news}
```

The three parts, separated by backticks, are: the page to show, how often to reload it in seconds (leave it out or use `0` for never), and a list of settings separated by `|`.

- `pid=news` gives the partial a name. A link can then reload it: `` `[Reload`p:news] `` reloads every partial named `news`. Several names can be listed: `p:news|status`.
- Other `key=value` settings are sent to the target page as fields, the same as a form button. Use them only for the same node as your page.
- The shortest reload time is 10 seconds, and reloading stops while the browser tab is hidden or the reader leaves the page.
- A page can show at most 10 partials. A partial can contain partials, but only two levels deep. Anything beyond that shows as plain text.
- If a partial cannot load, the reader sees a short note. If a reload fails, the last good content stays on screen.
- Search engines and link previews see the plain `[live]` text, not the loaded content.

## Security boundary

Anything in `site/pages/` runs as the NomadPortal process. There's no sandboxing — your scripts can read every file the process can, open any port, install anything. **The model is "the operator writes the scripts."** Don't accept user-uploaded `.mu` pages.

Field values from network requests **are** untrusted — sanitise them before using in shell commands, SQL queries, or filesystem paths. Standard practice.
