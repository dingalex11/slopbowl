// SLOPBOWL SOLO PRACTICE DRILL ENGINE
// Single-player buzz training that runs entirely in this page: no room, no sign-in and
// no server round trip is required, so practice is always available. It reuses the app's
// converter (parseScienceBowlText) for packet loading and the shell's sounds/toasts.
//
// The design separates stimulus delivery (spoken / typed / hybrid) from response capture
// (typed terminal / microphone), because either can be trained independently.

// =========================================================
// OPTIONS
// =========================================================
const SOLO_STORAGE_KEY = 'slopbowl.solo.options';
const SOLO_TOSS_UP_POINTS = 4;
const SOLO_LOCKOUT_SECONDS = 5.0;
// The drill's six design subjects. Packet categories are folded onto them so a packet's
// own labels ("Life Science") still land in a drill subject ("Biology").
const SOLO_SUBJECT_ALIASES = {
  'life science': 'Biology',
  'biology': 'Biology',
  'physical science': 'Physics',
  'physics': 'Physics',
  'chemistry': 'Chemistry',
  'earth science': 'Earth and Space',
  'earth and space': 'Earth and Space',
  'math': 'Math',
  'mathematics': 'Math',
  'energy': 'Energy',
  'general science': 'General Science',
};
const SOLO_SUBJECT_COLORS = {
  'Biology': '#00ff66',
  'Chemistry': '#ffaa00',
  'Physics': '#00f3ff',
  'Earth and Space': '#2dd4bf',
  'Math': '#b026ff',
  'Energy': '#ff0055',
  'General Science': '#94a3b8',
};
// Spoken progress is drawn from a realistic pace and corrected by the engine's own state,
// because a guessed duration must never be treated as the read being over.
const SOLO_SPOKEN_CHARS_PER_SECOND = 12.0;

const SOLO_DEFAULT_OPTIONS = {
  output: 'hybrid',
  input: 'typed',
  voice: '',
  rate: 1.1,
  wpm: 260,
  subjects: {},
  packets: null,  // null = every loaded packet
  mutedPackets: [],
};

const SOLO_SESSION_STORAGE_KEY = 'slopbowl.solo.sessions';
const SOLO_SESSION_KEEP = 20;
// Firestore documents are capped at 1 MiB; a session's per-question detail is trimmed.
const SOLO_SESSION_MAX_QUESTIONS = 200;

let soloOptions = null;
let soloMicChecked = false;
let soloPackets = [];
let soloSampleRequested = false;
let soloDrill = null;
let soloSession = null;

function soloLoadOptions() {
  const options = JSON.parse(JSON.stringify(SOLO_DEFAULT_OPTIONS));
  try {
    const stored = window.localStorage ? JSON.parse(window.localStorage.getItem(SOLO_STORAGE_KEY) || 'null') : null;
    if (stored && typeof stored === 'object') {
      if (['spoken', 'typed', 'hybrid'].includes(stored.output)) options.output = stored.output;
      if (['typed', 'audio'].includes(stored.input)) options.input = stored.input;
      if (typeof stored.voice === 'string') options.voice = stored.voice;
      if (Number.isFinite(stored.rate)) options.rate = Math.min(1.6, Math.max(0.6, stored.rate));
      if (Number.isFinite(stored.wpm)) options.wpm = Math.min(1200, Math.max(120, stored.wpm));
      if (stored.subjects && typeof stored.subjects === 'object') options.subjects = stored.subjects;
      if (Array.isArray(stored.packets)) options.packets = stored.packets;
      if (Array.isArray(stored.mutedPackets)) options.mutedPackets = stored.mutedPackets;
    }
  } catch (error) {
    console.warn('Solo practice options could not be read; using defaults.', error);
  }
  return options;
}

function soloSaveOptions() {
  try {
    if (window.localStorage) window.localStorage.setItem(SOLO_STORAGE_KEY, JSON.stringify(soloOptions));
  } catch (error) {
    console.warn('Solo practice options could not be stored.', error);
  }
}

function soloEnsureOptions() {
  if (!soloOptions) soloOptions = soloLoadOptions();
  return soloOptions;
}

// =========================================================
// PACKETS AND SUBJECTS
// =========================================================
function soloSubjectFor(category) {
  const raw = String(category || '').trim();
  if (!raw) return 'General Science';
  const alias = SOLO_SUBJECT_ALIASES[raw.toLowerCase()];
  return alias || raw;
}

function soloSubjectColor(subject) {
  return SOLO_SUBJECT_COLORS[subject] || '#94a3b8';
}

// Packets arrive from the solo loader, from a packet already loaded for a match, or from
// the bundled sample. Each keeps its own identity so packet selection can be remembered.
function soloAddPacket(name, questions, { announce = true } = {}) {
  if (!Array.isArray(questions) || !questions.length) {
    if (announce) showToast('That packet has no questions');
    return null;
  }
  const usable = questions.filter((question) => question && typeof question === 'object' && String(question.tossup_text || '').trim());
  if (!usable.length) {
    if (announce) showToast('That packet has no readable toss-ups');
    return null;
  }
  const id = `packet-${soloPackets.length + 1}`;
  const packet = { id, name: String(name || `Packet ${soloPackets.length + 1}`), questions: usable };
  const duplicate = soloPackets.find((entry) => entry.name === packet.name && entry.questions.length === packet.questions.length);
  if (duplicate) {
    duplicate.questions = usable;
    soloRenderPacketList();
    soloRenderSubjects();
    if (announce) showToast(`Refreshed ${packet.name} (${usable.length} toss-ups)`);
    return duplicate;
  }
  soloPackets.push(packet);
  if (soloOptions) {
    soloOptions.mutedPackets = soloOptions.mutedPackets.filter((entry) => entry !== id);
    soloSaveOptions();
  }
  soloRenderPacketList();
  soloRenderSubjects();
  if (announce) showToast(`Loaded ${packet.name} — ${usable.length} toss-ups`);
  return packet;
}

function soloSelectedQuestions() {
  soloEnsureOptions();
  const usable = soloPackets.filter((packet) => !soloOptions.mutedPackets.includes(packet.id));
  if (!usable.length) return [];  // Every packet muted means no drill, not every packet.
  return usable.flatMap((packet) => packet.questions.map((question) => ({ question, packet: packet.name })));
}

function soloSubjectUniverse() {
  const counts = new Map();
  soloSelectedQuestions().forEach(({ question }) => {
    const subject = soloSubjectFor(question.category);
    counts.set(subject, (counts.get(subject) || 0) + 1);
  });
  return counts;
}

function soloSubjectWeights() {
  soloEnsureOptions();
  const counts = soloSubjectUniverse();
  const weights = new Map();
  let total = 0;
  counts.forEach((_count, subject) => {
    const raw = Number(soloOptions.subjects[subject]);
    const value = Number.isFinite(raw) && raw > 0 ? raw : 0;
    weights.set(subject, value);
    total += value;
  });
  if (total < 100) {
    // A subject the user never touched starts even with the rest, so a new packet's
    // subjects are always drillable without visiting the setup screen.
    const untuned = [...weights.keys()].filter((subject) => !soloOptions.subjects[subject]);
    const share = (100 - total) / (untuned.length || 1);
    untuned.forEach((subject) => weights.set(subject, share));
  }
  return weights;
}

function soloEvenSplit() {
  const subjects = [...soloSubjectUniverse().keys()];
  const split = {};
  if (!subjects.length) return split;
  const base = Math.floor(100 / subjects.length);
  subjects.forEach((subject) => { split[subject] = base; });
  let remainder = 100 - base * subjects.length;
  for (let index = 0; index < remainder; index += 1) split[subjects[index]] += 1;
  return split;
}

function soloResetSubjects() {
  soloEnsureOptions();
  soloOptions.subjects = soloEvenSplit();
  soloSaveOptions();
  soloRenderSubjects();
  playSound('click');
}

function soloNormalizeSubjects() {
  soloEnsureOptions();
  const weights = soloSubjectWeights();
  const total = [...weights.values()].reduce((sum, value) => sum + value, 0);
  if (!total) {
    soloOptions.subjects = soloEvenSplit();
    return;
  }
  const normalized = {};
  weights.forEach((value, subject) => { normalized[subject] = Math.round((value / total) * 100); });
  const drift = 100 - Object.values(normalized).reduce((sum, value) => sum + value, 0);
  const first = Object.keys(normalized)[0];
  if (first) normalized[first] += drift;
  soloOptions.subjects = normalized;
}

// =========================================================
// SETUP SCREEN
// =========================================================
// Selection state is the accent colour of the section: cyan for output, amber for input.
function soloMarkCards(selector, chosen, accent) {
  document.querySelectorAll(selector).forEach((card) => {
    const value = card.dataset.soloOutput || card.dataset.soloInput;
    const dot = card.querySelector('[data-solo-dot]');
    const isActive = value === chosen;
    const border = accent === 'cyan' ? 'border-neonCyan' : 'border-neonAmber';
    const shadow = accent === 'cyan' ? 'shadow-neon-cyan' : 'shadow-neon-amber';
    const fill = accent === 'cyan' ? 'bg-neonCyan' : 'bg-neonAmber';
    card.classList.toggle(border, isActive);
    card.classList.toggle(shadow, isActive);
    card.classList.toggle('border-borderDark', !isActive);
    card.classList.toggle('bg-panelHover', isActive);
    if (dot) {
      dot.classList.toggle(fill, isActive);
      dot.classList.toggle(border, isActive);
      dot.classList.toggle('border-slate-600', !isActive);
    }
    if (isActive) card.setAttribute('aria-pressed', 'true'); else card.removeAttribute('aria-pressed');
  });
}

