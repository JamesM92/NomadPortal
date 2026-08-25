"""Tests for CallSettings/CallSettingsManager — per-account "allow
incoming calls" + "calls from contacts only" persistence. Same per-user
YAML-file-per-account storage shape as contact_store.py's
ContactStoreManager (see call_settings.py's own doc comment).
"""

from nomadnet_web.call_settings import CallSettings, CallSettingsManager


def test_defaults_match_call_managers_own_real_defaults(tmp_path):
    settings = CallSettings(str(tmp_path))
    # Same combination CallManager.__init__ defaults to -- see that
    # class's own doc comment for why calls_enabled=False +
    # contacts_only=True (not either extreme) is the safe starting point.
    assert settings.get_calls_enabled() is False
    assert settings.get_contacts_only() is True


def test_set_calls_enabled_persists(tmp_path):
    settings = CallSettings(str(tmp_path))
    settings.set_calls_enabled(True)
    assert settings.get_calls_enabled() is True

    reloaded = CallSettings(str(tmp_path))
    assert reloaded.get_calls_enabled() is True


def test_set_contacts_only_persists(tmp_path):
    settings = CallSettings(str(tmp_path))
    settings.set_contacts_only(False)
    assert settings.get_contacts_only() is False

    reloaded = CallSettings(str(tmp_path))
    assert reloaded.get_contacts_only() is False


def test_setting_one_field_does_not_disturb_the_other(tmp_path):
    settings = CallSettings(str(tmp_path))
    settings.set_calls_enabled(True)
    settings.set_contacts_only(False)
    settings.set_calls_enabled(False)

    assert settings.get_calls_enabled() is False
    assert settings.get_contacts_only() is False  # untouched by the calls_enabled flip


def test_as_dict_shape(tmp_path):
    settings = CallSettings(str(tmp_path))
    settings.set_calls_enabled(True)
    assert settings.as_dict() == {"calls_enabled": True, "contacts_only": True}


def test_manager_gives_the_same_store_for_the_same_user(tmp_path):
    mgr = CallSettingsManager(str(tmp_path))
    a = mgr.for_user("u1")
    b = mgr.for_user("u1")
    assert a is b


def test_manager_gives_independent_stores_for_different_users(tmp_path):
    mgr = CallSettingsManager(str(tmp_path))
    mgr.for_user("u1").set_calls_enabled(True)
    mgr.for_user("u2").set_calls_enabled(False)

    assert mgr.for_user("u1").get_calls_enabled() is True
    assert mgr.for_user("u2").get_calls_enabled() is False


def test_manager_state_survives_a_fresh_instance(tmp_path):
    mgr = CallSettingsManager(str(tmp_path))
    mgr.for_user("u1").set_calls_enabled(True)

    reloaded = CallSettingsManager(str(tmp_path))
    assert reloaded.for_user("u1").get_calls_enabled() is True


def test_a_store_file_that_predates_a_field_falls_back_to_its_default(tmp_path):
    # Simulates a settings.yml written before contacts_only existed --
    # loading it must not crash, and the missing field reads as its
    # documented default rather than None/False-by-accident.
    identities_dir = tmp_path / "call_settings"
    identities_dir.mkdir()
    import yaml
    with open(identities_dir / "u_abc.yml", "w", encoding="utf-8") as fh:
        yaml.dump({"calls_enabled": True}, fh)

    settings = CallSettings(str(identities_dir), filename="u_abc.yml")
    assert settings.get_calls_enabled() is True
    assert settings.get_contacts_only() is True  # falls back to the default
