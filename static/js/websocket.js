const synth = window.speechSynthesis;
let currentSpokenWord = "";
let isReading = false;

let ws;
window.ws = null;
let reconnectDelay = 2000;
let pingInterval;

let audioUnlocked = false;
let currentCategoryAbbrev = "GEN";
let currentStageType = "TOSSUP";
let assignedSeat = null;
let currentUserRole = null;
let currentRoomMode = 'in_person';
let currentReadingMode = 'stations';
let currentTeam = null;
let currentReadingId = null;
let onlineBuzzOpen = false;
let isOnlineCaptain = false;
let currentOnlineBuzzTeam = null;
let onlineCaptainOnly = false;
let currentMatchLog = [];
let currentMatchTeardown = false;
// True while the moderator's station is the room's only voice (shared-read mode).
let moderatorReading = { active: false, kind: null };
let isReadingOnline = false;
let onlineReadCompletions = new Set();
// Every read gets its own generation: restarting or stopping speech bumps it, so an
// utterance from an earlier read can never report completion for the read the server
// is waiting on.
let readingGeneration = 0;

// --- ACTION TIMER LOGIC ---
let actionTimerInterval = null;

function startActionTimer(seconds, warningAt = null) {
    if (actionTimerInterval) clearInterval(actionTimerInterval);

    const display = document.getElementById('action-timer-display');
    const bar = document.getElementById('action-timer-bar');
    const warning = document.getElementById('action-timer-warning');
    const box = document.getElementById('action-timer-box');

    let remaining = seconds;
    const total = seconds;

    if (warning) warning.classList.add('hidden');
    if (box) box.classList.remove('border-red-500', 'border-amber-500');
    if (bar) {
        bar.style.width = '100%';
        bar.className = 'bg-teamA h-full transition-all duration-100';
    }

    actionTimerInterval = setInterval(() => {
        remaining -= 0.1;

        if (remaining <= 0) {
            clearInterval(actionTimerInterval);
            if (display) display.innerText = '0.0s';
            if (bar) bar.style.width = '0%';
            if (warning) warning.classList.add('hidden');
            return;
        }

        if (display) display.innerText = remaining.toFixed(1) + 's';
        if (bar) bar.style.width = `${(remaining / total) * 100}%`;

        // 5-Second Warning Trigger
        if (warningAt && remaining <= warningAt) {
            if (warning) warning.classList.remove('hidden');
            if (box) box.classList.add('border-red-500');
            if (bar) bar.className = 'bg-red-500 h-full transition-all duration-100 animate-pulse';
        }
    }, 100);
}

function stopActionTimer() {
    if (actionTimerInterval) clearInterval(actionTimerInterval);
    const display = document.getElementById('action-timer-display');
    const bar = document.getElementById('action-timer-bar');
    const warning = document.getElementById('action-timer-warning');
    const box = document.getElementById('action-timer-box');

    if (display) display.innerText = '--';
    if (bar) bar.style.width = '100%';
    if (warning) warning.classList.add('hidden');
    if (box) box.classList.remove('border-red-500');
}

// --- AUDIO UNLOCK HACK FOR BROWSERS ---
document.addEventListener('click', () => {
    if (!audioUnlocked) {
        const u = new SpeechSynthesisUtterance('');
        u.volume = 0;
        synth.speak(u);
        audioUnlocked = true;
    }
}, { once: true });