function soloRenderSetup() {
  soloEnsureOptions();
  soloMarkCards('.solo-output-card', soloOptions.output, 'cyan');
  soloMarkCards('.solo-input-card', soloOptions.input, 'amber');

  const rate = document.getElementById('solo-rate-slider');
  const rateValue = document.getElementById('solo-rate-value');
  if (rate) rate.value = String(soloOptions.rate);
  if (rateValue) rateValue.innerText = `${soloOptions.rate.toFixed(2).replace(/0$/, '')}X`;

  const wpm = document.getElementById('solo-wpm-slider');
  const wpmValue = document.getElementById('solo-wpm-value');
  if (wpm) wpm.value = String(soloOptions.wpm);
  if (wpmValue) wpmValue.innerText = `${soloOptions.wpm} WPM`;

  soloRenderVoiceOptions();
  soloRenderSubjects();
  soloRenderPacketList();
  soloRenderMicStatus();
}

// The microphone card must state its availability before anything is clicked: a trainee
// who picks audio input and finds out later that the browser cannot transcribe has
// already lost a drill.
function soloRenderMicStatus() {
  if (soloMicChecked) return;
  const status = soloElement('solo-mic-status');
  if (!status) return;
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition) {
    status.innerText = 'Speech recognition unavailable — use typed input';
    status.className = 'text-[10px] font-mono font-bold text-neonAmber';
    return;
  }
  status.innerText = 'Speech Recognition Ready';
  status.className = 'text-[10px] font-mono font-bold text-neonGreen';
}

function soloSpeechAvailable() {
  return !!(window.speechSynthesis && window.SpeechSynthesisUtterance);
}

function soloPopulateVoices() {
  const select = document.getElementById('solo-voice-select');
  if (!select || !soloSpeechAvailable()) return;
  const voices = window.speechSynthesis.getVoices() || [];
  const previous = soloEnsureOptions().voice;
  select.replaceChildren();
  const auto = document.createElement('option');
  auto.value = '';
  auto.textContent = 'System Default (English)';
  select.appendChild(auto);
  voices.forEach((voice) => {
    const option = document.createElement('option');
    option.value = voice.name;
    option.textContent = `${voice.name} (${voice.lang})`;
    select.appendChild(option);
  });
  select.value = voices.some((voice) => voice.name === previous) ? previous : '';
}

function soloRenderVoiceOptions() {
  if (!soloSpeechAvailable()) {
    const status = document.getElementById('solo-test-voice');
    if (status) {
      status.disabled = true;
      status.innerText = 'No speech engine';
    }
    return;
  }
  soloPopulateVoices();
}

function soloRenderSubjects() {
  const holder = document.getElementById('solo-subject-list');
  const empty = document.getElementById('solo-subject-empty');
  if (!holder) return;
  const counts = soloSubjectUniverse();
  holder.replaceChildren();
  if (empty) empty.classList.toggle('hidden', counts.size > 0);
  if (!counts.size) {
    soloUpdateSubjectTotal();
    return;
  }
  const weights = soloSubjectWeights();
  const alphabet = /^[a-z]/;
  counts.forEach((count, subject) => {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-3';
    row.dataset.subject = subject;

    const label = document.createElement('span');
    label.className = 'w-36 shrink-0 text-[11px] font-mono font-bold';
    label.style.color = soloSubjectColor(subject);
    label.style.letterSpacing = alphabet.test(subject) ? '0.02em' : '0';
    label.textContent = subject;
    row.appendChild(label);

    const countBadge = document.createElement('span');
    countBadge.className = 'w-10 shrink-0 text-[10px] font-mono text-slate-500';
    countBadge.textContent = `${count} q`;
    row.appendChild(countBadge);

    const slider = document.createElement('input');
    slider.type = 'range';
    slider.min = '0';
    slider.max = '100';
    slider.step = '1';
    slider.value = String(Math.round(weights.get(subject) || 0));
    slider.className = 'solo-range flex-1';
    row.appendChild(slider);

    const number = document.createElement('input');
    number.type = 'number';
    number.min = '0';
    number.max = '100';
    number.value = slider.value;
    number.className = 'w-16 bg-black border border-borderDark rounded-lg px-2 py-1 text-xs text-slate-200 font-mono text-right focus:outline-none focus:border-neonCyan';
    row.appendChild(number);

    const suffix = document.createElement('span');
    suffix.className = 'text-[10px] text-slate-500 font-mono';
    suffix.textContent = '%';
    row.appendChild(suffix);

    const apply = (value) => {
      const next = Math.max(0, Math.min(100, Math.round(Number(value) || 0)));
      slider.value = String(next);
      number.value = String(next);
      soloEnsureOptions().subjects[subject] = next;
      soloSaveOptions();
      soloUpdateSubjectTotal();
    };
    slider.addEventListener('input', () => apply(slider.value));
    number.addEventListener('change', () => apply(number.value));
    holder.appendChild(row);
  });
  soloUpdateSubjectTotal();
}

function soloUpdateSubjectTotal() {
  const badge = document.getElementById('solo-subject-total');
  if (!badge) return;
  const weights = soloSubjectWeights();
  const total = Math.round([...weights.values()].reduce((sum, value) => sum + value, 0));
  badge.innerText = `TOTAL ${total}%`;
  const complete = Math.abs(total - 100) < 1;
  badge.className = complete ? 'text-[10px] font-mono font-bold text-neonGreen' : 'text-[10px] font-mono font-bold text-neonAmber';
}

function soloRenderPacketList() {
  const list = document.getElementById('solo-packet-list');
  const label = document.getElementById('solo-packet-label');
  soloEnsureOptions();
  const active = soloPackets.filter((packet) => !soloOptions.mutedPackets.includes(packet.id));
  if (label) {
    label.innerText = !soloPackets.length
      ? 'No packets loaded'
      : (active.length === soloPackets.length ? 'All packets (random)' : `${active.length} of ${soloPackets.length} packets`);
  }
  if (!list) return;
  list.replaceChildren();
  if (!soloPackets.length) {
    const note = document.createElement('p');
    note.className = 'text-[11px] text-slate-500 font-mono';
    note.textContent = 'Add a .json or .txt packet to drill from your own question sets.';
    list.appendChild(note);
    return;
  }
  soloPackets.forEach((packet) => {
    const row = document.createElement('label');
    row.className = 'flex items-center gap-2 text-[11px] font-mono text-slate-300 cursor-pointer';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = !soloOptions.mutedPackets.includes(packet.id);
    box.className = 'accent-neonCyan';
    box.addEventListener('change', () => {
      const muted = new Set(soloOptions.mutedPackets);
      if (box.checked) muted.delete(packet.id); else muted.add(packet.id);
      soloOptions.mutedPackets = [...muted];
      soloSaveOptions();
      soloRenderPacketList();
      soloRenderSubjects();
    });
    const text = document.createElement('span');
    text.textContent = `${packet.name} · ${packet.questions.length} toss-ups`;
    row.append(box, text);
    list.appendChild(row);
  });
}

async function soloLoadSamplePacket() {
  try {
    const response = await fetch('/api/sample-bank');
    if (!response.ok) return;
    const bank = await response.json();
    if (Array.isArray(bank) && bank.length) soloAddPacket('Sample packet', bank, { announce: false });
  } catch (error) {
    console.warn('Sample packet unavailable; add your own packet to drill.', error);
  }
}

function openSoloSetup() {
  soloEnsureOptions();
  navigateTo(11);
  soloRenderSetup();
  soloRefreshSaveHint();
  if (!soloPackets.length && !soloSampleRequested) {
    soloSampleRequested = true;
    soloLoadSamplePacket().then(soloRenderSetup);
  }
}

// Packets loaded for a match are reusable for practice without another upload.
function soloRegisterPacketFromApp(name, questions) {
  soloEnsureOptions();
  return soloAddPacket(name, questions, { announce: false });
}

// =========================================================
// DRILL: SUBJECT-WEIGHTED QUESTION ORDER
// =========================================================
function soloDrawSubject(weights) {
  const entries = [...weights.entries()].filter(([, value]) => value > 0);
  if (!entries.length) return null;
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  let roll = Math.random() * total;
  for (const [subject, value] of entries) {
    roll -= value;
    if (roll <= 0) return subject;
  }
  return entries[entries.length - 1][0];
}

function soloBuildQueue(size = 12) {
  const pool = soloSelectedQuestions();
  if (!pool.length) return [];
  const weights = soloSubjectWeights();
  const bySubject = new Map();
  pool.forEach((entry) => {
    const subject = soloSubjectFor(entry.question.category);
    if (!bySubject.has(subject)) bySubject.set(subject, []);
    bySubject.get(subject).push(entry);
  });
  const queue = [];
  let lastKey = null;
  for (let index = 0; index < size; index += 1) {
    let candidates = bySubject.get(soloDrawSubject(weights)) || [];
    if (!candidates.length) candidates = pool;
    const available = candidates.filter((entry) => `${entry.packet}|${entry.question.tossup_text}` !== lastKey);
    const pick = (available.length ? available : candidates)[Math.floor(Math.random() * (available.length ? available.length : candidates.length))];
    queue.push(pick);
    lastKey = `${pick.packet}|${pick.question.tossup_text}`;
  }
  return queue;
}

function soloQuestionWords(text) {
  return String(text || '').split(/\s+/).filter(Boolean);
}

function soloQuestionLabel(question) {
  const type = String(question.type || 'SA').toUpperCase();
  return type === 'MC' ? 'MULTIPLE CHOICE' : 'SHORT ANSWER';
}

