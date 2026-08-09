#!/usr/bin/env python3
"""NomadPortal showcase landing page (executable Micron page).

The site_server runs this script when the file has the execute bit set;
stdout becomes the page body. We use that to print a small request-context
block at the top, then hand off to the static showcase content below.

Env vars set by site_server:
  node_destination  — this node's destination hex hash
  link_id           — RNS link ID hex (only set when served over a link)
  remote_identity   — requester's identity hex (only set if identified)
  field_* / var_*   — any field/var data submitted with the request
"""
import os
import sys


def _line(label, value):
    return f"`F8cf{label}`f   {value}"


def _esc_micron(s: str) -> str:
    """Escape Micron control chars so user-typed text renders verbatim."""
    return (s or "").replace("\\", "\\\\").replace("`", "\\`")


def _render_submission_result():
    """When the user clicks one of the form-submit demo links, render a
    focused result page showing what was submitted and exit. No state is
    persisted — this is a pure echo back to the requester to demonstrate
    the click → POST → executable-page round-trip.

    Submitted fields arrive as `var_<name>` env vars. The password field
    is masked to `******` regardless of the actual length so the value
    typed never appears in the rendered page (it still travels in plain
    text inside the request, hence the demo-only warning in the form).
    """
    action = os.environ.get("var_action", "")
    if action not in ("submit_form", "submit_all"):
        return False

    print("#!bg=111")
    print("#!fg=ccc")
    print()
    print("`c`F4af╔══════════════════════════════════════════════════╗`f")
    print("`c`F4af║`f       `F4af`!F O R M   S U B M I S S I O N`!`f              `F4af║`f")
    print("`c`F4af╚══════════════════════════════════════════════════╝`f")
    print()
    print("`l")
    print()
    print(f"`F8cfYou clicked `f`F4af`!{action.replace('_', ' ')}`!`f`F8cf. The fields the link")
    print("forwarded to this script are echoed below.`f")
    print()
    print("-=")
    print()

    has_password = bool(os.environ.get("var_password"))

    if action == "submit_form":
        # The "Submit form" link only forwards username + password, so
        # only those two values arrive — anything else is irrelevant.
        username = os.environ.get("var_username", "")
        print("`F4af`!Username:`!`f       "
              f"`Ffd0{_esc_micron(username) or '(empty)'}`f")
        print("`F4af`!Password:`!`f       "
              f"`Ffd0{'******' if has_password else '(empty)'}`f")
    else:
        # submit_all: show every input the form rendered.
        username = os.environ.get("var_username", "")
        agree    = os.environ.get("var_agree")  # "yes" if checked, else None
        color    = os.environ.get("var_color", "")
        print("`F4af`!Username:`!`f       "
              f"`Ffd0{_esc_micron(username) or '(empty)'}`f")
        print("`F4af`!Password:`!`f       "
              f"`Ffd0{'******' if has_password else '(empty)'}`f")
        print("`F4af`!Agree (chk):`!`f    "
              f"`Ffd0{'checked' if agree else 'not checked'}`f")
        print("`F4af`!Colour (rad):`!`f   "
              f"`Ffd0{_esc_micron(color) or '(none selected)'}`f")

    print()
    print("-=")
    print()
    print("`c`F8cfNothing was saved — this page is a pure echo of the request.`f")
    print()
    print("`c`F4af`[← Back to the showcase`/page/index.mu]`f")
    return True


# Render the focused result view if this is a form submission, then exit
# so the rest of the showcase doesn't render below it.
if _render_submission_result():
    sys.exit(0)


# ---------------------------------------------------------------------------
# Page-level directives must come first
# ---------------------------------------------------------------------------
print("#!bg=111")
print("#!fg=ccc")
print()

# ---------------------------------------------------------------------------
# Static intro — banner + tagline. Comes before the dynamic block so the
# page leads with branding, not metadata.
# ---------------------------------------------------------------------------
sys.stdout.write(r"""`c`F4af╔══════════════════════════════════════════════════╗`f
`c`F4af║`f                                                  `F4af║`f
`c`F4af║`f             `F4af`!N O M A D   P O R T A L`!`f              `F4af║`f
`c`F4af║`f       `FbbbA NomadNet node served over the web`f        `F4af║`f
`c`F4af║`f                                                  `F4af║`f
`c`F4af╚══════════════════════════════════════════════════╝`f

`l

A web-based browser for `[NomadNet`https://github.com/markqvist/NomadNet] nodes,
packaged in Docker. Browse distributed NomadNet content, send LXMF messages,
and manage identities — all from a standard web browser.

Built on `[Reticulum`https://reticulum.network] and `[LXMF`https://github.com/markqvist/LXMF].

-=

""")

# ---------------------------------------------------------------------------
# Dynamic request-context block — sits just under the intro paragraph.
# ---------------------------------------------------------------------------
node_dest = os.environ.get("node_destination") or "(unknown — site server still starting)"

# Two possible delivery modes — Direct (local NomadPortal user) vs RNS
# (request arrived over a Reticulum link). The active tag is rendered
# bold-bright; the inactive one is dimmed.
is_rns = bool(os.environ.get("link_id"))


def _tag(label, active):
    if active:
        return f"`F4af`!◆ {label}`!`f"
    return f"`F555◇ {label}`f"


connection_tags = f"{_tag('Direct', not is_rns)}   {_tag('RNS', is_rns)}"

remote_id = os.environ.get("remote_identity")
if remote_id:
    identity_line = f"`F8fc{remote_id}`f  (identified)"
else:
    identity_line = "`F888Anonymous`f — no fingerprint sent"

print("`B221`F4af  ◈ Request context  `f`b")
print(_line("Node address ", f"`F4af{node_dest}`f"))
print(_line("Connection   ", connection_tags))
print(_line("Your identity", identity_line))
print()
print("-=")
print()

# ---------------------------------------------------------------------------
# Static showcase — disclaimer onwards
# ---------------------------------------------------------------------------
sys.stdout.write(r""">`F600`!Disclaimer`!`f

This software is provided `!"as is"`!, without warranty of any kind. The author
accepts `Ffa4no risk and no liability`f for any use, misuse, or consequences
arising from the use of this project.

Anyone who installs, runs, hosts, or otherwise uses this software does so
`!entirely at their own risk and on their own responsibility`! — including but
not limited to compliance with applicable laws, the content accessed or
transmitted through it, and any harm resulting from its operation.

""")

# ---------------------------------------------------------------------------
# Architecture diagram with a dynamic dark-blue "you are here" highlight.
# Direct connection → highlight above the Web Browser box.
# RNS connection    → highlight under the network line.
# The diagram is rendered as plain Micron lines (no `\`=…`\`= literal
# wrapper) so (a) color tokens on the marker apply, and (b) closing `\`=
# doesn't leave a phantom blank row between LoRa and the bottom marker.
# Whitespace is preserved by white-space:pre on .mu-line.
# ---------------------------------------------------------------------------

def _marker(arrows, indent):
    return f"{' ' * indent}`B116`! {arrows} you are here {arrows} `!`b"