// --- MAIN WEBSOCKET CONNECTION ---
// Update the signature to accept apiKey
function connectWebSocket(roomId, apiKey = null) {
    window._lastApiKey = apiKey || null;
    const wsProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${wsProtocol}//${window.location.host}/ws/${encodeURIComponent(roomId)}`);
    ws = socket;
    window.ws = socket;

    socket.onopen = async () => {
        const statusEl = document.getElementById('header-status');
        if (statusEl) statusEl.innerText = `CONNECTING TO MATCH (${roomId.toUpperCase()})`;
        try {
            const idToken = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
            if (!idToken || socket.readyState !== WebSocket.OPEN) {
                socket.close(4401, "Sign in required");
                return;
            }
            socket.send(JSON.stringify({ type: "AUTH_INIT", payload: { id_token: idToken, api_key: apiKey || "" } }));
        } catch (error) {
            console.warn('Unable to authenticate moderator station', error);
            socket.close(4401, "Sign in required");
        }
    };
    socket.onmessage = function(event) {
        let data;
        try {
            data = JSON.parse(event.data);
        } catch (error) {
            console.error('Ignoring malformed WebSocket message', error);
            return;
        }
        if (!data || typeof data !== 'object' || typeof data.type !== 'string') return;
        const payload = data.payload && typeof data.payload === 'object' ? data.payload : {};
        if (data.type === 'CONNECTED' && socket === ws) {
            reconnectDelay = 2000;
            if (payload.mode === 'online') window.onlineProfile = { name: payload.name, team: payload.team, captain: payload.captain };
            const connectedStatus = document.getElementById('header-status');
            if (connectedStatus) connectedStatus.innerText = `READY (ROOM: ${roomId.toUpperCase()})`;
            if (pingInterval) clearInterval(pingInterval);
            pingInterval = setInterval(() => {
                if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "PING", payload: {} }));
            }, 15000);
        }
        const handler = ROOM_MESSAGE_HANDLERS[data.type];
        if (Object.prototype.hasOwnProperty.call(ROOM_MESSAGE_HANDLERS, data.type) && typeof handler === 'function') {
            handler(payload);
        }
    };

    socket.onerror = (err) => {
        console.warn("WebSocket Error, closing to trigger reconnect...", err);
        socket.close();
    };

    socket.onclose = (event) => {
        // Deliberate close after a profile/team refresh: the caller opens a new
        // socket itself, so skip status resets and reconnect logic.
        if (window._profileRefresh === socket) {
            window._profileRefresh = null;
            return;
        }
        const statusEl = document.getElementById('header-status');
        currentUserRole = null;
        if (window.ws === socket) window.ws = null;
        if ([4400, 4401, 4403, 4404, 4409, 4410, 4411, 4429, 4503].includes(event.code)) {
            if (statusEl) {
                const messages = {4400: 'INVALID ROOM CODE', 4401: 'SIGN IN TO CONNECT', 4403: 'ROOM ACCESS DENIED', 4404: 'ROOM NOT AVAILABLE', 4409: 'MESSAGE TOO LARGE', 4410: 'ROOM CLOSED - NO ACTIVITY', 4411: 'MATCH ENDED - ROOM CLOSED', 4429: 'ROOM BUSY - RETRY MANUALLY', 4503: 'ROOM SERVICE UNAVAILABLE'};
                statusEl.innerText = messages[event.code] || 'CONNECTION CLOSED';
            }
            if (event.code === 4411) {
                window._matchClosing = false;
                if (typeof showToast === 'function') showToast('Match room closed.');
            }
            if (window.activeRoomId === roomId) window.activeRoomId = null;
            if (pingInterval) clearInterval(pingInterval);
            return;
        }
        if (statusEl) statusEl.innerText = 'DISCONNECTED - RECONNECTING...'; 
        
        if (pingInterval) clearInterval(pingInterval);
        
        // FIX: Wrap in an anonymous function so it doesn't evaluate immediately
        const closedSocket = socket;
        setTimeout(async () => {
            if (ws !== closedSocket) return;
            try {
                if (window.firebaseAuthReady) await window.firebaseAuthReady;
                const refreshedToken = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
                if (!refreshedToken) {
                    if (statusEl) statusEl.innerText = 'DISCONNECTED - SIGN IN TO RECONNECT';
                    return;
                }
                const refreshedKey = window.getUserApiKey ? await window.getUserApiKey() : null;
                if (ws === closedSocket) connectWebSocket(roomId, refreshedKey);
            } catch (error) {
                console.warn('Reconnect authentication failed', error);
                if (statusEl) statusEl.innerText = 'DISCONNECTED - SIGN IN TO RECONNECT';
            }
        }, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
    };
}