// =========================================================
// DRILL: RUNTIME
// =========================================================
function soloDrillState() {
  return soloDrill;
}

function soloElement(id) {
  return document.getElementById(id);
}

function soloClearTimers() {
  if (!soloDrill) return;
  soloDrill.timers.forEach((timer) => clearTimeout(timer));
  soloDrill.timers = [];
  soloDrill.intervals.forEach((timer) => clearInterval(timer));
  soloDrill.intervals = [];
  if (soloDrill.wordTimer) {
    clearInterval(soloDrill.wordTimer);
    soloDrill.wordTimer = null;
  }
  if (soloDrill.speechPoll) {
    clearInterval(soloDrill.speechPoll);
    soloDrill.speechPoll = null;
  }
  if (soloDrill.lockoutTimer) {
    clearInterval(soloDrill.lockoutTimer);
    soloDrill.lockoutTimer = null;
  }
}

function soloStopSpeech() {
  if (!soloSpeechAvailable()) return;
  try { window.speechSynthesis.cancel(); } catch (error) { /* engine already idle */ }
}

// An abandoned recognition session keeps the microphone open and can submit a second
// answer, so every path that ends a question closes it.
function soloStopRecognition() {
  if (!soloDrill || !soloDrill.recognition) return;
  try { soloDrill.recognition.abort(); } catch (error) { /* already stopped */ }
  soloDrill.recognition = null;
}

function soloStopDelivery() {
  if (!soloDrill) return;
  soloClearTimers();
  soloStopSpeech();
  soloStopRecognition();
  soloDrill.delivering = false;
  soloDrill.windowOpen = false;
}

// Leaving the drill slides must never leave speech or timers running.
function soloLeavePractice(slideNum) {
  if (slideNum === 11 || slideNum === 12) return;
  if (!soloDrill) return;
  soloStopDelivery();
}

function startSoloDrill() {
  soloEnsureOptions();
  soloNormalizeSubjects();
  soloSaveOptions();
  const queue = soloBuildQueue();
  if (!queue.length) {
    showToast('Load a question packet before starting a drill');
    return;
  }
  soloSession = {
    id: `solo-${new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14)}-${Math.random().toString(36).slice(2, 7)}`,
    startedAt: new Date().toISOString(),
    questions: [],
    streakBest: 0,
  };
  soloDrill = {
    queue,
    index: 0,
    entryIndex: null,
    score: 0,
    streak: 0,
    bestReaction: null,
    lastReaction: null,
    answered: false,
    delivering: false,
    windowOpen: false,
    streamDone: false,
    speechDone: false,
    wordsShown: 0,
    totalWords: 0,
    startedAt: 0,
    timers: [],
    intervals: [],
    wordTimer: null,
    speechPoll: null,
    lockoutTimer: null,
    recognition: null,
  };
  navigateTo(12);
  soloRenderStats();
  soloRenderQuestion({ ready: true });
  soloRenderTelemetry();
  playSound('click');
}

function soloRenderStats() {
  if (!soloDrill) return;
  const score = soloElement('solo-score');
  const streak = soloElement('solo-streak');
  const reaction = soloElement('solo-reaction');
  if (score) score.innerText = String(soloDrill.score);
  if (streak) streak.innerText = String(soloDrill.streak);
  if (reaction) reaction.innerText = soloDrill.lastReaction == null ? '-- S' : `${soloDrill.lastReaction.toFixed(2)} S`;
  const badge = soloElement('solo-scope-badge');
  const weights = soloSubjectWeights();
  const active = [...weights.entries()].filter(([, value]) => value > 0).map(([subject]) => subject);
  if (badge) badge.innerText = active.length === 1 ? active[0].toUpperCase() : 'ALL';
  const line = soloElement('solo-mode-line');
  if (line) {
    line.textContent = `OUTPUT: ${soloOptions.output.toUpperCase()} \u2022 INPUT: ${soloOptions.input.toUpperCase()} \u2022 ${soloOptions.wpm} WPM`;
  }
}

function soloRenderQuestion({ ready = false } = {}) {
  if (!soloDrill) return;
  const entry = soloDrill.queue[soloDrill.index];
  if (!entry) return;
  const question = entry.question;
  const label = soloElement('solo-q-label');
  if (label) label.innerText = `Toss-up #${soloDrill.index + 1} (${soloQuestionLabel(question)})`;
  const text = soloElement('solo-question-text');
  const words = soloQuestionWords(question.tossup_text);
  soloDrill.totalWords = words.length;
  soloDrill.wordsShown = 0;
  const counter = soloElement('solo-word-count');
  if (counter) counter.innerText = `Word 0 of ${words.length}`;
  const lockout = soloElement('solo-lockout');
  if (lockout) lockout.innerText = `Lockout: ${SOLO_LOCKOUT_SECONDS.toFixed(1)}s`;
  const bar = soloElement('solo-progress-bar');
  if (bar) bar.style.width = '0%';
  const status = soloElement('solo-drill-status');
  const feedback = soloElement('solo-feedback');
  if (feedback) { feedback.className = 'hidden mt-4 text-sm font-mono font-bold'; feedback.innerText = ''; }
  const answerPanel = soloElement('solo-answer-panel');
  if (answerPanel) answerPanel.classList.add('hidden');
  const answerInput = soloElement('solo-answer-input');
  if (answerInput) answerInput.value = '';
  const buzz = soloElement('solo-buzz-btn');
  if (buzz) buzz.disabled = true;
  if (status) status.innerText = ready ? 'Standby' : 'Delivery complete';
  if (text) {
    text.className = 'min-h-[132px] bg-black border border-borderDark rounded-xl p-5 text-lg md:text-xl text-slate-200 font-sans leading-relaxed';
    text.textContent = ready ? 'Ready. Hit "START QUESTION DRILL" to launch delivery.' : words.join(' ');
  }
}

function soloStartQuestion() {
  if (!soloDrill) return;
  if (soloDrill.delivering) return;
  const entry = soloDrill.queue[soloDrill.index];
  if (!entry) return;
  soloStopDelivery();
  const question = entry.question;
  soloRecordAsked(question);
  soloRenderTelemetry();
  const words = soloQuestionWords(question.tossup_text);
  soloDrill.totalWords = words.length;
  soloDrill.wordsShown = 0;
  soloDrill.answered = false;
  soloDrill.windowOpen = true;
  soloDrill.delivering = true;
  soloDrill.startedAt = performance.now();
  soloDrill.lastReaction = null;
  soloRecordAsked(question);

  const text = soloElement('solo-question-text');
  const counter = soloElement('solo-word-count');
  const status = soloElement('solo-drill-status');
  const bar = soloElement('solo-progress-bar');
  const buzz = soloElement('solo-buzz-btn');
  const feedback = soloElement('solo-feedback');
  const answerPanel = soloElement('solo-answer-panel');
  if (feedback) { feedback.className = 'hidden mt-4 text-sm font-mono font-bold'; feedback.innerText = ''; }
  if (answerPanel) answerPanel.classList.add('hidden');
  if (bar) bar.style.width = '0%';
  if (counter) counter.innerText = `Word 0 of ${words.length}`;
  if (buzz) buzz.disabled = false;
  if (status) {
    status.innerText = soloOptions.output === 'spoken'
      ? 'Delivering: spoken only'
      : (soloOptions.output === 'typed' ? 'Delivering: typed stream' : 'Delivering: hybrid voice + stream');
  }
  if (text) {
    if (soloOptions.output === 'spoken') {
      // Spoken delivery is audio-only by design: the question is never written to the page.
      text.className = 'min-h-[132px] bg-black border border-neonCyan/40 rounded-xl p-5 flex flex-col items-center justify-center text-center';
      const speaker = document.createElement('span');
      speaker.className = 'text-4xl';
      speaker.textContent = '\uD83D\uDD08';
      const caption = document.createElement('span');
      caption.className = 'text-[11px] text-neonCyan font-mono font-bold tracking-widest mt-3 uppercase';
      caption.textContent = 'Audio-only delivery — no visible text';
      text.replaceChildren(speaker, caption);
    } else {
      text.className = 'min-h-[132px] bg-black border border-borderDark rounded-xl p-5 text-lg md:text-xl text-slate-200 font-sans leading-relaxed';
      text.textContent = '';
    }
  }

  // A hybrid read is over only when both channels are: the spoken question and the
  // teleprompter run at their own paces, and the buzz window must not open before the
  // last of them has finished.
  const streamsText = soloOptions.output !== 'spoken';
  const speaksText = soloOptions.output !== 'typed';
  soloDrill.streamDone = !streamsText;
  soloDrill.speechDone = !speaksText;
  if (streamsText) soloStartWordStream(words);
  if (soloOptions.output === 'spoken') {
    // Progress for a spoken-only read follows the estimate until the engine confirms.
    soloAnimateSpokenProgress(question.tossup_text);
  }
  if (speaksText) {
    soloSpeak(question.tossup_text, (spoke) => {
      if (!soloDrill || !soloDrill.delivering) return;
      if (!spoke && status) {
        status.innerText = 'Speech engine did not start — check the voice, or use typed delivery.';
      }
      if (!streamsText) soloDrill.wordsShown = words.length;
      soloDrill.speechDone = true;
      soloFinishDelivery();
    });
  }
}

