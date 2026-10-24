// /static/js/auth.js

import { initializeApp } from "https://www.gstatic.com/firebasejs/12.0.0/firebase-app.js";
import { 
    getAuth, 
    signInAnonymously,
    signInWithEmailAndPassword, 
    createUserWithEmailAndPassword, 
    signOut, 
    onAuthStateChanged 
} from "https://www.gstatic.com/firebasejs/12.0.0/firebase-auth.js";
import { 
    getFirestore, 
    doc, 
    setDoc, 
    getDoc,
    updateDoc,
    deleteField,
    collection, 
    addDoc, 
    getDocs
} from "https://www.gstatic.com/firebasejs/12.0.0/firebase-firestore.js";

// Firebase's web config is public by design: it names the project, it does not grant access
// to it. index.html fills this in from the server environment with the project's own config
// as the fallback. A missing config must not abort this module — that used to leave every
// sign-in button silently dead, with no handler bound and no message on screen.
const firebaseConfig = window.FIREBASE_CONFIG || {};

let app = null;
let auth = null;
let db = null;
let configError = null;

try {
    if (!firebaseConfig.apiKey || !firebaseConfig.projectId) {
        throw new Error("Firebase is not configured on this server (FIREBASE_API_KEY / FIREBASE_PROJECT_ID).");
    }
    app = initializeApp(firebaseConfig);
    auth = getAuth(app);
    db = getFirestore(app);
} catch (error) {
    configError = error;
    console.error("Sign-in is unavailable:", error);
}

export { auth, db };

// DOM Elements
const authModal = document.getElementById('auth-modal');
const emailInput = document.getElementById('auth-email');
const passwordInput = document.getElementById('auth-password');
const btnSignIn = document.getElementById('btn-sign-in');
const btnSignUp = document.getElementById('btn-sign-up');
const authError = document.getElementById('auth-error');

const userSection = document.getElementById('auth-user-section');
const userEmailDisplay = document.getElementById('auth-user-email');
const btnShowLogin = document.getElementById('btn-show-login');
const btnSignOut = document.getElementById('btn-sign-out');

const settingsApiKeyInput = document.getElementById('settings-api-key');
const btnSaveSettings = document.getElementById('btn-save-settings');
const settingsStatus = document.getElementById('settings-status');
const settingsKeyBadge = document.getElementById('settings-key-badge');
const btnForgetKey = document.getElementById('btn-clear-api-key');
const settingsAccountEmail = document.getElementById('settings-account-email');
const btnSettingsAccount = document.getElementById('btn-settings-account');

const JUDGE_KEY_FIELD = 'judgeApiKey';
const LEGACY_JUDGE_KEY_FIELD = 'groqApiKey';
const CONFIG_HELP = 'Sign-in is not configured on this server. Set FIREBASE_API_KEY and FIREBASE_PROJECT_ID in the server environment and reload.';

// --- MESSAGES ---------------------------------------------------------------

function showSettingsStatus(message, tone) {
    if (!settingsStatus) return;
    const tones = {
        ok: 'text-emerald-400 bg-emerald-950/50 border border-emerald-900',
        warn: 'text-amber-400 bg-amber-950/50 border border-amber-900',
        error: 'text-red-400 bg-red-950/50 border border-red-900'
    };
    settingsStatus.innerText = message;
    settingsStatus.className = `text-xs ${tones[tone] || tones.warn} p-2 rounded font-mono block`;
}

function displayError(message) {
    if (!authError) return;
    authError.innerText = message;
    authError.classList.remove('hidden');
}

function clearError() {
    if (!authError) return;
    authError.classList.add('hidden');
    authError.innerText = '';
}

// Firebase's own messages ("auth/invalid-credential") tell a player nothing about what to
// fix. These say what happened, and point at the server config when that is the cause.
function authErrorMessage(error) {
    const code = (error && error.code) || '';
    const messages = {
        'auth/invalid-credential': 'That email and password do not match an account.',
        'auth/wrong-password': 'That email and password do not match an account.',
        'auth/user-not-found': 'No account uses that email. Use Sign Up to create one.',
        'auth/invalid-email': 'That email address is not valid.',
        'auth/missing-password': 'Enter a password.',
        'auth/email-already-in-use': 'That email already has an account. Use Sign In.',
        'auth/weak-password': 'Choose a password of at least 6 characters.',
        'auth/too-many-requests': 'Too many attempts. Wait a moment and try again.',
        'auth/network-request-failed': 'Could not reach the sign-in service. Check your connection.',
        'auth/api-key-not-valid': CONFIG_HELP,
        'auth/invalid-api-key': CONFIG_HELP,
        'auth/unauthorized-domain': 'This address is not an authorised sign-in domain for the project.',
        'auth/operation-not-allowed': 'Email sign-in is not enabled for this project.',
        'auth/admin-restricted-operation': 'Email sign-in is not enabled for this project.'
    };
    return messages[code] || (error && error.message) || 'Sign-in failed.';
}