// One handler per server message type. Own-property lookup keeps unknown or inherited
// names inert, exactly like the previous fall-through chain.
const ROOM_MESSAGE_HANDLERS = {
    CONNECTED(payload) {
        currentUserRole = payload.role || 'viewer';
        currentRoomMode = payload.mode || 'in_person';
        currentReadingMode = payload.reading_mode || 'stations';
        if (typeof renderReadingMode === 'function') renderReadingMode(currentReadingMode);
        // A station that joins mid-read still learns that the room's voice is the moderator's.
        renderModeratorReading(payload.moderator_reading?.active, payload.moderator_reading?.kind);
        assignedSeat = payload.seat || null;
        currentTeam = payload.team || null;
        isOnlineCaptain = Boolean(payload.captain);
        const onlineBuzzControls = document.getElementById('online-buzz-controls');
        const manualMode = Boolean(payload.manual_reader);
        onlineBuzzControls?.classList.toggle('hidden', currentRoomMode !== 'online');
        document.getElementById('match-layout')?.classList.toggle('is-manual', currentRoomMode === 'in_person' && manualMode);
        document.querySelectorAll('.moderator-only-control').forEach((control) => {
            control.classList.toggle('hidden', currentUserRole !== 'owner');
        });
        document.querySelectorAll('.viewer-only-control').forEach((control) => {
            control.classList.toggle('hidden', currentUserRole === 'owner');
        });
        const ownTeamName = currentTeam === 'Team A' ? document.getElementById('header-team-a-name') : document.getElementById('header-team-b-name');
        if (ownTeamName && payload.name) ownTeamName.title = `You are ${payload.name}${isOnlineCaptain ? ' (Captain)' : ''}`;
        if (typeof applyRoleVisibility === 'function') applyRoleVisibility(currentUserRole);
        if (typeof updateSeatLabels === 'function') updateSeatLabels(payload);
        updateOnlineBuzzButton();
        if (window.ManualReader) window.ManualReader.onConnected(payload);
        if (window.CameraBuzz && typeof window.CameraBuzz.applyState === 'function') {
            window.CameraBuzz.applyState({ enabled: Boolean(payload.camera_buzz), mode: currentRoomMode });
        }
        if (window._manualSetupPending && typeof window.openManualHudAfterSetup === 'function') {
            window.openManualHudAfterSetup();
        }
    },
    ROOM_ROSTER(payload) {
        renderOnlineRoster(payload.players || []);
    },
    UPDATE_STATUS(payload) {
        const statusEl = document.getElementById('header-status');
        if (statusEl) statusEl.innerText = payload.text || 'READY';
        const playerStatus = document.getElementById('player-lobby-status');
        if (playerStatus) playerStatus.innerText = payload.text || 'Connected';
        const micLabel = document.getElementById('player-mic-label');
        if (micLabel) micLabel.innerText = (payload.text || 'STANDBY').slice(0, 42).toUpperCase();

        // EXACT TRIGGER: Start the 5-second physical timer only when the backend opens the buzz window.
        // Manual rooms use the rebound-only window; their HUD owns that clock.
        if (payload.text === "⏳ 5 Seconds to Buzz..." && !(window.ManualReader && window.ManualReader.isManual())) {
            const label = document.getElementById('action-timer-label');
            if (label) label.innerText = "REBOUND (5s)";
            startActionTimer(5.0, null);
        }

        // A rejected packet must reach the page that uploaded it.
        const settingsStatus = document.getElementById('settings-packet-status');
        if (settingsStatus && /^❌/.test(payload.text || '')) {
            settingsStatus.innerText = payload.text;
            settingsStatus.className = 'mt-4 text-center text-xs font-mono font-bold text-neonPink min-h-5';
        }

        // The status line alone also proves the packet landed, so the pending
        // confirmation cannot hang on one missed message.
        const loaded = /Loaded (\d+) new questions/.exec(payload.text || '');
        if (loaded) announcePacketLoaded({ count: Number(loaded[1]) });
    },
    PREMATCH_READY(payload) {
        // A new packet is a new match: clear every score and row from the old one.
        resetMatchHud();
        announcePacketLoaded(payload);
    },
    MATCH_COMPLETE(payload) {
        currentMatchLog = Array.isArray(payload.log) ? payload.log : currentMatchLog;
        currentMatchTeardown = Boolean(payload.teardown);
        window.currentMatchTeardown = currentMatchTeardown;
        renderScoresheetFromLog(currentMatchLog);
        // The match may end while a question is still being read or answered.
        // Silence the room and stop any open recording before showing the final screen.
        cancelSpeech();
        cancelListening();
        stopActionTimer();
        // A forced end can land between the server's phase updates; leave the HUD
        // idle behind the summary rather than showing a read that is still running.
        if (window.ManualReader) window.ManualReader.setPhase({ phase: 'IDLE', seconds: null });
        if (window.MatchSummary) window.MatchSummary.show(payload, currentMatchLog);
        // Retry the cloud copy at the match boundary in case the final SYNC_LOG was
        // dropped; the end screen must not depend on the socket staying alive.
        if (window.saveMatchLogToCloud && typeof currentRoomId !== 'undefined') {
            window.saveMatchLogToCloud(currentRoomId, currentMatchLog);
        }
    },
    ERROR(payload) {
        const roomError = payload.message || 'The room rejected that action.';
        showEventFeedback(roomError);
        // Resolve the packet confirmation: a rejected upload must not sit on "waiting".
        const pendingStatus = document.getElementById('settings-packet-status');
        if (pendingStatus && (pendingStatus.className || '').includes('text-neonAmber')) {
            pendingStatus.innerText = `Packet rejected — ${roomError}`;
            pendingStatus.className = 'mt-4 text-center text-xs font-mono font-bold text-neonPink min-h-5';
        }
    },
    SYNC_LOG(payload) {
        currentMatchLog = Array.isArray(payload.log) ? payload.log : [];
        // The server's full log is authoritative: rebuilding removes any row the
        // moderator reopened with Override Last Question.
        renderScoresheetFromLog(currentMatchLog);
        // Push the full array of JSON log entries to Firestore
        if (window.saveMatchLogToCloud && typeof currentRoomId !== 'undefined') {
            window.saveMatchLogToCloud(currentRoomId, currentMatchLog);
        }
    },
    UI_STATE: applyRoomUiState,
    ROOM_CLOSED(payload) {
        const reason = payload.reason || 'Match ended';
        if (typeof showToast === 'function') showToast(`${reason} — room closed.`);
    },
    MANUAL_PHASE(payload) {
        if (window.ManualReader) window.ManualReader.setPhase(payload);
    },
    BUZZPOINT_SET(payload) {
        if (window.ManualReader) window.ManualReader.setBuzzpoint(payload.buzzpoint, payload.after_read);
    },
    UPDATE_QUESTION(payload) {
        currentStageType = "TOSSUP";
        if (payload.manual && window.ManualReader) {
            currentStageType = payload.phase === 'BONUS' ? 'BONUS' : 'TOSSUP';
            window.ManualReader.setQuestion(payload);
        } else {
            // Online AI and in-person AI rooms keep the original audio-only readout;
            // manual rooms render the packet into the reader panel instead.
            const camBadge = document.getElementById('buzz-source-badge');
            if (camBadge) camBadge.classList.add('hidden');
            setStage('READ_TOSSUP');
        }

        const catMap = {
            "BIOLOGY": "BIO",
            "CHEMISTRY": "CHEM",
            "PHYSICS": "PHY",
            "MATH": "MATH",
            "EARTH SCIENCE": "ESS",
            "ENERGY": "NRG",
            "EARTH AND SPACE": "ESS",
            "GENERAL SCIENCE": "GEN"
        };
        const rawCat = (payload.category || 'GENERAL').toUpperCase();
        currentCategoryAbbrev = catMap[rawCat] || rawCat.substring(0, 4);

        const catBadge = document.getElementById('stage-cat-badge');

        if (catBadge) catBadge.innerText = payload.category || 'GENERAL';
        // No question or answer text is written to the DOM.
    },
    NEW_LOG_ENTRY(payload) {
        applyLogEntry(payload);
    },
    MODERATOR_READING(payload) {
        renderModeratorReading(payload.active, payload.kind);
    },
    DOWNLOAD_CSV(payload) {
        const csv = typeof payload.csv_data === 'string' ? payload.csv_data : '';
        if (!csv) {
            showEventFeedback('The match log is empty.');
            return;
        }
        const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = 'slopbowl_match_log.csv';
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
    },
    READING_MODE(payload) {
        // The moderator's choice, echoed to every station in the room.
        currentReadingMode = payload.mode || 'stations';
        if (typeof renderReadingMode === 'function') renderReadingMode(currentReadingMode);
    },
    START_READING(payload) {
        if (window.ManualReader && window.ManualReader.isManual()) return;
        if (payload.moderator_only && currentUserRole !== 'owner') return;
        if (currentRoomMode === 'online') {
            currentReadingId = payload.reading_id || null;
            isReadingOnline = true;
            onlineReadCompletions = new Set();
        }
        advancePlayerToBuzzer();
        speakText(payload.text, payload.reading_id || null);
    },
    OPEN_BUZZ(payload) {
        onlineBuzzOpen = true;
        currentOnlineBuzzTeam = payload.team || null;
        onlineCaptainOnly = Boolean(payload.captain_only);
        updateOnlineBuzzButton();
        advancePlayerToBuzzer();
    },
    CLOSE_BUZZ(payload) {
        onlineBuzzOpen = false;
        currentOnlineBuzzTeam = null;
        onlineCaptainOnly = false;
        updateOnlineBuzzButton();
    },
    START_LISTENING(payload) {
        if (window.ManualReader && window.ManualReader.isManual()) return;
        // In-person requests only reach the moderator; online requests only reach the answerer.
        const timeout = payload.timeout || 5.0;
        // The answer window replaces the read, so the "moderator is reading" banner is done.
        renderModeratorReading(false);

        if (timeout > 15.0) {
            currentStageType = "BONUS";
            updateOnlineBuzzButton();
            const label = document.getElementById('action-timer-label');
            if (label) label.innerText = "BONUS CONFER (20s)";
            startActionTimer(20.0, 5.0);
        } else {
            const label = document.getElementById('action-timer-label');
            if (label) label.innerText = "ANSWERING";
            startActionTimer(timeout, 2.0);
        }

        startListening(timeout, payload.expires_at || null);
    },
    CAMERA_BUZZ_STATE(payload) {
        if (window.CameraBuzz && typeof window.CameraBuzz.applyState === 'function') {
            window.CameraBuzz.applyState(payload);
        }
    },
    DEBUG_FRAME(payload) {
        const img = new Image();
        img.onload = () => {
            const matchCanvas = document.getElementById('match-calib-canvas');
            if (matchCanvas && matchCanvas.offsetParent !== null) {
                matchCanvas.width = img.width;
                matchCanvas.height = img.height;
                const ctx = matchCanvas.getContext('2d');
                ctx.drawImage(img, 0, 0);
            }
        };
        img.src = payload.frame_data;
    },
    UPDATE_SCORE(payload) {
        ['score-header-a', 'hud-score-a'].forEach((id) => {
            const scoreA = document.getElementById(id);
            if (scoreA && payload.score_a !== undefined) scoreA.innerText = payload.score_a;
        });
        ['score-header-b', 'hud-score-b'].forEach((id) => {
            const scoreB = document.getElementById(id);
            if (scoreB && payload.score_b !== undefined) scoreB.innerText = payload.score_b;
        });
    },
    BUZZ(payload) {
        stopActionTimer();
        renderModeratorReading(false);
        if (currentRoomMode === 'in_person' && window.ManualReader && window.ManualReader.isManual()) {
            setStage('BUZZ');
            const manualTeam = payload.team || 'Unknown';
            const manualPlayer = payload.player || 'Player';
            const manualPlayerEl = document.getElementById('buzz-player-name');
            if (manualPlayerEl) manualPlayerEl.innerText = `${String(manualPlayer).toUpperCase()} BUZZED — ${manualTeam}`;
            window.ManualReader.beginBuzz(payload);
            return;
        }
        if (isReading) {
            const spokenWord = currentSpokenWord;
            // Stopping the read must not report a completion: the server takes the
            // next completion it sees as this read finishing, and that used to
            // start the next question's 5-second buzz timer before it was read.
            cancelSpeech();
            sendEvent('LOG_BUZZPOINT', { buzzpoint: spokenWord });
        }

        setStage('BUZZ');
        const team = payload.team || 'Unknown';
        const player = payload.player || 'Player';

        const playerEl = document.getElementById('buzz-player-name');
        if (playerEl) playerEl.innerText = `FLOOR: ${team} (${player})`;
        const camBadge = document.getElementById('buzz-source-badge');
        if (camBadge) camBadge.classList.toggle('hidden', payload.source !== 'camera');

        const playerStage = document.getElementById('player-stage-label');
        if (playerStage) playerStage.innerText = `BUZZ: ${team} (${player})`;
        document.body.style.backgroundColor = '#2a0505';
        setTimeout(() => document.body.style.backgroundColor = '#000000', 300);
    },
};