// Packet text is untrusted content: it is streamed as text nodes, never as markup.
function soloStartWordStream(words) {
  const msPerWord = Math.max(50, Math.round(60000 / Math.max(120, soloOptions.wpm)));
  const text = soloElement('solo-question-text');
  const counter = soloElement('solo-word-count');
  const bar = soloElement('solo-progress-bar');
  let index = 0;
  if (text) text.replaceChildren();
  const caret = document.createElement('span');
  caret.className = 'text-neonCyan animate-pulse';
  caret.textContent = '\u258c';
  soloDrill.wordTimer = setInterval(() => {
    if (!soloDrill || !soloDrill.delivering) {
      clearInterval(soloDrill.wordTimer);
      soloDrill.wordTimer = null;
      return;
    }
    index += 1;
    soloDrill.wordsShown = index;
    if (text && caret.parentNode === text) {
      const shown = document.createElement('span');
      shown.textContent = `${words[index - 1]} `;
      text.insertBefore(shown, caret);
    }
    if (counter) counter.innerText = `Word ${index} of ${words.length}`;
    // In hybrid delivery the spoken read outlasts the stream, so the bar only completes
    // when the read itself is over.
    if (bar) {
      const share = Math.round((index / words.length) * 100);
      bar.style.width = `${soloOptions.output === 'hybrid' ? Math.min(99, share) : share}%`;
    }
    if (index >= words.length) {
      clearInterval(soloDrill.wordTimer);
      soloDrill.wordTimer = null;
      if (text && caret.parentNode === text) caret.remove();
      if (counter) counter.innerText = `Word ${words.length} of ${words.length}`;
      soloDrill.streamDone = true;
      soloFinishDelivery();
    }
  }, msPerWord);
  if (text) text.appendChild(caret);
}

function soloAnimateSpokenProgress(source) {
  const bar = soloElement('solo-progress-bar');
  if (!bar) return;
  const estimate = Math.max(1.2, source.length / (SOLO_SPOKEN_CHARS_PER_SECOND * soloOptions.rate));
  const begin = performance.now();
  const timer = setInterval(() => {
    if (!soloDrill || !soloDrill.delivering) {
      clearInterval(timer);
      return;
    }
    const share = Math.min(0.95, (performance.now() - begin) / 1000 / estimate);
    bar.style.width = `${Math.round(share * 100)}%`;
    if (share >= 0.95) clearInterval(timer);
  }, 120);
  soloDrill.intervals.push(timer);
}

// Speech completion cannot rely on the 'end' event alone: the engine sometimes never
// delivers it, and the drill must not wait forever for a read that already stopped.
function soloSpeak(source, onDone) {
  if (!soloSpeechAvailable()) {
    onDone(false);
    return;
  }
  soloStopSpeech();
  const utterance = new SpeechSynthesisUtterance(source);
  utterance.rate = soloOptions.rate;
  const voices = window.speechSynthesis.getVoices() || [];
  const voice = voices.find((entry) => entry.name === soloOptions.voice);
  if (voice) utterance.voice = voice;
  let settled = false;
  let spoke = false;
  let pollTimer = null;
  const settle = () => {
    if (settled) return;
    settled = true;
    if (pollTimer) clearInterval(pollTimer);
    if (soloDrill && soloDrill.speechPoll === pollTimer) soloDrill.speechPoll = null;
    onDone(spoke);
  };
  utterance.onstart = () => { spoke = true; };
  utterance.onend = settle;
  utterance.onerror = () => {
    // A read the browser refused (or another page interrupted) must still release the drill.
    settle();
  };
  const begin = performance.now();
  pollTimer = setInterval(() => {
    if (settled) {
      clearInterval(pollTimer);
      return;
    }
    if (window.speechSynthesis.speaking || window.speechSynthesis.pending) {
      spoke = true;
      return;
    }
    // The engine stopped without an 'end' event, or never took the utterance at all.
    if (spoke || performance.now() - begin > 1500) settle();
  }, 200);
  if (soloDrill) soloDrill.speechPoll = pollTimer;
  window.speechSynthesis.speak(utterance);
}

// Called by every delivery channel; the read ends only when none is still running.
function soloFinishDelivery() {
  if (!soloDrill || soloDrill.answered) return;
  // Readiness is tracked per channel, so this only runs once both are done.
  if (!soloDrill.streamDone || !soloDrill.speechDone) return;
  const status = soloElement('solo-drill-status');
  const bar = soloElement('solo-progress-bar');
  if (bar) bar.style.width = '100%';
  if (status) status.innerText = 'Read complete — buzz window open (5.0s)';
  soloDrill.delivering = false;
  soloDrill.windowOpen = true;
  soloStartWindowCountdown();
}

function soloStartWindowCountdown() {
  const timer = setTimeout(() => {
    if (!soloDrill || soloDrill.answered) return;
    soloExpireQuestion('No buzz — question expired.');
  }, SOLO_LOCKOUT_SECONDS * 1000);
  soloDrill.timers.push(timer);
}

// The drill stops at the end of a window; moving on is the trainee's call, exactly as a
// moderator would start the next toss-up.
function soloExpireQuestion(message) {
  if (!soloDrill) return;
  soloStopDelivery();
  soloDrill.answered = true;
  soloDrill.streak = 0;
  soloResolveEntry('expired', false);
  soloRenderTelemetry();
  const buzz = soloElement('solo-buzz-btn');
  if (buzz) buzz.disabled = true;
  const status = soloElement('solo-drill-status');
  if (status) status.innerText = message;
  const feedback = soloElement('solo-feedback');
  if (feedback) {
    feedback.className = 'mt-4 text-sm font-mono font-bold text-slate-400';
    feedback.innerText = 'No points — press NEXT TOSSUP to keep training.';
  }
  soloRenderStats();
}

function soloBuzz() {
  if (!soloDrill || soloDrill.answered) return false;
  if (!soloDrill.windowOpen && !soloDrill.delivering) return false;
  if (!soloElement('slide-12') || soloElement('slide-12').classList.contains('hidden')) return false;
  soloDrill.lastReaction = (performance.now() - soloDrill.startedAt) / 1000;
  const entry = soloCurrentEntry();
  if (entry && typeof entry.reaction !== 'number') entry.reaction = soloDrill.lastReaction;
  soloStopDelivery();
  soloDrill.answered = true;
  soloDrill.windowOpen = false;
  soloRenderStats();
  playSound('buzz');

  const buzz = soloElement('solo-buzz-btn');
  if (buzz) buzz.disabled = true;
  const status = soloElement('solo-drill-status');
  if (status) status.innerText = `Locked out at ${soloDrill.lastReaction.toFixed(2)}s — answer now`;
  const panel = soloElement('solo-answer-panel');
  if (panel) panel.classList.remove('hidden');

  soloStartLockoutCountdown();
  if (soloOptions.input === 'audio') soloStartRecognition();
  else {
    const input = soloElement('solo-answer-input');
    if (input) input.focus();
  }
  return true;
}

function soloStartLockoutCountdown() {
  const label = soloElement('solo-answer-timer');
  const header = soloElement('solo-lockout');
  const deadline = performance.now() + SOLO_LOCKOUT_SECONDS * 1000;
  soloDrill.lockoutTimer = setInterval(() => {
    const remaining = Math.max(0, (deadline - performance.now()) / 1000);
    if (label) label.innerText = `${remaining.toFixed(1)}s`;
    if (header) header.innerText = `Lockout: ${remaining.toFixed(1)}s`;
    if (remaining <= 0) {
      clearInterval(soloDrill.lockoutTimer);
      soloDrill.lockoutTimer = null;
      soloRecordResult(false, 'Lockout expired before an answer arrived.');
    }
  }, 100);
}

function soloStartRecognition() {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const status = soloElement('solo-drill-status');
  if (!Recognition) {
    if (status) status.innerText = 'Speech recognition unavailable here — type the answer instead.';
    const input = soloElement('solo-answer-input');
    if (input) input.focus();
    return;
  }
  try {
    const recognition = new Recognition();
    recognition.lang = 'en-US';
    recognition.interimResults = true;
    recognition.maxAlternatives = 3;
    recognition.onresult = (event) => {
      const transcript = [...event.results].map((result) => result[0].transcript).join(' ').trim();
      if (status) status.innerText = `Heard: "${transcript}"`;
      if (event.results[event.results.length - 1].isFinal && transcript) {
        soloStopDelivery();
        soloSubmitAnswer(transcript);
      }
    };
    recognition.onerror = (event) => {
      if (status) status.innerText = `Microphone capture failed (${event.error}) — type the answer instead.`;
      const input = soloElement('solo-answer-input');
      if (input) input.focus();
    };
    recognition.start();
    soloDrill.recognition = recognition;
    if (status) status.innerText = 'Listening for your answer...';
  } catch (error) {
    if (status) status.innerText = 'Microphone capture could not start — type the answer instead.';
  }
}

