"""``prescribed_nodes`` handling in UISettings and clean_node_list."""

from nomadnet_web.ui_settings import MAX_PRESCRIBED_NODES, UISettings, clean_node_list

A = "aa" * 16
B = "bb" * 16


class TestCleanNodeList:
    def test_accepts_a_list(self):
        assert clean_node_list([A, B]) == [A, B]

    def test_accepts_newline_comma_and_space_separated_text(self):
        assert clean_node_list(f"{A}\n{B}") == [A, B]
        assert clean_node_list(f"{A}, {B}") == [A, B]
        assert clean_node_list(f"  {A}   {B}  ") == [A, B]

    def test_lowercases_and_deduplicates_keeping_first_seen_order(self):
        assert clean_node_list([B.upper(), A, B]) == [B, A]

    def test_drops_non_hex_entries(self):
        assert clean_node_list([A, "not-hex", "zz", "", "  "]) == [A]

    def test_wrong_types_give_an_empty_list(self):
        assert clean_node_list(None) == []
        assert clean_node_list(42) == []

    def test_length_is_capped(self):
        many = [f"{i:032x}" for i in range(MAX_PRESCRIBED_NODES + 10)]
        assert len(clean_node_list(many)) == MAX_PRESCRIBED_NODES


class TestStoredSetting:
    def make(self, tmp_path):
        return UISettings(str(tmp_path))

    def test_defaults_to_empty(self, tmp_path):
        assert self.make(tmp_path).get_all()["prescribed_nodes"] == []

    def test_update_normalises_and_persists(self, tmp_path):
        ui = self.make(tmp_path)
        ui.update({"prescribed_nodes": f"{A.upper()}\n{B}\n{A}"})
        assert ui.get_all()["prescribed_nodes"] == [A, B]
        assert self.make(tmp_path).get_all()["prescribed_nodes"] == [A, B]

    def test_default_node_is_left_alone(self, tmp_path):
        ui = self.make(tmp_path)
        ui.update({"default_node": A, "prescribed_nodes": [B]})
        data = ui.get_all()
        assert data["default_node"] == A and data["prescribed_nodes"] == [B]

    def test_mutating_a_returned_list_does_not_change_the_settings(self, tmp_path):
        ui = self.make(tmp_path)
        ui.update({"prescribed_nodes": [A]})
        ui.get_all()["prescribed_nodes"].append(B)
        assert ui.get_all()["prescribed_nodes"] == [A]
        (tmp_path / "x").mkdir()
        assert self.make(tmp_path / "x").get_all()["prescribed_nodes"] == []