function isRoomConnected() {
    return !!(ws && ws.readyState === WebSocket.OPEN);
}

// An open buzz window with no team set is open to both teams; the server arbitrates
// the same way, so a null window team must not lock every buzzer out.
function buzzWindowAllowsMe() {
    if (!onlineBuzzOpen) return false;
    if (currentOnlineBuzzTeam && currentTeam !== currentOnlineBuzzTeam) return false;
    return true;
}

// A dropped action looks like a dead button: always say what happened.
function showEventFeedback(message) {
    const statusEl = document.getElementById('header-status');
    if (statusEl) statusEl.innerText = message.toUpperCase();
    if (typeof showToast === 'function') showToast(message);
}

// Server acknowledgement that a question packet was accepted and validated. The
// pre-match page must never announce this before the server confirms it.
function announcePacketLoaded(payload) {
    const count = Number(payload.count) || 0;
    const category = payload.first_category ? String(payload.first_category) : 'GENERAL';
    const status = document.getElementById('settings-packet-status');
    if (status) {
        status.innerText = `${count} question ${count === 1 ? 'set' : 'sets'} ready — first category ${category}`;
        status.className = 'mt-4 text-center text-xs font-mono font-bold text-neonGreen min-h-5';
    }
    const inPersonName = document.getElementById('bank-file-name');
    if (inPersonName) {
        inPersonName.innerText = `${count} question${count === 1 ? '' : 's'} loaded — server confirmed`;
        inPersonName.className = 'text-xs text-neonGreen font-mono font-bold';
    }
}

