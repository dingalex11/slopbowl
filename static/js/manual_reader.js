// In-person manual-reader HUD.
//
// The moderator reads the packet aloud, clicks the named player who buzzed, then
// clicks the word where the buzz happened. Packet text is rendered with
// textContent only: a packet is untrusted input, so no packet string may become
// HTML. The server owns scoring; this module only presents the phase and sends
// the moderator's marks.
(function () {
    'use strict';

    const TEAM_SEATS = {
        'Team A': ['A-1', 'A-C', 'A-3', 'A-4', 'A-5'],
        'Team B': ['B-1', 'B-C', 'B-3', 'B-4', 'B-5']
    };
    const SEAT_PREFIX = { '1': 'P1', 'C': 'C', '3': 'P3', '4': 'P4', '5': 'P5' };
    const PHASE_LABELS = {
        IDLE: 'IDLE',
        READING: 'READING',
        BUZZ_WINDOW: '5s BUZZ WINDOW',
        ANSWERING: 'ANSWERING',
        BONUS_READ: 'BONUS READ',
        BONUS_CONFER: 'BONUS CONFER 20s'
    };

    let manualMode = false;
    let phase = 'IDLE';
    let question = { raw_text: '', answer: '', options: [], phase: 'TOSSUP', allowed_team: null };
    let roster = {
        teams: { 'Team A': 'Team A', 'Team B': 'Team B' },
        players: {}
    };
    let readStartedAt = null;
    let readClockInterval = null;
    let buzzActive = false;
    let selectedBuzzpoint = '';
    let afterRead = false;
    let verdictSent = false;
    let buzzClickTimeout = null;

    function $(id) {
        return document.getElementById(id);
    }

    function setText(id, text) {
        const el = $(id);
        if (el) el.textContent = text == null ? '' : String(text);
    }

    function phaseLabel(value) {
        return PHASE_LABELS[value] || String(value || 'IDLE').replace(/_/g, ' ');
    }

    function isReadPhase(value) {
        return value === 'READING' || value === 'BONUS_READ';
    }

    function canClickBuzzers() {
        return manualMode && !buzzActive && (phase === 'READING' || phase === 'BUZZ_WINDOW');
    }

    function isBonusPhase() {
        return phase === 'BONUS_READ' || phase === 'BONUS_CONFER';
    }

    function setVerdictLabels(bonus) {
        const correct = $('btn-manual-correct');
        const incorrect = $('btn-manual-incorrect');
        if (correct) correct.textContent = bonus ? 'Correct +10' : 'Correct +4';
        if (incorrect) incorrect.textContent = bonus ? 'Incorrect 0' : 'Incorrect';
    }

    // --- READ CLOCK ---------------------------------------------------------
    function formatSeconds(seconds) {
        const value = Math.max(0, Number(seconds) || 0);
        return value.toFixed(1) + 's';
    }

    function stopReadClock() {
        if (readClockInterval) clearInterval(readClockInterval);
        readClockInterval = null;
    }

    function startReadClock(startedAt) {
        stopReadClock();
        readStartedAt = Number(startedAt) || Date.now();
        const paint = () => {
            const elapsed = (Date.now() - readStartedAt) / 1000;
            setText('manual-read-clock', formatSeconds(elapsed));
        };
        paint();
        readClockInterval = setInterval(paint, 100);
    }

    function resetReadClock() {
        stopReadClock();
        readStartedAt = null;
        setText('manual-read-clock', '0.0s');
    }

    // --- ROSTER / BUZZERS ---------------------------------------------------
    function normalizeRoster(data) {
        if (!data || typeof data !== 'object') return;
        const teams = data.teams && typeof data.teams === 'object' ? data.teams : {};
        const players = data.players && typeof data.players === 'object' ? data.players : {};
        roster = {
            teams: {
                'Team A': String(teams['Team A'] || 'Team A').slice(0, 64),
                'Team B': String(teams['Team B'] || 'Team B').slice(0, 64)
            },
            players: {}
        };
        Object.keys(players).forEach((seat) => {
            if (TEAM_SEATS['Team A'].includes(seat) || TEAM_SEATS['Team B'].includes(seat)) {
                roster.players[seat] = String(players[seat] || '').slice(0, 64);
            }
        });
        renderBuzzers();
    }

    function seatLabel(seat) {
        return SEAT_PREFIX[seat.split('-')[1]] || 'P';
    }

    function renderBuzzers() {
        renderTeamBuzzers('Team A', 'manual-team-a-buzzers');
        renderTeamBuzzers('Team B', 'manual-team-b-buzzers');
    }

    function renderTeamBuzzers(team, holderId) {
        const holder = $(holderId);
        if (!holder) return;
        holder.replaceChildren();

        TEAM_SEATS[team].forEach((seat) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.dataset.seat = seat;
            button.dataset.team = team;
            button.className = 'manual-player-buzz min-w-[92px] flex-1 bg-black border border-borderDark text-slate-300 hover:border-neonCyan hover:text-neonCyan px-2 py-1.5 rounded-lg font-mono text-[11px] transition disabled:opacity-35 disabled:cursor-not-allowed';
            const name = roster.players[seat] || ('Player ' + seat.split('-')[1]);
            button.textContent = seatLabel(seat) + ' · ' + name;
            button.title = team + ' / ' + seat;
            button.addEventListener('click', () => buzz(team, seat));
            holder.appendChild(button);
        });
        updateBuzzButtons();
    }

    function updateBuzzButtons() {
        const allowed = canClickBuzzers();
        document.querySelectorAll('.manual-player-buzz').forEach((button) => {
            const team = button.dataset.team;
            const teamAllowed = !question.allowed_team || question.allowed_team === team;
            button.disabled = !allowed || !teamAllowed;
        });

        const hint = $('manual-buzz-hint');
        if (hint) {
            if (buzzActive) {
                hint.textContent = 'Buzz received. Mark the word where the player buzzed, then judge the answer.';
            } else if (phase === 'READING') {
                hint.textContent = 'Buzz buttons are live during the read. A wrong interrupt is a NEG (−4).';
            } else if (phase === 'BUZZ_WINDOW') {
                hint.textContent = '5-second window open. Buzz buttons are live.';
            } else if (phase === 'ANSWERING') {
                hint.textContent = 'Answer in progress. Mark the buzzpoint, then judge.';
            } else if (phase === 'BONUS_READ' || phase === 'BONUS_CONFER') {
                hint.textContent = 'Bonus: no buzz. Read the bonus, run the confer clock, then mark +10 or 0.';
            } else {
                hint.textContent = 'Start the next tossup to unlock the buzz buttons.';
            }
        }
    }

    function buzz(team, seat) {
        if (!canClickBuzzers()) return;
        // A wrong-team rebound click must never lock the legal team out.
        if (question.allowed_team && question.allowed_team !== team) return;

        buzzActive = true;
        updateBuzzButtons();
        if (typeof sendEvent === 'function') {
            sendEvent('BUZZ', { team: team, player: seat });
        }
        if (typeof playSound === 'function') playSound('buzz');

        // If the server ignored the click (for example the window just expired),
        // unlock the buttons again instead of leaving the moderator stuck.
        if (buzzClickTimeout) clearTimeout(buzzClickTimeout);
        buzzClickTimeout = setTimeout(() => {
            if (!buzzActive) return;
            if (phase === 'READING' || phase === 'BUZZ_WINDOW') {
                const marker = $('buzz-player-name');
                const text = marker ? marker.textContent : '';
                if (!text || text.indexOf('FLOOR:') === 0) {
                    buzzActive = false;
                    updateBuzzButtons();
                }
            }
        }, 1600);
    }

    // --- QUESTION / TOKENS --------------------------------------------------
    function renderQuestion(data) {
        question = {
            raw_text: String(data.raw_text || data.text || ''),
            answer: String(data.answer || ''),
            options: Array.isArray(data.options) ? data.options.map((item) => String(item)) : [],
            phase: data.phase === 'BONUS' ? 'BONUS' : 'TOSSUP',
            allowed_team: data.allowed_team || null
        };

        resetReadClock();
        buzzActive = false;
        selectedBuzzpoint = '';
        afterRead = false;
        verdictSent = false;
        if (buzzClickTimeout) clearTimeout(buzzClickTimeout);
        buzzClickTimeout = null;

        setText('reader-answer-key', question.answer || '—');
        setText('manual-verdict-preview', question.phase === 'BONUS' ? 'BONUS +10 / 0' : 'NEG −4');
        setVerdictLabels(question.phase === 'BONUS');
        setText('buzzpoint-current', 'No buzzpoint marked');
        setText('buzzpoint-status', 'Not marked');
        setFullReadEnabled(false);
        setVerdictEnabled(false);
        renderOptions();
        renderTokens();
        updateBuzzButtons();
    }

    function renderOptions() {
        const holder = $('reader-options');
        if (!holder) return;
        holder.replaceChildren();
        if (!question.options.length) return;
        const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
        question.options.forEach((option, index) => {
            const row = document.createElement('div');
            row.className = 'bg-panel border border-borderDark rounded-lg px-3 py-2 text-slate-300';
            row.textContent = (letters[index] || String(index + 1)) + '. ' + option;
            holder.appendChild(row);
        });
    }

    function renderTokens() {
        const holder = $('reader-question-text');
        if (!holder) return;
        holder.replaceChildren();
        const text = question.raw_text || 'Waiting for the next question…';
        const parts = text.split(/(\s+)/);
        parts.forEach((part) => {
            if (!part) return;
            if (/^\s+$/.test(part)) {
                holder.appendChild(document.createTextNode(' '));
                return;
            }
            const token = document.createElement('button');
            token.type = 'button';
            token.className = 'reader-token';
            token.dataset.word = part;
            token.textContent = part;
            token.disabled = true;
            token.addEventListener('click', () => markToken(part, token));
            holder.appendChild(token);
        });
        updateTokenState();
    }

    function updateTokenState() {
        const active = manualMode && buzzActive && !verdictSent;
        document.querySelectorAll('.reader-token').forEach((token) => {
            token.disabled = !active;
            token.classList.toggle('is-clickable', active);
            token.classList.toggle('is-selected', Boolean(selectedBuzzpoint) && token.dataset.word === selectedBuzzpoint);
        });
        const holder = $('reader-question-text');
        if (holder) holder.classList.toggle('reader-marking', active);
    }

    function markToken(word, element) {
        if (!buzzActive || verdictSent || !word) return;
        buzzActive = true; // a click can arrive after a phase event; keep marking open
        selectedBuzzpoint = word;
        afterRead = false;
        setText('buzzpoint-current', '“' + word + '”');
        setText('buzzpoint-status', 'Marked');
        setText('manual-verdict-preview', 'NEG −4');
        setVerdictEnabled(true);
        setFullReadEnabled(true);
        document.querySelectorAll('.reader-token').forEach((token) => {
            token.classList.toggle('is-selected', token === element || token.dataset.word === word);
        });
        if (typeof sendEvent === 'function') {
            sendEvent('LOG_BUZZPOINT', { buzzpoint: word, after_read: false });
        }
    }

    function setFullReadEnabled(enabled) {
        const button = $('btn-full-read');
        if (button) button.disabled = !enabled;
    }

    function setVerdictEnabled(enabled) {
        const correct = $('btn-manual-correct');
        const incorrect = $('btn-manual-incorrect');
        if (correct) correct.disabled = !enabled;
        if (incorrect) incorrect.disabled = !enabled;
    }

    function markFullRead() {
        if (!buzzActive || verdictSent) return;
        selectedBuzzpoint = 'Full Read';
        afterRead = true;
        setText('buzzpoint-current', 'Full read / after end');
        setText('buzzpoint-status', 'Full read');
        setText('manual-verdict-preview', 'INCORRECT (0)');
        const preview = $('manual-verdict-preview');
        if (preview) {
            preview.className = 'text-neonGreen font-bold';
        }
        document.querySelectorAll('.reader-token').forEach((token) => token.classList.remove('is-selected'));
        setVerdictEnabled(true);
        if (typeof sendEvent === 'function') {
            sendEvent('LOG_BUZZPOINT', { buzzpoint: 'Full Read', after_read: true });
        }
    }

    function applyBuzzpoint(buzzpoint, isAfterRead) {
        const value = String(buzzpoint || '').trim();
        if (!value) return;
        selectedBuzzpoint = value;
        afterRead = Boolean(isAfterRead) || value === 'Full Read';
        setText('buzzpoint-current', afterRead ? 'Full read / after end' : '“' + value + '”');
        setText('buzzpoint-status', afterRead ? 'Full read' : 'Marked');
        const preview = $('manual-verdict-preview');
        if (preview) {
            preview.textContent = afterRead ? 'INCORRECT (0)' : 'NEG −4';
            preview.className = afterRead ? 'text-neonGreen font-bold' : 'text-neonPink font-bold';
        }
        document.querySelectorAll('.reader-token').forEach((token) => {
            token.classList.toggle('is-selected', !afterRead && token.dataset.word === value);
        });
        setFullReadEnabled(true);
        setVerdictEnabled(true);
        updateTokenState();
    }

    // --- PHASES -------------------------------------------------------------
    function setPhase(payload) {
        const data = payload && typeof payload === 'object' ? payload : {};
        const nextPhase = data.phase || 'IDLE';
        phase = nextPhase;
        setText('manual-phase-label', phaseLabel(nextPhase));

        if (nextPhase === 'READING') {
            buzzActive = false;
            startReadClock(data.started_at);
            setBuzzReadButton(true);
            setBonusReadButton(false);
            if (typeof stopActionTimer === 'function') stopActionTimer();
        } else if (nextPhase === 'BUZZ_WINDOW') {
            buzzActive = false;
            stopReadClock();
            setBuzzReadButton(false);
            setBonusReadButton(false);
            setBuzzWindowTimer(data.seconds || 5, data.started_at);
        } else if (nextPhase === 'ANSWERING') {
            stopReadClock();
            setBuzzReadButton(false);
            setBonusReadButton(false);
            startAnswerTimer(data.seconds || 4, data.started_at);
            if (data.after_read || data.buzzpoint === 'Full Read') {
                applyBuzzpoint('Full Read', true);
            }
        } else if (nextPhase === 'BONUS_READ') {
            buzzActive = false;
            startReadClock(data.started_at);
            setBuzzReadButton(false);
            setBonusReadButton(true);
            setVerdictLabels(true);
            setText('manual-verdict-preview', 'BONUS +10 / 0');
            setText('buzzpoint-current', 'Bonus — no buzzpoint');
            setText('buzzpoint-status', 'Ready');
            setFullReadEnabled(false);
            setVerdictEnabled(!verdictSent);
            if (typeof stopActionTimer === 'function') stopActionTimer();
        } else if (nextPhase === 'BONUS_CONFER') {
            buzzActive = false;
            stopReadClock();
            setBuzzReadButton(false);
            setBonusReadButton(false);
            setVerdictLabels(true);
            setText('manual-verdict-preview', 'BONUS +10 / 0');
            setText('buzzpoint-current', 'Bonus — no buzzpoint');
            setText('buzzpoint-status', 'Conferring');
            setFullReadEnabled(false);
            setVerdictEnabled(!verdictSent);
            startConferTimer(data.seconds || 20, data.started_at);
        } else {
            stopReadClock();
            setBuzzReadButton(false);
            setBonusReadButton(false);
            if (typeof stopActionTimer === 'function') stopActionTimer();
        }
        updateBuzzButtons();
        updateTokenState();
    }

    function setBuzzReadButton(visible) {
        const button = $('btn-manual-question-read');
        if (button) button.classList.toggle('hidden', !visible);
    }

    function setBonusReadButton(visible) {
        const button = $('btn-manual-bonus-read');
        if (button) button.classList.toggle('hidden', !visible);
    }

    function elapsedSeconds(startedAt) {
        const started = Number(startedAt);
        if (!started) return null;
        return Math.max(0, (Date.now() - started) / 1000);
    }

    function setBuzzWindowTimer(seconds, startedAt) {
        const total = Number(seconds) || 5;
        const elapsed = elapsedSeconds(startedAt);
        const remaining = elapsed == null ? total : Math.max(0, total - elapsed);
        const label = $('action-timer-label');
        if (label) label.textContent = 'TOSSUP (5s BUZZ)';
        if (typeof startActionTimer === 'function') startActionTimer(remaining || 0.1, null);
    }

    function startAnswerTimer(seconds, startedAt) {
        const total = Number(seconds) || 4;
        const elapsed = elapsedSeconds(startedAt);
        const remaining = elapsed == null ? total : Math.max(0, total - elapsed);
        const label = $('action-timer-label');
        if (label) label.textContent = 'ANSWER (4s)';
        if (typeof startActionTimer === 'function') startActionTimer(remaining || 0.1, 2.0);
    }

    function startConferTimer(seconds, startedAt) {
        const total = Number(seconds) || 20;
        const elapsed = elapsedSeconds(startedAt);
        const remaining = elapsed == null ? total : Math.max(0, total - elapsed);
        const label = $('action-timer-label');
        if (label) label.textContent = 'BONUS CONFER (20s)';
        if (typeof startActionTimer === 'function') startActionTimer(remaining || 0.1, 5.0);
    }

    // --- CONNECTION ---------------------------------------------------------
    function setManual(enabled) {
        manualMode = Boolean(enabled);
        const panel = $('manual-reader-panel');
        if (panel) panel.classList.toggle('hidden', !manualMode);
        const controls = $('manual-buzz-controls');
        if (controls) {
            controls.classList.toggle('hidden', !manualMode);
            controls.classList.toggle('flex', manualMode);
        }
        if (!manualMode) {
            stopReadClock();
            if (typeof stopActionTimer === 'function') stopActionTimer();
        }
        updateBuzzButtons();
        updateTokenState();
    }

    function onConnected(payload) {
        setManual(Boolean(payload && payload.manual_reader));
        if (payload && payload.manual_phase) setPhase(payload.manual_phase);
        updateBuzzButtons();
    }

    function beginBuzz(payload) {
        const data = payload && typeof payload === 'object' ? payload : {};
        buzzActive = true;
        if (buzzClickTimeout) clearTimeout(buzzClickTimeout);
        buzzClickTimeout = null;
        stopReadClock();
        setText('buzzpoint-status', 'Not marked');
        setText('buzzpoint-current', 'Click the word where the player buzzed');
        setVerdictEnabled(false);
        setFullReadEnabled(true);
        updateBuzzButtons();
        updateTokenState();
        if (data.after_read) applyBuzzpoint('Full Read', true);
    }

    function verdict(correct) {
        if (verdictSent) return;
        if (!isBonusPhase() && (!buzzActive || !selectedBuzzpoint)) return;
        verdictSent = true;
        setVerdictEnabled(false);
        setFullReadEnabled(false);
        setText('buzzpoint-status', 'Verdict sent');
        updateTokenState();
        if (typeof sendEvent === 'function') {
            sendEvent('FORCE_JUDGMENT', { correct: Boolean(correct) });
        }
    }

    function reset() {
        question = { raw_text: '', answer: '', options: [], phase: 'TOSSUP', allowed_team: null };
        phase = 'IDLE';
        buzzActive = false;
        selectedBuzzpoint = '';
        afterRead = false;
        verdictSent = false;
        if (buzzClickTimeout) clearTimeout(buzzClickTimeout);
        buzzClickTimeout = null;
        resetReadClock();
        setText('manual-phase-label', 'IDLE');
        setText('reader-question-text', 'Load a packet to see the question here.');
        setText('reader-answer-key', '—');
        setText('buzzpoint-current', 'No buzzpoint marked');
        setText('buzzpoint-status', 'Not marked');
        setText('manual-verdict-preview', 'NEG −4');
        setVerdictLabels(false);
        const options = $('reader-options');
        if (options) options.replaceChildren();
        setFullReadEnabled(false);
        setVerdictEnabled(false);
        updateBuzzButtons();
        updateTokenState();
    }

    window.ManualReader = {
        setManual: setManual,
        isManual: () => manualMode,
        onConnected: onConnected,
        setRoster: normalizeRoster,
        setQuestion: renderQuestion,
        setPhase: setPhase,
        beginBuzz: beginBuzz,
        setBuzzpoint: applyBuzzpoint,
        markFullRead: markFullRead,
        verdict: verdict,
        reset: reset
    };
})();
