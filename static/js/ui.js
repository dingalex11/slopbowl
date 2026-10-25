// SLOPBOWL app glue: room lifecycle, setup, stage badges.
// DOM navigation lives in slopbowl_ui.js (navigateTo/openModal/showToast).
let setupComplete = false;
let isMatchPaused = false;
let currentRoomId = null;

// --- ROSTER ---
function updateRosterNames(rosterData) {
    if (!rosterData) return;

    const nameA = rosterData.teams["Team A"];
    const nameB = rosterData.teams["Team B"];

    const headerA = document.getElementById('header-team-a-name');
    const headerB = document.getElementById('header-team-b-name');
    if (headerA) headerA.innerText = nameA.toUpperCase();
    if (headerB) headerB.innerText = nameB.toUpperCase();

    const scoreLabelA = document.getElementById('scoresheet-team-a-label');
    const scoreLabelB = document.getElementById('scoresheet-team-b-label');
    if (scoreLabelA) scoreLabelA.innerText = nameA.toUpperCase() + ':';
    if (scoreLabelB) scoreLabelB.innerText = nameB.toUpperCase() + ':';

    for (const [key, val] of Object.entries(rosterData.players)) {
        const pill = document.getElementById(`match-roster-${key}`);
        if (pill) {
            const prefix = key.endsWith('C') ? 'C' : `P${key.split('-')[1]}`;
            pill.innerText = `${prefix}: ${val}`;
        }
    }
}

// --- CHALLENGE ---
function triggerChallenge() {
    // Kill the Text-to-Speech immediately so the reader shuts up
    if (window.speechSynthesis) {
        window.speechSynthesis.cancel();
    }

    // Tell the backend to kill the current question and advance
    sendEvent('CHALLENGE', {});
    showToast('Challenge submitted — question paused and skipped');
}

// --- STAGE BADGES (question text is never rendered; audio-only readout) ---
function setStage(stage) {
    const cardPanel = document.getElementById('stage-card-panel');
    const liveDot = document.getElementById('stage-live-dot');
    const buzzTeamIndicator = document.getElementById('buzz-team-indicator');
    const playerName = document.getElementById('buzz-player-name');
    const interruptBadge = document.getElementById('interrupt-badge');
    const outcomeBadge = document.getElementById('outcome-badge');

    if (!cardPanel) return;

    if (stage === 'READ_TOSSUP') {
        if (liveDot) liveDot.className = "w-2 h-2 rounded-full bg-neonCyan opacity-80";
        if (buzzTeamIndicator) buzzTeamIndicator.className = "w-2.5 h-2.5 rounded-full bg-slate-600";
        if (playerName) playerName.innerText = "FLOOR: AWAITING BUZZ";
        if (interruptBadge) { interruptBadge.innerText = "INTERRUPT: --"; interruptBadge.className = "px-3 py-1 rounded-full bg-black border border-borderDark text-slate-500 font-bold"; }
        if (outcomeBadge) { outcomeBadge.innerText = "OUTCOME: PENDING"; outcomeBadge.className = "px-3 py-1 rounded-full bg-black border border-borderDark text-slate-500 font-bold"; }
    }
    else if (stage === 'BUZZ') {
        if (liveDot) liveDot.className = "w-2 h-2 rounded-full bg-neonCyan animate-pulse";
        if (buzzTeamIndicator) buzzTeamIndicator.className = "w-2.5 h-2.5 rounded-full bg-neonCyan animate-pulse";
        if (outcomeBadge) { outcomeBadge.innerText = "AWAITING ANSWER"; outcomeBadge.className = "px-3 py-1 rounded-full bg-black border border-neonGreen text-neonGreen font-bold"; }
    }
}

// --- QUESTION BANK JSON LOADER ---
const bankFileInput = document.getElementById('bank-file-input');
const bankFileName = document.getElementById('bank-file-name');

// --- PRE-MATCH SETTINGS PACKET UPLOAD (real .json / .txt) ---
const settingsPacketInput = document.getElementById('settings-packet-input');
if (settingsPacketInput) {
    settingsPacketInput.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (file) handleSettingsPacket(file);
    });
}

