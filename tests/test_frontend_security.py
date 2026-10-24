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
        # Buzz eligibility must come from the server's window: closed windows and the
        # other team are excluded, while an open window (no team set) admits both teams.
        self.assertIn("function buzzWindowAllowsMe()", source)
        self.assertIn("if (!onlineBuzzOpen) return false;", source)
        self.assertIn("if (currentOnlineBuzzTeam && currentTeam !== currentOnlineBuzzTeam) return false;", source)
        self.assertIn("onlineCaptainOnly && !isOnlineCaptain", source)
        self.assertIn("renderOnlineRoster", source)
        self.assertIn("ROOM_ROSTER", source)

    def test_a_stopped_or_replaced_read_never_reports_completion(self):
        """Only the read that is actually being spoken may report completion.

        Chrome fires 'error: interrupted' when speech is cancelled, which the old
        client forwarded as READING_DONE. The server takes the next completion as the
        current read finishing, so those phantom replies skipped the following
        question and started its 5-second buzz timer at the same moment.
        """
        source = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
        self.assertIn("let readingGeneration = 0;", source)
        self.assertIn("const generation = ++readingGeneration;", source)
        self.assertIn("if (generation !== readingGeneration) return;", source)
        self.assertIn("function cancelSpeech()", source)
        self.assertIn("function sendReadingDone(readingId)", source)
        # A station whose browser cannot speak must not claim the read happened: it
        # retries once and then reports the failure with the read's id.
        self.assertIn("function sendReadingFailed(readingId)", source)
        self.assertIn("sendEvent('READING_FAILED', { reading_id: readingId || currentReadingId });", source)
        self.assertIn("scheduleUtterance(text, readingId, generation, 1);", source)
        self.assertIn("}, attempt === 0 ? 50 : 350);", source)
        # The buzz path stops the read through cancelSpeech (handlers detached) rather
        # than cancelling the speech engine directly, which left a completion behind.
        self.assertNotIn("if (isReading) {\n                synth.cancel();", source)
        # Completions carry the id of the read they belong to, not whichever read the
        # page happens to be on when the event fires.
        self.assertIn("const readingId = payload.reading_id || currentReadingId;", source)
        self.assertIn("speakText(payload.text, payload.reading_id || null);", source)

    def test_a_read_whose_end_event_never_arrives_still_reports_completion(self):
        """A long question can stop speaking without Chromium delivering 'end'.

        The engine's own state is then the only truth about the read. Without polling it
        the station never reports the question as finished, so the room waits out the
        server's watchdog and the 5-second buzz timer starts long after the read.
        """
        source = (ROOT / "static/js/websocket.js").read_text(encoding="utf-8")
        self.assertIn("const watchSpokenEnd = () => {", source)
        # The poll must actually run for the utterance, not merely exist.
        self.assertIn("\n        watchSpokenEnd();", source)
        self.assertIn("if (synth.speaking || synth.pending) {", source)
        # A paused match holds the utterance, so waiting on the engine cannot end the read.
        self.assertIn("so a pause is not an end", source)
        self.assertIn("utterance.onstart = () => { spoken = true; };", source)
        # The utterance may only report one outcome: 'end', the poll and a retry cannot
        # each claim the same read finished or failed.
        self.assertIn("if (generation !== readingGeneration || finished) return;", source)

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
        # Joining uses a default team and captain; players and the moderator claim their
        # real seat on the pre-match and lobby screens.
        self.assertIn("window.createOnlineRoomApi(name, 'Team A', false)", ui_source)
        self.assertIn("window.joinOnlineRoomApi(roomId, name, 'Team A', false)", ui_source)


class SignInAndSavedJudgeKeyTests(unittest.TestCase):
    """The Settings page must sign in, and keep the judge key on the account.

    Two failures are covered here. A missing Firebase config used to abort auth.js at
    module scope, so every sign-in button was dead and said nothing; the page now carries
    the project's own public web config as a fallback, and a config problem is reported on
    screen instead of thrown. And the judge key used to be deleted from the profile on
    every sign-in, so a captain re-pasted it each session.
    """

    def setUp(self):
        self.auth = (ROOT / "static/js/auth.js").read_text(encoding="utf-8")
        self.index = (ROOT / "index.html").read_text(encoding="utf-8")

    def test_missing_firebase_config_does_not_abort_the_auth_module(self):
        self.assertNotIn('throw new Error("Firebase configuration is missing', self.auth)
        self.assertIn("if (!firebaseConfig.apiKey || !firebaseConfig.projectId) {", self.auth)
        self.assertIn("configError = error;", self.auth)
        # The buttons are still wired, and explain the problem rather than doing nothing.
        self.assertIn("if (!auth) {", self.auth)
        self.assertIn("displayError(CONFIG_HELP);", self.auth)

    def test_index_carries_the_project_config_as_an_injected_fallback(self):
        self.assertIn('apiKey: "__FIREBASE_API_KEY__"', self.index)
        self.assertIn('projectId: "__FIREBASE_PROJECT_ID__"', self.index)
        self.assertIn("config[key] = injected[key] || project[key];", self.index)

    def test_settings_sign_in_opens_the_login_overlay(self):
        self.assertIn("btnSettingsAccount?.addEventListener('click'", self.auth)
        self.assertIn("openModal('auth-modal')", self.auth)

    def test_settings_account_row_tracks_the_signed_in_user(self):
        self.assertIn('id="settings-account-email"', self.index)
        self.assertIn('id="btn-settings-account"', self.index)
        self.assertIn("settingsAccountEmail.innerText = user ? (user.email || 'Signed in') : 'Not signed in';", self.auth)
        self.assertIn("btnSettingsAccount.innerText = user ? 'Sign out' : 'Sign in';", self.auth)

    def test_judge_key_is_saved_to_the_signed_in_account(self):
        self.assertIn("const JUDGE_KEY_FIELD = 'judgeApiKey';", self.auth)
        self.assertIn("await setDoc(doc(db, 'users', user.uid), {", self.auth)
        self.assertIn("[JUDGE_KEY_FIELD]: apiKey,", self.auth)
        self.assertIn("settings-key-badge", self.index)
        # Saving is offered, and so is taking the key back off the account.
        self.assertIn('id="btn-clear-api-key"', self.index)
        self.assertIn("[JUDGE_KEY_FIELD]: deleteField(),", self.auth)

    def test_a_saved_key_is_loaded_on_sign_in_instead_of_being_wiped(self):
        self.assertIn("async function loadJudgeKey(user)", self.auth)
        self.assertIn("const key = await loadJudgeKey(user);", self.auth)
        self.assertIn("settingsApiKeyInput.value = key;", self.auth)
        self.assertIn("window.sessionGroqApiKey = key;", self.auth)
        # Reading the key must never clear the stored one. Only the abandoned legacy field
        # is removed, as part of moving it to the current one.
        load_body = self.auth[self.auth.index("async function loadJudgeKey(user)"):]
        load_body = load_body[:load_body.index("\n}")]
        self.assertNotIn("[JUDGE_KEY_FIELD]: deleteField()", load_body)
        self.assertIn("[LEGACY_JUDGE_KEY_FIELD]: deleteField()", load_body)


if __name__ == "__main__":
    unittest.main()