function closeLoginOverlay() {
    if (authModal && !authModal.classList.contains('hidden')) {
        if (typeof window.routerCloseLogin === 'function') window.routerCloseLogin();
        else {
            authModal.classList.add('hidden');
            authModal.classList.remove('flex');
        }
    }
}

// --- SAVED JUDGE KEY --------------------------------------------------------
// The key belongs to the signed-in account so a captain does not have to find and paste it
// again on every device. Firestore rules keep `/users/{uid}` readable by that uid only, and
// the key is still sent to the match owner over the authenticated WebSocket exactly as
// before — nothing about the judging path changes.

function judgeKeyFromData(data) {
    if (!data || typeof data !== 'object') return '';
    const current = typeof data[JUDGE_KEY_FIELD] === 'string' ? data[JUDGE_KEY_FIELD].trim() : '';
    if (current) return current;
    return typeof data[LEGACY_JUDGE_KEY_FIELD] === 'string' ? data[LEGACY_JUDGE_KEY_FIELD].trim() : '';
}

async function loadJudgeKey(user) {
    if (!user || !db) return '';
    const snapshot = await getDoc(doc(db, 'users', user.uid));
    if (!snapshot.exists()) return '';
    const data = snapshot.data() || {};
    const key = judgeKeyFromData(data);
    if (key && !data[JUDGE_KEY_FIELD]) {
        // Keys an earlier build wrote under `groqApiKey` move to the current field.
        await setDoc(doc(db, 'users', user.uid), { [JUDGE_KEY_FIELD]: key }, { merge: true });
        await updateDoc(doc(db, 'users', user.uid), { [LEGACY_JUDGE_KEY_FIELD]: deleteField() });
    }
    return key;
}

window.getUserApiKey = async () => {
    if (window.sessionGroqApiKey) return window.sessionGroqApiKey;
    const user = auth && auth.currentUser;
    if (!user) return null;
    const key = await loadJudgeKey(user).catch(() => '');
    if (key) window.sessionGroqApiKey = key;
    return key || null;
};

// --- ACCOUNT ROW ON THE SETTINGS CARD ---------------------------------------

function renderSettingsAccount(user) {
    if (settingsAccountEmail) {
        settingsAccountEmail.innerText = user ? (user.email || 'Signed in') : 'Not signed in';
    }
    if (btnSettingsAccount) {
        btnSettingsAccount.innerText = user ? 'Sign out' : 'Sign in';
        btnSettingsAccount.className = user
            ? 'px-4 py-2 bg-black border border-neonPink/70 text-neonPink text-xs font-mono font-bold rounded-lg active:scale-95 transition'
            : 'px-4 py-2 bg-black border border-neonCyan text-neonCyan text-xs font-mono font-bold rounded-lg active:scale-95 transition';
    }
    if (settingsKeyBadge) {
        settingsKeyBadge.innerText = user ? 'SAVED TO ACCOUNT' : 'SESSION ONLY';
        settingsKeyBadge.className = user
            ? 'text-[9px] font-mono font-bold bg-black text-neonCyan border border-neonCyan/60 rounded-full px-2 py-0.5 tracking-widest'
            : 'text-[9px] font-mono font-bold bg-black text-neonAmber border border-neonAmber/60 rounded-full px-2 py-0.5 tracking-widest';
    }
}

btnSettingsAccount?.addEventListener('click', async () => {
    if (auth && auth.currentUser) {
        await signOut(auth).catch((error) => console.error("Error signing out: ", error));
        return;
    }
    if (configError) {
        displayError(CONFIG_HELP);
        return;
    }
    if (typeof openModal === 'function') openModal('auth-modal');
    else if (authModal) {
        authModal.classList.remove('hidden');
        authModal.classList.add('flex');
    }
});

// --- CONSOLIDATED AUTHENTICATION STATE OBSERVER ---
let resolveFirebaseAuthReady;
window.firebaseAuthReady = new Promise((resolve) => { resolveFirebaseAuthReady = resolve; });

