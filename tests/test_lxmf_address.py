"""Accepting the ways people share an LXMF address: a bare hash,
``lxmf@<hash>``, and the ``lxma://<hash>:<public key>`` link this portal's
QR code produces. A link with a public key lets the portal message that
person before their announce arrives, but only if the key really belongs to
the address; otherwise anyone could plant a false key in the cache."""

import pytest
import RNS

from nomadnet_web.lxmf_address import LxmfAddress, parse_lxmf_address, remember_identity

HASH = "ab" * 16
KEY = "cd" * 64


class TestParse:
    @pytest.mark.parametrize("text", [
        HASH, HASH.upper(), f"  {HASH}\n", f"lxmf@{HASH}", f"LXMF@{HASH}", f"<{HASH}>",
        f"<lxmf@{HASH}>", f"lxmf://{HASH}", f"lxmf:{HASH}", f"lxmf@ {HASH}", f"{HASH[:8]} {HASH[8:]}",
    ])
    def test_accepted_forms_give_the_bare_hash(self, text):
        a = parse_lxmf_address(text)
        assert a.dest_hash == HASH and a.public_key is None

    def test_a_qr_link_carries_the_public_key(self):
        a = parse_lxmf_address(f"lxma://{HASH}:{KEY}")
        assert (a.dest_hash, a.public_key) == (HASH, KEY)

    def test_case_and_whitespace_in_a_qr_link(self):
        a = parse_lxmf_address(f"  LXMA://{HASH.upper()}:{KEY.upper()}  ")
        assert (a.dest_hash, a.public_key) == (HASH, KEY)

    @pytest.mark.parametrize("text,why", [
        ("", "enter"), ("   ", "enter"), (None, "enter"),
        ("ab" * 15, "32"), ("ab" * 17, "32"), ("zz" * 16, "32"), (f"lxmf@{'ab' * 15}", "32"),
        (f"lxma://{HASH}", "public key"), (f"lxma://{HASH}:", "public key"),
        (f"lxma://{HASH}:{'cd' * 10}", "public key"), (f"lxma://{HASH}:{'zz' * 64}", "public key"),
        (f"lxma://{'ab' * 15}:{KEY}", "32"),
        ("https://example.com", "32"), ("nomadnetwork://" + HASH + "/page/index.mu", "32"),
    ])
    def test_bad_input_is_refused_with_a_reason(self, text, why):
        with pytest.raises(ValueError) as e:
            parse_lxmf_address(text)
        assert why in str(e.value).lower()

    def test_a_non_string_is_refused(self):
        with pytest.raises(ValueError):
            parse_lxmf_address(12345)


class TestRememberIdentity:
    """Uses real Reticulum identities (no network needed)."""

    @pytest.fixture
    def ident(self, monkeypatch):
        # Newer Reticulum's recall() notes the lookup on the running instance;
        # there is none here, so give it one that ignores the note.
        class _Instance:
            def _used_destination_data(self, _h):
                pass
        monkeypatch.setattr(RNS.Reticulum, "get_instance", staticmethod(lambda: _Instance()))
        i = RNS.Identity()
        dest = RNS.Destination.hash_from_name_and_identity("lxmf.delivery", i)
        yield i, dest
        RNS.Identity.known_destinations.pop(dest, None)

    def test_a_matching_key_makes_the_identity_known(self, ident):
        i, dest = ident
        assert RNS.Identity.recall(dest) is None
        addr = LxmfAddress(dest.hex(), i.get_public_key().hex())
        assert remember_identity(RNS, addr) is True
        recalled = RNS.Identity.recall(dest)
        assert recalled is not None and recalled.get_public_key() == i.get_public_key()

    def test_a_key_that_belongs_to_another_address_is_refused(self, ident):
        i, dest = ident
        other = RNS.Identity()
        addr = LxmfAddress(dest.hex(), other.get_public_key().hex())
        with pytest.raises(ValueError) as e:
            remember_identity(RNS, addr)
        assert "does not belong" in str(e.value)
        assert RNS.Identity.recall(dest) is None                     # nothing was planted

    def test_an_address_without_a_key_changes_nothing(self, ident):
        i, dest = ident
        assert remember_identity(RNS, LxmfAddress(dest.hex(), None)) is False
        assert RNS.Identity.recall(dest) is None

    def test_garbage_key_bytes_are_refused_not_crashed_on(self, ident):
        i, dest = ident
        with pytest.raises(ValueError):
            remember_identity(RNS, LxmfAddress(dest.hex(), "00" * 64))

    def test_an_already_known_identity_is_left_alone(self, ident):
        i, dest = ident
        RNS.Identity.remember(None, dest, i.get_public_key())
        addr = LxmfAddress(dest.hex(), i.get_public_key().hex())
        assert remember_identity(RNS, addr) is True