const settingsPacketDrop = document.getElementById('settings-packet-drop');
if (settingsPacketDrop) {
    settingsPacketDrop.addEventListener('dragover', (e) => {
        e.preventDefault();
        settingsPacketDrop.classList.add('border-neonAmber');
    });
    settingsPacketDrop.addEventListener('dragleave', () => {
        settingsPacketDrop.classList.remove('border-neonAmber');
    });
    settingsPacketDrop.addEventListener('drop', (e) => {
        e.preventDefault();
        settingsPacketDrop.classList.remove('border-neonAmber');
        if (e.dataTransfer.files.length) handleSettingsPacket(e.dataTransfer.files[0]);
    });
}

async function handleSettingsPacket(file) {
    const status = document.getElementById('settings-packet-status');
    try {
        let bankJson;
        if (file.name.toLowerCase().endsWith('.json')) {
            bankJson = JSON.parse(await file.text());
        } else if (file.name.toLowerCase().endsWith('.txt')) {
            bankJson = parseScienceBowlText(await file.text());
        } else {
            throw new Error('Choose a .json or .txt question package.');
        }
        if (!Array.isArray(bankJson) || !bankJson.length || bankJson.length > 500 || !bankJson[0].tossup_text) {
            throw new Error('Invalid package: expected 1-500 question sets with tossup text.');
        }
        // The room must accept the packet before this page may call it ready: a local
        // "ready" message without a live socket would be a lie.
        if (typeof isRoomConnected !== 'function' || !isRoomConnected()) {
            throw new Error('Not connected to the match room — the packet was not sent. Reopen the room and try again.');
        }
        sendEvent('LOAD_BANK', { bank: bankJson });
        // A packet loaded for a match is also useful for solo practice, with no re-upload.
        if (typeof soloRegisterPacketFromApp === 'function') soloRegisterPacketFromApp(file.name.replace(/\.(json|txt)$/i, ''), bankJson);
        if (status) {
            status.innerText = `Sending ${file.name} to the room — waiting for confirmation...`;
            status.className = 'mt-4 text-center text-xs font-mono font-bold text-neonAmber min-h-5';
        }
    } catch (error) {
        if (status) {
            status.innerText = `Error: ${error.message}`;
            status.className = 'mt-4 text-center text-xs font-mono font-bold text-neonPink min-h-5';
        }
    }
}

if (bankFileInput) {
    bankFileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        if (bankFileName) {
            bankFileName.innerText = `Loading ${file.name}...`;
            bankFileName.className = "text-xs text-neonAmber font-mono font-bold animate-pulse";
        }

        try {
            if (!file.name.toLowerCase().endsWith('.json') || file.size > 5 * 1024 * 1024) {
                throw new Error("Choose a JSON question bank no larger than 5 MB.");
            }
            const text = await file.text();
            const bankJson = JSON.parse(text);
            if (!Array.isArray(bankJson) || bankJson.length === 0 || bankJson.length > 500 || !bankJson[0].tossup_text) {
                throw new Error("Invalid format. Choose 1-500 questions with tossup text.");
            }
            if (typeof isRoomConnected !== 'function' || !isRoomConnected()) {
                throw new Error("Not connected to the match room — the packet was not sent.");
            }
            sendEvent('LOAD_BANK', { bank: bankJson });
            if (typeof soloRegisterPacketFromApp === 'function') soloRegisterPacketFromApp(file.name.replace(/\.(json|txt)$/i, ''), bankJson);
            if (bankFileName) {
                bankFileName.innerText = `Sending ${file.name} (${bankJson.length} questions) — waiting for confirmation...`;
                bankFileName.className = "text-xs text-neonAmber font-mono font-bold animate-pulse";
            }
        } catch (err) {
            if (bankFileName) {
                bankFileName.innerText = `Error: ${err.message || "Invalid JSON file"}`;
                bankFileName.className = "text-xs text-neonPink font-mono font-bold";
            }
        }
    });
}

// --- ROOM CONNECTION ---
async function connectToRoom(roomId, apiKey = null, expectedRole = 'owner', entrySlide = null) {
    const existingRoom = window.activeRoomId;
    if (existingRoom && existingRoom === roomId) return;
    window.activeRoomId = roomId;
    currentRoomId = roomId;
    updateRoomCodeDisplay();
    window.history.pushState({ room: roomId }, "", `?room=${roomId}`);
    if (typeof setupComplete !== 'undefined') setupComplete = true;

    // Role-based entry view: moderators land on the pre-match settings page,
    // players land on the pre-match lobby to pick their team.
    navigateTo(entrySlide ?? (expectedRole === 'player' ? 7 : 5));
    connectWebSocket(roomId, apiKey);
}

