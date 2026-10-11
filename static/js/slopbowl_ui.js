// SLOPBOWL shell UI: slide navigation, modals, WebAudio sounds, toasts,
// role-based visibility, and the wired packet converter.

// =========================================================
// WEB AUDIO API SYNTHESIZER (interface sounds)
// =========================================================
let audioContext = null;
let audioMuted = false;

function getAudioCtx() {
  if (!audioContext) {
    const AudioClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioClass) return null;
    audioContext = new AudioClass();
  }
  return audioContext;
}

function playSound(type = 'click') {
  if (audioMuted) return;
  const ctx = getAudioCtx();
  if (!ctx) return;
  if (ctx.state === 'suspended') ctx.resume();

  const now = ctx.currentTime;

  if (type === 'buzz') {
    // Low sawtooth thud for buzzer presses
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sawtooth';
    osc.frequency.setValueAtTime(180, now);
    osc.frequency.exponentialRampToValueAtTime(60, now + 0.25);
    gain.gain.setValueAtTime(0.22, now);
    gain.gain.exponentialRampToValueAtTime(0.001, now + 0.3);
    osc.connect(gain).connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.3);
    return;
  }

  if (type === 'correct') {
    // Rising two-note chime
    [660, 990].forEach((freq, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.setValueAtTime(freq, now + i * 0.12);
      gain.gain.setValueAtTime(0.15, now + i * 0.12);
      gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.12 + 0.25);
      osc.connect(gain).connect(ctx.destination);
      osc.start(now + i * 0.12);
      osc.stop(now + i * 0.12 + 0.25);
    });
    return;
  }

  // Default: short click blip
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = 'square';
  osc.frequency.setValueAtTime(880, now);
  gain.gain.setValueAtTime(0.05, now);
  gain.gain.exponentialRampToValueAtTime(0.001, now + 0.06);
  osc.connect(gain).connect(ctx.destination);
  osc.start(now);
  osc.stop(now + 0.06);
}

function toggleAudioMute() {
  audioMuted = !audioMuted;
  const btn = document.getElementById('soundMuteBtn');
  if (btn) btn.textContent = audioMuted ? 'OFF' : 'ON';
}

// =========================================================
// SLIDE NAVIGATION
// =========================================================
function navigateTo(slideNum) {
  // Stop voice when switching views
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (typeof clearInterval === 'function') {
    if (window._slopbowlReadInterval) clearInterval(window._slopbowlReadInterval);
  }

  document.querySelectorAll('.slide-view').forEach(s => s.classList.add('hidden'));
  const target = document.getElementById(`slide-${slideNum}`);
  if (target) target.classList.remove('hidden');

  // Update nav dock buttons
  document.querySelectorAll('.nav-dock-btn').forEach(btn => {
    btn.classList.remove('bg-neonCyan', 'text-black', 'shadow-neon-cyan');
    btn.classList.add('bg-panel', 'text-slate-300');
  });
  const activeBtn = document.getElementById(`nav-btn-${slideNum}`);
  if (activeBtn) {
    activeBtn.classList.remove('bg-panel', 'text-slate-300');
    activeBtn.classList.add('bg-neonCyan', 'text-black', 'shadow-neon-cyan');
  }

  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// =========================================================
// MODALS / TOASTS
// =========================================================
function openModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.remove('hidden');
  el.classList.add('flex');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.classList.add('hidden');
  el.classList.remove('flex');
}

function openWipModal() {
  openModal('wipModal');
}

let toastTimer = null;
function showToast(msg) {
  const toast = document.getElementById('toastAlertBanner');
  const text = document.getElementById('toastAlertText');
  if (!toast || !text) return;
  text.innerText = msg;
  toast.classList.remove('hidden');
  toast.classList.add('flex');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toast.classList.add('hidden');
    toast.classList.remove('flex');
  }, 4000);
}

// =========================================================
// ROLE-BASED VISIBILITY
// =========================================================
// Roles: "owner" (moderator), "player", or null (browsing).
function applyRoleVisibility(role) {
  const isPlayer = role === 'player';

  // Players get a clean view: moderator tooling is hidden for them.
  // Browsers and moderators keep the full navigation.
  document.querySelectorAll('.moderator-nav-control').forEach(el => {
    el.classList.toggle('hidden', isPlayer);
  });
  document.querySelectorAll('.player-nav-control').forEach(el => {
    el.classList.toggle('hidden', !isPlayer);
  });
}

// =========================================================
// WIRED PACKET CONVERTER (uses the real parseScienceBowlText engine)
// =========================================================
let convertedBankData = null;

function parseRawPacket() {
  const input = document.getElementById('converterRawInput');
  const output = document.getElementById('converterOutput');
  if (!input || !output) return;

  const raw = input.value.trim();
  if (!raw) {
    output.textContent = '// Paste raw packet text first.';
    output.className = output.className.replace('text-neonGreen', 'text-neonAmber');
    return;
  }

  try {
    const parsed = parseScienceBowlText(raw);
    if (!parsed || !parsed.length) throw new Error('No questions detected in the input');
    convertedBankData = parsed;
    output.textContent = JSON.stringify(parsed, null, 2);
    output.className = output.className.replace('text-neonAmber', 'text-neonGreen');
    showToast(`Parsed ${parsed.length} question set(s)`);
  } catch (err) {
    convertedBankData = null;
    output.textContent = '// Parse error: ' + (err.message || 'invalid packet text');
    output.className = output.className.replace('text-neonGreen', 'text-neonAmber');
  }
}

function loadConvertedToMatch() {
  if (!convertedBankData || !convertedBankData.length) {
    showToast('Parse a packet first');
    return;
  }
  sendEvent('LOAD_BANK', { bank: convertedBankData });
  closeModal('converterModal');
  showToast(`Loaded ${convertedBankData.length} question set(s) into the match`);
}

// Close modals with Escape
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    ['auth-modal', 'converterModal', 'analyticsModal', 'wipModal'].forEach(closeModal);
  }
});

// Default view on load
window.addEventListener('DOMContentLoaded', () => {
  navigateTo(1);
});
