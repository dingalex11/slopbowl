/* One page, one address per screen.
 *
 * Every screen is reachable at its own hash URL, and the URL — not a hidden class — is the
 * truth about which screen is open. That is what makes the back button, a refresh, and a
 * link the captain pastes into chat actually work.
 *
 * A "screen" is one of the existing `slide-N` sections, the two stations that used to be
 * popups (the packet converter and match analytics), or the challenge play screen. The
 * login form stays an overlay, but it is addressed too (`#/login`) so it can be linked and
 * the back button closes it.
 *
 * `navigateTo(n)` in slopbowl_ui.js writes a hash instead of touching the DOM, so every
 * existing caller keeps working and gains a real URL for free. The router listens for
 * `#/...` links, `pushState` navigation, `popstate` and `hashchange`, and performs the
 * actual show/hide in one place.
 */

(function () {
  'use strict';

  // Slide screens keep their numbers; the URL uses a readable name for each.
  const SLIDES = {
    home: 1,
    settings: 2,
    online: 3,
    'in-person': 4,
    hud: 5,
    bank: 6,
    prematch: 7,
    buzzer: 8,
    lobby: 9,
    'mod-setup': 10,
    solo: 11,
    'solo-dojo': 12,
    board: 13,
  };

  // Screens where two teams are actually playing. The header's team scores mean nothing
  // anywhere else, so a 0–0 pill over the home page, the stats board or solo practice is
  // just noise: it is shown only on these, and only once the match has begun.
  const LIVE_SCORE_SCREENS = ['hud', 'buzzer'];

  // Full-page stations that are not part of the numbered slide deck.
  const STATIONS = {
    converter: 'screen-converter',
    analytics: 'screen-analytics',
    challenge: 'screen-challenge',
  };

  const NAMES = Object.keys(SLIDES).concat(Object.keys(STATIONS)).concat(['login']);

  // ------------------------------------------------------------------ parsing

  function splitHash(hash) {
    const raw = String(hash == null ? window.location.hash : hash).replace(/^#/, '');
    const queryAt = raw.indexOf('?');
    const path = queryAt === -1 ? raw : raw.slice(0, queryAt);
    return path.split('/').filter(Boolean).map((part) => decodeURIComponent(part));
  }

  // `/challenge/ab12` and `/join/ab12` carry a parameter; everything else is a plain name.
  function parseHash(hash) {
    const parts = splitHash(hash);
    let name = parts[0] || 'home';
    const params = {};
    if (name === 'challenge') {
      params.code = String(parts[1] || '').toLowerCase();
    } else if (name === 'join') {
      name = 'online';
      params.room = String(parts[1] || '').toLowerCase();
    } else if (name === 'solo' && parts[1] === 'dojo') {
      name = 'solo-dojo';
    } else if (name === 'analytics') {
      // /analytics/<code> opens the analytics station on that challenge's board.
      params.code = String(parts[1] || '').toLowerCase();
    }
    if (NAMES.indexOf(name) === -1) {
      return { name: 'home', params: {}, unknown: true };
    }
    return { name: name, params: params };
  }

  function hashFor(name, params) {
    const p = params || {};
    if (name === 'challenge') return p.code ? `#/challenge/${encodeURIComponent(p.code)}` : '#/challenge';
    if (name === 'analytics' && p.code) return `#/analytics/${encodeURIComponent(p.code)}`;
    if (name === 'online' && p.room) return `#/join/${encodeURIComponent(p.room)}`;
    if (name === 'home') return '#/';
    return `#/${name}`;
  }

  function hashForSlide(slideNum) {
    const number = Number(slideNum);
    const name = Object.keys(SLIDES).filter((key) => SLIDES[key] === number)[0];
    return name ? hashFor(name, {}) : '#/';
  }

  // ------------------------------------------------------------------ elements

  function targetFor(name) {
    if (STATIONS[name]) return document.getElementById(STATIONS[name]);
    return SLIDES[name] ? document.getElementById(`slide-${SLIDES[name]}`) : null;
  }

  function loginEl() {
    return document.getElementById('auth-modal');
  }

  function showLogin(visible) {
    const el = loginEl();
    if (!el) return;
    el.classList.toggle('hidden', !visible);
    el.classList.toggle('flex', visible);
  }

  function loginIsOpen() {
    const el = loginEl();
    return Boolean(el && !el.classList.contains('hidden'));
  }

  function setActiveNav(name) {
    document.querySelectorAll('[data-route]').forEach((el) => {
      const active = el.getAttribute('data-route') === name;
      el.classList.toggle('is-active', active);
      if (active) el.setAttribute('aria-current', 'page');
      else el.removeAttribute('aria-current');
    });
  }

  // The HUD opens on a BEGIN MATCH overlay, so while that overlay is up the match has not
  // started and the score is still meaningless. beginMatch() calls this again to reveal it.
  function updateHeaderScore() {
    const pill = document.getElementById('header-match-score');
    if (!pill) return;
    const view = document.body.getAttribute('data-view');
    const overlay = document.getElementById('match-start-overlay');
    const awaitingBegin = view === 'hud' && Boolean(overlay) && !overlay.classList.contains('hidden');
    const live = Boolean(view) && LIVE_SCORE_SCREENS.indexOf(view) !== -1 && !awaitingBegin;
    pill.classList.toggle('is-live', live);
  }

  // ------------------------------------------------------------------ applying

  let currentRoute = null;
  let loginReturn = '#/';

  function apply(hash, options) {
    const opts = options || {};
    const route = parseHash(hash);

    // The login overlay sits over the screen you were already on, so opening or closing it
    // must not lose that screen — the URL records it for the way back.
    if (route.name === 'login') {
      loginReturn = currentRoute
        ? hashFor(currentRoute.name, currentRoute.params)
        : (window.location.hash && window.location.hash !== '#/login' ? window.location.hash : '#/');
      showLogin(true);
      setActiveNav('login');
      return route;
    }

    const same = currentRoute
      && currentRoute.name === route.name
      && (currentRoute.params.code || '') === (route.params.code || '')
      && (currentRoute.params.room || '') === (route.params.room || '');
    if (same && !loginIsOpen() && !opts.force) return route;

    const target = targetFor(route.name);
    if (!target) {
      // An unknown or missing screen still has to leave the app somewhere sane.
      navigate('#/', { replace: true, force: true });
      return route;
    }

    document.querySelectorAll('.slide-view').forEach((el) => el.classList.add('hidden'));
    target.classList.remove('hidden');
    showLogin(false);

    // A challenge run owns the whole window: no site header, no half-visible station.
    document.body.classList.toggle('app-focus', route.name === 'challenge');
    document.body.setAttribute('data-view', route.name);
    updateHeaderScore();

    const previous = currentRoute;
    currentRoute = route;

    // Leaving a station releases anything that was speaking, exactly as navigateTo did.
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    if (typeof window.soloLeavePractice === 'function') {
      window.soloLeavePractice(SLIDES[route.name] || null);
    }

    setActiveNav(route.name);
    if (!opts.silent) {
      document.dispatchEvent(new CustomEvent('app:view', {
        detail: { name: route.name, params: route.params, previous: previous ? previous.name : null },
      }));
    }
    window.scrollTo({ top: 0, behavior: opts.instant ? 'auto' : 'smooth' });
    return route;
  }

  function navigate(hash, options) {
    const opts = options || {};
    let target;
    if (typeof hash === 'number') target = hashForSlide(hash);
    else target = (hash && String(hash).charAt(0) === '#') ? String(hash) : '#/';

    const url = window.location.pathname + window.location.search + target;
    if (window.location.hash === target) {
      // Same address: no new history entry, but the screen still has to be right.
      apply(target, opts);
      return target;
    }
    if (opts.replace) window.history.replaceState({ route: target }, '', url);
    else window.history.pushState({ route: target }, '', url);
    apply(target, opts);
    return target;
  }

  function replace(hash, options) {
    return navigate(hash, Object.assign({}, options || {}, { replace: true }));
  }

  // Closing the login overlay returns to the screen recorded when it opened — the URL in
  // the bar and the screen behind the overlay never disagree.
  function closeLogin() {
    const back = loginReturn || '#/';
    loginReturn = '#/';
    showLogin(false);
    const target = replace(back);
    // The screen behind the overlay never changed, so apply() treats the return as a
    // no-op and the mark still says "login": hand it back to the screen we returned to.
    setActiveNav(parseHash(target).name);
    return target;
  }

  function current() {
    return currentRoute ? { name: currentRoute.name, params: Object.assign({}, currentRoute.params) } : null;
  }

  function start() {
    const hash = window.location.hash;
    const room = (new URLSearchParams(window.location.search).get('room') || '').toLowerCase();
    const atHome = !hash || hash === '#' || hash === '#/';
    // ?room= keeps working: it lands on the online hub with the code already filled in. A
    // page that has already navigated itself (the room join does) is left alone.
    if (room && atHome) {
      replace(`#/join/${encodeURIComponent(room)}`, { force: true, instant: true });
      return;
    }
    if (hash && hash !== '#') {
      apply(hash, { force: true, instant: true });
      return;
    }
    replace('#/', { force: true, instant: true });
  }

  window.addEventListener('hashchange', () => apply(window.location.hash));
  window.addEventListener('popstate', () => apply(window.location.hash));

  window.routerApply = apply;
  window.routerNavigate = navigate;
  window.routerReplace = replace;
  window.routerCurrent = current;
  window.routerHashForSlide = hashForSlide;
  window.routerCloseLogin = closeLogin;
  window.updateHeaderScore = updateHeaderScore;
  window.routerStart = start;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