async function handleSignedIn(user) {
    // 1. Update Core Header UI
    userSection.classList.remove('hidden');
    userSection.classList.add('flex');
    btnShowLogin.classList.add('hidden');
    userEmailDisplay.innerText = user.email || '';
    closeLoginOverlay();
    if (passwordInput) passwordInput.value = '';

    // 2. Reveal Cloud Feature Buttons
    const cloudPacketSection = document.getElementById('cloud-packet-section');
    const btnSaveCloud = document.getElementById('btn-save-cloud');
    const btnCloudAnalytics = document.getElementById('btn-cloud-analytics');
    if (cloudPacketSection) {
        cloudPacketSection.classList.remove('hidden');
        cloudPacketSection.classList.add('flex');
    }
    if (btnSaveCloud) btnSaveCloud.classList.remove('hidden');
    if (btnCloudAnalytics) btnCloudAnalytics.classList.remove('hidden');

    // 3. Load the saved judge key, so the next match judges with it without any typing.
    if (settingsApiKeyInput) settingsApiKeyInput.value = '';
    window.sessionGroqApiKey = null;
    renderSettingsAccount(user);
    const keyLoad = (async () => {
        try {
            const key = await loadJudgeKey(user);
            if (key) {
                window.sessionGroqApiKey = key;
                if (settingsApiKeyInput) settingsApiKeyInput.value = key;
                showSettingsStatus('Saved key loaded from your account.', 'ok');
                setTimeout(() => settingsStatus?.classList.add('hidden'), 2500);
            }
        } catch (error) {
            console.warn("Could not load the saved judge key:", error);
            showSettingsStatus('Could not read your saved key. Paste it again and save.', 'error');
        }
    })();
    // Readiness must not wait forever on a slow Firestore read: joining a match asks for
    // the key right after this resolves, and getUserApiKey() reads it again if it is not
    // back yet.
    await Promise.race([keyLoad, new Promise((resolve) => setTimeout(resolve, 5000))]);
    resolveFirebaseAuthReady(user);

    await window.populateCloudPackets();
}

async function handleSignedOut() {
    // 1. Reset Core Header UI
    userSection.classList.remove('flex');
    userSection.classList.add('hidden');
    btnShowLogin.classList.remove('hidden');
    userEmailDisplay.innerText = '';
    if (settingsApiKeyInput) settingsApiKeyInput.value = '';
    window.sessionGroqApiKey = null;
    if (settingsStatus) settingsStatus.classList.add('hidden');
    renderSettingsAccount(null);

    // 2. Hide Cloud Feature Buttons
    const cloudPacketSection = document.getElementById('cloud-packet-section');
    const btnSaveCloud = document.getElementById('btn-save-cloud');
    const btnCloudAnalytics = document.getElementById('btn-cloud-analytics');
    if (cloudPacketSection) {
        cloudPacketSection.classList.add('hidden');
        cloudPacketSection.classList.remove('flex');
    }
    if (btnSaveCloud) btnSaveCloud.classList.add('hidden');
    if (btnCloudAnalytics) btnCloudAnalytics.classList.add('hidden');
}

if (auth) {
    onAuthStateChanged(auth, (user) => {
        if (user) handleSignedIn(user).catch((error) => {
            console.error("Error applying signed-in state:", error);
            resolveFirebaseAuthReady(user);
        });
        else {
            handleSignedOut().catch((error) => console.error("Error applying signed-out state:", error));
            resolveFirebaseAuthReady(null);
        }
    });
} else {
    // The page still works without accounts (solo practice, in-person matches); it just says
    // why sign-in cannot work instead of offering a button that does nothing.
    resolveFirebaseAuthReady(null);
    renderSettingsAccount(null);
    showSettingsStatus(CONFIG_HELP, 'error');
    if (btnSettingsAccount) btnSettingsAccount.title = CONFIG_HELP;
}

// --- UI EVENT LISTENERS ---

btnSignIn?.addEventListener('click', async () => {
    clearError();
    if (!auth) {
        displayError(CONFIG_HELP);
        return;
    }
    const email = (emailInput?.value || '').trim();
    const password = passwordInput?.value || '';
    if (!email || !password) {
        displayError('Enter your email and password.');
        return;
    }
    btnSignIn.disabled = true;
    btnSignIn.innerText = 'Signing in…';
    try {
        await signInWithEmailAndPassword(auth, email, password);
    } catch (error) {
        displayError(authErrorMessage(error));
    } finally {
        btnSignIn.disabled = false;
        btnSignIn.innerText = 'Sign In';
    }
});