function sendEvent(eventType, payload = {}) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        const playerEvents = currentRoomMode === 'online' && ['PING', 'BUZZ', 'ANSWER_AUDIO', 'READING_DONE', 'READING_FAILED'].includes(eventType);
        if (currentUserRole !== 'owner' && !playerEvents) {
            console.warn(`Room permission required for ${eventType}.`);
            showEventFeedback('Moderator controls are locked in this room.');
            return;
        }
        if (currentRoomMode === 'online' && eventType === 'BUZZ' && (!buzzWindowAllowsMe() || (onlineCaptainOnly && !isOnlineCaptain))) return;
        if (currentRoomMode === 'online' && eventType === 'READING_DONE') {
            const readingId = payload.reading_id || currentReadingId;
            if (!readingId || onlineReadCompletions.has(readingId)) return;
            onlineReadCompletions.add(readingId);
            payload = { reading_id: readingId };
        }
        ws.send(JSON.stringify({ type: eventType, payload: payload }));
    } else {
        console.warn(`Cannot send event ${eventType}: WebSocket is not open.`);
        showEventFeedback(window.activeRoomId || currentRoomId
            ? 'Not connected to the match room — reconnecting. If this persists, sign in again.'
            : 'Not connected to a match. Start an in-person match first.');
    }
}

// --- SCORESHEET ---------------------------------------------------------
const SCORESHEET_PLACEHOLDER = `
    <tr>
        <td class="py-3 px-4 text-center"></td>
        <td class="py-3 px-4 text-center"></td>
        <td class="py-3 px-2 text-center match-empty-score">
            Match data will populate here...
        </td>
        <td class="py-3 px-4 text-center"></td>
        <td class="py-3 px-4 text-center"></td>
    </tr>`;

function clearMatchScoresheet() {
    const tbody = document.getElementById('scoresheet-body');
    if (tbody) tbody.innerHTML = SCORESHEET_PLACEHOLDER;
}

function makeScoreCellEditable(cell, payload, colName) {
    cell.title = 'Double-click to edit';
    cell.classList.add('cursor-pointer', 'hover:bg-isoborder');
    cell.ondblclick = () => {
        const currentText = cell.innerText.replace(' *', '').trim();
        const input = document.createElement('input');
        input.type = 'text';
        input.className = 'w-full bg-isodark text-white font-mono text-center border border-teamA py-1 focus:outline-none';
        input.value = currentText;
        cell.replaceChildren(input);
        input.focus();

        const saveEdit = () => {
            const newVal = input.value.trim();
            cell.innerText = newVal + ' *';
            sendEvent('EDIT_LOG_ENTRY', {
                q_num: payload.Q_Num,
                team: payload.Team,
                column: colName,
                new_val: newVal,
            });
        };

        let editSaved = false;
        input.onblur = () => {
            if (editSaved) return;
            editSaved = true;
            saveEdit();
        };
        input.onkeydown = (event) => { if (event.key === 'Enter') input.blur(); };
    };
}

