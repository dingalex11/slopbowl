let setupComplete = false;
let matchTimerInterval = null;
let matchTimeSeconds = 480; 
let isMatchPaused = false;

function goToSetup() { switchTab('setup'); }

function switchTab(tab) {
    if (tab === 'match' && !setupComplete) {
        alert("🔒 Please confirm your roster and setup before entering the Match View.");
        return;
    }

    const views = ['landing', 'match', 'setup', 'tools', 'analytics','lobby']; 
    
    views.forEach(v => {
        const el = document.getElementById('view-' + v);
        if (el) {
            el.classList.add('hidden');
            el.classList.remove('flex');
        }
    });

    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.remove('active', 'text-white');
        btn.classList.add('text-slate-400');
    });

    const activeView = document.getElementById('view-' + tab);
    if (activeView) {
        activeView.classList.remove('hidden');
        activeView.classList.add('flex');
    }

    const activeBtn = document.getElementById('tab-btn-' + tab);
    if(activeBtn) {
        activeBtn.classList.remove('text-slate-400');
        activeBtn.classList.add('active', 'text-white');
    }
}

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
function triggerChallenge() {
    // 1. Immediately pause the clock if it's currently running
    if (typeof isMatchPaused !== 'undefined' && !isMatchPaused) {
        togglePause();
    }
    
    // 2. Kill the Text-to-Speech immediately so the reader shuts up
    if (window.speechSynthesis) {
        window.speechSynthesis.cancel();
    }
    
    // 3. Tell the backend to kill the current question and advance
    sendEvent('CHALLENGE', {});
}
function setStage(stage) {
    const cardPanel = document.getElementById('stage-card-panel');
    const liveDot = document.getElementById('stage-live-dot');
    const titleText = document.getElementById('stage-title-text');
    const subText = document.getElementById('stage-subtitle-text');
    const teamIndicator = document.getElementById('buzz-team-indicator');
    const playerName = document.getElementById('buzz-player-name');
    const interruptBadge = document.getElementById('interrupt-badge');
    const outcomeBadge = document.getElementById('outcome-badge');

    if(!cardPanel) return;

    cardPanel.className = "bg-isopanel border border-teamA/30 rounded-xl p-6 shadow-md flex flex-col md:flex-row justify-between items-center gap-6 relative overflow-hidden transition-all duration-300";

    if (stage === 'READ_TOSSUP') {
        cardPanel.classList.add('border-teal-500/30');
        if(liveDot) liveDot.className = "w-2 h-2 rounded-full bg-teamA opacity-80";
        if(titleText) {
            titleText.className = "text-2xl md:text-3xl font-black tracking-wide text-teamA font-mono";
            titleText.innerText = "READING TOSSUP QUESTION";
        }
        if(subText) subText.innerText = "Question reader active. Players may buzz in at any time. Interrupt penalty (-4) applies before completion.";
        if(teamIndicator) teamIndicator.className = "w-2.5 h-2.5 rounded-full bg-zinc-600";
        if(playerName) playerName.innerText = "FLOOR: AWAITING BUZZ";
        if(interruptBadge) { interruptBadge.innerText = "INTERRUPT: --"; interruptBadge.className = "px-3 py-1 rounded bg-isodark border border-isoborder text-zinc-500 font-bold"; }
        if(outcomeBadge) { outcomeBadge.innerText = "OUTCOME: PENDING"; outcomeBadge.className = "px-3 py-1 rounded bg-isodark border border-isoborder text-zinc-500 font-bold"; }
    } 
    else if (stage === 'BUZZ') {
        cardPanel.classList.add('border-teamA', 'glow-teamA');
        if(liveDot) liveDot.className = "w-2 h-2 rounded-full bg-teamA animate-pulse";
        if(titleText) {
            titleText.className = "text-2xl md:text-3xl font-black tracking-wide text-teamA font-mono";
            titleText.innerText = "BUZZ DETECTED!";
        }
        if(subText) subText.innerText = "Reader muted! Awaiting player answer.";
        if(teamIndicator) teamIndicator.className = "w-2.5 h-2.5 rounded-full bg-teamA animate-pulse";
        if(outcomeBadge) { outcomeBadge.innerText = "AWAITING ANSWER"; outcomeBadge.className = "px-3 py-1 rounded bg-teal-950/80 text-teal-300 border border-teal-800 font-bold"; }
    }
}

const bankFileInput = document.getElementById('bank-file-input');
const bankFileName = document.getElementById('bank-file-name');

