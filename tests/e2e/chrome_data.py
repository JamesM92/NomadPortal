"""Demo and worst-case data for the interface around the page (header,
sidebar lists, chats, user bar, admin tables). Used by the mock backend
only; nothing here ships.

The worst-case values are realistic: real naming patterns, real address
shapes, or the longest values the app accepts. Each row of a list carries a
different failure, as real data does. Datasets: demo, worst, empty, one, huge.
"""

import time

NOW = time.time()
H = lambda n: ("%032x" % (n * 0x9E3779B97F4A7C15 % (1 << 128)))   # noqa: E731 (stable 32-hex hashes)

LONG_SITE = ("The Northern Prairie Amateur Radio & Emergency Communications Society "
             "- Regional Gateway Node #7 (Winnipeg / Brandon / Thompson)")
LONG_URL = "https://example.org/a/very/long/path/with-no-spaces/" + "segment-" * 12 + "end?utm_source=mesh&ref=abcdef0123456789"

WORST_NODE_NAMES = [
    LONG_SITE,                                    # long, with an ampersand
    "Café & Bar <Main St>",                       # markup characters in a real name
    "Jo",                                         # shortest realistic
    "山田 太郎 のノード",                           # CJK
    "👩🏽‍🚀 Mission Control",                      # emoji-first, ZWJ sequence
    "مركز الشبكة الرئيسي",                         # RTL
    "",                                           # announced with no name
    "Supercalifragilisticexpialidocious-Repeater-On-The-Hill-North-Facing-Antenna-Array-9000",  # no spaces
    "node-with-a-name-that-is-exactly-sixty-four-characters-long-ok-12",
]


def _node(i, name, hops, last_seen, count, **kw):
    d = {"hash": H(i + 1), "name": name, "hops": hops, "last_seen": last_seen,
         "first_seen": last_seen - 86400 if last_seen else 0, "announce_count": count,
         "favorited": False, "is_hosted": False, "is_default": False, "last_load_ok": None}
    d.update(kw)
    return d


def nodes(kind):
    if kind == "empty":
        return []
    if kind == "one":
        return [_node(0, "Test node", 2, NOW - 60, 1)]
    if kind == "demo":
        return [_node(i, n, h, NOW - 60 * (i + 1), 4 + i) for i, (n, h) in enumerate(
            [("NomadNet Hub", 1), ("Weather Station", 2), ("Library", 3), ("Mesh Wiki", 2), ("Radio Club", 4)])]
    if kind == "huge":
        return [_node(i, f"Node {i:04d}", i % 9, NOW - i * 30, i % 50 + 1) for i in range(1500)]
    hops = [0, 1, None, 2, 128, 3, 12, 1, None]
    seen = [NOW - 5, NOW - 86400 * 400, 0, NOW + 7200, NOW - 90, NOW - 3600 * 5, NOW - 86400 * 30, NOW - 59, NOW - 1]
    counts = [99999, 1, 0, 1284, 2, 3, 12, 1, 10**6]
    out = [_node(i, WORST_NODE_NAMES[i], hops[i], seen[i], counts[i]) for i in range(len(WORST_NODE_NAMES))]
    out[0]["is_hosted"] = True
    out[1]["is_default"] = True
    out[3]["favorited"] = True
    out[7]["last_load_ok"] = False
    return out