function validatePlayerName(name) {
    if (!name || name.length > 64 || /[\u0000-\u001f]/.test(name)) throw new Error('Enter a name between 1 and 64 characters.');
}

function validateRoomIdentity(roomId, name) {
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(roomId)) throw new Error('Enter a valid room code.');
    validatePlayerName(name);
}

function clearLobbyStatus() {
    const status = document.getElementById('online-lobby-status');
    if (status) status.innerText = '';
}

async function joinOnlineRoom() {
    const status = document.getElementById('online-lobby-status');
    try {
        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        if (!window.getFirebaseUser?.()) {
            openModal('auth-modal');
            throw new Error('Sign in before joining an online match.');
        }
        const roomId = document.getElementById('room-id-input')?.value.trim().toLowerCase() || '';
        const name = document.getElementById('online-player-name')?.value.trim() || '';
        validateRoomIdentity(roomId, name);
        // Team is picked on the pre-match screen; join with a default first.
        const profile = await window.joinOnlineRoomApi(roomId, name, 'Team A', false);
        window.onlineProfile = profile;
        await connectToRoom(roomId, await window.getUserApiKey?.(), 'player', 7);
    } catch (error) {
        if (status) status.innerText = error.message;
    }
}

async function createOnlineRoom() {
    const status = document.getElementById('online-lobby-status');
    try {
        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        if (!window.getFirebaseUser?.()) {
            openModal('auth-modal');
            throw new Error('Sign in before creating an online match.');
        }
        const name = document.getElementById('create-player-name')?.value.trim() || '';
        validatePlayerName(name);
        if (status) status.innerText = 'Allocating a fresh room code...';
        // The server picks the code, so creating a match cannot collide with a taken one.
        const profile = await window.createOnlineRoomApi(name, 'Team A', false);
        window.onlineProfile = profile;
        const roomId = profile?.room_id;
        if (!roomId) throw new Error('The room service did not return a room code.');
        // The moderator joins with a default team and picks their real team/captain
        // seat in the pre-match lobby.
        await connectToRoom(roomId, await window.getUserApiKey?.(), 'owner', 10);
    } catch (error) {
        if (status) status.innerText = error.message;
    }
}

// --- READING MODE (online matches) ---
// stations: every player's browser speaks the question itself (the old behaviour).
// moderator: only the moderator's station speaks; the room listens to that one voice, so
// the read, the interrupt boundary and the 5-second buzz clock are shared by both teams.
const READING_MODE_ON = 'bg-neonCyan text-black border-neonCyan shadow-neon-cyan';
const READING_MODE_OFF = 'bg-black text-slate-300 border-borderDark hover:border-neonCyan';
const READING_MODE_BUTTON = 'py-2.5 text-[11px] font-mono font-black uppercase rounded-xl border transition active:scale-95';

function renderReadingMode(mode) {
    const shared = mode === 'moderator';
    const stationsBtn = document.getElementById('reading-mode-stations');
    const moderatorBtn = document.getElementById('reading-mode-moderator');
    const badge = document.getElementById('reading-mode-badge');
    const note = document.getElementById('reading-mode-note');
    if (stationsBtn) stationsBtn.className = `${READING_MODE_BUTTON} ${shared ? READING_MODE_OFF : READING_MODE_ON}`;
    if (moderatorBtn) moderatorBtn.className = `${READING_MODE_BUTTON} ${shared ? READING_MODE_ON : READING_MODE_OFF}`;
    if (badge) {
        badge.innerText = shared ? 'MODERATOR READS' : 'EACH DEVICE';
        badge.className = `text-[9px] font-mono font-bold bg-black ${shared ? 'text-neonGreen border-neonGreen/60' : 'text-neonCyan border-neonCyan/60'} border rounded-full px-2 py-0.5 tracking-widest`;
    }
    if (note) {
        note.innerText = shared
            ? "Player devices stay silent. Share this station's audio with the call (whole screen + system audio, not tab audio): the read, the interrupt boundary and the 5-second buzz clock all follow this voice."
            : "Each player's browser speaks the tossup with its own voice, so read speeds differ. Moderator mode keeps every player device silent and runs the read off one voice.";
    }
}