function applyLogEntry(payload, categoryLabel = currentCategoryAbbrev) {
    const tbody = document.getElementById('scoresheet-body');
    if (!tbody || !payload || typeof payload !== 'object') return;
    if (tbody.children.length === 1 && tbody.textContent.toLowerCase().includes('match data will populate here')) {
        tbody.innerHTML = '';
    }

    const isOverride = payload.Event_Type === 'MANUAL_OVERRIDE';
    let tr = null;
    if (!isOverride) tr = tbody.querySelector(`tr[data-qnum="${payload.Q_Num}"]`);
    if (!tr) {
        tr = document.createElement('tr');
        if (!isOverride) tr.setAttribute('data-qnum', payload.Q_Num);
        tr.className = 'border-b border-isoborder/50 hover:bg-isodark/50 transition';
        tr.innerHTML = `
            <td class="py-3 px-4 text-center ta-toss font-mono text-xs"></td>
            <td class="py-3 px-4 text-center ta-bonus font-mono text-xs"></td>
            <td class="py-2 px-2 text-center bg-isodark border-x border-isoborder min-w-[120px]">
                <div class="flex justify-center items-center space-x-2 mb-1">
                    <span class="text-white font-bold text-xs q-label"></span>
                    <span class="text-slate-400 font-bold text-[10px] q-cat"></span>
                </div>
                <div class="flex justify-center items-center space-x-4 text-sm font-black">
                    <span class="text-teamA q-score-a"></span>
                    <span class="text-teamB q-score-b"></span>
                </div>
            </td>
            <td class="py-3 px-4 text-center tb-toss font-mono text-xs"></td>
            <td class="py-3 px-4 text-center tb-bonus font-mono text-xs"></td>
        `;
        tbody.prepend(tr);
    }

    tr.querySelector('.q-label').innerText = isOverride ? 'OVERRIDE' : 'Q' + payload.Q_Num;
    if (!isOverride) {
        tr.querySelector('.q-cat').innerText = categoryLabel || 'GEN';
    }
    tr.querySelector('.q-score-a').innerText = payload.Score_A;
    tr.querySelector('.q-score-b').innerText = payload.Score_B;

    if (payload.Event_Type === 'DEAD_TOSSUP') return;

    const setTossup = (selector, points, player, teamColorClass) => {
        const cell = tr.querySelector(selector);
        cell.classList.remove('text-teamA', 'text-teamB', 'text-red-400', 'text-zinc-500', 'text-emerald-400');
        if (points > 0) {
            cell.innerText = `${player} +${points}`;
            cell.classList.add(teamColorClass, 'font-bold');
        } else if (points < 0) {
            cell.innerText = `${player} ${points}`;
            cell.classList.add('text-red-400', 'font-bold');
        } else {
            cell.innerText = `${player} X`;
            cell.classList.add('text-zinc-500', 'font-bold');
        }
        if (payload.Edited) cell.innerText += ' *';
        makeScoreCellEditable(cell, payload, 'TOSSUP');
    };

    const setBonus = (selector, points) => {
        const cell = tr.querySelector(selector);
        cell.classList.remove('text-teamA', 'text-teamB', 'text-red-400', 'text-zinc-500', 'text-emerald-400');
        if (points === 10) {
            cell.innerText = '10';
            cell.classList.add('text-emerald-400', 'font-bold');
        } else {
            cell.innerText = '0';
            cell.classList.add('text-zinc-500', 'font-bold');
        }
        if (payload.Edited) cell.innerText += ' *';
        makeScoreCellEditable(cell, payload, 'BONUS');
    };

    if (payload.Team === 'Team A') {
        if (payload.Event_Type.includes('TOSSUP')) {
            setTossup('.ta-toss', payload.Points, payload.Player, 'text-teamA');
        } else if (payload.Event_Type === 'BONUS') {
            setBonus('.ta-bonus', payload.Points);
        } else if (isOverride) {
            tr.querySelector('.ta-toss').innerText = '🔧';
            setBonus('.ta-bonus', payload.Points);
        }
    } else if (payload.Team === 'Team B') {
        if (payload.Event_Type.includes('TOSSUP')) {
            setTossup('.tb-toss', payload.Points, payload.Player, 'text-teamB');
        } else if (payload.Event_Type === 'BONUS') {
            setBonus('.tb-bonus', payload.Points);
        } else if (isOverride) {
            tr.querySelector('.tb-toss').innerText = '🔧';
            setBonus('.tb-bonus', payload.Points);
        }
    }
}

function renderScoresheetFromLog(log) {
    const tbody = document.getElementById('scoresheet-body');
    if (!tbody) return;
    const categories = new Map();
    tbody.querySelectorAll('tr[data-qnum]').forEach((row) => {
        categories.set(String(row.dataset.qnum), row.querySelector('.q-cat')?.innerText || '');
    });
    clearMatchScoresheet();
    (Array.isArray(log) ? log : []).forEach((entry) => {
        const qNum = entry && entry.Q_Num != null ? String(entry.Q_Num) : '';
        applyLogEntry(entry, categories.get(qNum) || currentCategoryAbbrev);
    });
}

function resetMatchHud() {
    currentMatchLog = [];
    currentMatchTeardown = false;
    window.currentMatchTeardown = false;
    if (window.MatchSummary) {
        window.MatchSummary.hide();
        if (typeof window.MatchSummary.reset === 'function') window.MatchSummary.reset();
    }
    clearMatchScoresheet();
    ['score-header-a', 'score-header-b'].forEach((id) => {
        const score = document.getElementById(id);
        if (score) score.innerText = '0';
    });
    const question = document.getElementById('reader-question-text');
    if (question) question.innerText = 'Load a packet to see the question here.';
    const answer = document.getElementById('reader-answer-key');
    if (answer) answer.innerHTML = '&mdash;';
    const options = document.getElementById('reader-options');
    if (options) options.replaceChildren();
    const category = document.getElementById('stage-cat-badge');
    if (category) category.innerText = '--';
    const kind = document.getElementById('hud-question-kind');
    if (kind) kind.innerText = 'TOSS-UP';
    const buzz = document.getElementById('buzz-player-name');
    if (buzz) buzz.innerText = 'FLOOR: CLEAR';
    document.getElementById('btn-override-last-question')?.classList.add('hidden');
    document.getElementById('btn-end-match')?.classList.remove('hidden');
    if (window.ManualReader) {
        window.ManualReader.reset();
        window.ManualReader.setPhase({ phase: 'IDLE', seconds: null });
    }
}

window.clearMatchScoresheet = clearMatchScoresheet;
window.renderMatchScoresheet = renderScoresheetFromLog;
window.resetMatchHud = resetMatchHud;

// --- TEXT TO SPEECH (TTS) LOGIC ---
function updateOnlineBuzzButton() {
    const buttons = document.querySelectorAll('.js-online-buzz');
    if (!buttons.length) return;
    const windowOpenToMe = buzzWindowAllowsMe();
    const canBuzz = windowOpenToMe && (!onlineCaptainOnly || isOnlineCaptain);
    buttons.forEach((button) => {
        button.disabled = currentRoomMode !== 'online' || !canBuzz;
        if (button.classList.contains('buzz-btn')) {
            // Giant player buzzer keeps its label short.
            button.textContent = canBuzz ? 'BUZZ' : (onlineBuzzOpen ? 'STANDBY' : 'BUZZ CLOSED');
        } else if (onlineBuzzOpen && !windowOpenToMe) {
            button.textContent = 'OTHER TEAM BUZZING';
        } else if (onlineBuzzOpen && onlineCaptainOnly && !isOnlineCaptain) {
            button.textContent = 'CAPTAIN BUZZES BONUS';
        } else {
            button.textContent = 'MY BUZZER';
        }
    });
}

