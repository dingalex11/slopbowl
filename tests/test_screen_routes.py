"""One address per screen: the shell is navigated by URL, not by hidden classes.

The routing and the challenge screen live in the browser, so — like the other frontend
tests here — these assert on the source. What they protect:

* every screen is reachable at a hash URL, and the home page carries the links (the bottom
  dock that used to carry them is gone)
* the packet converter and the analytics are real screens now, while the login form stays
  an overlay that is itself addressed
* joining a challenge lands on the challenge screen and does NOT begin the packet: the read
  starts when Start is pressed, and then the play area is the only thing on screen
* a shared link (#/challenge/CODE) resumes a claimed attempt and asks for a name otherwise
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX_SOURCE = (ROOT / "index.html").read_text(encoding="utf-8")
ROUTER_SOURCE = (ROOT / "static/js/router.js").read_text(encoding="utf-8")
SHELL_SOURCE = (ROOT / "static/js/slopbowl_ui.js").read_text(encoding="utf-8")
BUZZPOINT_SOURCE = (ROOT / "static/js/buzzpoint.js").read_text(encoding="utf-8")
UI_SOURCE = (ROOT / "static/js/ui.js").read_text(encoding="utf-8")

# Every screen a visitor can open, and the address that opens it.
SCREEN_URLS = (
    "#/", "#/board", "#/online", "#/analytics", "#/converter", "#/solo", "#/settings",
    "#/in-person", "#/hud", "#/bank", "#/lobby", "#/buzzer",
)


def function_source(source, name):
    """The text of one top-level function, from its definition to its closing brace."""
    start = source.index(f"function {name}(")
    end = source.index("\n}\n", start) + 3
    return source[start:end]


class RouteTableTests(unittest.TestCase):
    def test_every_slide_screen_has_a_route(self):
        self.assertIn("const SLIDES = {", ROUTER_SOURCE)
        for name, number in (("home", 1), ("online", 3), ("hud", 5), ("bank", 6),
                             ("buzzer", 8), ("board", 13)):
            self.assertIn(f"{name}: {number}", ROUTER_SOURCE,
                          f"slide screen {name} must keep its address")

    def test_the_stations_that_were_popups_have_their_own_screens(self):
        self.assertIn("const STATIONS = {", ROUTER_SOURCE)
        self.assertIn("converter: 'screen-converter'", ROUTER_SOURCE)
        self.assertIn("analytics: 'screen-analytics'", ROUTER_SOURCE)
        self.assertIn("challenge: 'screen-challenge'", ROUTER_SOURCE)

    def test_the_router_listens_to_every_way_a_url_changes(self):
        self.assertIn("window.addEventListener('hashchange'", ROUTER_SOURCE)
        self.assertIn("window.addEventListener('popstate'", ROUTER_SOURCE)
        self.assertIn("window.history.pushState({ route: target }", ROUTER_SOURCE)
        self.assertIn("window.history.replaceState({ route: target }", ROUTER_SOURCE)

    def test_the_router_publishes_the_route_the_screen_will_need(self):
        self.assertIn("window.routerNavigate = navigate;", ROUTER_SOURCE)
        self.assertIn("window.routerHashForSlide = hashForSlide;", ROUTER_SOURCE)
        self.assertIn("window.routerCloseLogin = closeLogin;", ROUTER_SOURCE)
        self.assertIn("new CustomEvent('app:view'", ROUTER_SOURCE)

    def test_a_challenge_url_carries_its_code_and_so_does_a_room_link(self):
        self.assertIn("params.code = String(parts[1] || '').toLowerCase();", ROUTER_SOURCE)
        self.assertIn("if (name === 'challenge') return p.code ? `#/challenge/${encodeURIComponent(p.code)}`", ROUTER_SOURCE)
        self.assertIn("if (name === 'join')", ROUTER_SOURCE)
        self.assertIn("return `#/join/${encodeURIComponent(p.room)}`;", ROUTER_SOURCE)

    def test_an_unknown_address_lands_home_instead_of_a_blank_page(self):
        self.assertIn("if (NAMES.indexOf(name) === -1)", ROUTER_SOURCE)
        self.assertIn("navigate('#/', { replace: true, force: true });", ROUTER_SOURCE)

    def test_the_router_loads_before_anything_that_navigates(self):
        self.assertLess(INDEX_SOURCE.index("static/js/router.js"),
                        INDEX_SOURCE.index("static/js/slopbowl_ui.js"))
        self.assertLess(INDEX_SOURCE.index("static/js/router.js"),
                        INDEX_SOURCE.index("static/js/ui.js"))


class ScreenLinkTests(unittest.TestCase):
    def test_the_bottom_dock_is_gone(self):
        self.assertNotIn("<!-- Bottom Nav Dock -->", INDEX_SOURCE)
        self.assertNotIn("nav-dock-btn", INDEX_SOURCE)
        self.assertNotIn("nav-btn-", INDEX_SOURCE)

    def test_the_home_page_carries_a_link_to_every_screen(self):
        for url in SCREEN_URLS:
            self.assertIn(f'href="{url}"', INDEX_SOURCE, f"no link to {url}")
        self.assertIn("data-route=", INDEX_SOURCE)

    def test_the_buzzboard_challenge_is_a_main_card(self):
        """The contest is a top-level mode, not one of the small links at the bottom."""
        grid = INDEX_SOURCE[INDEX_SOURCE.index('id="home-main-cards"')
                            :INDEX_SOURCE.index("<!-- Every other screen")]
        self.assertIn('href="#/board"', grid)
        self.assertIn('data-route="board"', grid)
        self.assertIn("Buzzboard Challenge", grid)
        # A real link, so it is keyboard-reachable and marks the screen it opens.
        self.assertIn("app-nav-link", grid)

    def test_the_small_link_row_drops_the_link_the_card_replaced(self):
        row = INDEX_SOURCE[INDEX_SOURCE.index("<!-- Every other screen")
                           :INDEX_SOURCE.index("<!-- Slide 1 Footer Buttons -->")]
        # The board is a main card now, so the row no longer duplicates it.
        self.assertNotIn('data-route="board"', row)
        # Everything else stays. The row cannot be dropped: the question bank and the HUD have
        # no other door in the app, and the row is what marks the screen you are on.
        for url in ("#/online", "#/solo", "#/analytics", "#/converter", "#/in-person",
                    "#/hud", "#/bank", "#/settings"):
            self.assertIn(f'href="{url}"', row, f"no link to {url}")

    def test_screen_links_keep_the_role_gating_the_dock_had(self):
        self.assertIn("app-nav-link moderator-nav-control", INDEX_SOURCE)
        self.assertIn("app-nav-link player-nav-control", INDEX_SOURCE)

    def test_the_current_screen_is_marked_from_the_route(self):
        self.assertIn("function setActiveNav(name)", ROUTER_SOURCE)
        self.assertIn("el.classList.toggle('is-active', active);", ROUTER_SOURCE)
        self.assertIn(".app-nav-link.is-active {", INDEX_SOURCE)

    def test_navigate_to_writes_a_url_instead_of_hiding_dom(self):
        body = function_source(SHELL_SOURCE, "navigateTo")
        self.assertIn("window.routerNavigate(window.routerHashForSlide(slideNum));", body)
        self.assertNotIn("classList.add('hidden')", body.split("// Fallback")[0])

    def test_a_popup_is_closed_by_its_url_and_not_only_by_a_hidden_class(self):
        self.assertIn("window.routerNavigate('#/login');", SHELL_SOURCE)
        self.assertIn("window.routerCloseLogin();", SHELL_SOURCE)
        self.assertIn("if (e.key === 'Escape') closeModal('auth-modal');", SHELL_SOURCE)
        self.assertNotIn("['auth-modal', 'converterModal', 'analyticsModal']", SHELL_SOURCE)


class PopupToScreenTests(unittest.TestCase):
    def test_the_converter_and_analytics_are_screens(self):
        self.assertIn('id="screen-converter" class="slide-view hidden', INDEX_SOURCE)
        self.assertIn('id="screen-analytics" class="slide-view hidden', INDEX_SOURCE)
        # The popup wrappers are gone, not merely renamed.
        self.assertNotIn('id="converterModal"', INDEX_SOURCE)
        self.assertNotIn('id="analyticsModal"', INDEX_SOURCE)
        self.assertNotIn('max-h-[90vh] overflow-y-auto custom-scrollbar', INDEX_SOURCE)
        self.assertNotIn("closeModal('converterModal')", INDEX_SOURCE)
        self.assertNotIn("closeModal('analyticsModal')", INDEX_SOURCE)

    def test_loading_a_converted_packet_leaves_the_converter_screen(self):
        self.assertIn("if (typeof window.routerNavigate === 'function') window.routerNavigate('#/');", SHELL_SOURCE)

    def test_login_is_still_an_overlay(self):
        self.assertIn('id="auth-modal" class="fixed inset-0 z-50', INDEX_SOURCE)


class ChallengeScreenTests(unittest.TestCase):
    def test_the_screen_has_four_stages(self):
        self.assertIn('id="screen-challenge" class="slide-view hidden', INDEX_SOURCE)
        for stage in ('id="challenge-stage-join"', 'id="challenge-stage-start"',
                      'id="buk-run"', 'id="challenge-stage-done"'):
            self.assertIn(stage, INDEX_SOURCE)
        self.assertIn("join: 'challenge-stage-join', start: 'challenge-stage-start',", BUZZPOINT_SOURCE)
        self.assertIn("run: 'buk-run', done: 'challenge-stage-done',", BUZZPOINT_SOURCE)

    def test_the_run_lives_in_the_challenge_screen_not_the_board(self):
        challenge_at = INDEX_SOURCE.index('id="screen-challenge"')
        run_at = INDEX_SOURCE.index('id="buk-run"')
        converter_at = INDEX_SOURCE.index('id="screen-converter"')
        self.assertLess(challenge_at, run_at)
        self.assertLess(run_at, converter_at)
        # The board slide keeps the captain tools and the join card, and nothing else.
        board_at = INDEX_SOURCE.index('id="slide-13"')
        self.assertLess(board_at, challenge_at)
        board_slice = INDEX_SOURCE[board_at:challenge_at]
        self.assertIn('id="buk-board"', board_slice)
        self.assertNotIn('id="buk-run"', board_slice)

    def test_joining_does_not_start_the_packet(self):
        body = function_source(BUZZPOINT_SOURCE, "bukJoinFromBoard")
        self.assertIn("await bukClaimAttempt(code, displayName);", body)
        self.assertIn("window.routerNavigate(`#/challenge/", body)
        self.assertNotIn("bukStartQuestion", body)

    def test_the_start_button_is_what_begins_the_read(self):
        body = function_source(BUZZPOINT_SOURCE, "bukStartChallenge")
        self.assertIn("await bukApi(`/api/challenges/${encodeURIComponent(bukRun.code)}/next`)", body)
        self.assertIn("bukShowStage('run');", body)
        self.assertIn("bukStartQuestion(next);", body)
        self.assertIn('id="buk-start-btn"', INDEX_SOURCE)
        self.assertIn("startBtn.addEventListener('click', bukStartChallenge);", BUZZPOINT_SOURCE)

    def test_the_start_screen_shows_the_settings_the_captain_chose(self):
        body = function_source(BUZZPOINT_SOURCE, "bukRenderChallengeStart")
        self.assertIn("set('buk-start-count'", body)
        self.assertIn("meta.readBonuses ? 'On (20s)' : 'Off'", body)
        self.assertIn("meta.displayMode === 'audio-only' ? 'Audio only' : 'Words + audio'", body)
        self.assertIn("(meta.packetSummary || {}).subjects", body)
        body_start = function_source(BUZZPOINT_SOURCE, "bukShowStartStage")
        self.assertIn("/api/challenges/${encodeURIComponent(bukRun.code)}", body_start)

    def test_a_rejoin_says_resume(self):
        self.assertIn("bukRun.joinStatus === 'resumed' ? 'Resume challenge' : 'Start challenge'",
                      BUZZPOINT_SOURCE)
        self.assertIn("bukRun.joinStatus = 'resumed';", BUZZPOINT_SOURCE)

    def test_a_shared_link_asks_for_a_name_and_resumes_a_claimed_attempt(self):
        body = function_source(BUZZPOINT_SOURCE, "bukEnterChallenge")
        # 403 is "join this challenge before playing": the member has not claimed it yet.
        self.assertIn("if (err.status === 403)", body)
        self.assertIn("bukShowStage('join');", body)
        self.assertIn("if (bukRun.claimedCode === clean) { bukShowStartStage(); return; }", body)
        self.assertIn("const next = await bukApi(`/api/challenges/${encodeURIComponent(clean)}/next`)", body)
        self.assertIn("document.addEventListener('app:view'", BUZZPOINT_SOURCE)
        self.assertIn("bukEnterChallenge(detail.params ? detail.params.code : '');", BUZZPOINT_SOURCE)

    def test_a_claimed_attempt_is_never_entered_twice(self):
        self.assertIn("function bukIsEntered(code)", BUZZPOINT_SOURCE)
        self.assertIn("if (bukIsEntered(clean)) return;", BUZZPOINT_SOURCE)

    def test_the_captain_gets_a_link_to_the_challenge(self):
        self.assertIn("async function bukCopyChallengeLink(code)", BUZZPOINT_SOURCE)
        self.assertIn("`${window.location.origin}${window.location.pathname}#/challenge/${code}`",
                      BUZZPOINT_SOURCE)
        self.assertIn('id="buk-captain-link"', INDEX_SOURCE)

    def test_exiting_asks_and_returns_to_the_board(self):
        body = function_source(BUZZPOINT_SOURCE, "bukExitChallenge")
        self.assertIn("window.confirm(", body)
        self.assertIn("window.routerNavigate('#/board');", body)
        # Nothing stays armed behind the exit: re-entering the URL decides the stage again.
        self.assertIn("bukShowStage('none');", body)
        self.assertIn("bukRun.stage = stage === 'none' ? null : stage;", BUZZPOINT_SOURCE)

    def test_leaving_by_any_route_stops_the_read(self):
        """The back button is not an exit, but it must not leave a question being read."""
        body = function_source(BUZZPOINT_SOURCE, "bukLeaveChallengeScreen")
        self.assertIn("bukStopDelivery();", body)
        self.assertIn("bukRun.question = null;", body)
        self.assertIn("bukShowStage('none');", body)
        self.assertIn("if (detail.name !== 'challenge') {", BUZZPOINT_SOURCE)
        self.assertIn("bukLeaveChallengeScreen();", BUZZPOINT_SOURCE)

    def test_the_finished_run_gives_way_to_the_summary(self):
        body = function_source(BUZZPOINT_SOURCE, "bukFinishRun")
        self.assertIn("bukShowStage('done');", body)
        self.assertIn("bukRenderOwnSummary();", body)
        self.assertIn('id="buk-own-summary" class="mt-4"></div>', INDEX_SOURCE)
        self.assertIn('href="#/board"', INDEX_SOURCE)

    def test_a_run_owns_the_window(self):
        self.assertIn("document.body.classList.toggle('app-focus', route.name === 'challenge');",
                      ROUTER_SOURCE)
        self.assertIn("body.app-focus > header {", INDEX_SOURCE)
        self.assertIn('id="buk-exit"', INDEX_SOURCE)


class RoomLinkTests(unittest.TestCase):
    def test_the_invite_link_is_a_screen_address(self):
        self.assertIn("#/join/${code}", UI_SOURCE)

    def test_a_room_link_fills_the_room_code_in(self):
        self.assertIn("document.addEventListener('app:view'", UI_SOURCE)
        self.assertIn("if (input && !input.value) input.value = room;", UI_SOURCE)
        self.assertIn("const room = detail.params && detail.params.room;", UI_SOURCE)

    def test_the_old_room_query_still_works(self):
        self.assertIn("const roomParam = params.get('room');", UI_SOURCE)
        self.assertIn("if (room && atHome)", ROUTER_SOURCE)


if __name__ == "__main__":
    unittest.main()