function setReadingMode(mode) {
    if (mode !== 'stations' && mode !== 'moderator') return;
    // Draw it immediately; the server echo (READING_MODE) is what makes it true for the room.
    renderReadingMode(mode);
    sendEvent('SET_READING_MODE', { mode });
}

// --- PRE-MATCH SETTINGS -> LOBBY -> MATCH ---
function proceedToLobby() {
    updateRoomCodeDisplay();
    navigateTo(9);
}

function startMatchFromLobby() {
    // Start and open HUD: the clock runs immediately, no BEGIN MATCH overlay.
    beginMatch();
    const overlay = document.getElementById('match-start-overlay');
    if (overlay) overlay.classList.add('hidden');
    navigateTo(5);
}

// --- TEAM / CAPTAIN CLAIMING (real re-join + reconnect) ---
async function rejoinRoom(team, captain) {
    const roomId = window.activeRoomId || currentRoomId;
    if (!roomId) {
        showToast('Join a room first');
        return;
    }
    const name = window.onlineProfile?.name || document.getElementById('online-player-name')?.value.trim() || document.getElementById('create-player-name')?.value.trim() || 'Player';
    try {
        const profile = await window.joinOnlineRoomApi(roomId, name, team, Boolean(captain));
        window.onlineProfile = profile;
        refreshRoomConnection();
        showToast(`Seat updated: ${team}${captain ? ' (Captain)' : ''}`);
    } catch (error) {
        showToast(error.message || 'Unable to update seat');
    }
}

function switchTeam(team) {
    const keepCaptain = Boolean(window.onlineProfile?.captain) && window.onlineProfile?.team === team;
    rejoinRoom(team, keepCaptain);
}

function claimCaptain(team) {
    rejoinRoom(team || window.onlineProfile?.team || 'Team A', true);
}

function refreshRoomConnection() {
    const roomId = window.activeRoomId || currentRoomId;
    if (!roomId) return;
    const oldSocket = window.ws;
    if (oldSocket) {
        window._profileRefresh = oldSocket;
        try { oldSocket.close(4000, 'profile-refresh'); } catch (e) { /* already closing */ }
    }
    connectWebSocket(roomId, window._lastApiKey || null);
}

// --- LOBBY SHORTCUTS ---
function updateRoomCodeDisplay() {
    const code = (window.activeRoomId || currentRoomId || '').toUpperCase();
    const lobbyCode = document.getElementById('mod-lobby-room-code');
    if (lobbyCode) lobbyCode.innerText = code || '—';
}

async function copyRoomCode() {
    const code = window.activeRoomId || currentRoomId || '';
    if (!code) return;
    try {
        await navigator.clipboard.writeText(code);
        showToast(`Room code "${code}" copied`);
    } catch (e) {
        showToast(`Room code: ${code}`);
    }
}

async function copyInviteLink() {
    const code = window.activeRoomId || currentRoomId || '';
    if (!code) return;
    // The hash form is the screen's own address, so the link lands on the online hub with
    // the room code already filled in rather than jumping straight into a seat.
    const link = `${location.origin}${location.pathname}#/join/${code}`;
    try {
        await navigator.clipboard.writeText(link);
        showToast('Invite link copied');
    } catch (e) {
        showToast(link);
    }
}

async function joinRoom() {
    const input = document.getElementById('room-id-input');

    if (input && input.value.trim() !== '') {
        const requestedRoomId = input.value.trim().toLowerCase();
        if (window.ws && window.ws.readyState === WebSocket.OPEN && window.activeRoomId !== requestedRoomId) {
            showToast("Disconnect from the current moderator station before opening another match room.");
            return;
        }
        currentRoomId = requestedRoomId;
        if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(currentRoomId)) {
            showToast("Room code must be 1-64 letters, numbers, or hyphens and start with a letter or number.");
            return;
        }

        window.history.pushState({ room: currentRoomId }, "", `?room=${currentRoomId}`);

        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        const idToken = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
        if (!idToken) {
            showToast("Please sign in before creating or joining a match room.");
            openModal('auth-modal');
            return;
        }
        const user = window.getFirebaseUser ? window.getFirebaseUser() : null;
        if (!user) {
            showToast("Sign-in is still initializing. Please try again.");
            return;
        }
        try {
            const ownsRoom = await window.ensureRoomOwnership(currentRoomId, user.uid);
            if (!ownsRoom) {
                showToast("This room is not yours. Use the online match join form for player rooms.");
                return;
            }
        } catch (error) {
            showToast(`Unable to initialize room: ${error.message}`);
            return;
        }
        const existingRoom = window.activeRoomId;
        if (existingRoom && existingRoom === currentRoomId) return;
        window.activeRoomId = currentRoomId;
        try {
            navigateTo(5);
            connectWebSocket(currentRoomId, window.getUserApiKey ? await window.getUserApiKey() : null);
        } finally {
            if (input) input.disabled = false;
        }
    } else {
        showToast("Please enter a valid room code.");
    }
}