// =========================================================
// JUDGING / SCORING
// =========================================================
function soloNormalizeText(value) {
  return String(value || '')
    .toLowerCase()
    .replace(/[\u2018\u2019\u201c\u201d]/g, "'")
    .replace(/[^a-z0-9'\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

// =========================================================
// ANSWER GRADING (mirrors core/grading.py — keep the two in step)
// =========================================================
// A packet line may carry alternatives — "6,000 (ACCEPT: 6 x 10^3)" — and a line may list
// its answers with a semicolon ("MACH; KNOTS"). Every listed form is equally correct, so
// judging is candidate based: any one of them matching makes the response correct.
// Parentheticals that are not ACCEPT groups are annotations — "(in any order)",
// "(Solution: ...)" — and are never part of the answer.

const SOLO_SUPERSCRIPTS = { '\u2070': '0', '\u00b9': '1', '\u00b2': '2', '\u00b3': '3', '\u2074': '4', '\u2075': '5', '\u2076': '6', '\u2077': '7', '\u2078': '8', '\u2079': '9', '\u207b': '-' };
const SOLO_ACCEPT_GROUP = /\(\s*accept\s*:?\s*([^)]*)\)/gi;
const SOLO_PARENTHETICAL = /\([^)]*\)/g;
const SOLO_ACCEPT_PREFIX = /^\s*accept\s*:?\s*(.+)$/i;
const SOLO_ALT_SPLIT = /\s*(?:;|\bor\b)\s*/i;
// The letter must be followed by a bracket/period/colon or whitespace, so a short answer
// that merely begins with a choice letter ("X-ray") is never read as option X.
const SOLO_MC_LINE = /^\s*([WXYZ])\s*(?:[).:]\s*|\s+)(\S.*)$/i;
// A reply that leads with a choice letter. The separator or end of input is required, so a
// word that merely starts with a choice letter ("water") is never read as the letter W.
const SOLO_MC_REPLY = /^\s*([wxyz])\s*(?:[).:\-]\s*(.*)|$)/i;
const SOLO_PLAIN_NUMBER = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/;
const SOLO_NUMBER_PATTERN = '([+-]?(?:\\d+(?:\\.\\d*)?|\\.\\d+))';
const SOLO_SCIENTIFIC = new RegExp(`^${SOLO_NUMBER_PATTERN}\\s*(?:[x*\u00d7]|times)\\s*10\\s*(?:\\^|to the|to)?\\s*([+-]?\\d+)(?:st|nd|rd|th)?$`);
const SOLO_SCIENTIFIC_SPOKEN = new RegExp(`^${SOLO_NUMBER_PATTERN}\\s*(?:[x*\u00d7]|times)\\s*10\\s*(?:to the\\s+)?(squared|cubed|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)$`);
const SOLO_EXPONENT = new RegExp(`^${SOLO_NUMBER_PATTERN}[eE]([+-]?\\d+)$`);
const SOLO_ORDINAL_POWERS = { squared: 2, cubed: 3, third: 3, fourth: 4, fifth: 5, sixth: 6, seventh: 7, eighth: 8, ninth: 9, tenth: 10 };
const SOLO_NUMBER_WORDS = {
  zero: 0, one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seven: 7, eight: 8, nine: 9,
  ten: 10, eleven: 11, twelve: 12, thirteen: 13, fourteen: 14, fifteen: 15, sixteen: 16,
  seventeen: 17, eighteen: 18, nineteen: 19, twenty: 20, thirty: 30, forty: 40, fifty: 50,
  sixty: 60, seventy: 70, eighty: 80, ninety: 90, hundred: 100, thousand: 1000,
  million: 1e6, billion: 1e9, half: 0.5, quarter: 0.25,
};
// Units may trail a numeric answer: the NSB appendix does not require units to be stated.
// Only these tokens — or words the packet's own answer line uses — may be dropped, so
// "90 south" is never read as a bare 90 when the packet's answer says north.
const SOLO_UNIT_WORDS = new Set(`
  m cm mm nm km meter meters metre metres centimeter centimeters centimetre centimetres
  millimeter millimeters millimetre millimetres micrometer micrometers micron microns
  nanometer nanometers angstrom angstroms inch inches in foot feet ft yard yards yd
  mile miles mi g gram grams gm kg kilogram kilograms kilogramme mg milligram milligrams
  pound pounds lb lbs ounce ounces oz ton tons tonne tonnes amu dalton daltons
  s sec secs second seconds minute minutes min hour hours hr hrs day days week weeks
  month months year years yr millisecond milliseconds microsecond microseconds
  nanosecond nanoseconds k kelvin kelvins celsius centigrade fahrenheit rankine
  degree degrees a amp ampere amperes amps v volt volts ohm ohms watt watts w joule
  joules j coulomb coulombs farad farads henry henries tesla teslas weber webers siemens
  hertz hz pascal pascals pa newton newtons n bar bars atmosphere atmospheres atm psi
  torr mmhg calorie calories cal kilocalorie kilocalories kcal electronvolt electronvolts
  ev kilojoule kilojoules kj megajoule megajoules mj mole moles mol molar candela lumen
  lumens lux l liter liters litre litres ml milliliter milliliters millilitre millilitres
  cc cubic gallon gallons quart quarts pint pints cup cups becquerel becquerels curie
  curies sievert sieverts gray grays rad rem percent percentage ppm
`.split(/\s+/).filter(Boolean));
// Words that carry no answer content: articles, prepositions, and the spoken preamble a
// typed answer often opens with. They may surround a numeric answer, but a word that means
// something ("south") never may.
const SOLO_FILLER_WORDS = new Set(`
  a an the is are was were be of and or it its that this i my me answer answered think
  believe maybe probably approximately approx roughly around about so then there their
  in any order
`.split(/\s+/).filter(Boolean));

function soloNumbersEqual(first, second) {
  return Math.abs(first - second) <= Math.max(1e-9 * Math.max(Math.abs(first), Math.abs(second)), 1e-12);
}

function soloScaled(coefficient, exponent) {
  const value = Number(coefficient) * Math.pow(10, Number(exponent));
  return Number.isFinite(value) ? value : null;
}

// Handles the forms a science packet and a hurried typist actually produce: thousands
// separators, scientific notation with any multiplication sign, superscript digits, and a
// plain decimal written with or without a leading zero.
function soloNumericValue(value) {
  let text = String(value == null ? '' : value).trim().toLowerCase();
  for (const [mark, digit] of Object.entries(SOLO_SUPERSCRIPTS)) text = text.split(mark).join(digit);
  text = text.replace(/[\u2018\u2019\u201c\u201d]/g, "'");
  while (/(\d),(\d)/.test(text)) text = text.replace(/(\d),(\d)/g, '$1$2');
  text = text.replace(/\s+/g, ' ').trim();
  if (!text) return null;
  let match = text.match(SOLO_SCIENTIFIC);
  if (match) return soloScaled(match[1], match[2]);
  match = text.match(SOLO_SCIENTIFIC_SPOKEN);
  if (match) return soloScaled(match[1], SOLO_ORDINAL_POWERS[match[2]]);
  match = text.replace(/\s+/g, '').match(SOLO_EXPONENT);
  if (match) return soloScaled(match[1], match[2]);
  const compact = text.replace(/\s+/g, '');
  if (SOLO_PLAIN_NUMBER.test(compact)) {
    const number = Number(compact);
    return Number.isFinite(number) ? number : null;
  }
  return null;
}

// A single spelled-out word ("zero", "half"); a compound such as "seven tenths" carries
// its own meaning and is not guessed at, so multiple-choice wording stays exact.
function soloSpelledNumberValue(value) {
  const text = soloNormalizeText(value);
  if (!text || text.includes(' ')) return null;
  return Object.prototype.hasOwnProperty.call(SOLO_NUMBER_WORDS, text) ? SOLO_NUMBER_WORDS[text] : null;
}

function soloAnswerValue(value) {
  const number = soloNumericValue(value);
  return number !== null ? number : soloSpelledNumberValue(value);
}

function soloIgnorableWord(word, allowedWords) {
  return SOLO_FILLER_WORDS.has(word) || SOLO_UNIT_WORDS.has(word) || allowedWords.has(word);
}

// Only a trailing unit word is dropped and never a trailing number, so a reply of
// "6 x 10^3" stays the single number 6000 and is never reduced to a bare 6.
function soloReplyNumber(value, allowedWords) {
  const text = String(value == null ? '' : value).replace(SOLO_PARENTHETICAL, ' ').replace(/\s+/g, ' ').trim();
  if (!text) return null;
  const whole = soloAnswerValue(text);
  if (whole !== null) return whole;
  let tokens = text.split(' ');
  while (tokens.length > 1) {
    if (soloAnswerValue(tokens[tokens.length - 1]) !== null) break;
    if (!soloIgnorableWord(soloNormalizeText(tokens[tokens.length - 1]), allowedWords)) break;
    tokens = tokens.slice(0, -1);
    const head = soloAnswerValue(tokens.join(' '));
    if (head !== null) return head;
  }
  return null;
}

// A numeric answer is decided by value, never by the digits appearing inside a longer
// reply: "90 south" does not answer 90 NORTH, and "6 x 10^3" does not answer 6.
function soloNumericReplyMatches(answerNumber, replyRaw, replyWords, allowedWords) {
  const replyNumber = soloReplyNumber(replyRaw, allowedWords);
  if (replyNumber !== null) return soloNumbersEqual(answerNumber, replyNumber);
  const numbers = replyWords.filter((word) => soloAnswerValue(word) !== null);
  if (numbers.length !== 1 || !soloNumbersEqual(answerNumber, soloAnswerValue(numbers[0]))) return false;
  return replyWords.filter((word) => soloAnswerValue(word) === null).every((word) => soloIgnorableWord(word, allowedWords));
}

// Bounded on purpose: a misspelling of a long word is a slip, but a short answer has too
// few characters for a typo to stay unambiguous, so it must be spelled out.
function soloTypoAllowance(word) {
  if (word.length < 6) return 0;
  if (word.length < 9) return 1;
  if (word.length < 15) return 2;
  return 3;
}

function soloIsPluralPair(first, second) {
  for (const [short, long] of [[first, second], [second, first]]) {
    if (long === short + 's' || long === short + 'es') return true;
    if (short.endsWith('y') && long === short.slice(0, -1) + 'ies') return true;
    if (short.endsWith('us') && long === short.slice(0, -2) + 'i') return true;
    if (short.endsWith('is') && long === short.slice(0, -2) + 'es') return true;
  }
  return false;
}