if (bankFileInput) {
    bankFileInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;
        
        if(bankFileName) {
            bankFileName.innerText = `Loading ${file.name}...`;
            bankFileName.className = "text-xs text-amber-400 font-mono font-bold animate-pulse";
        }
        
        try {
            if (!file.name.toLowerCase().endsWith('.json') || file.size > 5 * 1024 * 1024) {
                throw new Error("Choose a JSON question bank no larger than 5 MB.");
            }
            const text = await file.text();
            const bankJson = JSON.parse(text);
            if (!Array.isArray(bankJson) || bankJson.length === 0 || bankJson.length > 500 || !bankJson[0].tossup_text) {
                throw new Error("Invalid format. Choose 1–500 questions with tossup text.");
            }
            sendEvent('LOAD_BANK', { bank: bankJson });
            if(bankFileName) {
                bankFileName.innerText = `✅ Loaded ${file.name} (${bankJson.length} questions). Game reset!`;
                bankFileName.className = "text-xs text-teamA font-mono font-bold";
            }
        } catch (err) {
            if(bankFileName) {
                bankFileName.innerText = `❌ ${err.message || "Invalid JSON file"}`;
                bankFileName.className = "text-xs text-red-500 font-mono font-bold";
            }
        }
    });
}
let currentRoomId = null;

async function connectToRoom(roomId, apiKey = null) {
    const existingRoom = window.activeRoomId;
    if (existingRoom && existingRoom === roomId) return;
    window.activeRoomId = roomId;
    currentRoomId = roomId;
    window.history.pushState({ room: roomId }, "", `?room=${roomId}`);
    document.getElementById('view-lobby')?.classList.add('hidden');
    document.getElementById('view-lobby')?.classList.remove('flex');
    document.getElementById('view-match')?.classList.remove('hidden');
    document.getElementById('view-match')?.classList.add('flex');
    if (typeof setupComplete !== 'undefined') setupComplete = true;
    connectWebSocket(roomId, apiKey);
}

function onlineLobbyValues() {
    const roomId = document.getElementById('room-id-input')?.value.trim().toLowerCase() || '';
    const name = document.getElementById('online-player-name')?.value.trim() || '';
    const team = document.getElementById('online-team-choice')?.value;
    const captain = Boolean(document.getElementById('online-captain-claim')?.checked);
    if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(roomId)) throw new Error('Enter a valid room code.');
    if (!name || name.length > 64 || /[\\u0000-\\u001f]/.test(name)) throw new Error('Enter a name between 1 and 64 characters.');
    return { roomId, name, team, captain };
}

async function joinOnlineRoom() {
    const status = document.getElementById('online-lobby-status');
    try {
        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        if (!window.getFirebaseUser?.()) throw new Error('Sign in before joining an online match.');
        const { roomId, name, team, captain } = onlineLobbyValues();
        const profile = await window.joinOnlineRoom(roomId, name, team, captain);
        window.onlineProfile = profile;
        await connectToRoom(roomId, await window.getUserApiKey?.());
    } catch (error) {
        if (status) status.innerText = error.message;
    }
}

async function createOnlineRoom() {
    const status = document.getElementById('online-lobby-status');
    try {
        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        if (!window.getFirebaseUser?.()) throw new Error('Sign in before creating an online match.');
        const { roomId, name, team, captain } = onlineLobbyValues();
        const profile = await window.createOnlineRoom(roomId, name, team, captain);
        window.onlineProfile = profile;
        await connectToRoom(roomId, await window.getUserApiKey?.());
    } catch (error) {
        if (status) status.innerText = error.message;
    }
}