_DIAGRAM = [
    "                    ┌─────────────────────┐",
    "                    │     Web Browser     │  ← the internet",
    "                    └──────────┬──────────┘",
    "                               │ HTTPS",
    "                    ┌──────────┴──────────┐",
    "                    │     NomadPortal     │  ← this software",
    "                    └──────────┬──────────┘",
    "                               │ Reticulum RNS",
    "                    ┌──────────┴──────────┐",
    "                    │    NomadNet Mesh    │  ← the network",
    "                    └─────────────────────┘",
    "                   LoRa · TCP · Serial · I2P",
]

if not is_rns:
    print(_marker("▼▼▼", 20))
for line in _DIAGRAM:
    print(line)
if is_rns:
    print(_marker("▲▲▲", 20))

# Resume the static showcase
sys.stdout.write(r"""
-=

>`!Features`!
`F4a4◆`f Browse NomadNet pages rendered from Micron markup to HTML
`F4a4◆`f Send and receive LXMF messages, with per-user inboxes
`F4a4◆`f Per-user contact books with MeshChat icon support
`F4a4◆`f Manage RNS identities and announce to the mesh
`F4a4◆`f Node discovery via Reticulum announces
`F4a4◆`f HTTPS with auto-generated self-signed certificate
`F4a4◆`f Local username/password login or OIDC/SSO (Keycloak, Authentik, Auth0, Google)
`F4a4◆`f Admin panel for interfaces, users, node blocklist, and diagnostics
`F4a4◆`f Micron-formatted application title with live preview
`F4a4◆`f Mobile-responsive layout
`F4a4◆`f Rate limiting and CSRF protection throughout

-=

>`!Quick Start`!
`F4af`!Requirements`!`f

`Fbbb◆`f Docker and Docker Compose v2
`Fbbb◆`f A Reticulum interface reachable from the host (TCP, LoRa, etc.)

-~

`F4af`!1. Clone and configure`!`f

Clone the repository and edit `!docker-compose.yml`! — set at minimum:

`=
  ADMIN_PASSWORD: your-strong-password-here
  FLASK_SECRET_KEY: some-random-string-here
`=

-~

`F4af`!2. Configure Reticulum interfaces`!`f

Edit `!config/reticulum/config`! to add your network interfaces.
The default config connects to the public RNS testnet over TCP —
remove or replace it for a private network.

-~

`F4af`!3. Start`!`f

`=
  ./start.sh
  ./start.sh --build   # rebuild after a code change
  ./start.sh --fg      # stream logs to the terminal
`=

Then open `F4af`!https://localhost:8443`!`f in your browser.
Accept the self-signed certificate warning — HTTP on port `F4af8080`f redirects automatically.

-=

>`!Configuration`!
All options are set via environment variables in `!docker-compose.yml`!.

>>Core

`=
  ADMIN_USERNAME    admin     Local admin login username
  ADMIN_PASSWORD    (unset)   Required to enable local login
  FLASK_SECRET_KEY  (auto)    Session signing key — set explicitly
  WEB_PORT          8080      HTTP redirect port
  WEB_PORT_HTTPS    8443      HTTPS port (main app)
  CACHE_TTL         300       Page cache TTL in seconds
  LOG_LEVEL         INFO      DEBUG / INFO / WARNING / ERROR
`=

-~

>>Reverse Proxy / TLS

`=
  TRUSTED_PROXIES   0         Proxy hops to trust for X-Forwarded-For
  HTTPS_REDIRECT    false     Set true when TLS is terminated upstream
`=

-~

>>OIDC / SSO

`=
  OIDC_CLIENT_ID        OIDC application client ID
  OIDC_CLIENT_SECRET    OIDC application client secret
  OIDC_DISCOVERY_URL    Provider discovery URL
  OIDC_ALLOWED_EMAILS   Comma-separated email allowlist
  OIDC_ADMIN_EMAILS     Comma-separated admin emails
  OIDC_ADMIN_SUBJECTS   Comma-separated admin subject claims
`=

Users on neither admin list are treated as standard users. Bootstrap
with the local `!ADMIN_USERNAME`! / `!ADMIN_PASSWORD`! account, then
promote OIDC users from `!Admin → Users`!.

-~

>>Discovery URL Examples

`=
  Keycloak   https://auth.example.com/realms/<realm>/
  Authentik  https://auth.example.com/application/o/<slug>/
  Auth0      https://<tenant>.auth0.com/
  Google     https://accounts.google.com/
`=

-=

>`!Hosting a NomadNet Site`!
NomadPortal can run as a full NomadNet node, serving your own
pages and files to any NomadNet client on the mesh.

>>Quick Setup

`F4a4◆`f Add your pages to `!./site/pages/`! — create at least `!index.mu`!
`F4a4◆`f Optionally add downloadable files to `!./site/files/`!
`F4a4◆`f Set your node name in docker-compose.yml: `!SITE_NAME: "My Node"`!
`F4a4◆`f Start or restart the container

The node is detected automatically when `!./site/pages/`! exists.

-~

>>Site Structure

`=
  site/
  ├── pages/
  │   ├── index.mu       home page
  │   ├── about.mu
  │   └── subdir/
  │       └── page.mu    reachable at /page/subdir/page.mu
  └── files/
      └── document.pdf   reachable at /file/document.pdf
`=

-~

>>Executable Pages

Pages with the execute bit set (`!chmod +x`!) are run as scripts.
The script's stdout is served as the page content.

Available environment variables:

`=
  node_destination  This node's destination hex hash
  link_id           Hex ID of the RNS link  (unset for local browse)
  remote_identity   Hex hash of the requesting identity  (set if identified)
  field_* / var_*   Form field values submitted with the request
  PYTHONPATH        Includes /site/lib so packages in requirements.txt import
`=

-~

>>Adding Python packages

Drop a `!site/requirements.txt`! file in the volume — the entrypoint
runs `!pip install --target /site/lib -r site/requirements.txt`! on
every start. Installed packages persist across restarts.

`=
  # site/requirements.txt
  requests>=2.32
  psycopg2-binary>=2.9
  redis>=5.0
`=

Inside an executable .mu page:

`=
  #!/usr/bin/env python3
  import requests, sys
  r = requests.get("https://api.example.com/status", timeout=5)
  print(f"`F4af`!API: {r.status_code}`!`f")
`=

First start after editing requirements is slower (pip downloads). Subsequent
starts are near-instant — pip skips already-satisfied entries.

-~

>>Persistent state and databases

For local persistence use `!site/data/`! and Python's stdlib `!sqlite3`!
— no extra packages needed:

`=
  #!/usr/bin/env python3
  import sqlite3
  conn = sqlite3.connect("/site/data/scores.sqlite")
  conn.execute("CREATE TABLE IF NOT EXISTS scores (name TEXT, points INT)")
  for name, pts in conn.execute("SELECT * FROM scores"):
      print(f"{name}: {pts}")
`=

For external databases, add the driver to `!requirements.txt`! and
connect over the host network from inside the script. NomadPortal does
not embed any database — `!site/data/`! is just a writable directory in
the bind-mounted volume.

`Ffa4! Trust model:`f executable pages run with the same privileges as
the NomadPortal process. Anything in `!site/pages/`! and `!requirements.txt`!
is fully trusted — only put scripts and packages there that you trust.

-~

>>Node Identity

The node's RNS identity is stored at
`!config/reticulum/site_identity.id`! and persists across restarts.
The node announces every 30 minutes and re-scans pages/files every 5 minutes.

-=

>`!Reverse Proxy`!
If you run NomadPortal behind nginx, Caddy, or similar:

`F4a4◆`f Set `!TRUSTED_PROXIES: "1"`! so client IPs are read from X-Forwarded-For
`F4a4◆`f Set `!HTTPS_REDIRECT: "true"`! to redirect plain HTTP
`F4a4◆`f Your proxy can terminate TLS with its own certificate

-~

>>Example Caddy Snippet

`=
  nomadnet.example.com {
      reverse_proxy localhost:8443 {
          transport http {
              tls_insecure_skip_verify
          }
      }
  }
`=

-=

>`!Data Storage`!
All persistent data is stored in `!./config/`! (volume-mounted):

`=
  config/reticulum/        Reticulum config, routing tables, identity keys
  config/reticulum/lxmf/   LXMF message router storage (per-user)
  config/identities/       Named RNS identity keypairs
  config/ssl/              Auto-generated TLS certificate
  config/messages.json     Sent/received LXMF message history
  config/contacts/         Per-user contact books
  config/nodes.json        Discovered NomadNet nodes
  config/blocklist.json    Admin-managed node blocklist
  config/users.yml         User account records
`=

`=
  ┌─────────────────────────────────────────────────────┐
  │  ! Back up config/reticulum/identities/ regularly   │
  │    These files contain your RNS private keys and    │
  │    cannot be recovered if lost.                     │
  └─────────────────────────────────────────────────────┘
`=

-=

>`!Architecture`!
`=
  ┌──────────────────────────────────────────────────┐
  │  Docker Container                                │
  │                                                  │
  │  entrypoint.sh ── SSL cert                       │
  │  redirect_http.py ── :8080 → :8443               │
  │                                                  │
  │  ┌────────────────────────────────────────────┐  │
  │  │  Gunicorn  (HTTPS :8443)                   │  │
  │  │  ┌──────────────────────────────────────┐  │  │
  │  │  │  Flask  1 worker · 8 threads         │  │  │
  │  │  │                                      │  │  │
  │  │  │  ┌────────────┐  ┌───────────────┐   │  │  │
  │  │  │  │ Reticulum  │  │     LXMF      │   │  │  │
  │  │  │  │  network   │  │  per-user     │   │  │  │
  │  │  │  │  + nodes   │  │  messaging    │   │  │  │
  │  │  │  └─────┬──────┘  └──────┬────────┘   │  │  │
  │  │  │        └────────┬────────┘           │  │  │
  │  │  │            /config  (volume)         │  │  │
  │  │  └──────────────────────────────────────┘  │  │
  │  └────────────────────────────────────────────┘  │
  └──────────────────────────────────────────────────┘
`=

-=

>`!Development`!
Run directly without Docker (requires Reticulum and dependencies):

`=
  pip install -r requirements.txt
  python app.py
`=

Tests for the Micron rendering library live in the
`[Micron2HTML`https://github.com/JamesM92/Micron2HTML] repository.

-=

>`!Operator Guidance`!
NomadPortal acts as a `*conduit`* to the mesh — content from other
nodes passes through your server but is not created or modified by you.

>>Recommended defaults for public hosting

`F4a4◆`f `!Node lockdown: Locked for guests`! — guests see only your site
`F4a4◆`f Nodes/Messages sidebars: `!Logged-in users only`!
`F4a4◆`f Address bar: `!Hidden for guests`!

Unlocking guest access means visitors can browse the full NomadNet
network freely. That network is unmoderated — a content warning dialog
is shown before each external node is visited.

-~

>>Liability

`Fbbb◆`f Set an `!abuse contact`! in Admin → Settings for abuse reports
`Fbbb◆`f Use the `!node blocklist`! (Admin → Settings) to block harmful nodes
`Fbbb◆`f Keep `!CACHE_TTL`! short (`!60`!) on public instances — default is 300 s
`Fbbb◆`f Sent and received LXMF messages are stored in `!config/messages.json`!
`Fbbb◆`f Private operation (closed OIDC group) carries far lower risk
`Fbbb◆`f Jurisdiction matters — know your local laws before going public

`Ffa4This is not legal advice. Consult a lawyer in your jurisdiction.`f

-=

`c`Ffa4┌──────────────────────────────────────────────────────┐`f
`c`Ffa4│`f  `Ffa4`!! SECURITY — read before exposing to any network`!`f    `Ffa4│`f
`c`Ffa4└──────────────────────────────────────────────────────┘`f

`l

`Ffa4◆`f Change `!ADMIN_PASSWORD`! before exposing to any network
`Ffa4◆`f Set `!FLASK_SECRET_KEY`! to a stable random value for persistent sessions
`Ffa4◆`f The self-signed certificate will trigger browser warnings — expected
`Ffa4◆`f CSRF tokens are required on all state-changing requests
`Ffa4◆`f Rate limiting applies to page fetches and message sends

-=

>`!Related Projects`!
`=
   NomadPortal
   │
   ├─ NomadNet      node software
   ├─ Reticulum     network stack
   ├─ LXMF          messaging protocol
   └─ Micron2HTML   Micron renderer
`=

`[NomadNet`https://github.com/markqvist/NomadNet]
`Fbbb The NomadNet node software`f

`[Reticulum`https://github.com/markqvist/Reticulum]
`Fbbb The network stack`f

`[LXMF`https://github.com/markqvist/LXMF]
`Fbbb The messaging protocol`f

`[Micron2HTML`https://github.com/JamesM92/Micron2HTML]
`Fbbb The Micron to HTML library used by this project`f

`[Ansi2MicronMU`https://github.com/JamesM92/Ansi2MicronMU]
`Fbbb Convert ANSI terminal output to Micron — pair with executable `!.mu`! pages
to expose existing CLI tools (git log, htop, etc.) on your NomadNet site`f

-=

>`!Micron Feature Showcase`!
The section below exercises every Micron feature so you can compare rendering
side-by-side with `[Reticulum MeshChat`https://github.com/liamcottle/reticulum-meshchat] for parity.

-=

>>Headings  (H1, H2, H3)

\`>\` produces an H1 (the bar above this paragraph is a \`>>\` H2).
\`>>>\` produces an H3 — see below.

>>>Third-level heading example

-=

>>Inline formatting

Plain. `!Bold`! and back. `*Italic`* and back. `_Underline`_ and back.
`!`*`_All three combined`_`*`! together, then back to plain.

-=

>>Foreground colours

`Ff00`!Red`!`f  `F0f0`!Green`!`f  `F00f`!Blue`!`f  `Fff0`!Yellow`!`f  `F4af`!Cyan`!`f  `Ffa4`!Orange`!`f  `Fa4f`!Magenta`!`f

-=

>>Background colours

`B400 red bg `b  `B040 green bg `b  `B004 blue bg `b  `Bf80 orange bg `b  `B0aa cyan bg `b

-=

>>24-bit exact colours (`\`FT<6hex>` / `\`BT<6hex>`)

NomadNet's reference parser accepts a longer form for exact
24-bit colour: `\`FTrrggbb` for foreground and `\`BTrrggbb` for
background. Micron2HTML supports it too.

`FT8b4513`!Saddle brown`!`f  `FT4b0082`!Indigo`!`f  `FT2c3e50`!Slate`!`f  `BT8b4513 saddle brown bg `b

`F933`!Discouraged for general use.`!`f MeshChat's Micron parser doesn't
render this form — pages relying on `\`FT<6hex>` look wrong in
MeshChat. Every byte counts on a mesh, and the 3-hex `\`Fxxx`
shorthand costs half as many characters *and* renders correctly
in every mainstream client. Reach for `\`FT<6hex>` only when
exact colour genuinely matters and you know your audience is
rendering with NomadNet or Micron2HTML.

-=

>>Alignment

`l Left aligned (default)
`c Centered text
`r Right aligned
``After reset, default alignment again.

-=

>>Dividers

Line below: \`-\` (single dash)
-
Line below: \`--\` (double dash)
--
Line below: \`-=\` (double rule)
-=
Line below: \`-~\` (styled with tildes)
-~
Line below: \`-*\` (styled with stars)
-*

-=

>>Literal block  (\`= … \`=)

`=
This is a literal block — no Micron tokens are interpreted inside.
Leading    whitespace    is    preserved.
Backticks (`) and `! tokens render as raw text.
   ┌───────────────────────┐
   │  Box-drawing aligned  │
   └───────────────────────┘
`=

-=

>>Links

External:   `[Reticulum site`https://reticulum.network]
Same-node:  `[About page`/page/about.mu]
Bare URL:   `[`https://example.com]

-=

>>Form fields  (read-only for guests)

Text field:        `<24|username`alice>
Password:          `<!16|password`>
Checkbox:          `<?|agree|yes|*`I agree>
Radio:             `<^|color|red|*`Red> `<^|color|green`Green> `<^|color|blue`Blue>

-=

>>Links with field specs

A link can carry field names whose values get submitted on click. Syntax
is \`[Label\`URL\`field1|field2]. NomadPortal stores the field list in a
data-field-spec attribute; MeshChat wires up an onclick that POSTs the
named field values.

The two buttons below post the form back to this page, which renders a
focused result view echoing the values it received. Nothing is saved —
the password value is masked to \`****** \` in the result regardless of
what was typed.

  `B226`!`[▶ Submit form`:/page/index.mu`action=submit_form|username|password]`!`b

  `B226`!`[▶ Submit all`:/page/index.mu`action=submit_all|username|password|agree|color]`!`b

  Submit form sends only `*username`* and `*password`*. Submit all also
  includes the `*agree`* checkbox state and the selected `*colour`* radio.

>>>How to actually persist these values

This demo is intentionally read-only — both buttons just echo what they
received. To save values across requests, an executable \`.mu\` page would
write to a file under \`/site/data/\` (Python stdlib \`sqlite3\` works
against a file in the same directory, no install needed). See
\`docs/AUTHORING.md\` for end-to-end examples. We deliberately skip the
write here so anything typed into the password field — or any other
input — never touches disk.

-=

>>Backslash escape

Render literal special chars by prefixing with \\ (a backslash):
\`  → \\\`   (escaped backtick)
\<  → \\<    (escaped lt)
\>  → \\>    (escaped gt)
\\  → \\\\   (escaped backslash)

-=

>>Inline literal  (\`= … \`= mid-line)

Outside literal: `!Bold`! works.
Inside literal:  `=`!Bold`!`= — the `! tokens render as raw text.
Then formatting resumes: `!Bold again`!

-=

>>Reset-all  (\`\` nukes active formatting)

`!`Ff00 bold red`` reset → following text should be plain.

-=

>>Section reset  (\< at start of line resets depth)

After a `>>>` heading the body is indented. A line beginning with \<
returns to depth 0. The next paragraph below is at depth 0.

>>>Deep heading first

Indented body text under a deep heading.

<This line resets depth to 0 — should be flush left.

-=

>>Comments

The next line is a Micron comment and should produce NO output:
# this is a comment — invisible in render
The line after the comment is normal text.

-=

>>Mixed inline formatting

Bold + colour:        `Ff00`!Red bold`!`f
Underline + colour:   `F0f0`_Green underline`_`f
All three:            `F4af`!`_`*Cyan bold italic underline`*`_`!`f
fg + bg:              `Ff00`B004 red on dark blue`b`f end

-=

>>Long line wrapping

This is an intentionally long sentence to verify how each renderer wraps text at the viewport edge — characters should flow to the next visual line without horizontal scroll, and Micron tokens like `!emphasis`! should still apply across the wrap boundary.

-=

>>Boundary edge cases

Empty heading (next line is `>` with no text — should be invisible):
>

Trailing whitespace at end of this line:
…and the next line picks up normally.

Two blank lines below this one (more space):


…visible gap.

-=

>>Page-level colour headers (\#!fg / \#!bg)

The very first lines of this file are:
\#!bg=111
\#!fg=ccc
MeshChat IGNORES those (treats # lines as comments). NomadPortal also
ignores them in the rendered CSS — both should render at the same
default page colours regardless of the source declaration.

-=

>>Dynamic includes (\`{URL\`refresh})

Below is a dynamic-include token that NomadPortal renders as [live] and
MeshChat doesn't recognise at all (its parser has no case for \`{ ).
Expected divergence — flagged here for awareness.

Token: `{https://example.com`60}

-=

>>Very deep nesting (>>>>+)

The h-level cap is 3 in both renderers' default styles, so any heading
deeper than `>>>` reuses the h3 visual. Body indent however keeps
incrementing per depth, so under a deep heading you get more left margin
than the heading bar itself.

>>>>Level-4 heading

Body text after a level-4 heading. Indented by depth, while the heading
bar sits at the h3 visual.

>>>>>Level-5 heading

Body under a level-5 heading.

<

(line above is `\<` — section reset back to depth 0)

-=

>>HTML / script injection safety

Both parsers HTML-escape user content. The line below should appear as
literal text — never as an executable script or as `<` triggering a
field token (that only happens after a backtick).

<script>alert('xss')</script> & < > " ' / \\

Inline within text: hello <b>world</b> and `<malformed> end.

-=

>>A rubber duck for science

Generated with `[Img2ContourAscii`https://github.com/JamesM92/img2contourascii] from a CC BY 2.0 photo by Flickr user 21723187@N04 ("flickr.com/photos/21723187@N04/2608418319").
ANSI colour piped through `[Ansi2MicronMU`https://github.com/JamesM92/Ansi2MicronMU] for Micron output. 80 cols × 39 rows at the converter's default 4:3 char-aspect — natural ratio.

`FadfUii`F8dfiiiii`F7cfii`F8cfiiiii`F6afi`F7cfi`F5cf|`F3cf!`F3af!`F3cf||||||||`F3af!||||!-`F7cfB?`F9df&`F3af!!!!!`F2af!`F2bf!`F3af!`F29f!`F3af!`F4bf!`F49f!`F29f!`F2bf!`F4bf:`F49f:!`` `F29f:!`` `F4bf!`` `F4bf:`F49f!`F29f:`F2af-`` `F2bf!`` `F2af\``F2bf!`F2af:`F2bf:!`F29f:`F2bf!!``  `F09f-`F2bf!
`F8cf|`F7af||||`F6af||||`F6cf1i`F7cf||||`F5cf!`F5af!`F4bf!!!`F3af!!!!!!!!!!!!!`F4bf!-`F9cfB`F7bf!`F9cf&`F3af-`F4bf!`F2bf!`F4bf!`F3af!`F2bf!!`F2af!`` `F29f!`F2bf::`F29f!`F2af\``F2bf:`F49f:!`` `F29f-`F2af\``` `F2af-`` `F49f!`F29f!`F2bf:``  `F29f\``F2bf:`` `F2af!`F29f:!!!!-``   `F2af:
`F6bf!!!`F4bf!!!!!!!`F5af!!!`F6bf:`F4bf::`F6bf!`F5af!`F5cf|`F5af!`F3af!!!!`F4bf'`F9df.`Fffaa`Fffca`F7df:`F4bf!!`F3af!!`F4bf!-`FacfB`F7bf!`F9cfG`F5af-`F4bf!`F2bf!`F4bf!!`F29f!!!`F2bf!`F49f!!`F29f!>`` `F29fc`F5af::`F2bf:`F29f-`` `F4bfc`` `F4bf-!`` `F29f!`` `F27f!`` `F2af!-`` `F2af:``  `F29f:!`F2af:`` `F07f-`` `F39f:
`F4bf!!!!!!`F3af!!!!`F5cfii|cci`F4afii`F6afi`F5cfiii`F5af!`F7df!`Ffeag`Ffc6B`Ffd6B`Ffc5U`Ffb5n`F7bf!`F3af!!!`F4bf!-`FadfB`F7cf:`F9cfB`F7cf-`F4bf!`F49f!`F4bf!`F49f!`F4bf!!`F29f!`F2bf:`F49f<:`F29f:`F49f>`F2af.`F49f!`F2af-\``F4bfv`F2af:`` `F49f!`F2af-:`` `F2af-`F29f!`` `F2af::`F2bf!`F29f-`` `F4bf:-`F39f-`F29f:`F2bf!`F2af!-`` `F39f::
`F5cfiiiiciiii|ii`F4afiiii`F4cfiii`F6cfi`F6afi`F5cf!`Fbff.`Ffd7B`Ffc6BB`Ffd5B`Ffc5GU`Ffc6o`Fdfd_`F5cf!`F3af!!-`FadfB`` `F8cfA`F7cf>`F3af!`F49f!`F4bf!!!!`F29f!`F4bf::c`F2bf:`F4bf-`F2af:`F2bf\`-`` `F49f!`F2af\``F3cf:`F2af\``F2bf:`F39f\``` `F2bf:`F29f-`` `F39f-`F2af:\``F2bf:``  `F39f-`F2af:`F2bf!`` `F39f-`F2af:`` `F39f:-
`F5cf||||||||||`F4af||iiii`F4cfiii!`F8df:`Ffeag`Ffd6B`Ffc6BBB`Ffd6BBB`Ffc6BB`Ffc7&`Fdfd;`F3af\``F4af=`FadfB`F4af:`F7bf3r`F3af!`F4bf!!!!`F49f!`F2bf!`F4bf!!!`F29f!`F4bf:`F2af\``` `F4bfc`` `F4bf!`F3cf-`F39f\``` `F4bft`F39f-:`F2bf!``  `F39f--`` `F2bf!``   `F2bf!!``  `F39f:`` `F0bf:``
`F7cfzi`F5cf||i|i`F4afi`F6afi`F6cfii`F4afiiiii`F4cfi`F4bfi!`Fdfca`Ffd7B`Ffd6BBBBBBBBB`Ffc6B`Ffc5B`Ffd6B`Ffc7&`Fcdf;`Facf&`F5cf!`F7bf<`F8cfe`F3af!!!!`F4bf!`F2af!!`F49f!`F4bf:`F49f!`F2bf!`F2af-!:`F4bf!`F2bf:-`F5af:`F39f-`F5af:`F2bf!`F2af-:`F29f!`F2af:`F4bf-`F39f:``  `F29f!`F2bf-``  `F2bf!`F2af-`F39f--``  `F07f--
`F6cf|`F6af|`F5cf|!|||`F4af|||||iiii`F4cf|`F6cf\\`Ffe9@`Ffd6BBB`Ffc6R`Ffd7P`Ffc7PPPPfR`Ffd7RB`Ffd6B`Ffc6G`Ffb6c`Ffffh`F3af:`F6af<`F9cf&`F3af-!!c`F2bf!`F2af!`F3afi`F2bf!`F29f!`F2bf-`F3af!`F4bf!:`F2af-`F4bf!`F3af!`` `F5af!`` `F5af!`F2af\``F2bf-`F2af--`F49f!`` `F39f!``  `F2af\``F2bf:`` `F0af-`F2bf!`` `F39f--``  `F07f--
`F6cfi`F5cf!!`F3af!!`F3cf!`F5cf!`F4af|||`F5cf|`F3cf|`F4afiii|`F7df\\`Ffe98`Ffd7P`Ffc7"`Fffc\```   `Fffc\`'``   `Faaf..`` `Fffc\``Ffd7T`Ffb5U`Ffc7!`F4bf-`F5cf-`F9cf&`F5af-`F4bf!!`F3af!`F2bf!`F3af!|`F2af!`F2bf!:`F3af!`F4bf!!`F2af:`` `F3cfc`F2af.!`F5af!`F2af-`` `F2bfc`F29f-`` `F2af!`F39f.`F4bf-`` `F39f-`` `F2bf!`` `F0af:`F2af-`` `F2af:`F39f-``   `F09f:
`F6cfi`F5cfc||`F4afiiiiii`F3af||!i`F4cfi`F4afL`Ffdad`Ffc7'``  `F5af--`` `Fffb\``Ffa2!`Ff93!`Ffb4:``  `F5af-\```      `F3cf-`F9cfB`F6af-`F3af!`F4bf!`F2af!`F49f!!`F2af!`F29f!`F2bf!`F3af|`F4bf!`F3cf!`F2bf\`!-`F4bf!`F2bf~`F2af---`F29f-`F4bf!`F2bfr`F2af.`F39f-`F2af!!`F39f-``  `F2bf<:!`F07f-`` `F0bf-`F09f:``   `F39f:
`F6cfi`F4af|i`F3afi`F4afi`F3afi`F3bfi`F4bfi`F3bfi`F3afi!`F1af!|`F3af|`F3bf|``        `Ffff\``Fffc"`Fffa"`Ffff'``   `F5af-``       `F9dfB`F7bf>`F3af!!!`F49f!!`F2af!`F29f:`F2bf!!`F29f:`F3af!`F4bf:!`F2bf!`F4bf::`F2af::\``F2bfc`` `F2bf-`F29f!`` `F2af!`` `F2af-`` `F29f!-`F2bfv`F29f!`` `F2af-`F09f-`F39f:`` `F0bf:``
`F4bfi|`F3bfi`F3afi`F3bfiiiiiii`F1afiii`F3bfi``       `F5af-`` `Ffb7.`Ffa5.`Ffc6.``         `Ffb4:`Ffa2:`F9cf8`F7bf>`F3af!`F3cfi`F3afz!!`F2af!`F2bf!:`F29f!`F2bf!:`F5af!`F2af!`F2bf!`` `F4bfc`F2bf:`F2af!`F2bf:`` `F29f::>`F2af!!\`!-`F2bf:`F29f:!`` `F2bf:!`` `F39f!!!:``
`F4bfii`F3bf||||ii|||`F1af|i|!``        `Ff94:`Ffc78`Ffc6A`Ffb6U`Ffb4:``        `Ffb2!`Ff92:`Fcdf9`F8cf>`F3af1i|i|!!`F2bf:`F29f!`F4bf!`F49f!`F4bf!`F2af:-`F2bf!:`F2af-:`` `F3cf!`F29f-`` `F2bfc`F2af::!-`F3cf-`F2bf!`F29f!`` `F29f:`F2bf!`F09f-`F07f-`F09f-`F27f:`` `F07f:`F09f-
`F4bfiii`F3bfi`F4bfi`F3bf|iiii`F1bf|`F1af|c|:`FdfbL``      `Ff94:`Ff75F`Ff44.`Ff56L`Ff53\``Ff75!`Ff94:``      `Ffc3:`Ffb2!`Ff92!`FfffJ`F8cf>`F3cf!`F3af||i`F3cf|`F3afv`F3cfi`F2bf!`F2af!`F49f!`F4bf!!`F2af!!`F2bf-`F2af!`` `F2af!-`F29f!.`F2bf-`F29f!!`F2af!`F3cf-:`F2af:`F0af\``F29f!`` `F29f!`F2bf!`F0bf!`F09f!-`F0cf:`F09f:`F0af:`F09f-
`F5bfU`F4afiU`F4bfii`F3bf||iii`F1bfi`F1afi|i`F1bfc`Fffa&`Fcff:``   `F222-`Ff74v`Ff32\``Ff12:`Ff33!`Ff34:`Ff23!`Ff22:.`Ff41-`Ff63.`Ffa5.`` `Ffa5.`Ffa2:`Ffb2!!`Ff92!`FfffJ`F8cfn`F3cfv!`F2bf!`F3af!`F2af!`F19f!`F3afi`F2bf:`F2af!`F4bf:!:`F2af!`F4bfv`F2bf-!`F4bf!`F39f-`F2af!`` `F2bf:`F2af-.:!`F39f-`F2af\`\``F2bf!!`F29f:\``F2bf!:`F0bf!`F0af:`F2af:`F0af::`F09f:
`F4bf1ii`F3bf|ii|ii`F1bfii`F1af|`F1bf|ic`Fdfd1`Ffd7z`Fff9v`Fffc.`Ffc6.`Ff74!`` `Ff12!`Ff22-`Ff35<`Ff23\``Ff22!`Ff12:`Ff11::-`Ff31\``Ff52!`Ff92!!!`Ffb2!`Ff92:`Fdff3`Facfn`F5cfU`F4cfi`F3cf:`F2af!!!`F3af|`F2af::`F3cfc`F3afc`F4bf!:!`` `F2bf!`F4bf!`` `F2af:-`F2bf!`F2af:-`F39f-`` `F39f-`F3cf!`` `F2af:`` `F2bf!!!!!`F39f-`F2af:`F3cf:`F0af:`F09f:
`F3bf||!|`F4bfiii`F3bf||iiii`F3cfii`F6dfU`Fff9U`Fff7i`Ffd6|`Ff63!`Ff12.~`Ff13.`Ff35-`Ff44-\``` `Ff22.`` `Ff22-`Ff11::-`Ff22-`Ff72!`Ff92!!-`Facfd`Fabfz`F5bf|`F6cf|`F5cfU`F4cfc`F4bfz`F2bf!`F4bf!`F2af!`F1af!`F3af!`F5cfi`F4bf:`` `F4bf!`F2af-!`F2bf:`F2af:::`F2bf:`F3cf\``F2af:`F3cf!`F39f!-`` `F39f:`` `F2bf:-`F2af:`` `F29f!`F2af-`F39f\``` `F4bf-`F2af:`F39f-
`F3bfii`F4bfii`F5bfUUU`F4bfUU}}`F2bf}`F3bf|`F3cfi`F1bfi`F2bf|`Fbfd1`Fff71`Ffa7!`Ff12-`Ff33!`Ff44:``           `Ff60:`Ff92!!!`Ffb6:`F6bfV`Facf&`F5bf!`F4cfi`F5bf|`F5cf}`F4cf1`F3afc!`F2af!`F2bf!`F29f!`F3af|`F4bf!`F2bf:`F3af!`F4bf!:`F2bf!!!<`F4bf-`` `F4bf!`F6cf\``F69f:\``` `F3cf:`` `F4bfv`F2bf-`F2af:`F39f:`` `F47f.`F39f:``  `F39f:`F4bf-
}iU}U`F5bf66`F4bf6V1`F3cf1`F3bf|`F1bfiiii`F4cf|`Fff9U`Fff7n`Ff87:`Ff64\```           `Ff62:`Ff90!`Ff92!!-`F7dfU`F5cf!`Fbcf&`F5bf!`F4bf!`Fcff;`Fcfcy`F8df:`F3cf!`F4cfU`F3af:`F29f!`F2bf!`F4bf!`F49f!:`F3afc`F4bf:!`F2bf!`` `F2af!`F2bfv`F5af-!`F5cf!`F5af!`F7bf:-`` `F5af!`` `F3af!`` `F2af::`F39f-`F47f:`F4bf!:`F47f-:`F07f:
`F3bf||||`F2bfi`F4bfUUi`F3bfiiii`F1bfi`F1cfii`F1bfii`F7ff|`Ffd7Ui`Ffc5c`Ff44.``        `Ff72.!`Ff90!`Ff92!!`Ff70!`Ffb6!`F4cf}`F4bfv`FadfB`F5bf!`Fffcs`Ffc5B`Ffd6B`Ffc5B`Ffd9z`F6df?`F3cf!`F2af!`F3af!`F2bf:`F4bf:`F2bf!`F4bf!!`F2bf!`F2af\``F2bf:`F2af-`F4bf!`F5af:`F6cf!`F5af:!`F6cf-`` `F6cf:`` `F4bfv`F2bf!`` `F39f-`F29f!`F39f-:!`F47f-`` `F47f--
`F3bfiiii`F2bfiVi`F1af1`F1bf|ii`F1af!`F3cf!`F9ff;`F7ffc`F2bf?`F1bfii`Fafd1`Ffc4U1}`Ffa5u`Ff94:.`Ff64..`Ff94::`Ffb2!`Ff90!!!!!`Ff80!`Ff92:`Ffb6:`Ffff!f!`Ffc3U`Ffc4B`Ffc5BG&`Ffc7r`F3cfv`F2cf!`F5bfU`F7cf!`F4cfi`F4bf:`F2bf:`F2af\`!`` `F2bf!:`F4bf-`F5af:`` `F6dfv`F5af-::`F6cf:`` `F4bf!-`` `F7bf-`F2af!-`F39f:-``  `F47f-`F4bf:
`F1bfiiiii`F1afiii`F1bfii`F1af|`F5cf\\`Ffd7B`Ffc6B`Ffc5B`Ffe7U`F4bf|`F1cfV`F2cfV`FffbV`Ffa3ii`Ffc3i`Ffa2|i`Ffb2|z`Ff92!`Ff90|`Ff92c`Ff90!!!`Ff70!!`Ff72!`Ff92!!`Ff94!v`Ff93=`Ffc5UBBB&`Ffa4h`Fbff!`F2cf:`F5cf!`F6df!`F5cf&`F6df!`F3cfr`F2bf:`F2af-`` `F2bf!!`` `F5af!\``F4bf!-`F6cf\``F7bf:`F6cf!`F5af.`F4bf!`F5af:`F69f\``` `F3cf!`F29f:`F2af!`F39f-`` `F5af-`` `F39f!
`F1bfii`F1afi`F1bfiiUU1`F1afUU`F4bft`Ffd8d`Ffc6BB`Ffc4B`Ffb3G`Ffc7o`Fefcd`Ffd7|`Ffa3!|`Ffc3||`Ffa3||`Ffa2||`Ff82|`Ff90!!!!`Ff92!!`Ff72!`Ff92!`Ffb2!!`Ff92!`Ffa2!`Ffc3v`Ffd68`Ffc5B`Ffc6B`Ffd6B`Ffc5U`Ffb4i`Ffdb!`F3bf!`F3af!`F3cf<`F4cfr`F4bf:`F5dft`F2bf-\``F2afv.`` `F3cf:`F6cf!`` `F4bf:c`F2af:``  `F4dfv`F4bf!`F5cf:`F6cf-!`F69f-`F2bf!`F2af-`` `F47f\``` `F47f-`F39f-
`F1bfViiiUUU`F1afUUt`Fffcd`Ffd6B`Ffc6BB`Ffc5BB`Ffd6B`Ffc6B`Ffd6&`Ffc5a`Ffb4z`Ffa3!|`Ffa2i|ic`Ff92!!`Ff90!`Ff92!!!!!!!!!`Ffa3!`Ffb5U`Ffd6B`Ffc6B`Ffc5B1`Ffb41`Ffa3!`Ffc7-`F2bfU`F3bfv`F3af<`F4cfv`F4dfv`F4bf\``F4df!!`F3cfr`F2bf:`F2af-`F39f-`F4bf--`` `F5af::`F3cf--`F4bf!!!`F49f!`` `F5af:`F2af!-`F39f-:`` `F4bf:`F39f!
`F1bfUUi1U`F1afUUUU`F5cft`Ffc5U`Ffd6B`Ffc6BB`Ffd6B`Ffd5B`Ffd6B`Ffd5B`Ffc5BBB`Ffb4z`Ffa3||`Ffc3|`Ffa2|ic`Ffb2!`Ff92!!!!!!`Ffa3v`Ffb4U`Ffb5U`Ffb4UUU`Ffc5US`Ffb4U1`Ffa3!`Ff82:`Fcff.`F1af!:`F4bfY`F3cf!`F2bf:`F4bf::`F2bf!`F5cf|!:`F2af!:``  `F4bf-`F6cf\``` `F3cf:`F2af:`F4bf::`F49f!`` `F4bf!`F2af-!`F3cf::`` `F39f!`F2af:
`F1bf6UU`F1afUU`F2afUUU`F1afi`Fffdd`Ffc5B`Ffd6BB`Ffc6B`Ffc58`Ffd5BBBBB`Ffc5B`Ffd6G`Ffb5z`Ffc3!`Ffa3|`Ffc3||`Ffa3|||z|`Ffa2!!`Ffb4u`Ffb5UU8`Ffb48UUU`Ffb3c`Ffa31|`Ff92!-`F3cfc`F3bfz!;`F2bf\`!!`F3cfc`F2bf:`F2cf-`F4bf-`F2cf!`F4bf:`F2bf:`F2af-`F3cf-`F5ff.`` `F39f-`F3cf:`` `F4bf!c`` `F69f-`F4bf!:`F2af!`F3cf!`F4bf-`` `F4bf:`F2af:
`F1bfiiiii`F1af|`F1bfi`F1afUU`FffaV`Ffc5U`Ffd68`Ffc5BGBBBB`Ffd5BBB`Ffd6B`Ffe7B`Ffd7&`Ffc6y`Ffb5;`Ffc4;`Ffb4!`Ffc4!`Ffa3!`Ffa4!`Ffc4;`Ffb4u`Ffb5U`Ffc5U8G`Ffb48UU1`Ffb3i`Ffa3i`Ff92.!:`Ffc7!`F3cft`F2cf:`F1af!`F3cf!`F29f-`F49f:`F2bf-`F3afV`F2bf!<`F4bf:`` `F4bf\``F4cf}`F3cf:`` `F3cf-`F2cf:``   `F3cf-`F4bf!`F5af:`` `F4bf!:`F29f!`F2af:`F39f-`F5af-`F4bf-`F2af:
`F1bf1UUU`F1af&UUU5`Faffr`Ffc5UU8`Ffd5B`Ffc5BBBB`Ffd5BBBB`Ffd6BBBBB`Ffd7BB8`Ffd688`Ffc6B`Ffc5BBU`Ffa3U`Ffb4U1U`Ffc3n`Ffa3i`Ffc3|`Ffb4v`Ff92!!`Ffa2:`Ffc6:`Ffc5!`Fbffc`F2cf!`F2af!`F2cf:`F3afv`F4bf\``F2af<`F1af<`F3af<`F2cf-`F4bf:`F3cf!>`F6ff!`F3cf-|`F4bf-`F2af:``   `F4bf:`F39f-`F2af!`F29f!`F2bf!`F2af:`F39f-`` `F47f-`F39f:
`F2bfU`F2afU`F1bfU`F2afUU`F2cfUUUU5`Fffbi`Ffc5V8`Ffd5BB`Ffd6BBB`Ffc5BB`Ffd5B`Ffd6BBBBBB`Ffc6BB`Ffd7B`Ffd6BB`Ffc6B`Ffc5B8`Ffc4G`Ffb4U&U`Ffc3i`Ffa31`Ffc3|`Ffb4!!`Ffb2!!!`Ffb4:`Ffb6;`Ffd7c`F3cf|`F1af!`F1cf|`F3cf:`F3af<`` `F0bf\``F3af<`` `F2bf!`F4bf-`` `F2cf!`F4df<`F3bfi`F4bf-`F3afc!`F2af:``  `F4bf--`F39f-`F2af:`F29f:`F0af!`F0bf-`` `F09f:
`F2cfU&&&UUU}`F2bf|`F9dfV`Fffb!`Ffd6U`Ffc588`Ffd5BBB`Ffd6BBB`Ffc5B`Ffc6BB`Ffd6BBB`Ffc6BB`Ffd7B`Ffd6B8`Ffc5888U`Ffb4UUU`Ffc3ii`Ffa3!`Ffb4!`Ffd4!`Ffc5!`Ffd4c`Ffd7iV||`F9fd.`F3cf!`F2bf!`F2af!`F2bf::`F2cfr`` `F3afr`F0bf-`F1afL`F2bfc`F2af-`F29f!`F2bfc`F4bf\``F2cf.`F2df!`F3cf!`F2bf!`F0af-`F09f-`F0bf-`` `F0bf-`` `F39f!`F0af:``  `F0af:
`F2cfUUUUUU`F2bf&`F3cfc`FdfcU`FffcB`FdfaUA`Ffe7G8`Ffc58`Ffe5B`Ffd58BB`Ffd6BBBBBBBBB8`Ffc5GUU`Ffb5UU`Ffc41U`Ffb4U`Ffc4i`Ffc5|`Ffd6i`Fff6i`Fff7i`Fcf7!`Fafa!`F9ff;`F5dfx:`F3cfz`F1afv`F1bfv`F2cf:!`F2af!`F0af:`F2cfc!`F2bf:`F0af!`F09f-`F3afL`F3cft`F0bf--`F1af|`F29f-`F2af!`F0af:`F2bf!`F2cf:`F2bf!`F0af--.``   `F0cf:`F09f-`F0af-`F09f!
`F2cfUUU`F2bfU`F3bfUU`F3cfU&`F7efUua`F7ffc`Fafc|`FdfaA`Fff9DB`Ffe7U8`Ffc6B`Ffe6BDBDDD`Ffc6GGS`Ffe5SU`Ffc5U`Ffd66UU`Fff7U`Ffd7i`Fff7i`Fdf7i`Fdf9!`Fbf9!`F9fd.`F7ff;`F4dfu`F4cfU`F4bfV`F3bfvn`F1afv`F1bfi`F1afc|`F1cf!`F1afc`F2af!:`F0bf.`F2bf!`F09f:`F0cf<`` `F0af!`` `F2bf:`F3bf|`F2bf:\``F0bf!`` `F3aft`F2bf-!`F0bf:!`F09f!`F0bf:`F0af.``  `F0af-`F0bf!
`F2bfUU`F3bfU`F3cfUUUUUUU`F5dfUU`F6dfB`F7ef&`Fafey`Fafc}`FcfaVV`Fdf966UUU`Fff9UU`Fffa6&`Fdf9XA`FdfaA`Fdf8VV`Fdf9||`Fbfb|c`F9fdv`F7ffu`F6dfz`F4cf6U}`F4bfUi`F4cfi|`F3cf|`F1af!!`F1bf!`F1af!`F0cf!`F1af!`F2af!`F2cfc`F09f\``F0bf!!-`F0af-`F09f\``` `F3bfYt`F2af<`F09f:`F0bf!`F2af:`F29f\``F2bf:`F0af\``F09f!`F0af:!!`F09f!:-`` `F0af-
`F2cfUU`F3cfUUUUUUUU`F5dfU`F6dfG8`F7ef88BB&&`F8ffa`Faffay`Fafey`Faffy`Fafey`Fafdi`Fcffz`FafdV`Faff&`Fbff|`F9dfU`F8dfri`F7cfzn`F7dfi`F5cfVUUUUUU`F4cf6i`F4bfi`F4cf|`F3af!`F1af!`F1bfi`F1af!!`F0af!v!`` `F09f!`F29f\``F0af.`F2af:`F1afvY`F2af-`` `F29f~`F0bf:`F29f!`F2bf\``` `F2bfc`F0af-`F09f~`F1af|`F1cfi`F1afv`F09f:!-`` `F09f:
`F3cfUUU`F3bfUUU`F3cfUUUU`F6dfG`F7dfG`F7efB`F8efB`F7efB8BBB`F6dfBB`F7dfBBBB`F9ef&`Faef5`Facf5A`Fadfz`F8efU`F8cf&`F8df!`F7cf&`F7dfi`F7cfA`F7df|`F6cfU`F5cfUUUU6UU`F5bfU`F4cf!`F3cf!`F1af!!`F1bfii`F0af\``F0cf:`F1afv`F0af>`F2af=`F2bf-`F2cf!`F2af\``F29f..`F2af:`F1af!`` `F0af:`F29f-:`F2bf:`F2af!`F0bf-:`F09f-`F1af!`F2cf!`F2bf!!`` `F0bf\``F09f-
`F3bfUUUUUU`F3cfUUUUU`F7dfB`F8efBBB`F7efBBB`F6dfBBBB`F7dfBB`F7efB`F8ef8`Faef|`Facf|}`F9df}U`F8ef&`F9ff!`F8cf&`F7cfsU`F7dfc`F6cfUU`F5cfUUG`F6ef&`F7ef8`F6cfUU`F5cfi!`F1af!`F1bf|;z`F1af!\``F2af.`F0af=`F1afn!`F2cf!`F0bf.`F09f\``F2af!`` `F2af:`F09f-`F29f\`::!`F2cf!`F2bf:`F0bf!`F2af-!`F2cf!`F2af!`` `F09f:`F0cf:`F09f-
`F2cfU`F2bfUU`F3bfU`F2bfU`F3cfUUUUUUU`F5cfB`F8efB`F7efBBB`F5df8`F6dfBBBBBBB`F7efG`F7cfU`F9dfx`F8cfY`F7dfn`F7cf!Y`F7df!`F6cfYzs`F5cf|`F5efU`F5cfUV`F4cf}}`F6cfU`F7ef8U`F6cfUUz`F0af:v`F1af:`F19f:`F0afv!!:`F0bf\``F0af:`F1af4`F0af\``F29f.`F1af!`` `F0bf\``F0af:`` `F29f!`F08f-`` `F29f\``F3afc``  `F2af-`F29f\``F2bf-`` `F07f-`F0af!:
`F2cfU`F2bfUU`F3bfU`F2bfU`F2cfU`F3cfUUUUBUU`F4cf}`F6dfB8`F5df5`F2cfi2`F4cf}`F6dfBBB`F5df8D`F4dfU`F5cfn`F4cf|!`F4bfz`F5af!`F4afcc`F3cf!`F4af.`F3af:`F4bfc`F3cf|`F2bfi`F2cfV`F3cfU`F2cfi`F4cfU`F6efU`F7efU`F6cfUU`F2bft`F1bf!`F0af!`F1bfi`F09f!`F0af!c!!:c`F0cf/`F1af!`F0bf:`F2bf!`F0bf.`` `F09f!:`` `F0af!:`F2bf-`F3bfi`F2af!`F0bf:``  `F2bf!`F29f:`F2bf!`F09f-!
`F2cfUU`F2bfUU`F2cfUU`F3cfUG8G8UUU`F2cfUs`F2bfU`F1bfUU`F1cfU`F2cfU`F4dfUGBBG`F3cfi`F4bfi`F4afc`F4bfi`F3af:`F4afii`F3af!!!`F3bf|<`F1cfUi`F3cfU`F2cf}`F1afc`F4cf|`F5cf}`F4cf|`F2bfj`F1bfic`F1af!`F0af!`F1aft`F0af:i`F0cf:`F0af:`F1af!c`F0bf:`F2cf\\`F09f\`!`F0af!`F0bf:`F0af:`F09f!.`F0bfi`F1af!`` `F1afv`F0af-`F0bf!`F0af-`F2bfv`F2af!!`F2bf>`` `F09f:
`F2cfUU`F2bfU`F2cfUUUU`F3cfUUU`F2cfUU`F3cfU`F2cfUUU`F1bfU`F1cfU`F2cfUU`F3cfSDGD8`F4dfB`F3df&`F4bf>`F4af!`F4bfi`F3cf!`F4af|`F5cf!`F4bf!`F3bfi`F3af!`F4bfi`F3cfv`F1cfU1`F3bfU`F2cfi`F1afU1i}`F1cf&`F1bfii`F0af!i!c!!`F0cf!`F1bfc`F0af>c`F0bf!`F0af<!:!`F0bf!-`F09f-:\``F0afc`F1af!`F0af-`` `F09f:`F2af!`F29f!`F2af!>`F0cf-`F0af!``
`f
-=

`c`F4af`!End of showcase — if everything above matches MeshChat, parity is good.`!`f
`c`F4afNomadPortal intentionally deviates in two places: larger font (16.64 vs 16px) and no line-wrap (horizontal scroll instead).`f

-=

---

`cMIT License  ·  `[github.com/JamesM92/NomadPortal`https://github.com/JamesM92/NomadPortal]
""")