btnSignUp?.addEventListener('click', async () => {
    clearError();
    if (!auth) {
        displayError(CONFIG_HELP);
        return;
    }
    const email = (emailInput?.value || '').trim();
    const password = passwordInput?.value || '';
    if (!email || !password) {
        displayError('Enter an email and a password to create an account.');
        return;
    }
    btnSignUp.disabled = true;
    btnSignUp.innerText = 'Creating…';
    try {
        await createUserWithEmailAndPassword(auth, email, password);
    } catch (error) {
        displayError(authErrorMessage(error));
    } finally {
        btnSignUp.disabled = false;
        btnSignUp.innerText = 'Sign Up';
    }
});

btnSignOut?.addEventListener('click', async () => {
    try {
        await signOut(auth);
    } catch (error) {
        console.error("Error signing out: ", error);
    }
});

// --- USER SETTINGS (API KEY) ---

btnSaveSettings?.addEventListener('click', async () => {
    const apiKey = (settingsApiKeyInput?.value || '').trim();
    if (!apiKey) {
        showSettingsStatus('Paste your Groq API key first.', 'error');
        return;
    }

    const user = auth && auth.currentUser;
    if (!user) {
        // Signed out there is nowhere to store it, so it stays for this browser session and
        // the card says so.
        window.sessionGroqApiKey = apiKey;
        showSettingsStatus('Held for this browser session only. Sign in to keep it on your account.', 'warn');
        return;
    }

    btnSaveSettings.disabled = true;
    btnSaveSettings.innerText = 'Saving…';
    try {
        await setDoc(doc(db, 'users', user.uid), {
            [JUDGE_KEY_FIELD]: apiKey,
            judgeApiKeyUpdatedAt: new Date().toISOString()
        }, { merge: true });
        window.sessionGroqApiKey = apiKey;
        showSettingsStatus(`Saved to ${user.email || 'your account'}. It fills in automatically next time.`, 'ok');
    } catch (error) {
        showSettingsStatus(`Could not save your key: ${error.message}`, 'error');
    } finally {
        btnSaveSettings.disabled = false;
        btnSaveSettings.innerText = 'Save Settings';
    }
});

btnForgetKey?.addEventListener('click', async () => {
    window.sessionGroqApiKey = null;
    if (settingsApiKeyInput) settingsApiKeyInput.value = '';
    const user = auth && auth.currentUser;
    if (!user) {
        showSettingsStatus('Cleared for this browser session.', 'ok');
        return;
    }
    try {
        await updateDoc(doc(db, 'users', user.uid), {
            [JUDGE_KEY_FIELD]: deleteField(),
            [LEGACY_JUDGE_KEY_FIELD]: deleteField()
        });
        showSettingsStatus('Saved key removed from your account.', 'ok');
    } catch (error) {
        showSettingsStatus(`Could not remove the saved key: ${error.message}`, 'error');
    }
});

// --- GLOBAL EXPORTS FOR OTHER SCRIPTS ---

window.getFirebaseIdToken = async () => auth && auth.currentUser ? auth.currentUser.getIdToken() : null;
window.getFirebaseUser = () => (auth ? auth.currentUser : null);

// Buzzpoint Board members sign in anonymously so a teammate can play without an account.
// If Anonymous sign-in is off in the Firebase console, say so plainly rather than fail
// silently or fall back to a fake identity.
window.signInAnonymouslyForBoard = async () => {
    if (!auth) throw new Error(CONFIG_HELP);
    try {
        const credential = await signInAnonymously(auth);
        return credential.user;
    } catch (err) {
        const code = err && err.code;
        if (code === 'auth/operation-not-allowed' || code === 'auth/admin-restricted-operation') {
            throw new Error('Anonymous sign-in is not enabled (Firebase console -> Authentication -> Sign-in method).');
        }
        throw err;
    }
};

window.joinOnlineRoomApi = async (roomId, name, team, captain) => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("Sign in before joining an online match.");
    const token = await user.getIdToken();
    const response = await fetch(`/api/rooms/${encodeURIComponent(roomId)}/join`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
        body: JSON.stringify({ name, team, captain: Boolean(captain) })
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Unable to join online match.");
    return result;
};