def peers(kind):
    if kind == "empty":
        return []
    if kind == "one":
        return [{"hash": H(50), "name": "Alice", "hops": 1, "last_seen": NOW - 30, "first_seen": NOW - 99,
                 "announce_count": 1, "call_capable": False}]
    names = {
        "demo": ["Alice", "Bob", "Carol", "Dave"],
        "worst": ["Aleksandra Wiśniewska-Kowalczyk", "Jo", "", "bartholomew.fitzgerald@northwind-industries-holdings.example.com",
                  "👩🏽‍🚀", "O'Brien <script>alert(1)</script>", "  ", "Мария Ивановна Петрова-Сидорова"],
    }
    if kind == "huge":
        return [{"hash": H(100 + i), "name": f"Peer {i}", "hops": i % 7, "last_seen": NOW - i * 20,
                 "first_seen": NOW - i * 20, "announce_count": 1, "call_capable": i % 5 == 0} for i in range(2500)]
    return [{"hash": H(50 + i), "name": n, "hops": [1, None, 0, 2, 5, 128, 3, 1][i % 8] if kind == "worst" else i + 1,
             "last_seen": NOW - 40 * (i + 1), "first_seen": NOW - 4000, "announce_count": i * 700 + 1,
             "call_capable": i % 2 == 0} for i, n in enumerate(names[kind])]


def relays(kind):
    if kind == "empty":
        return []
    n = {"one": 1, "demo": 3, "worst": 6, "huge": 400}[kind]
    return [{"hash": H(200 + i), "hops": [1, 2, None, 0, 4, 9][i % 6], "first_seen": NOW - 9999, "last_seen": NOW - i * 700,
             "announce_count": i * 1000 + 1} for i in range(n)]


def contacts(kind):
    ps = peers(kind) if kind != "huge" else peers("worst")
    return [{"hash": p["hash"], "name": p["name"]} for p in ps[:6]]


def messages(kind):
    """(received, sent)"""
    if kind == "empty":
        return [], []
    ps = peers("demo" if kind == "demo" else ("one" if kind == "one" else "worst"))
    rec, sent = [], []
    texts = {
        "demo": ["Hi there", "Are you on the mesh tonight?", "Thanks!", "See you"],
        "worst": ["ok", LONG_URL, "A " + "very long unbroken message " * 12 + "end", "x" * 90,
                  "Line one\nLine two\nLine three\nLine four\nLine five\nLine six", "<b>bold?</b> &amp; \"quotes\" 'single'",
                  "", "😀" * 40],
    }
    pool = texts.get(kind, texts["demo"])
    count = {"one": 1, "demo": 4, "worst": 8, "huge": 8}[kind]
    for i in range(count):
        p = ps[i % len(ps)]
        rec.append({"id": f"r{i}", "source": p["hash"], "content": pool[i % len(pool)], "received_at": NOW - 600 * i,
                    "read": i % 3 != 0, "owner": "admin"})
        if i % 2 == 0:
            sent.append({"id": f"s{i}", "dest": p["hash"], "content": pool[(i + 1) % len(pool)],
                         "sent_at": NOW - 600 * i - 30, "state": "delivered", "owner": "admin"})
    if kind == "huge":
        for i in range(600):
            rec.append({"id": f"h{i}", "source": H(300 + i % 150), "content": f"message {i}", "received_at": NOW - i * 60,
                        "read": i % 7 != 0, "owner": "admin"})
    return rec, sent


PANELS = {"admins_nodes_panel": True, "admins_messages_panel": True, "admins_network_panel": True,
          "admins_address_bar": "enabled"}


def ui(kind):
    """Settings the header and footer show, with every panel switched on."""
    d = _ui_text(kind)
    d.update(PANELS)
    return d


def _ui_text(kind):
    if kind == "worst":
        return {"app_title": "`F4af■ The Northern Prairie Amateur Radio & Emergency Communications Society Portal`f",
                "app_title_html": '<span style="color:#4af">■ The Northern Prairie Amateur Radio &amp; Emergency Communications Society Portal</span>',
                "abuse_contact": "abuse-reports@northern-prairie-radio-emergency-communications.example.org"}
    return {"app_title_html": '<span style="color:#4af">■ NomadPortal</span>', "abuse_contact": ""}


def user_name(kind):
    return {"worst": "Aleksandra Wiśniewska-Kowalczyk", "demo": "admin", "one": "Jo", "empty": "", "huge": "admin"}[kind]