function soloEditDistanceWithin(first, second, limit) {
  if (Math.abs(first.length - second.length) > limit) return false;
  let previous = Array.from({ length: second.length + 1 }, (_, index) => index);
  for (let row = 1; row <= first.length; row += 1) {
    const current = [row];
    for (let column = 1; column <= second.length; column += 1) {
      current.push(Math.min(
        previous[column] + 1,
        current[column - 1] + 1,
        previous[column - 1] + (first[row - 1] === second[column - 1] ? 0 : 1),
      ));
    }
    if (Math.min(...current) > limit) return false;
    previous = current;
  }
  return previous[previous.length - 1] <= limit;
}

function soloWordsMatch(answerWord, replyWords) {
  if (replyWords.includes(answerWord)) return true;
  const allowance = soloTypoAllowance(answerWord);
  return replyWords.some((replyWord) => {
    if (soloIsPluralPair(answerWord, replyWord)) return true;
    return allowance > 0 && replyWord.length >= 5 && soloEditDistanceWithin(answerWord, replyWord, allowance);
  });
}

// Hyphen and space placement should not decide an answer. Equality only — never
// containment — so "iron" cannot be found inside "environment".
function soloCompactEqual(answer, reply, replyWords) {
  const compact = String(answer == null ? '' : answer).toLowerCase().replace(/[^a-z0-9]+/g, '');
  if (compact.length < 4) return false;
  const replyCompact = String(reply == null ? '' : reply).toLowerCase().replace(/[^a-z0-9]+/g, '');
  if (compact === replyCompact) return true;
  return replyWords.some((word) => String(word).toLowerCase().replace(/[^a-z0-9]+/g, '') === compact);
}

function soloMatchesCandidate(candidate, replyRaw, reply, replyWords, allowedWords) {
  const answer = soloNormalizeText(candidate);
  if (!answer) return false;
  if (reply === answer) return true;
  const answerNumber = soloNumericValue(candidate);
  if (answerNumber !== null) return soloNumericReplyMatches(answerNumber, replyRaw, replyWords, allowedWords);
  if (` ${reply} `.includes(` ${answer} `)) return true;
  if (soloCompactEqual(answer, reply, replyWords)) return true;
  const answerWords = answer.split(' ').filter(Boolean);
  if (answerWords.length === 1) return soloWordsMatch(answerWords[0], replyWords);
  const significant = answerWords.filter((word) => word.length >= 4);
  if (!significant.length) return answerWords.every((word) => soloWordsMatch(word, replyWords));
  return significant.every((word) => soloWordsMatch(word, replyWords));
}

function soloLooksNumeric(text) {
  return SOLO_PLAIN_NUMBER.test(String(text).replace(/\s+/g, ''));
}

// Commas are only separators when the pieces are not all plain numbers, so
// "(ACCEPT: 1,000)" stays one answer while "(ACCEPT: RADII, RADIUS)" becomes two.
function soloAlternatives(group) {
  const pieces = [];
  for (const piece of group.split(SOLO_ALT_SPLIT)) {
    const trimmed = piece.trim();
    if (!trimmed) continue;
    const commaParts = trimmed.split(',').map((part) => part.trim()).filter(Boolean);
    if (commaParts.length > 1 && !commaParts.every(soloLooksNumeric)) pieces.push(...commaParts);
    else pieces.push(trimmed);
  }
  return pieces;
}

function soloCandidateAnswers(answerText) {
  const raw = String(answerText == null ? '' : answerText);
  const candidates = [];
  let base = raw.replace(SOLO_PARENTHETICAL, ' ').trim();
  const prefixed = base.match(SOLO_ACCEPT_PREFIX);
  if (prefixed) base = prefixed[1].trim();
  for (const piece of base.split(';')) {
    const trimmed = piece.trim();
    if (trimmed && !candidates.includes(trimmed)) candidates.push(trimmed);
  }
  for (const group of raw.match(SOLO_ACCEPT_GROUP) || []) {
    const inner = group.replace(/^\(\s*accept\s*:?\s*/i, '').replace(/\)\s*$/, '');
    for (const alternative of soloAlternatives(inner)) {
      if (alternative && !candidates.includes(alternative)) candidates.push(alternative);
    }
  }
  return candidates;
}

function soloPrintedAnswer(answerText) {
  return soloNormalizeText(String(answerText == null ? '' : answerText).replace(SOLO_PARENTHETICAL, ' '));
}

function soloJudgeKeyword(answerText, response) {
  const replyRaw = String(response == null ? '' : response);
  const reply = soloNormalizeText(replyRaw);
  if (!reply) return false;
  // Typing the answer line exactly as the packet prints it is always correct, even when the
  // line lists several values or carries a solution.
  const printed = soloPrintedAnswer(answerText);
  if (printed && reply === printed) return true;
  const replyWords = reply.split(' ').filter(Boolean);
  const candidates = soloCandidateAnswers(answerText);
  // Words the packet itself uses are acceptable trailing qualifiers, so a question whose
  // answer line says "90 NORTH" still accepts "90 degrees north" and rejects "90 south".
  const allowedWords = new Set();
  for (const candidate of candidates) {
    for (const word of soloNormalizeText(candidate).split(' ')) if (word) allowedWords.add(word);
  }
  return candidates.some((candidate) => soloMatchesCandidate(candidate, replyRaw, reply, replyWords, allowedWords));
}

function soloChoiceLine(answerText) {
  const text = String(answerText == null ? '' : answerText).trim();
  const match = text.match(SOLO_MC_LINE);
  if (match) return [match[1].toLowerCase(), match[2].trim()];
  if (text.length === 1 && 'wxyz'.includes(text.toLowerCase())) return [text.toLowerCase(), ''];
  return [null, ''];
}

function soloReplyChoice(response) {
  const match = String(response == null ? '' : response).match(SOLO_MC_REPLY);
  if (!match) return [null, ''];
  return [match[1].toLowerCase(), (match[2] || '').trim()];
}

function soloPhrasesEqual(phrase, reply) {
  if (soloNormalizeText(phrase) === soloNormalizeText(reply)) return true;
  const phraseValue = soloAnswerValue(phrase);
  const replyValue = soloAnswerValue(reply);
  return phraseValue !== null && replyValue !== null && soloNumbersEqual(phraseValue, replyValue);
}

// Rule 3-3: the letter or the choice text; Rule 3-6: both, if both are given. The choice
// text must match exactly once case and punctuation are folded.
function soloJudgeMultipleChoice(answerText, response) {
  const candidates = soloCandidateAnswers(answerText);
  const base = candidates.length ? candidates[0] : '';
  const [letter, phrase] = soloChoiceLine(base);
  if (letter === null) return soloJudgeKeyword(answerText, response);
  const [replyLetter, replyPhrase] = soloReplyChoice(response);
  if (replyLetter === null) {
    return candidates.some((candidate) => {
      const text = soloChoiceLine(candidate)[1] || candidate;
      return Boolean(text) && soloPhrasesEqual(text, response);
    });
  }
  if (replyLetter !== letter) return false;
  if (replyPhrase && !soloPhrasesEqual(phrase, replyPhrase)) return false;
  return true;
}

// The entry point: alternatives, numeric equality, and the NSB multiple-choice rules.
function soloJudgeAnswer(question, response) {
  const answerText = String((question || {}).tossup_answer || '');
  const candidates = soloCandidateAnswers(answerText);
  const isMultipleChoice = String((question || {}).type || '').trim().toUpperCase() === 'MC'
    || (candidates.length > 0 && soloChoiceLine(candidates[0])[0] !== null);
  if (isMultipleChoice) return soloJudgeMultipleChoice(answerText, response);
  return soloJudgeKeyword(answerText, response);
}

function soloSubmitAnswer(response) {
  if (!soloDrill || !soloDrill.answered) return;
  const entry = soloDrill.queue[soloDrill.index];
  if (!entry) return;
  const correct = soloJudgeAnswer(entry.question, response);
  soloRecordResult(correct, correct ? `Correct — "${entry.question.tossup_answer}"` : `Incorrect — answer: "${entry.question.tossup_answer}"`);
}

function soloRecordResult(correct, message, outcome = 'answered') {
  if (!soloDrill) return;
  if (soloDrill.lockoutTimer) {
    clearInterval(soloDrill.lockoutTimer);
    soloDrill.lockoutTimer = null;
  }
  soloStopSpeech();
  soloStopRecognition();
  if (correct) {
    soloDrill.score += SOLO_TOSS_UP_POINTS;
    soloDrill.streak += 1;
    playSound('correct');
  } else {
    soloDrill.streak = 0;
    playSound('buzz');
  }
  soloDrill.answered = true;
  soloDrill.windowOpen = false;
  soloResolveEntry(outcome, correct);
  soloRenderTelemetry();
  const feedback = soloElement('solo-feedback');
  if (feedback) {
    feedback.className = `mt-4 text-sm font-mono font-bold ${correct ? 'text-neonGreen' : 'text-neonPink'}`;
    feedback.innerText = `${correct ? '✅' : '❌'} ${message}`;
  }
  const status = soloElement('solo-drill-status');
  if (status) status.innerText = correct ? 'Point awarded — press NEXT TOSSUP' : 'No points — press NEXT TOSSUP';
  const header = soloElement('solo-lockout');
  if (header) header.innerText = `Lockout: ${SOLO_LOCKOUT_SECONDS.toFixed(1)}s`;
  soloRenderStats();
}

