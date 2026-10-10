import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class FrontendSecurityTests(unittest.TestCase):
    def test_roster_editor_uses_dom_value_not_html_interpolation(self):
        source = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
        self.assertIn("input.value = currentText", source)
        self.assertNotIn('value="${currentText}"', source)

    def test_analytics_renders_imported_values_as_text(self):
        source = (ROOT / "static/js/analytics.js").read_text(encoding="utf-8")
        self.assertIn("playerName.textContent", source)
        self.assertIn("teamCell.textContent", source)
        self.assertNotIn("innerHTML = `", source)

    def test_csv_parser_handles_commas_quotes_and_multiline_fields(self):
        source = (ROOT / "static/js/analytics.js").read_text(encoding="utf-8")
        self.assertIn("function parseCSVRows(csvText)", source)
        self.assertIn("csvText[i + 1] === '\"'", source)
        self.assertIn("char === '\\n'", source)
        self.assertIn("file.size > 10 * 1024 * 1024", source)

    def test_match_controls_are_locked_to_the_moderator_station(self):
        source = (ROOT / "services/ui_service.py").read_text(encoding="utf-8")
        self.assertIn('OWNER_ONLY_OUTPUTS = {"START_READING", "START_LISTENING", "UPDATE_QUESTION", "UI_STATE", "NEW_LOG_ENTRY", "SYNC_LOG", "DOWNLOAD_CSV", "DEBUG_FRAME"}', source)
        self.assertIn('kind in OWNER_ONLY_OUTPUTS', source)

    def test_online_controls_are_bound_to_server_phase_team_and_captain(self):
        source = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
        self.assertIn("currentTeam === currentOnlineBuzzTeam", source)
        self.assertIn("onlineCaptainOnly && !isOnlineCaptain", source)
        self.assertIn("renderOnlineRoster", source)
        self.assertIn("ROOM_ROSTER", source)

    def test_room_codes_are_validated_instead_of_rewritten(self):
        source = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
        self.assertIn("!/^[a-z0-9][a-z0-9-]{0,63}$/.test(currentRoomId)", source)
        self.assertNotIn("replace(/[^a-z0-9-]/g, '-')", source)

    def test_online_room_api_helpers_do_not_override_lobby_handlers(self):
        auth_source = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
        ui_source = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")
        self.assertIn("window.createOnlineRoomApi = async", auth_source)
        self.assertIn("window.joinOnlineRoomApi = async", auth_source)
        self.assertNotIn("window.createOnlineRoom = async", auth_source)
        self.assertNotIn("window.joinOnlineRoom = async", auth_source)
        self.assertIn("window.createOnlineRoomApi(roomId, name, team, captain)", ui_source)
        self.assertIn("window.joinOnlineRoomApi(roomId, name, team, captain)", ui_source)


if __name__ == "__main__":
    unittest.main()