async function joinRoom() {
    const input = document.getElementById('room-id-input');
    
    if (input && input.value.trim() !== '') {
        const requestedRoomId = input.value.trim().toLowerCase();
        if (window.ws && window.ws.readyState === WebSocket.OPEN && window.activeRoomId !== requestedRoomId) {
            alert("Disconnect from the current moderator station before opening another match room.");
            return;
        }
        currentRoomId = requestedRoomId;
        if (!/^[a-z0-9][a-z0-9-]{0,63}$/.test(currentRoomId)) {
            alert("Room code must be 1–64 letters, numbers, or hyphens and start with a letter or number.");
            return;
        }
        
        window.history.pushState({room: currentRoomId}, "", `?room=${currentRoomId}`);
        
        const lobbyView = document.getElementById('view-lobby');
        
        if (window.firebaseAuthReady) await window.firebaseAuthReady;
        const idToken = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
        if (!idToken) {
            alert("Please sign in before creating or joining a match room.");
            document.getElementById('auth-modal')?.classList.remove('hidden');
            return;
        }
        const user = window.getFirebaseUser ? window.getFirebaseUser() : null;
        if (!user) {
            alert("Sign-in is still initializing. Please try again.");
            return;
        }
        try {
            const ownsRoom = await window.ensureRoomOwnership(currentRoomId, user.uid);
            if (!ownsRoom) {
                alert("This room is not yours. Use the online match join form for player rooms.");
                return;
            }
        } catch (error) {
            alert(`Unable to initialize room: ${error.message}`);
            return;
        }
        const existingRoom = window.activeRoomId;
        if (existingRoom && existingRoom === currentRoomId) return;
        window.activeRoomId = currentRoomId;
        const joinButton = document.querySelector('#view-lobby button[onclick="joinRoom()"]');
        if (joinButton) joinButton.disabled = true;
        try {
            connectWebSocket(currentRoomId, window.getUserApiKey ? await window.getUserApiKey() : null);
        } finally {
            if (joinButton) joinButton.disabled = false;
        }
    } else {
        alert("⚠️ Please enter a valid room code.");
    }
}

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
        alert(error.message);
        return;
    }
    
    sendEvent('SAVE_ROSTER', rosterData);
    updateRosterNames(rosterData); 
    
    setupComplete = true; 
    switchTab('match');
}

function beginMatch() {
    const overlay = document.getElementById('match-start-overlay');
    if (overlay) overlay.classList.add('hidden');
    
    const timerDisplay = document.getElementById('main-timer-display');
    
    if (matchTimerInterval) clearInterval(matchTimerInterval);
    
    matchTimerInterval = setInterval(() => {
        if (isMatchPaused) return; 
        
        if (matchTimeSeconds <= 0) {
            clearInterval(matchTimerInterval);
            timerDisplay.innerText = "00:00";
            timerDisplay.classList.add("text-red-500", "animate-pulse");
            timerDisplay.classList.remove("text-teamA");
            return;
        }
        
        matchTimeSeconds--;
        const m = Math.floor(matchTimeSeconds / 60);
        const s = matchTimeSeconds % 60;
        timerDisplay.innerText = `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
    }, 1000);
}

function togglePause() {
    isMatchPaused = !isMatchPaused;
    const pauseBtn = document.getElementById('btn-pause-match');
    
    if (isMatchPaused) {
        if(pauseBtn) {
            pauseBtn.innerHTML = '▶ RESUME';
            pauseBtn.classList.replace('bg-zinc-700', 'bg-rose-600');
            pauseBtn.classList.replace('hover:bg-zinc-600', 'hover:bg-rose-500');
        }
        if (window.speechSynthesis) window.speechSynthesis.pause();
    } else {
        if(pauseBtn) {
            pauseBtn.innerHTML = '⏸ PAUSE';
            pauseBtn.classList.replace('bg-rose-600', 'bg-zinc-700');
            pauseBtn.classList.replace('hover:bg-rose-500', 'hover:bg-zinc-600');
        }
        if (window.speechSynthesis) window.speechSynthesis.resume();
    }
    
    sendEvent('PAUSE_MATCH', { paused: isMatchPaused }); 
}

// --- AUTO-JOIN FROM URL ---
window.addEventListener('DOMContentLoaded', () => {
    // Look at the URL for "?room=something"
    const params = new URLSearchParams(window.location.search);
    const roomParam = params.get('room');
    
    if (roomParam) {
        // Automatically fill the input box and click the join button behind the scenes
        const input = document.getElementById('room-id-input');
        if (input) {
            input.value = roomParam;
            joinRoom();
        }
    }
});
// ... [Existing ui.js code] ...

const btnLoadCloudPacket = document.getElementById('btn-load-cloud-packet');
const cloudPacketSelect = document.getElementById('cloud-packet-select');

if (btnLoadCloudPacket) {
    btnLoadCloudPacket.addEventListener('click', () => {
        const selectedId = cloudPacketSelect?.value;
        if (!selectedId || !window.cloudPacketsCache || !window.cloudPacketsCache[selectedId]) {
            alert("⚠️ Please select a valid cloud packet first.");
            return;
        }

        const bankJson = window.cloudPacketsCache[selectedId];
        const bankName = cloudPacketSelect.options[cloudPacketSelect.selectedIndex].text;
        
        if (bankFileName) {
            bankFileName.innerText = `✅ Loaded ${bankName} from Cloud. Game reset!`;
            bankFileName.className = "text-xs text-teamA font-mono font-bold";
        }

        // Emit to backend exactly like a local file
        sendEvent('LOAD_BANK', { bank: bankJson });
    });
}