function soloRevealAnswer() {
  if (!soloDrill) return;
  const entry = soloDrill.queue[soloDrill.index];
  if (!entry) return;
  const wasAnswered = soloDrill.answered;
  soloStopDelivery();
  soloDrill.answered = true;
  soloDrill.windowOpen = false;
  if (!wasAnswered) {
    soloDrill.streak = 0;
    soloResolveEntry('revealed', false);
    soloRenderStats();
  }
  soloRenderTelemetry();
  const feedback = soloElement('solo-feedback');
  if (feedback) {
    feedback.className = 'mt-4 text-sm font-mono font-bold text-neonAmber';
    feedback.innerText = `💡 Answer: ${entry.question.tossup_answer}`;
  }
  const text = soloElement('solo-question-text');
  if (text && soloOptions.output === 'spoken') {
    text.className = 'min-h-[132px] bg-black border border-borderDark rounded-xl p-5 text-lg md:text-xl text-slate-200 font-sans leading-relaxed';
    text.textContent = soloQuestionWords(entry.question.tossup_text).join(' ');
  }
  const status = soloElement('solo-drill-status');
  if (status) status.innerText = 'Answer revealed';
}

function soloNextTossup() {
  if (!soloDrill) return;
  soloStopDelivery();
  soloDrill.index += 1;
  if (soloDrill.index >= soloDrill.queue.length) {
    soloDrill.queue = soloBuildQueue();
    soloDrill.index = 0;
  }
  if (!soloDrill.queue.length) {
    showToast('No questions left — load another packet');
    return;
  }
  soloDrill.answered = false;
  soloDrill.entryIndex = null;
  soloRenderQuestion({ ready: true });
  soloRenderTelemetry();
}

// =========================================================
// SESSION TELEMETRY (celerity, accuracy, per-subject detail)
// =========================================================
function soloCurrentEntry() {
  if (!soloDrill || soloDrill.entryIndex == null || !soloSession) return null;
  return soloSession.questions[soloDrill.entryIndex] || null;
}

// One entry per toss-up asked: re-reading a question trains it again but is not a new
// question, so the accuracy of a subject is never diluted by second attempts.
function soloRecordAsked(question) {
  if (!soloDrill || !soloSession) return;
  const existing = soloCurrentEntry();
  if (existing) {
    existing.reReads += 1;
    return;
  }
  soloSession.questions.push({
    n: soloSession.questions.length + 1,
    subject: soloSubjectFor(question.category),
    category: String(question.category || ''),
    type: String(question.type || 'SA').toUpperCase(),
    answer: String(question.tossup_answer || ''),
    outcome: 'pending',
    correct: null,
    reaction: null,
    reReads: 0,
  });
  soloDrill.entryIndex = soloSession.questions.length - 1;
}

function soloResolveEntry(outcome, correct) {
  const entry = soloCurrentEntry();
  if (!entry || entry.outcome !== 'pending') return;
  entry.outcome = outcome;
  entry.correct = Boolean(correct);
  if (soloSession && soloDrill.streak > soloSession.streakBest) soloSession.streakBest = soloDrill.streak;
}

// Pure aggregation behind the telemetry card and every saved session.
function soloSessionStats(session = soloSession) {
  const questions = (session && session.questions) || [];
  const resolved = questions.filter((entry) => entry.outcome !== 'pending');
  const correct = resolved.filter((entry) => entry.correct === true);
  const reactions = questions.filter((entry) => typeof entry.reaction === 'number').map((entry) => entry.reaction);
  const mean = (values) => (values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null);
  const subjects = new Map();
  questions.forEach((entry) => {
    const row = subjects.get(entry.subject) || { subject: entry.subject, asked: 0, resolved: 0, correct: 0, reactions: [] };
    row.asked += 1;
    if (entry.outcome !== 'pending') row.resolved += 1;
    if (entry.correct === true) row.correct += 1;
    if (typeof entry.reaction === 'number') row.reactions.push(entry.reaction);
    subjects.set(entry.subject, row);
  });
  return {
    asked: questions.length,
    resolved: resolved.length,
    correct: correct.length,
    accuracy: resolved.length ? (correct.length / resolved.length) * 100 : null,
    // Celerity is the buzz-to-lockout reaction; the per-subject figure uses every buzz so
    // a subject cannot look fast just by being answered correctly.
    celerity: mean(reactions),
    celerityCorrect: mean(correct.filter((entry) => typeof entry.reaction === 'number').map((entry) => entry.reaction)),
    fastest: reactions.length ? Math.min(...reactions) : null,
    subjects: [...subjects.values()]
      .sort((left, right) => right.asked - left.asked || left.subject.localeCompare(right.subject))
      .map((row) => ({
        subject: row.subject,
        asked: row.asked,
        resolved: row.resolved,
        correct: row.correct,
        accuracy: row.resolved ? (row.correct / row.resolved) * 100 : null,
        celerity: mean(row.reactions),
      })),
  };
}

function soloSecondsText(value) {
  return typeof value === 'number' ? `${value.toFixed(2)} S` : '-- S';
}

function soloPercentText(value) {
  return typeof value === 'number' ? `${Math.round(value)}%` : '--%';
}

