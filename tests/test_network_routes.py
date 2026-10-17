"""The data behind the network map: for every destination the portal has
heard, which interface its packets would leave through and which node is the
next hop. Read straight from Reticulum's routing table; best effort, never
raises."""

import pytest
from flask import Flask

from nomadnet_web import routes
from nomadnet_web.browser import NodeBrowser

A = "aa" * 16
B = "bb" * 16
C = "cc" * 16
HOP1 = bytes.fromhex("11" * 16)


class Iface:
    def __init__(self, name, online=True, rxb=0, txb=0):
        self.name, self.online, self.rxb, self.txb = name, online, rxb, txb


class Transport:
    def __init__(self, table):
        self.table = table                              # hex -> (hops, via bytes|None, Iface|None)
        self.interfaces = sorted({t[2] for t in table.values() if t[2]}, key=lambda i: i.name)

    def hops_to(self, h):
        t = self.table.get(h.hex())
        return t[0] if t else 128

    def next_hop(self, h):
        t = self.table.get(h.hex())
        return t[1] if t else None

    def next_hop_interface(self, h):
        t = self.table.get(h.hex())
        return t[2] if t else None


class Rns:
    def __init__(self, table):
        self.Transport = Transport(table)


def browser_with(table):
    b = object.__new__(NodeBrowser)                      # no Reticulum start-up
    b._rns = Rns(table)
    return b


class TestRouteInfo:
    def test_reports_interface_hops_and_next_hop(self):
        tcp, lora = Iface("TCP hub"), Iface("LoRa")
        b = browser_with({A: (2, HOP1, tcp), B: (1, None, lora)})
        out = b.route_info([A, B])
        assert out[A] == {"hops": 2, "iface": "TCP hub", "via": HOP1.hex()}
        assert out[B] == {"hops": 1, "iface": "LoRa", "via": None}

    def test_a_next_hop_that_is_the_destination_itself_means_direct(self):
        b = browser_with({A: (1, bytes.fromhex(A), Iface("x"))})
        assert b.route_info([A])[A]["via"] is None

    def test_a_destination_with_no_path_is_all_none(self):
        b = browser_with({})
        assert b.route_info([C]) == {C: {"hops": None, "iface": None, "via": None}}

    def test_unknown_hop_count_is_none_not_128(self):
        b = browser_with({A: (128, None, None)})
        assert b.route_info([A])[A]["hops"] is None

    def test_bad_hashes_are_skipped(self):
        b = browser_with({A: (1, None, Iface("x"))})
        out = b.route_info([A, "not-hex", "", "zz" * 16])
        assert list(out) == [A]

    def test_one_failing_lookup_does_not_lose_the_rest(self):
        t = Transport({A: (1, None, Iface("x")), B: (2, None, Iface("y"))})
        orig = t.next_hop_interface
        t.next_hop_interface = lambda h: (_ for _ in ()).throw(RuntimeError("boom")) if h.hex() == A else orig(h)
        b = object.__new__(NodeBrowser)
        b._rns = types_ns(Transport=t)
        out = b.route_info([A, B])
        assert out[A]["iface"] is None and out[B]["iface"] == "y"

    def test_the_number_of_hashes_is_bounded(self):
        b = browser_with({})
        many = [f"{i:032x}" for i in range(5000)]
        assert len(b.route_info(many)) <= 2000


def types_ns(**kw):
    import types
    return types.SimpleNamespace(**kw)


class FakeBrowser:
    def __init__(self, table, nodes):
        self._b = browser_with(table)
        self._nodes = nodes

    def get_nodes(self, **kw):
        return self._nodes

    def route_info(self, hashes):
        return self._b.route_info(hashes)

    def get_status(self):
        return {"interfaces": [{"name": i.name, "online": i.online, "rxb": i.rxb, "txb": i.txb}
                               for i in self._b._rns.Transport.interfaces]}


class FakeTracker:
    def get_peers(self):
        return [{"hash": B}]


class FakeProp:
    def list_known_nodes(self):
        return [{"hash": C}]


@pytest.fixture
def client():
    tcp = Iface("TCP hub", True, 10, 20)
    table = {A: (2, HOP1, tcp), B: (1, None, tcp), C: (3, HOP1, tcp)}
    app = Flask(__name__)
    app.config.update(TESTING=True, BROWSER=FakeBrowser(table, [{"hash": A}]),
                      LXMF_TRACKER=FakeTracker(), PROP_SYNC=FakeProp())
    app.register_blueprint(routes.bp)
    return app.test_client()


class TestEndpoint:
    def test_covers_sites_peers_and_relays_together(self, client):
        body = client.get("/api/network/routes").get_json()
        assert set(body["routes"]) == {A, B, C}
        assert body["routes"][A]["via"] == HOP1.hex() and body["routes"][B]["via"] is None

    def test_lists_the_interfaces_with_their_state(self, client):
        ifaces = client.get("/api/network/routes").get_json()["interfaces"]
        assert ifaces == [{"name": "TCP hub", "online": True, "rxb": 10, "txb": 20}]

    def test_is_a_public_read_like_the_lists_it_summarises(self, client):
        assert client.get("/api/network/routes").status_code == 200

    def test_missing_trackers_do_not_break_it(self):
        app = Flask(__name__)
        app.config.update(TESTING=True, BROWSER=FakeBrowser({}, []))
        app.register_blueprint(routes.bp)
        assert app.test_client().get("/api/network/routes").get_json()["routes"] == {}