// --- SETUP / ROSTER CONFIRM ---
function confirmSetup() {
    const getVal = (id, defaultVal) => {
        const el = document.getElementById(id);
        const value = (el && el.value.trim() !== '') ? el.value.trim() : defaultVal;
        if (value.length > 64 || /[\u0000-\u001f]/.test(value)) {
            throw new Error('Team and player names must be 64 characters or fewer and contain no control characters.');
        }
        return value;
    };

    let rosterData;
    try {
        rosterData = {
            teams: {
                "Team A": getVal('input-team-a-name', "Team A"),
                "Team B": getVal('input-team-b-name', "Team B")
            },
            players: {
                "A-1": getVal('roster-A-1', "Player 1"),
                "A-C": getVal('roster-A-C', "Captain"),
                "A-3": getVal('roster-A-3', "Player 3"),
                "A-4": getVal('roster-A-4', "Player 4"),
                "A-5": getVal('roster-A-5', "Player 5"),
                "B-1": getVal('roster-B-1', "Player 1"),
                "B-C": getVal('roster-B-C', "Captain"),
                "B-3": getVal('roster-B-3', "Player 3"),
                "B-4": getVal('roster-B-4', "Player 4"),
                "B-5": getVal('roster-B-5', "Player 5")
            }
        };
    } catch (error) {
        showToast(error.message);
        return;
    }

    sendEvent('SAVE_ROSTER', rosterData);
    updateRosterNames(rosterData);

    setupComplete = true;
    navigateTo(5);
}

// --- MATCH START ---
// There is no global match clock: the match runs off live play and question counts.
function beginMatch() {
    const overlay = document.getElementById('match-start-overlay');
    if (overlay) overlay.classList.add('hidden');
    // The match clock is not the only thing that starts here: the header scores become
    // meaningful the moment the overlay goes, so ask the router to reveal them.
    if (typeof window.updateHeaderScore === 'function') window.updateHeaderScore();
}

function togglePause() {
    isMatchPaused = !isMatchPaused;
    const pauseBtn = document.getElementById('btn-pause-match');

    if (isMatchPaused) {
        if (pauseBtn) pauseBtn.innerHTML = '&#9654; RESUME';
        if (window.speechSynthesis) window.speechSynthesis.pause();
    } else {
        if (pauseBtn) pauseBtn.innerHTML = '&#9208; PAUSE';
        if (window.speechSynthesis) window.speechSynthesis.resume();
    }

    sendEvent('PAUSE_MATCH', { paused: isMatchPaused });
}

// --- PLAYER SEAT LABELS (filled from CONNECTED payload) ---
function updateSeatLabels(payload) {
    if (!payload) return;
    const seat = document.getElementById('player-seat-label');
    const team = document.getElementById('player-team-label');
    const room = document.getElementById('player-room-label');
    const name = payload.name || 'Player';
    const captain = payload.captain ? ' (Captain)' : '';
    if (seat) seat.innerText = `${name}${captain}`;
    if (team) team.innerText = payload.team || '—';
    if (room) room.innerText = (window.activeRoomId || '').toUpperCase() || '—';
}

// --- AUTO-JOIN FROM URL ---
window.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    const roomParam = params.get('room');

    if (roomParam) {
        const input = document.getElementById('room-id-input');
        if (input) {
            input.value = roomParam;
            joinRoom();
        }
    }
});

// --- SCREEN LINKS ---
// #/join/CODE is the shareable room link: it opens the online hub with the code ready.
document.addEventListener('app:view', (event) => {
    const detail = event.detail || {};
    if (detail.name !== 'online') return;
    const room = detail.params && detail.params.room;
    if (!room) return;
    const input = document.getElementById('room-id-input');
    if (input && !input.value) input.value = room;
});