// Players waiting on the pre-match screen move to their buzzer as soon as the
// room opens the buzz window or starts reading.
function advancePlayerToBuzzer() {
    if (currentUserRole !== 'player' || typeof navigateTo !== 'function') return;
    const preMatch = document.getElementById('slide-7');
    if (preMatch && !preMatch.classList.contains('hidden')) navigateTo(8);
}

function renderModeratorReading(active, kind = null) {
    moderatorReading = { active: Boolean(active), kind: active ? (kind || null) : null };

    const banner = document.getElementById('player-read-banner');
    if (banner) {
        banner.classList.toggle('hidden', !moderatorReading.active);
        const text = document.getElementById('player-read-banner-text');
        if (text) {
            text.innerText = moderatorReading.kind === 'bonus'
                ? 'Listen to the shared audio — the captain answers when the read ends.'
                : 'Listen to the shared audio — buzz in when you know it.';
        }
    }

    const label = document.getElementById('player-stage-label');
    const dot = document.getElementById('player-stage-dot');
    if (!label) return;
    if (moderatorReading.active) {
        label.innerText = moderatorReading.kind === 'bonus' ? 'MODERATOR READING BONUS' : 'MODERATOR IS READING';
        if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-neonCyan animate-pulse';
    } else if (/^MODERATOR (IS READING|READING BONUS)/.test(label.innerText)) {
        // Do not clobber a BUZZ label the same message may have just set.
        label.innerText = 'AWAITING QUESTION';
        if (dot) dot.className = 'w-2.5 h-2.5 rounded-full bg-slate-600';
    }
}

function renderOnlineRoster(players) {
    const holders = document.querySelectorAll('.js-online-roster');
    holders.forEach((holder) => {
        holder.replaceChildren();
        players.forEach((player) => {
            const item = document.createElement('span');
            item.className = 'px-2 py-1 rounded bg-black border border-borderDark text-xs text-slate-300';
            item.textContent = `${player.name} · ${player.team}${player.captain ? ' · Captain' : ''}`;
            holder.appendChild(item);
        });
    });

    // Moderator lobby team panels
    const listA = document.getElementById('lobby-team-a-list');
    const listB = document.getElementById('lobby-team-b-list');
    if (listA || listB) {
        if (listA) listA.replaceChildren();
        if (listB) listB.replaceChildren();
        players.forEach((player) => {
            const item = document.createElement('div');
            item.className = 'flex items-center justify-between bg-panel border border-borderDark rounded px-2 py-1';
            const nameEl = document.createElement('span');
            nameEl.textContent = player.name;
            item.appendChild(nameEl);
            if (player.captain) {
                const cpt = document.createElement('span');
                cpt.className = 'text-[9px] font-bold text-neonCyan';
                cpt.textContent = 'CPT';
                item.appendChild(cpt);
            }
            (player.team === 'Team B' ? listB : listA)?.appendChild(item);
        });
    }
}

function sendReadingDone(readingId) {
    if (currentRoomMode === 'online') {
        sendEvent('READING_DONE', { reading_id: readingId || currentReadingId });
    } else {
        sendEvent('READING_DONE', {});
    }
}

function sendReadingFailed(readingId) {
    // Distinct from a completion: a station that could not speak has not read the
    // question, and the server must not open the buzz window believing it was read.
    if (currentRoomMode === 'online') {
        sendEvent('READING_FAILED', { reading_id: readingId || currentReadingId });
    } else {
        sendEvent('READING_FAILED', {});
    }
}

function cancelSpeech() {
    readingGeneration++;  // Anything still speaking is stale from here on.
    if (window._activeUtterance) {
        window._activeUtterance.onend = null;
        window._activeUtterance.onerror = null;
    }
    window._activeUtterance = null;
    isReading = false;
    synth.cancel();
}

// A long question can end without Chromium ever firing the utterance 'end' event, so the
// station cannot rely on that event alone to report the read as finished. Polling the
// engine's own state is the ground truth for when speaking stopped; the start grace
// covers the delay before an utterance actually begins.
const SPEECH_POLL_MS = 200;
const SPEECH_START_GRACE_MS = 1500;

function speakText(text, readingId = null) {
    try {
        const generation = ++readingGeneration;

        if (window._activeUtterance) {
            window._activeUtterance.onend = null;
            window._activeUtterance.onerror = null;
        }
        window._activeUtterance = null;

        synth.cancel(); 
        
        if (!text || text.trim() === "") {
            isReading = false;
            sendReadingDone(readingId);
            return;
        }

        // A read counts as in flight from the request onward, so a buzz during the
        // 50ms start delay stops it before it is spoken.
        isReading = true;
        scheduleUtterance(text, readingId, generation, 0);
    } catch (err) {
        sendReadingDone(readingId);
    }
}