window.createOnlineRoomApi = async (name, team, captain) => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("Sign in before creating an online match.");
    const token = await user.getIdToken();
    // The server allocates the room code, so a match can never collide with a taken one.
    const response = await fetch("/api/rooms", {
        method: "POST",
        headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token}` },
        body: JSON.stringify({ name, team, captain: Boolean(captain) })
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Unable to create online match.");
    return result;
};

window.assignRoomSeat = async (roomId, uid, seat) => {
    const user = auth && auth.currentUser;
    if (!user || user.uid !== uid || !["A-1", "A-C", "A-3", "A-4", "A-5", "B-1", "B-C", "B-3", "B-4", "B-5"].includes(seat)) {
        throw new Error("Invalid seat assignment");
    }
    const roomSnapshot = await getDoc(doc(db, "rooms", roomId));
    if (!roomSnapshot.exists() || roomSnapshot.data().ownerUid !== user.uid) {
        throw new Error("Only the room moderator may assign seats");
    }
    await setDoc(doc(db, "rooms", roomId, "participants", uid), { seat });
};

window.ensureRoomOwnership = async (roomId, uid) => {
    if (!db) throw new Error("Cloud storage is unavailable.");
    const roomRef = doc(db, "rooms", roomId);
    const roomSnapshot = await getDoc(roomRef);
    if (roomSnapshot.exists()) return roomSnapshot.data().ownerUid === uid;
    try {
        await setDoc(roomRef, { ownerUid: uid, createdAt: new Date().toISOString() });
    } catch (error) {
        const concurrentCreate = await getDoc(roomRef);
        if (!concurrentCreate.exists()) throw error;
        return concurrentCreate.data().ownerUid === uid;
    }
    return true;
};

window.savePacketToCloud = async (packetName, packetData) => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("You must be logged in to save to the cloud.");

    const packetsRef = collection(db, "users", user.uid, "packets");
    await addDoc(packetsRef, {
        name: packetName,
        data: packetData,
        createdAt: new Date().toISOString()
    });
};

window.populateCloudPackets = async () => {
    const user = auth && auth.currentUser;
    const select = document.getElementById('cloud-packet-select');
    if (!user || !select) return;

    select.innerHTML = '<option value="">Fetching...</option>';
    
    try {
        const packetsRef = collection(db, "users", user.uid, "packets");
        const querySnapshot = await getDocs(packetsRef);
        
        select.innerHTML = '<option value="">-- Select a Cloud Packet --</option>';
        window.cloudPacketsCache = {}; 
        
        querySnapshot.forEach((doc) => {
            const data = doc.data();
            window.cloudPacketsCache[doc.id] = data.data; 
            
            const option = document.createElement('option');
            option.value = doc.id;
            option.text = `${data.name} (${data.data.length} Qs)`;
            select.appendChild(option);
        });
    } catch (err) {
        select.innerHTML = '<option value="">Error loading packets</option>';
        console.error("Failed to load cloud packets:", err);
    }
};

window.saveMatchLogToCloud = async (roomId, logData) => {
    const user = auth && auth.currentUser;
    if (!user || !roomId) return;
    
    try {
        const logRef = doc(db, "users", user.uid, "match_logs", roomId);
        await setDoc(logRef, {
            roomId: roomId,
            log: logData,
            updatedAt: new Date().toISOString()
        }, { merge: true }); 
    } catch (err) {
        console.error("Failed to sync match log to cloud:", err);
    }
};

window.loadMatchLogsFromCloud = async () => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("You must be logged in to fetch cloud analytics.");
    
    const logsRef = collection(db, "users", user.uid, "match_logs");
    const snapshot = await getDocs(logsRef);
    
    const logs = [];
    snapshot.forEach(doc => logs.push(doc.data()));
    return logs;
};

// Solo drill sessions live beside the match logs, under the same owner-only rules.
// A session is written under its own id, so saving the same session twice updates it
// instead of piling up duplicates.
window.saveSoloSessionToCloud = async (session) => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("Sign in to save a drill session to your account.");
    if (!session || !session.id) throw new Error("Start a drill before saving a session.");

    const sessionRef = doc(db, "users", user.uid, "solo_sessions", session.id);
    await setDoc(sessionRef, { ...session, updatedAt: new Date().toISOString() }, { merge: true });
    return session.id;
};

window.loadSoloSessionsFromCloud = async (limit = 10) => {
    const user = auth && auth.currentUser;
    if (!user) throw new Error("Sign in to read saved drill sessions.");

    const sessionsRef = collection(db, "users", user.uid, "solo_sessions");
    const snapshot = await getDocs(sessionsRef);
    const sessions = [];
    snapshot.forEach((entry) => sessions.push(entry.data()));
    // Newest first; the collection is small enough that sorting here needs no index.
    sessions.sort((left, right) => String(right.startedAt || "").localeCompare(String(left.startedAt || "")));
    return sessions.slice(0, limit);
};
