// /static/js/auth.js

import { initializeApp } from "https://www.gstatic.com/firebasejs/12.0.0/firebase-app.js";
import { 
    getAuth, 
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

// Firebase web config is public by design; use environment-injected values at build/deploy time.
const firebaseConfig = window.FIREBASE_CONFIG;
if (!firebaseConfig || !firebaseConfig.apiKey || !firebaseConfig.projectId) {
    throw new Error("Firebase configuration is missing. Configure FIREBASE_CONFIG before loading auth.js.");
}

const app = initializeApp(firebaseConfig);
export const auth = getAuth(app);
export const db = getFirestore(app);

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

// --- CONSOLIDATED AUTHENTICATION STATE OBSERVER ---
let resolveFirebaseAuthReady;
window.firebaseAuthReady = new Promise((resolve) => { resolveFirebaseAuthReady = resolve; });

onAuthStateChanged(auth, async (user) => {
    resolveFirebaseAuthReady(user || null);
    const cloudPacketSection = document.getElementById('cloud-packet-section');
    const btnSaveCloud = document.getElementById('btn-save-cloud');
    const btnCloudAnalytics = document.getElementById('btn-cloud-analytics');

    if (user) {
        // 1. Update Core Header UI
        userSection.classList.remove('hidden');
        userSection.classList.add('flex');
        btnShowLogin.classList.add('hidden');
        userEmailDisplay.innerText = user.email;
        authModal.classList.add('hidden'); 
        
        // 2. Reveal Cloud Feature Buttons
        if (cloudPacketSection) {
            cloudPacketSection.classList.remove('hidden');
            cloudPacketSection.classList.add('flex');
        }
        if (btnSaveCloud) btnSaveCloud.classList.remove('hidden');
        if (btnCloudAnalytics) btnCloudAnalytics.classList.remove('hidden');
        
        // Remove keys written by the old implementation; new keys stay in this session only.
        settingsApiKeyInput.value = "";
        const legacyUserDoc = await getDoc(doc(db, "users", user.uid));
        if (legacyUserDoc.exists() && legacyUserDoc.data().groqApiKey) {
            await updateDoc(legacyUserDoc.ref, { groqApiKey: deleteField() });
        }
        await window.populateCloudPackets();

    } else {
        // 1. Reset Core Header UI
        userSection.classList.remove('flex');
        userSection.classList.add('hidden');
        btnShowLogin.classList.remove('hidden');
        userEmailDisplay.innerText = "";
        settingsApiKeyInput.value = "";
        window.sessionGroqApiKey = null;

        // 2. Hide Cloud Feature Buttons
        if (cloudPacketSection) {
            cloudPacketSection.classList.add('hidden');
            cloudPacketSection.classList.remove('flex');
        }
        if (btnSaveCloud) btnSaveCloud.classList.add('hidden');
        if (btnCloudAnalytics) btnCloudAnalytics.classList.add('hidden');
    }
});

// --- UI EVENT LISTENERS ---
const displayError = (msg) => {
    authError.innerText = msg;
    authError.classList.remove('hidden');
};

btnSignIn?.addEventListener('click', async () => {
    try {
        authError.classList.add('hidden');
        await signInWithEmailAndPassword(auth, emailInput.value, passwordInput.value);
    } catch (error) {
        displayError(error.message);
    }
});

btnSignUp?.addEventListener('click', async () => {
    try {
        authError.classList.add('hidden');
        await createUserWithEmailAndPassword(auth, emailInput.value, passwordInput.value);
    } catch (error) {
        displayError(error.message);
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
    const user = auth.currentUser;
    if (!user) {
        settingsStatus.innerText = "⚠️ You must be logged in to save settings.";
        settingsStatus.className = "text-xs text-red-400 bg-red-950/50 border border-red-900 p-2 rounded font-mono block";
        return;
    }

    const apiKey = settingsApiKeyInput.value.trim();
    try {
        settingsStatus.innerText = "Saving...";
        settingsStatus.className = "text-xs text-amber-400 bg-amber-950/50 border border-amber-900 p-2 rounded font-mono block";
        
        window.sessionGroqApiKey = apiKey;
        settingsStatus.innerText = "✅ API Key held for this browser session only.";
        settingsStatus.className = "text-xs text-emerald-400 bg-emerald-950/50 border border-emerald-900 p-2 rounded font-mono block";
        
        setTimeout(() => {
            settingsStatus.classList.add('hidden');
            document.getElementById('settings-modal').classList.add('hidden');
        }, 2000);
    } catch (error) {
        settingsStatus.innerText = `❌ Error: ${error.message}`;
        settingsStatus.className = "text-xs text-red-400 bg-red-950/50 border border-red-900 p-2 rounded font-mono block";
    }
});

// --- GLOBAL EXPORTS FOR OTHER SCRIPTS ---

window.getUserApiKey = async () => window.sessionGroqApiKey || null;
window.getFirebaseIdToken = async () => auth.currentUser ? auth.currentUser.getIdToken() : null;
window.getFirebaseUser = () => auth.currentUser;

window.joinOnlineRoomApi = async (roomId, name, team, captain) => {
    const user = auth.currentUser;
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
    const user = auth.currentUser;
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
    const user = auth.currentUser;
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
    const user = auth.currentUser;
    if (!user) throw new Error("You must be logged in to save to the cloud.");

    const packetsRef = collection(db, "users", user.uid, "packets");
    await addDoc(packetsRef, {
        name: packetName,
        data: packetData,
        createdAt: new Date().toISOString()
    });
};

window.populateCloudPackets = async () => {
    const user = auth.currentUser;
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
    const user = auth.currentUser;
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
    const user = auth.currentUser;
    if (!user) throw new Error("You must be logged in to fetch cloud analytics.");
    
    const logsRef = collection(db, "users", user.uid, "match_logs");
    const snapshot = await getDocs(logsRef);
    
    const logs = [];
    snapshot.forEach(doc => logs.push(doc.data()));
    return logs;
};