function scheduleUtterance(text, readingId, generation, attempt) {
    setTimeout(() => {
        // A newer read replaced this one, so this utterance must stay silent.
        if (generation !== readingGeneration) return;

        const utterance = new SpeechSynthesisUtterance(text);
        window._activeUtterance = utterance;

        utterance.rate = 1.1;

        utterance.onboundary = (event) => {
            if (event.name === 'word') {
                const wordStr = text.substring(event.charIndex);
                currentSpokenWord = wordStr.split(/[ \n\r\t]+/, 1)[0].replace(/[^a-zA-Z0-9-]/g, '');
                // AUDIO-ONLY READOUT: the spoken word is tracked for buzzpoint
                // logging but never displayed on screen.
            }
        };

        // Exactly one report per utterance: 'end' and the spoken-end poll below both
        // finish the read, and the room must not be told twice.
        let spoken = false;
        let finished = false;
        let endWatch = null;

        const finishReading = (failed) => {
            if (generation !== readingGeneration || finished) return;
            finished = true;
            if (endWatch) clearInterval(endWatch);
            isReading = false;
            window._activeUtterance = null;
            if (currentRoomMode === 'online') isReadingOnline = false;
            if (failed) {
                sendReadingFailed(readingId);
                return;
            }
            currentSpokenWord = "";
            sendReadingDone(readingId);
            // The timer trigger has been removed from here so it doesn't fire after reading the result!
        };

        const retryReading = () => {
            finished = true;  // This utterance is spent: a silence report cannot report it too.
            if (endWatch) clearInterval(endWatch);
            synth.cancel();
            scheduleUtterance(text, readingId, generation, 1);
        };

        // The engine stops speaking whether or not it delivers 'end'. Waiting on its own
        // state reports the read when the audio really stopped, instead of leaving the
        // room to wait out its watchdog with the buzz timer starting long after.
        const watchSpokenEnd = () => {
            const began = performance.now();
            endWatch = setInterval(() => {
                if (generation !== readingGeneration || finished) {
                    clearInterval(endWatch);
                    return;
                }
                if (synth.speaking || synth.pending) {
                    // True while a paused match is holding the utterance, so a pause is not an end.
                    spoken = true;
                    return;
                }
                if (!spoken) {
                    if (performance.now() - began < SPEECH_START_GRACE_MS) return;
                    clearInterval(endWatch);
                    if (attempt === 0) {
                        retryReading();
                        return;
                    }
                    finishReading(true);
                    return;
                }
                finishReading(false);
            }, SPEECH_POLL_MS);
        };

        utterance.onstart = () => { spoken = true; };
        utterance.onend = () => finishReading(false);
        utterance.onerror = () => {
            // An utterance we stopped ourselves is already filtered by the generation
            // guard, so anything arriving here came from outside us: another page that
            // stole the speech engine, or a browser that cannot speak at all. Neither
            // means the question has been read, so retry once, then say so plainly.
            if (generation !== readingGeneration || finished) return;
            if (attempt === 0) {
                retryReading();
                return;
            }
            finishReading(true);
        };

        synth.speak(utterance);
        watchSpokenEnd();
    }, attempt === 0 ? 50 : 350);
}

// --- MICROPHONE RECORDING LOGIC (FOR GROQ WHISPER) ---
let mediaRecorder = null;
let audioChunks = [];
let micStream = null;
let recordTimeout = null;

function cancelListening() {
    if (recordTimeout) {
        clearTimeout(recordTimeout);
        recordTimeout = null;
    }
    if (mediaRecorder) {
        // A forced match end must not upload the partial answer after the summary opens.
        mediaRecorder.ondataavailable = null;
        mediaRecorder.onstop = null;
        if (mediaRecorder.state !== "inactive") mediaRecorder.stop();
    }
    audioChunks = [];
}

async function startListening(timeoutSeconds, expiresAt) {
    const statusEl = document.getElementById('header-status');
    const timeMs = timeoutSeconds * 1000;

    if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
    }
    if (recordTimeout) {
        clearTimeout(recordTimeout);
    }
    audioChunks = [];

    try {
        if (!micStream) {
            micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        }
        
        mediaRecorder = new MediaRecorder(micStream, { mimeType: 'audio/webm' });
        
        mediaRecorder.ondataavailable = (event) => {
            if (event.data.size > 0) {
                audioChunks.push(event.data);
            }
        };

        mediaRecorder.onstop = () => {
            if (statusEl) statusEl.innerText = "🚀 Uploading Audio...";
            
            const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
            const reader = new FileReader();
            
            reader.readAsDataURL(audioBlob);
            reader.onloadend = () => {
                const base64Audio = reader.result;
                sendEvent('ANSWER_AUDIO', { audio_data: base64Audio });
            };
        };

        mediaRecorder.start();
        if (statusEl) statusEl.innerText = "🎤 Recording (Groq Whisper)...";

        recordTimeout = setTimeout(() => {
            if (mediaRecorder.state !== "inactive") {
                mediaRecorder.stop();
            }
        }, timeMs);

    } catch (err) {
        console.error("Microphone access denied or error:", err);
        sendEvent('ANSWER_AUDIO', { audio_data: "" });
    }
}

// --- MANUAL BACKUP BUZZERS ---
document.addEventListener('keydown', function(event) {        if (document.activeElement?.tagName === 'INPUT' || document.activeElement?.tagName === 'TEXTAREA') return;
    const key = event.key.toLowerCase();
    if ((key === 'a' || key === 'b') && currentUserRole === 'owner' && currentRoomMode !== 'online') {
        const team = key === 'a' ? 'Team A' : 'Team B';
        sendEvent('BUZZ', { team, player: key === 'a' ? 'Manual-A' : 'Manual-B' });
    }
});