function soloRenderTelemetry() {
  const stats = soloSessionStats();
  const tiles = {
    'solo-stat-asked': String(stats.asked),
    'solo-stat-accuracy': soloPercentText(stats.accuracy),
    'solo-stat-celerity': soloSecondsText(stats.celerity),
    'solo-stat-fastest': soloSecondsText(stats.fastest),
  };
  Object.entries(tiles).forEach(([id, value]) => {
    const node = soloElement(id);
    if (node) node.innerText = value;
  });

  const tbody = soloElement('solo-subject-stats');
  const empty = soloElement('solo-subject-stats-empty');
  if (empty) empty.classList.toggle('hidden', stats.subjects.length > 0);
  if (!tbody) return;
  tbody.replaceChildren();
  stats.subjects.forEach((row) => {
    const tr = document.createElement('tr');
    tr.className = 'border-t border-borderDark text-[11px] font-mono';
    const cells = [
      { text: row.subject, className: 'py-2 pr-3 font-bold', color: soloSubjectColor(row.subject) },
      { text: String(row.asked), className: 'py-2 px-3 text-center text-slate-300' },
      { text: String(row.correct), className: 'py-2 px-3 text-center text-neonGreen' },
      { text: soloPercentText(row.accuracy), className: 'py-2 px-3 text-center text-slate-200' },
      { text: soloSecondsText(row.celerity), className: 'py-2 pl-3 text-center text-neonCyan' },
    ];
    cells.forEach((cell) => {
      const td = document.createElement('td');
      td.className = cell.className;
      td.textContent = cell.text;
      if (cell.color) td.style.color = cell.color;
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
}

function soloSaveStatus(message, tone = 'slate') {
  const status = soloElement('solo-save-status');
  if (!status) return;
  const tones = { slate: 'text-slate-500', green: 'text-neonGreen', amber: 'text-neonAmber', pink: 'text-neonPink' };
  status.className = `text-[11px] font-mono mb-4 ${tones[tone] || tones.slate}`;
  status.innerText = message;
}

function soloCloudUser() {
  return typeof window.getFirebaseUser === 'function' ? window.getFirebaseUser() : null;
}

// Sessions are always kept on the device first, so a signed-out trainee still owns the
// data; saving to the account is an explicit second step.
function soloSessionPayload() {
  const stats = soloSessionStats();
  return {
    id: soloSession.id,
    startedAt: soloSession.startedAt,
    endedAt: new Date().toISOString(),
    options: {
      output: soloOptions.output,
      input: soloOptions.input,
      voice: soloOptions.voice,
      rate: soloOptions.rate,
      wpm: soloOptions.wpm,
    },
    score: soloDrill ? soloDrill.score : 0,
    streakBest: soloSession.streakBest,
    stats: {
      asked: stats.asked,
      resolved: stats.resolved,
      correct: stats.correct,
      accuracy: stats.accuracy,
      celerity: stats.celerity,
      celerityCorrect: stats.celerityCorrect,
      fastest: stats.fastest,
    },
    subjects: stats.subjects,
    questions: soloSession.questions.slice(0, SOLO_SESSION_MAX_QUESTIONS),
  };
}

function soloLocalSessions() {
  try {
    const stored = window.localStorage ? JSON.parse(window.localStorage.getItem(SOLO_SESSION_STORAGE_KEY) || '[]') : [];
    return Array.isArray(stored) ? stored : [];
  } catch (error) {
    console.warn('Stored drill sessions could not be read.', error);
    return [];
  }
}

function soloKeepLocalSession(payload) {
  const sessions = soloLocalSessions().filter((entry) => entry.id !== payload.id);
  sessions.unshift(payload);
  const trimmed = sessions.slice(0, SOLO_SESSION_KEEP);
  try {
    if (window.localStorage) window.localStorage.setItem(SOLO_SESSION_STORAGE_KEY, JSON.stringify(trimmed));
  } catch (error) {
    console.warn('Drill session could not be stored on this device.', error);
  }
  return trimmed;
}

function soloRefreshSaveHint() {
  if (typeof window.saveSoloSessionToCloud !== 'function') {
    soloSaveStatus('Sessions are kept on this device. Saving to an account needs a configured Firebase page.', 'slate');
    return;
  }
  const user = soloCloudUser();
  if (!user) {
    soloSaveStatus('Sessions are kept on this device — sign in to save them to your account.', 'slate');
    return;
  }
  soloSaveStatus(`Sessions can be saved to ${user.email || 'your account'}.`, 'slate');
}

async function soloSaveSession() {
  if (!soloSession || !soloSession.questions.length) {
    soloSaveStatus('Nothing to save yet — run a few toss-ups first.', 'amber');
    return;
  }
  const payload = soloSessionPayload();
  soloKeepLocalSession(payload);
  const kept = 'Kept on this device.';
  if (typeof window.saveSoloSessionToCloud !== 'function') {
    soloSaveStatus(`${kept} Cloud saving is unavailable on this page (Firebase is not configured).`, 'amber');
    return;
  }
  const user = soloCloudUser();
  if (!user) {
    soloSaveStatus(`${kept} Sign in to save it to your account.`, 'amber');
    return;
  }
  soloSaveStatus('Saving session...');
  try {
    await window.saveSoloSessionToCloud(payload);
    soloSaveStatus(`Saved to ${user.email || 'your account'}.`, 'green');
    playSound('correct');
  } catch (error) {
    const reason = (error && error.message) || 'unknown error';
    // A session that is still local-only because nobody is signed in is guidance, not a
    // failure — the store is doing what it was asked.
    if (/sign in/i.test(reason)) soloSaveStatus(`${kept} Sign in to save it to your account.`, 'amber');
    else soloSaveStatus(`Cloud save failed: ${reason} — kept on this device.`, 'pink');
  }
}

function soloDescribeSession(session) {
  const stats = (session && session.stats) || {};
  const asked = Number(stats.asked) || 0;
  const accuracy = typeof stats.accuracy === 'number' ? `${Math.round(stats.accuracy)}%` : '--%';
  const celerity = typeof stats.celerity === 'number' ? `${stats.celerity.toFixed(2)} S` : '-- S';
  const when = String((session && session.startedAt) || '').replace('T', ' ').slice(0, 16) || 'unknown date';
  return `${when} · ${asked} q · ${accuracy} · ${celerity}`;
}

async function soloToggleSavedSessions() {
  const list = soloElement('solo-saved-list');
  if (!list) return;
  if (!list.classList.contains('hidden')) {
    list.classList.add('hidden');
    return;
  }
  list.classList.remove('hidden');
  list.replaceChildren();
  const note = (message, tone) => {
    const line = document.createElement('p');
    line.className = `text-[11px] font-mono ${tone}`;
    line.textContent = message;
    list.appendChild(line);
  };

  const user = soloCloudUser();
  let sessions = [];
  let source = 'on this device';
  if (typeof window.loadSoloSessionsFromCloud !== 'function') {
    sessions = soloLocalSessions();
    source = 'on this device (no cloud on this page)';
  } else if (!user) {
    sessions = soloLocalSessions();
    source = 'on this device — sign in to sync';
  } else {
    note('Loading saved sessions...', 'text-slate-500');
    try {
      sessions = await window.loadSoloSessionsFromCloud(8);
      source = user.email || 'your account';
    } catch (error) {
      sessions = soloLocalSessions();
      source = 'on this device (cloud read failed)';
    }
  }

  list.replaceChildren();
  if (!sessions.length) {
    note('No saved sessions yet.', 'text-slate-500');
    return;
  }
  const heading = document.createElement('p');
  heading.className = 'text-[10px] font-mono font-bold tracking-widest text-slate-400 uppercase mb-2';
  heading.textContent = `Saved sessions — ${source}`;
  list.appendChild(heading);
  sessions.forEach((session) => {
    const row = document.createElement('div');
    row.className = 'bg-black border border-borderDark rounded-lg px-3 py-2';
    const text = document.createElement('span');
    text.className = 'text-[11px] font-mono text-slate-300';
    text.textContent = soloDescribeSession(session);
    row.appendChild(text);
    list.appendChild(row);
  });
}

// =========================================================
// WIRING
// =========================================================
function soloBindSetup() {
  document.querySelectorAll('.solo-output-card').forEach((card) => {
    card.addEventListener('click', () => {
      soloEnsureOptions().output = card.dataset.soloOutput;
      soloSaveOptions();
      soloRenderSetup();
      playSound('click');
    });
  });
  document.querySelectorAll('.solo-input-card').forEach((card) => {
    card.addEventListener('click', () => {
      soloEnsureOptions().input = card.dataset.soloInput;
      soloSaveOptions();
      soloRenderSetup();
      playSound('click');
    });
  });

  const rate = soloElement('solo-rate-slider');
  if (rate) {
    rate.addEventListener('input', () => {
      soloEnsureOptions().rate = Number(rate.value);
      soloSaveOptions();
      const label = soloElement('solo-rate-value');
      if (label) label.innerText = `${soloOptions.rate.toFixed(2).replace(/0$/, '')}X`;
      if (soloDrill) soloRenderStats();
    });
  }
  const wpm = soloElement('solo-wpm-slider');
  if (wpm) {
    wpm.addEventListener('input', () => {
      soloEnsureOptions().wpm = Number(wpm.value);
      soloSaveOptions();
      const label = soloElement('solo-wpm-value');
      if (label) label.innerText = `${soloOptions.wpm} WPM`;
      // A drill already running follows the new pace on its next read, and its header
      // must not keep advertising the old one.
      if (soloDrill) soloRenderStats();
    });
  }
  const voice = soloElement('solo-voice-select');
  if (voice) {
    voice.addEventListener('change', () => {
      soloEnsureOptions().voice = voice.value;
      soloSaveOptions();
    });
  }
  const testVoice = soloElement('solo-test-voice');
  if (testVoice) {
    testVoice.addEventListener('click', () => {
      if (!soloSpeechAvailable()) {
        showToast('This browser has no speech engine');
        return;
      }
      soloSpeak('Solo practice ready. Ten seconds, buzzer armed.', () => {});
    });
  }
  const micTest = soloElement('solo-test-mic');
  if (micTest) {
    micTest.addEventListener('click', soloTestMic);
  }
  const packetInput = soloElement('solo-packet-input');
  if (packetInput) {
    packetInput.addEventListener('change', async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        if (file.size > 5 * 1024 * 1024) throw new Error('Choose a packet no larger than 5 MB.');
        let bank;
        if (file.name.toLowerCase().endsWith('.json')) bank = JSON.parse(await file.text());
        else bank = parseScienceBowlText(await file.text());
        if (!Array.isArray(bank) || !bank.length) throw new Error('That packet has no questions.');
        soloAddPacket(file.name.replace(/\.(json|txt)$/i, ''), bank);
      } catch (error) {
        showToast(`Packet rejected — ${error.message}`);
      }
    });
  }
  const toggle = soloElement('solo-toggle-packets');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const list = soloElement('solo-packet-list');
      if (list) list.classList.toggle('hidden');
      playSound('click');
    });
  }
  const start = soloElement('solo-start-drill');
  if (start) start.addEventListener('click', startSoloDrill);

  const startQuestion = soloElement('solo-start-question');
  if (startQuestion) startQuestion.addEventListener('click', () => { soloStartQuestion(); playSound('click'); });
  const buzz = soloElement('solo-buzz-btn');
  if (buzz) buzz.addEventListener('click', () => soloBuzz());
  const submit = soloElement('solo-answer-submit');
  if (submit) submit.addEventListener('click', () => {
    const input = soloElement('solo-answer-input');
    soloSubmitAnswer(input ? input.value : '');
  });
  const answerInput = soloElement('solo-answer-input');
  if (answerInput) {
    answerInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        soloSubmitAnswer(answerInput.value);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        soloRecordResult(false, 'Answer forfeited.', 'forfeit');
      }
    });
  }
  const reveal = soloElement('solo-reveal-answer');
  if (reveal) reveal.addEventListener('click', () => { soloRevealAnswer(); playSound('click'); });
  const next = soloElement('solo-next-tossup');
  if (next) next.addEventListener('click', () => { soloNextTossup(); playSound('click'); });
  const saveSession = soloElement('solo-save-session');
  if (saveSession) saveSession.addEventListener('click', () => { soloSaveSession(); playSound('click'); });
  const savedSessions = soloElement('solo-load-sessions');
  if (savedSessions) savedSessions.addEventListener('click', () => { soloToggleSavedSessions(); playSound('click'); });
  if (window.firebaseAuthReady && typeof window.firebaseAuthReady.then === 'function') {
    window.firebaseAuthReady.then(() => soloRefreshSaveHint());
  } else {
    soloRefreshSaveHint();
  }

  document.addEventListener('keydown', (event) => {
    const tag = (document.activeElement && document.activeElement.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.code === 'Space' || event.key === ' ') {
      if (soloBuzz()) event.preventDefault();
    }
  });

  window.addEventListener('focus', () => { if (soloDrill) soloRenderStats(); });
}

async function soloTestMic() {
  const status = soloElement('solo-mic-status');
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  soloMicChecked = true;
  if (!Recognition) {
    if (status) {
      status.innerText = 'Speech recognition unavailable — use typed input';
      status.className = 'text-[10px] font-mono font-bold text-neonAmber';
    }
    showToast('This browser cannot recognize speech; typed input still works');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    stream.getTracks().forEach((track) => track.stop());
    if (status) {
      status.innerText = 'Speech Recognition Active';
      status.className = 'text-[10px] font-mono font-bold text-neonGreen';
    }
    showToast('Microphone ready');
  } catch (error) {
    if (status) {
      status.innerText = 'Microphone blocked — use typed input';
      status.className = 'text-[10px] font-mono font-bold text-neonPink';
    }
    showToast('Microphone permission denied');
  }
}

function soloResetOptions() {
  soloOptions = JSON.parse(JSON.stringify(SOLO_DEFAULT_OPTIONS));
  soloSaveOptions();
  soloRenderSetup();
  showToast('Solo drill options reset');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', soloBindSetup);
} else {
  soloBindSetup();
}

if (soloSpeechAvailable() && typeof window.speechSynthesis.addEventListener === 'function') {
  window.speechSynthesis.addEventListener('voiceschanged', () => {
    if (soloElement('slide-11') && !soloElement('slide-11').classList.contains('hidden')) soloPopulateVoices();
  });
}
