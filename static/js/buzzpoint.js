/* Buzzpoint Board — a captain-created packet challenge compiled into buzz-word comparison.
 *
 * The non-negotiable primitive lives at the top: track every word the speech engine reads,
 * and when a buzz fires, log the word being spoken at that instant as the buzz-point.
 * The browser's speech API is NOT a reliable word clock, so the tracker keeps a labelled
 * capture source (boundary | counted | estimated) and never lets a cancelled read be
 * recorded as a completed word.
 *
 * Everything renders through text nodes (createElement/textContent), never injected as
 * markup: packet text is untrusted.
 */

// ============================================================
// 1. WORD TRACKER — the primitive (reusable; exposed for solo mode too)
// ============================================================

// Must stay identical to core/words.py WORD_PATTERN. tests/test_buzzpoint_board.py pins them.
const BUK_WORD_PATTERN = "[A-Za-z0-9]+(?:[.'\u2019][A-Za-z0-9]+)*";

function buzzTokenizeWords(text) {
  const source = String(text || '');
  const re = new RegExp(BUK_WORD_PATTERN, 'g');
  const words = [];
  let match;
  while ((match = re.exec(source)) !== null) {
    words.push({ word: match[0], start: match.index, end: match.index + match[0].length });
  }
  return words;
}

function buzzWordIndexAt(words, charIndex) {
  if (!words || !words.length) return 0;
  let index = Number.isFinite(charIndex) ? Math.trunc(charIndex) : 0;
  if (index < 0) index = 0;
  let result = 0;
  for (let i = 0; i < words.length; i += 1) {
    if (words[i].start <= index) result = i;
    if (words[i].start <= index && index < words[i].end) return i;
  }
  return result;
}

// A tracker over one utterance's text. Three capture sources, two word values, and a hard
// rule: a cancel is never a completed word.
function createBuzzWordTracker(text, options) {
  const opts = options || {};
  const words = buzzTokenizeWords(text);
  const totalWords = words.length;
  const wordsPerSecond = Number(opts.wordsPerSecond) > 0 ? Number(opts.wordsPerSecond) : 2.5;

  let wordInProgress = 0;
  let lastConfirmedWord = null;
  let captureSource = totalWords ? 'counted' : 'estimated';
  let confidenceNote = totalWords ? 'read start' : 'no words';
  let startedAt = null;
  let advanced = false;
  let cancelRequested = false;

  const clamp = (index) => Math.max(0, Math.min(totalWords - 1, index));

  function noteStart() {
    startedAt = performance.now();
    wordInProgress = 0;
    advanced = false;
    captureSource = totalWords ? 'counted' : 'estimated';
    confidenceNote = 'read start';
  }

  function noteBoundary(charIndex) {
    if (cancelRequested) return;
    const index = buzzWordIndexAt(words, charIndex);
    if (index > wordInProgress) lastConfirmedWord = words[wordInProgress].word;
    wordInProgress = index;
    advanced = true;
    captureSource = 'boundary';
    confidenceNote = 'word boundary event';
  }

  function noteCounted(index) {
    if (cancelRequested) return;
    const next = clamp(Math.trunc(index));
    if (next > wordInProgress) lastConfirmedWord = words[wordInProgress].word;
    wordInProgress = next;
    advanced = true;
    captureSource = 'counted';
    confidenceNote = 'counted read position';
  }

  // Fallback when neither a boundary nor a counted reveal has moved us: estimate from
  // elapsed speaking time on a calibrated words/second baseline. Labelled approximate.
  function snapshot() {
    if (!advanced && startedAt !== null && totalWords) {
      const elapsedSeconds = (performance.now() - startedAt) / 1000;
      if (elapsedSeconds > 1 / wordsPerSecond) {
        wordInProgress = clamp(Math.floor(elapsedSeconds * wordsPerSecond));
        captureSource = 'estimated';
        confidenceNote = 'elapsed-time estimate';
      }
    }
    const current = totalWords ? words[wordInProgress] : null;
    return {
      word: current ? current.word : null,
      wordIndex: totalWords ? wordInProgress : null,
      charIndex: current ? current.start : null,
      wordsHeard: totalWords ? wordInProgress + 1 : 0,
      totalWords,
      captureSource,
      lastConfirmedWord,
      confidence: confidenceNote,
    };
  }

  // Flag BEFORE speechSynthesis.cancel(): cancel fires terminal events, and a cancel must
  // never be mistaken for the read finishing (or a word completing).
  function markCancelled() { cancelRequested = true; }
  function isCancelled() { return cancelRequested; }
  function noteNaturalEnd() {
    if (cancelRequested || !totalWords) return;
    lastConfirmedWord = words[wordInProgress].word;
    wordInProgress = totalWords - 1;
  }

  return {
    words, totalWords, noteStart, noteBoundary, noteCounted,
    snapshot, markCancelled, isCancelled, noteNaturalEnd,
  };
}

// Reusable helper so the same tracker can later serve solo mode (rule 10).
window.BuzzWordTracker = createBuzzWordTracker;
window.buzzTokenizeWords = buzzTokenizeWords;

// ============================================================
// 2. SUBJECTS + MEMBER COLORS
// ============================================================

const BUK_SUBJECT_ALIASES = {
  'life science': 'Biology', 'biology': 'Biology', 'physical science': 'Physics',
  'physics': 'Physics', 'chemistry': 'Chemistry', 'earth science': 'Earth and Space',
  'earth and space': 'Earth and Space', 'math': 'Math', 'mathematics': 'Math',
  'energy': 'Energy', 'general science': 'General Science',
};
function bukSubjectFor(category) {
  const raw = String(category || '').trim();
  return BUK_SUBJECT_ALIASES[raw.toLowerCase()] || raw || 'General';
}

// ============================================================
// 3. API + IDENTITY
// ============================================================

async function bukApi(path, options) {
  const opts = options || {};
  const token = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(path, { ...opts, headers });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const err = new Error(result.detail || 'Request failed');
    err.status = response.status;
    throw err;
  }
  return result;
}

// A member signs in anonymously. If Anonymous sign-in is off in the Firebase console,
// name that as the cause instead of silently failing.
async function bukEnsureIdentity() {
  if (window.getFirebaseUser && window.getFirebaseUser()) return window.getFirebaseUser();
  if (typeof window.signInAnonymouslyForBoard === 'function') {
    return await window.signInAnonymouslyForBoard();
  }
  return null;
}

function bukEl(id) { return document.getElementById(id); }
function bukStatus(id, message, color) {
  const el = bukEl(id);
  if (!el) return;
  el.innerText = message;
  if (color) el.style.color = color;
}

// ============================================================
// 4. CAPTAIN — create a challenge from an uploaded packet
// ============================================================

// .json is the packet format (the bank). A PDF is converted to a bank first through the
// existing BANK station, then uploaded here as JSON.
async function bukParsePacketFile(file) {
  const name = (file.name || '').toLowerCase();
  if (name.endsWith('.json')) {
    const bank = JSON.parse(await file.text());
    if (!Array.isArray(bank)) throw new Error('Packet JSON must be an array of questions');
    return bank;
  }
  throw new Error('Upload a .json packet (convert a PDF to a bank in the BANK station first)');
}

async function bukCreateChallenge() {
  const titleEl = bukEl('buk-captain-title');
  const fileEl = bukEl('buk-captain-packet');
  const title = titleEl ? titleEl.value : '';
  const file = fileEl && fileEl.files ? fileEl.files[0] : null;
  if (!title.trim()) { bukStatus('buk-captain-status', 'Give the challenge a title.', '#ffaa00'); return; }
  if (!file) { bukStatus('buk-captain-status', 'Choose a .json packet first.', '#ffaa00'); return; }
  const bonusesEl = bukEl('buk-captain-bonuses');
  const displayEl = bukEl('buk-captain-display');
  const readBonuses = bonusesEl ? Boolean(bonusesEl.checked) : true;
  const displayMode = displayEl && displayEl.value === 'audio-only' ? 'audio-only' : 'hybrid';
  try {
    bukStatus('buk-captain-status', 'Uploading packet…');
    const bank = await bukParsePacketFile(file);
    const result = await bukApi('/api/challenges', {
      method: 'POST', body: JSON.stringify({ title, bank, readBonuses, displayMode }),
    });
    const codeEl = bukEl('buk-captain-code');
    if (codeEl) codeEl.textContent = result.code;
    // The share link is the same address the member's screen uses, so the code is enough.
    const linkBtn = bukEl('buk-captain-link');
    if (linkBtn) linkBtn.classList.remove('hidden');
    bukStatus('buk-captain-status', `Challenge ready — share code ${result.code}.`, '#00ff66');
  } catch (err) {
    bukStatus('buk-captain-status', err.message || 'Could not create the challenge.', '#ff4d4d');
  }
}

// ============================================================
// 5. MEMBER RUN — speak, buzz mid-read, typed answer
// ============================================================

const bukRun = {
  code: null, meta: null, question: null, tracker: null,
  delivering: false, windowOpen: false, answered: false,
  readStart: 0, buzzAt: 0, firstKeystrokeAt: 0,
  wordStreamDone: false, speechDone: false, displayMode: 'hybrid',
  streamTimer: null, windowTimer: null, lockoutTimer: null, speechPoll: null,
  utterance: null, words: [], shownWords: 0, wordSpans: [], revealIndex: -1,
  sawBoundary: false, fallbackTimer: null, fallbackInterval: null,
  answering: false, awaitingNext: false,
  records: [], finished: false,
  // The challenge screen: which stage is showing, and which attempt this browser claimed.
  stage: null, claimedCode: null, joinStatus: null, displayName: '',
};

const BUK_TOSSUP_WINDOW_S = 5.0;
const BUK_ANSWER_WINDOW_S = 5.0;
const BUK_BONUS_WINDOW_S = 20.0;
// Realistic spoken pace (chars/sec at rate 1) for the reveal fallback when the engine gives
// no word boundaries. Matches the app's measured pace (SOLO_SPOKEN_CHARS_PER_SECOND).
const BUK_SPOKEN_CHARS_PER_SECOND = 12;

function bukStopSpeech() {
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (bukRun.speechPoll) { clearInterval(bukRun.speechPoll); bukRun.speechPoll = null; }
}

function bukStopDelivery() {
  bukRun.delivering = false;
  if (bukRun.streamTimer) { clearInterval(bukRun.streamTimer); bukRun.streamTimer = null; }
  if (bukRun.fallbackTimer) { clearTimeout(bukRun.fallbackTimer); bukRun.fallbackTimer = null; }
  if (bukRun.fallbackInterval) { clearInterval(bukRun.fallbackInterval); bukRun.fallbackInterval = null; }
  bukStopSpeech();
}

// Stream the words on a timer (the deterministic counted source) and speak aloud in
// parallel. A read is over only when BOTH channels are.
function bukStartQuestion(question) {
  bukRun.question = question;
  bukRun.answered = false;
  bukRun.windowOpen = false;
  bukRun.delivering = true;
  bukRun.wordStreamDone = false;
  bukRun.speechDone = false;
  bukRun.firstKeystrokeAt = 0;
  bukRun.buzzAt = 0;
  bukRun.words = buzzTokenizeWords(question.text || '');
  bukRun.shownWords = 0;
  // The captain chose this at creation; the server repeats it on every question payload.
  bukRun.displayMode = question.displayMode === 'audio-only' ? 'audio-only' : 'hybrid';
  bukRun.tracker = createBuzzWordTracker(question.text || '', { wordsPerSecond: 2.6 });

  const progressLabel = bukEl('buk-run-progress');
  if (progressLabel) {
    progressLabel.textContent = question.phase === 'bonus'
      ? `Bonus for Q${question.qIndex + 1} of ${question.totalQuestions}`
      : `Question ${question.qIndex + 1} of ${question.totalQuestions}`;
  }
  const catEl = bukEl('buk-run-category');
  if (catEl) catEl.textContent = `${question.subject || bukSubjectFor(question.category)} \u2022 ${question.phase === 'bonus' ? 'BONUS' : (question.type || 'TOSSUP')}`;

  const textEl = bukEl('buk-question-text');
  if (textEl) textEl.replaceChildren();
  bukRun.readStart = performance.now();
  bukRun.tracker.noteStart();
  bukPrepareReveal();
  bukSpeak(question.text || '');
}

function bukPrepareReveal() {
  const textEl = bukEl('buk-question-text');
  if (textEl) textEl.replaceChildren();
  if (bukRun.displayMode === 'audio-only') {
    // Audio-only: the words are never shown. A status line and the progress bar are the
    // only on-screen record of the read; the tracker still follows the audio for the buzz.
    bukRun.wordSpans = [];
    bukRun.revealIndex = -1;
    const notice = document.createElement('p');
    notice.className = 'text-sm font-mono font-black text-neonCyan uppercase tracking-widest';
    notice.textContent = 'Reading question\u2026';
    if (textEl) textEl.appendChild(notice);
    if (!bukRun.words.length) bukRun.wordStreamDone = true;
    return;
  }
  // Pre-create every word as a hidden text node so the reveal only toggles visibility:
  // the layout stays put and untrusted packet text stays text (never markup).
  bukRun.wordSpans = bukRun.words.map((word) => {
    const span = document.createElement('span');
    span.textContent = `${word.word} `;
    span.style.visibility = 'hidden';
    if (textEl) textEl.appendChild(span);
    return span;
  });
  bukRun.revealIndex = -1;
  if (!bukRun.words.length) bukRun.wordStreamDone = true;
}

function bukRevealUpTo(index) {
  const words = bukRun.words;
  const last = words.length - 1;
  const target = Math.max(-1, Math.min(index, last));
  if (target <= bukRun.revealIndex) return;
  // Audio-only reveals no words: only the progress bar moves.
  if (bukRun.displayMode !== 'audio-only') {
    for (let i = bukRun.revealIndex + 1; i <= target; i += 1) {
      if (bukRun.wordSpans[i]) bukRun.wordSpans[i].style.visibility = 'visible';
    }
  }
  bukRun.revealIndex = target;
  bukRun.shownWords = target + 1;
  const counter = bukEl('buk-word-count');
  if (counter) counter.textContent = bukRun.displayMode === 'audio-only'
    ? ''
    : `Word ${Math.max(1, target + 1)} of ${words.length}`;
  const bar = bukEl('buk-progress-bar');
  if (bar) bar.style.width = `${Math.min(100, Math.round(((target + 1) / Math.max(1, words.length)) * 100))}%`;
  if (target >= last) {
    bukRun.wordStreamDone = true;
    bukFinishDelivery();
  }
}

// Fallback for engines that never fire word boundaries: pace the reveal to a realistic
// spoken rate (not a fixed WPM) so the text tracks the audio as closely as it can. The
// first real boundary cancels this in favour of exact, audio-driven sync.
function bukStartFallbackReveal() {
  if (bukRun.fallbackTimer || bukRun.fallbackInterval) return;
  bukRun.fallbackTimer = setTimeout(() => {
    bukRun.fallbackTimer = null;
    if (bukRun.sawBoundary || !bukRun.delivering) return;
    const rate = (bukRun.utterance && bukRun.utterance.rate) || 1;
    const charsPerSecond = BUK_SPOKEN_CHARS_PER_SECOND * rate;
    bukRun.fallbackInterval = setInterval(() => {
      if (bukRun.sawBoundary || !bukRun.delivering) {
        clearInterval(bukRun.fallbackInterval); bukRun.fallbackInterval = null;
        return;
      }
      const elapsedSeconds = (performance.now() - bukRun.readStart) / 1000;
      bukRevealUpTo(bukWordIndexAt(bukRun.words, elapsedSeconds * charsPerSecond));
      if (bukRun.tracker) bukRun.tracker.noteCounted(bukRun.revealIndex);
    }, 120);
  }, 120);
}

function bukSpeak(text) {
  if (!window.speechSynthesis) {
    bukRun.speechDone = true;
    bukStartFallbackReveal();
    bukFinishDelivery();
    return;
  }
  bukStopSpeech();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.rate = 1.05;
  bukRun.utterance = utterance;
  bukRun.sawBoundary = false;
  let settled = false;
  let spoke = false;
  const settle = () => {
    if (settled) return;
    settled = true;
    if (bukRun.speechPoll) { clearInterval(bukRun.speechPoll); bukRun.speechPoll = null; }
    if (bukRun.fallbackTimer) { clearTimeout(bukRun.fallbackTimer); bukRun.fallbackTimer = null; }
    if (bukRun.fallbackInterval) { clearInterval(bukRun.fallbackInterval); bukRun.fallbackInterval = null; }
    bukRun.speechDone = true;
    bukRevealUpTo(bukRun.words.length - 1);
    bukFinishDelivery();
  };
  utterance.onstart = () => {
    spoke = true;
    if (bukRun.tracker) bukRun.tracker.noteStart();
    bukRevealUpTo(0);
    bukStartFallbackReveal();
  };
  utterance.onboundary = (event) => {
    if (!event || event.name !== 'word') return;
    // The boundary IS the audio's position, so the reveal follows the sound exactly.
    bukRun.sawBoundary = true;
    if (bukRun.fallbackInterval) { clearInterval(bukRun.fallbackInterval); bukRun.fallbackInterval = null; }
    const index = buzzWordIndexAt(bukRun.words, event.charIndex);
    bukRevealUpTo(index);
    if (bukRun.tracker) bukRun.tracker.noteBoundary(event.charIndex);
  };
  utterance.onend = () => {
    // A cancelled read must never be recorded as a completed word or a finished read.
    if (bukRun.tracker && bukRun.tracker.isCancelled()) return;
    if (bukRun.tracker) bukRun.tracker.noteNaturalEnd();
    settle();
  };
  utterance.onerror = () => settle();
  const begin = performance.now();
  bukRun.speechPoll = setInterval(() => {
    if (settled) { clearInterval(bukRun.speechPoll); bukRun.speechPoll = null; return; }
    if (window.speechSynthesis.speaking || window.speechSynthesis.pending) { spoke = true; return; }
    if (spoke || performance.now() - begin > 1500) settle();
  }, 200);
  window.speechSynthesis.speak(utterance);
}

function bukFinishDelivery() {
  if (bukRun.answered || !bukRun.delivering) return;
  if (!bukRun.wordStreamDone || !bukRun.speechDone) return;
  bukRun.delivering = false;
  if (bukRun.question && bukRun.question.phase === 'bonus') {
    bukStatus('buk-run-status', `Read complete \u2014 ${BUK_BONUS_WINDOW_S}s to type your bonus answer.`, '#00f3ff');
    bukOpenAnswerPanel(BUK_BONUS_WINDOW_S, 'bonus');
  } else {
    bukRun.windowOpen = true;
    bukStatus('buk-run-status', `Read complete \u2014 buzz window open (${BUK_TOSSUP_WINDOW_S.toFixed(1)}s)`, '#00f3ff');
    const buzzBtn = bukEl('buk-buzz-btn'); if (buzzBtn) buzzBtn.disabled = false;
    bukRun.windowTimer = setTimeout(() => {
      if (bukRun.answered) return;
      bukRun.windowOpen = false;
      bukSubmitToServer('', null, true, 'expired');  // no buzz: wrong, no answer
    }, BUK_TOSSUP_WINDOW_S * 1000);
  }
}

function bukBuzz() {
  if (!bukRun.question || bukRun.answered) return false;
  if (!bukRun.windowOpen && !bukRun.delivering) return false;
  if (bukRun.question.phase === 'bonus') return false;

  // THE rule: snapshot the word BEFORE cancelling the read.
  const snapshot = bukRun.tracker ? bukRun.tracker.snapshot() : null;
  if (bukRun.tracker) bukRun.tracker.markCancelled();
  bukStopDelivery();
  if (bukRun.windowTimer) { clearTimeout(bukRun.windowTimer); bukRun.windowTimer = null; }
  bukRun.windowOpen = false;
  bukRun.buzzAt = performance.now();
  const reactionSeconds = (bukRun.buzzAt - bukRun.readStart) / 1000;

  bukRun.pendingBuzz = { ...snapshot, reactionSeconds: Number(reactionSeconds.toFixed(3)) };
  const buzzBtn = bukEl('buk-buzz-btn'); if (buzzBtn) buzzBtn.disabled = true;
  const wordEl = bukEl('buk-buzz-word');
  if (wordEl) wordEl.textContent = snapshot && snapshot.word ? snapshot.word : '(no word)';
  const srcEl = bukEl('buk-buzz-source');
  if (srcEl) srcEl.textContent = snapshot ? `${snapshot.captureSource} \u2022 ${snapshot.wordsHeard}/${snapshot.totalWords} words` : '';
  bukStatus('buk-run-status', `Locked out at ${reactionSeconds.toFixed(2)}s on "${snapshot ? snapshot.word : ''}" \u2014 answer now`, '#ffaa00');
  bukOpenAnswerPanel(BUK_ANSWER_WINDOW_S, 'tossup');
  return true;
}

function bukOpenAnswerPanel(seconds, phase) {
  const panel = bukEl('buk-answer-panel');
  if (panel) panel.classList.remove('hidden');
  const input = bukEl('buk-answer-input');
  if (input) { input.value = ''; input.focus(); }
  bukRun.answerPhase = phase;
  bukRun.answering = true;
  const label = bukEl('buk-answer-timer');
  const deadline = performance.now() + seconds * 1000;
  bukRun.lockoutTimer = setInterval(() => {
    const remaining = Math.max(0, (deadline - performance.now()) / 1000);
    if (label) label.textContent = `${remaining.toFixed(1)}s`;
    if (remaining <= 0) {
      clearInterval(bukRun.lockoutTimer); bukRun.lockoutTimer = null;
      bukSubmitToServer(input ? input.value : '', bukRun.pendingBuzz, true, 'expired');
    }
  }, 100);
}

function bukSubmitTyped() {
  const input = bukEl('buk-answer-input');
  const typed = input ? input.value : '';
  const isBonus = bukRun.answerPhase === 'bonus';
  bukSubmitToServer(typed, isBonus ? null : bukRun.pendingBuzz, false, 'answered');
}

// Every submission goes to the server, which grades and is the only writer of the run.
async function bukSubmitToServer(typedAnswer, buzz, expired, outcome) {
  if (!bukRun.question || bukRun.submitting) return;
  bukRun.submitting = true;
  if (bukRun.lockoutTimer) { clearInterval(bukRun.lockoutTimer); bukRun.lockoutTimer = null; }
  bukStopDelivery();
  const now = performance.now();
  const submitMs = Math.max(0, Math.round(now - (bukRun.buzzAt || bukRun.readStart)));
  const firstKeystrokeMs = bukRun.firstKeystrokeAt ? Math.round(bukRun.firstKeystrokeAt - (bukRun.buzzAt || bukRun.readStart)) : null;
  const timing = {
    reactionSeconds: buzz ? buzz.reactionSeconds : null,
    firstKeystrokeMs,
    submitMs,
  };
  const isBonus = bukRun.question.phase === 'bonus';
  try {
    const result = await bukApi(`/api/challenges/${encodeURIComponent(bukRun.code)}/answer`, {
      method: 'POST',
      body: JSON.stringify({
        qIndex: bukRun.question.qIndex,
        phase: bukRun.question.phase,
        typedAnswer: typedAnswer || '',
        buzz: isBonus ? null : buzz,
        timing,
        expired: Boolean(expired),
        outcome,
      }),
    });
    bukRun.records.push({ qIndex: bukRun.question.qIndex, phase: bukRun.question.phase, buzz: isBonus ? null : buzz, typedAnswer, verdict: result.verdict, correct: result.correct, answer: result.answer });
    const feedback = bukEl('buk-feedback');
    if (feedback) {
      feedback.textContent = result.correct
        ? `Correct \u2014 "${result.answer}"`
        : `${expired ? (isBonus ? 'Bonus time expired.' : 'Expired.') : 'Incorrect.'} Answer: "${result.answer}"`;
      feedback.style.color = result.correct ? '#00ff66' : '#ff6b6b';
    }
    const panel = bukEl('buk-answer-panel'); if (panel) panel.classList.add('hidden');
    bukAdvance(result.next);
  } catch (err) {
    bukStatus('buk-run-status', err.message || 'Could not record that answer.', '#ff4d4d');
  } finally {
    bukRun.submitting = false;
    bukRun.answering = false;
  }
}

function bukAdvance(next) {
  if (!next || next.done) {
    bukFinishRun();
    return;
  }
  // Correct toss-up -> the bonus is next (same question); otherwise the next toss-up.
  const nextBtn = bukEl('buk-next-btn'); if (nextBtn) nextBtn.classList.remove('hidden');
  bukRun.pendingNext = next;
  bukRun.awaitingNext = true;
}

function bukNextQuestion() {
  const nextBtn = bukEl('buk-next-btn'); if (nextBtn) nextBtn.classList.add('hidden');
  const feedback = bukEl('buk-feedback'); if (feedback) feedback.textContent = '';
  if (bukRun.pendingNext) {
    const next = bukRun.pendingNext;      bukRun.pendingNext = null;
      bukRun.awaitingNext = false;
    bukStartQuestion(next);
  } else {
    bukFinishRun();
  }
}

function bukFinishRun() {
  bukRun.finished = true;
  bukStopDelivery();
  bukStatus('buk-run-status', 'Packet complete \u2014 your buzz words are below.', '#00ff66');
  // The play area gives way to your own summary; the captain's compile stays on the board.
  bukShowStage('done');
  bukRenderOwnSummary();
}

// The member sees their own buzz words (the captain's compile stays captain-only).
function bukRenderOwnSummary() {
  const holder = bukEl('buk-own-summary');
  if (!holder) return;
  holder.replaceChildren();
  if (!bukRun.records.length) {
    // A finished attempt reopened from a link: the buzz words live on the board, not here.
    const empty = document.createElement('p');
    empty.className = 'text-xs font-mono text-slate-500';
    empty.textContent = 'This attempt is already complete — the board holds every run.';
    holder.appendChild(empty);
    return;
  }
  const title = document.createElement('p');
  title.className = 'text-xs font-mono font-bold text-neonCyan uppercase tracking-widest mb-2';
  title.textContent = 'Your buzz words';
  holder.appendChild(title);
  for (const rec of bukRun.records) {
    const row = document.createElement('div');
    row.className = 'flex items-center gap-2 text-xs font-mono text-slate-300 border-b border-borderDark py-1';
    const q = document.createElement('span'); q.className = 'text-slate-500'; q.textContent = `Q${rec.qIndex + 1}`;
    const w = document.createElement('span'); w.className = 'text-neonCyan'; w.textContent = rec.buzz && rec.buzz.word ? rec.buzz.word : '\u2014';
    const s = document.createElement('span'); s.className = 'text-slate-500'; s.textContent = rec.buzz ? rec.buzz.captureSource : '';
    const v = document.createElement('span'); v.textContent = rec.verdict || '';
    v.style.color = rec.correct ? '#00ff66' : '#ff6b6b';
    row.append(q, w, s, v);
    holder.appendChild(row);
  }
}

// ============================================================
// 6. BOARD — the captain's compile
// ============================================================

function bukRenderBoard(data) {
  const holder = bukEl('buk-board');
  if (!holder) return;
  holder.replaceChildren();
  const runs = data.runs || [];
  const questions = data.questions || [];
  if (!runs.length) {
    const empty = document.createElement('p');
    empty.className = 'text-xs font-mono text-slate-500';
    empty.textContent = 'No runs yet \u2014 share the code, or use Seed demo runs (dev).';
    holder.appendChild(empty);
    return;
  }
  // The board is a data export, not a report. Every statistic it used to draw now lives
  // in the Analytics station, which reads this CSV back in \u2014 so the only control here
  // is the download itself.
  bukBoardCsvButton(holder, runs, questions);
}

// One flat row per toss-up attempt, carrying enough of the packet that the Analytics
// buzzboard view can rebuild the highlighted question cards and the player matrix.
function bukBoardCsvRows(runs, questions) {
  const rows = [[
    'Q_Num', 'Subject', 'Category', 'Type', 'QuestionText', 'Answer', 'Member',
    'BuzzWord', 'BuzzWordIndex', 'BuzzDepthPct', 'WordsHeard', 'TotalWords',
    'CaptureSource', 'ReactionSeconds', 'TypedAnswer', 'Verdict', 'BonusVerdict',
  ]];
  for (const run of runs) {
    for (const r of run.questions || []) {
      const question = questions[r.qIndex] || {};
      const depth = (typeof r.buzzWordIndex === 'number' && r.totalWords)
        ? Math.round(((r.buzzWordIndex + 1) / r.totalWords) * 100)
        : '';
      rows.push([
        r.qIndex + 1,
        r.subject || bukSubjectFor(r.category),
        r.category || question.category || '',
        r.type || question.type || '',
        question.tossup_text || '',
        question.tossup_answer || '',
        run.displayName || run.uid,
        r.buzzWord == null ? '' : r.buzzWord,
        r.buzzWordIndex == null ? '' : r.buzzWordIndex + 1,
        depth,
        r.wordsHeard == null ? '' : r.wordsHeard,
        r.totalWords == null ? '' : r.totalWords,
        r.captureSource || '',
        r.reactionSeconds == null ? '' : r.reactionSeconds,
        r.typedAnswer || '',
        r.verdict || '',
        r.bonusVerdict || '',
      ]);
    }
  }
  return rows;
}

function bukBoardCsvText(runs, questions) {
  const rows = bukBoardCsvRows(runs, questions);
  return rows.map((row) => row.map((cell) => {
    let value = String(cell);
    // A leading =, +, - or @ makes a spreadsheet treat the cell as a formula.
    if (/^[=+\-@\t\r]/.test(value)) value = `'${value}`;
    return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
  }).join(',')).join('\n');
}

function bukBoardCsvButton(holder, runs, questions) {
  const wrap = document.createElement('div');
  wrap.className = 'mt-4 flex flex-wrap items-center gap-3';
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'px-5 py-3 bg-black border border-neonCyan text-neonCyan text-xs font-mono font-black uppercase rounded-xl active:scale-95 transition';
  btn.textContent = 'Download CSV';
  btn.addEventListener('click', () => {
    const csv = bukBoardCsvText(runs, questions);
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'buzzboard.csv';
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  });
  const note = document.createElement('p');
  note.className = 'text-[11px] font-mono text-slate-500';
  note.textContent = 'Download it for a spreadsheet, or read it in Analytics \u2192 Buzzboard CSV by challenge code.';
  wrap.append(btn, note);
  holder.appendChild(wrap);
}
// ============================================================
// 7. WIRING
// ============================================================

async function bukLoadBoard(code) {
  try {
    bukStatus('buk-board-status', 'Loading board\u2026');
    const data = await bukApi(`/api/challenges/${encodeURIComponent(code)}/board`);
    bukRenderBoard(data);
    bukStatus('buk-board-status', `Board for ${data.meta.title} \u2014 ${data.runs.length} run(s).`, '#00ff66');
  } catch (err) {
    bukStatus('buk-board-status', err.status === 403 ? 'Only the captain can see this board.' : (err.message || 'Could not load the board.'), '#ff4d4d');
  }
}

async function bukSeedBoard(code) {
  try {
    await bukApi(`/api/challenges/${encodeURIComponent(code)}/seed`, { method: 'POST', body: '{}' });
    await bukLoadBoard(code);
  } catch (err) {
    bukStatus('buk-board-status', err.status === 404 ? 'Seeding is disabled in production.' : (err.message || 'Could not seed.'), '#ff4d4d');
  }
}

// ------------------------------------------------ the challenge screen's four stages
//
// Joining opens #/challenge/CODE, which shows the start button and nothing else; pressing
// it is what begins the packet, and the packet is read in the play area alone. The router
// owns which screen is visible, so this file only decides which stage that screen shows.

// "none" arms nothing: it is what an exit leaves behind, so re-entering the URL decides
// the stage again instead of reopening a panel nobody dismissed.
function bukShowStage(stage) {
  bukRun.stage = stage === 'none' ? null : stage;
  const stages = {
    join: 'challenge-stage-join', start: 'challenge-stage-start',
    run: 'buk-run', done: 'challenge-stage-done',
  };
  Object.keys(stages).forEach((key) => {
    const el = bukEl(stages[key]);
    if (el) el.classList.toggle('hidden', key !== stage);
  });
  const codeEl = bukEl('buk-screen-code');
  if (codeEl && bukRun.code) codeEl.textContent = bukRun.code.toUpperCase();
}

function bukScreenCode() {
  const el = bukEl('buk-screen-code');
  return el ? el.textContent.trim().toLowerCase() : '';
}

// An attempt this browser already claimed: re-entering the same URL must not restart it.
function bukIsEntered(code) {
  return Boolean(code) && bukRun.claimedCode === code && Boolean(bukRun.stage);
}

// Claim the single attempt, or resume the one already in progress.
async function bukClaimAttempt(code, displayName) {
  if (!code || !displayName) throw new Error('Enter a code and a display name.');
  await bukEnsureIdentity();
  const result = await bukApi(`/api/challenges/${encodeURIComponent(code)}/join`, {
    method: 'POST', body: JSON.stringify({ displayName }),
  });
  bukRun.code = code;
  bukRun.displayName = displayName;
  bukRun.claimedCode = code;
  bukRun.joinStatus = result.status;
  bukRun.records = [];
  bukRun.finished = false;
  return result;
}

function bukJoinError(err, fallback) {
  if (err.status === 409) return 'You have already used your attempt on this challenge.';
  if (err.status === 404) return 'That challenge code does not exist.';
  if (/anonymous|operation-not-allowed|sign/i.test(err.message || '')) {
    return 'Anonymous sign-in failed \u2014 enable Anonymous sign-in in the Firebase console.';
  }
  return err.message || fallback;
}

// The board's join card: claim the attempt, then hand over to the challenge's own URL.
async function bukJoinFromBoard() {
  const codeEl = bukEl('buk-member-code');
  const nameEl = bukEl('buk-member-name');
  const code = (codeEl ? codeEl.value : '').trim().toLowerCase();
  const displayName = (nameEl ? nameEl.value : '').trim();
  if (!code || !displayName) { bukStatus('buk-member-status', 'Enter a code and a display name.', '#ffaa00'); return; }
  try {
    bukStatus('buk-member-status', 'Joining\u2026');
    const result = await bukClaimAttempt(code, displayName);
    bukStatus('buk-member-status', result.status === 'resumed'
      ? 'Attempt found \u2014 ready to resume.' : 'Attempt locked \u2014 good luck!', '#00ff66');
    // The play screen is its own address, so the join carries the code into the URL. The
    // router dispatches app:view, which puts up the start stage; without it, do it here.
    if (typeof window.routerNavigate === 'function') window.routerNavigate(`#/challenge/${encodeURIComponent(code)}`);
    else bukShowStartStage();
  } catch (err) {
    bukStatus('buk-member-status', bukJoinError(err, 'Could not join.'), '#ff4d4d');
  }
}

// The challenge screen's own form: how a shared link is played.
async function bukJoinFromScreen() {
  const nameEl = bukEl('buk-join-name');
  const displayName = (nameEl ? nameEl.value : '').trim();
  const code = (bukRun.code || bukScreenCode() || '').toLowerCase();
  bukStatus('buk-join-status', 'Joining\u2026');
  try {
    const result = await bukClaimAttempt(code, displayName);
    bukStatus('buk-join-status', result.status === 'resumed' ? 'Resuming your attempt\u2026' : '', '#00ff66');
    bukShowStartStage();
  } catch (err) {
    bukStatus('buk-join-status', bukJoinError(err, 'Could not join.'), '#ff4d4d');
  }
}

// The start screen: which packet this is, how it is set up, and the button that begins it.
async function bukShowStartStage() {
  bukShowStage('start');
  const btn = bukEl('buk-start-btn');
  if (btn) btn.textContent = bukRun.joinStatus === 'resumed' ? 'Resume challenge' : 'Start challenge';
  if (!bukRun.code) return;
  try {
    bukRenderChallengeStart(await bukApi(`/api/challenges/${encodeURIComponent(bukRun.code)}`));
  } catch (err) {
    bukStatus('buk-start-status', err.message || 'Could not read the challenge.', '#ff4d4d');
  }
}

function bukRenderChallengeStart(meta) {
  bukRun.meta = meta;
  const set = (id, text) => { const el = bukEl(id); if (el) el.textContent = text; };
  set('buk-start-title', meta.title || 'Challenge');
  set('buk-start-code', String(meta.code || bukRun.code || '').toUpperCase());
  set('buk-start-count', String(meta.questionCount || 0));
  set('buk-start-bonuses', meta.readBonuses ? 'On (20s)' : 'Off');
  set('buk-start-display', meta.displayMode === 'audio-only' ? 'Audio only' : 'Words + audio');
  set('buk-screen-code', String(meta.code || bukRun.code || '').toUpperCase());
  const holder = bukEl('buk-start-subjects');
  if (!holder) return;
  holder.replaceChildren();
  const subjects = (meta.packetSummary || {}).subjects || {};
  Object.keys(subjects).forEach((name) => {
    const pill = document.createElement('span');
    pill.className = 'px-2 py-0.5 rounded-full bg-panel border border-borderDark text-[10px] font-mono text-slate-300';
    pill.textContent = `${name} ${subjects[name]}`;
    holder.appendChild(pill);
  });
}

// Start challenge: only here does the packet begin, and it begins in the play area alone.
async function bukStartChallenge() {
  if (!bukRun.code) return;
  const btn = bukEl('buk-start-btn');
  if (btn) btn.disabled = true;
  bukStatus('buk-start-status', 'Starting\u2026');
  try {
    const next = await bukApi(`/api/challenges/${encodeURIComponent(bukRun.code)}/next`);
    bukStatus('buk-start-status', '');
    if (next.done) { bukRun.finished = true; bukShowStage('done'); bukRenderOwnSummary(); return; }
    bukShowStage('run');
    bukStartQuestion(next);
  } catch (err) {
    bukStatus('buk-start-status', bukJoinError(err, 'Could not start the challenge.'), '#ff4d4d');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// Off the play screen, nothing of the run may keep going: the read is stopped, the timers
// are cleared and no stage stays armed. The attempt itself stays open on the server, so the
// challenge link resumes it.
function bukLeaveChallengeScreen() {
  if (!bukRun.question && !bukRun.delivering) return;
  bukStopDelivery();
  if (bukRun.windowTimer) { clearTimeout(bukRun.windowTimer); bukRun.windowTimer = null; }
  if (bukRun.lockoutTimer) { clearInterval(bukRun.lockoutTimer); bukRun.lockoutTimer = null; }
  bukRun.question = null;
  bukRun.answering = false;
  bukRun.awaitingNext = false;
  bukRun.pendingNext = null;
  bukShowStage('none');
}

// Leaving mid-packet is a real choice, so it asks.
function bukExitChallenge() {
  const midRun = Boolean(bukRun.question) && !bukRun.finished;
  if (midRun && !window.confirm('Leave the challenge? Your attempt stays open \u2014 the challenge link will resume it.')) return;
  bukLeaveChallengeScreen();
  bukShowStage('none');
  if (typeof window.routerNavigate === 'function') window.routerNavigate('#/board');
}

// #/challenge/ab12 opens this screen. A claimed attempt goes straight to the start button;
// a member who has not joined yet gets the join form with the code already filled in.
async function bukEnterChallenge(code) {
  const clean = String(code || '').trim().toLowerCase();
  if (!clean) { bukShowStage('join'); return; }
  bukRun.code = clean;
  if (bukIsEntered(clean)) return;
  // The board's join card already claimed this attempt: no second probe needed.
  if (bukRun.claimedCode === clean) { bukShowStartStage(); return; }
  const nameEl = bukEl('buk-join-name');
  if (nameEl && !nameEl.value && bukRun.displayName) nameEl.value = bukRun.displayName;
  bukShowStage('join');
  bukStatus('buk-join-status', 'Checking your attempt\u2026');
  try {
    await bukEnsureIdentity();
    // A claimed attempt answers /next; a member who has not joined is refused with 403.
    const next = await bukApi(`/api/challenges/${encodeURIComponent(clean)}/next`);
    bukRun.claimedCode = clean;
    bukRun.joinStatus = 'resumed';
    bukRun.records = [];
    if (next && next.done) { bukRun.finished = true; bukShowStage('done'); bukRenderOwnSummary(); return; }
    bukStatus('buk-join-status', '');
    bukShowStartStage();
  } catch (err) {
    if (err.status === 404) { bukStatus('buk-join-status', 'That challenge code does not exist.', '#ff4d4d'); return; }
    // 403 means "join this challenge before playing" \u2014 the honest answer is the join form.
    if (err.status === 403) { bukStatus('buk-join-status', ''); return; }
    bukStatus('buk-join-status', bukJoinError(err, 'Could not open the challenge.'), '#ff4d4d');
  }
}

async function bukCopyChallengeLink(code) {
  const link = `${window.location.origin}${window.location.pathname}#/challenge/${code}`;
  try {
    await navigator.clipboard.writeText(link);
    if (typeof showToast === 'function') showToast('Challenge link copied');
  } catch (err) {
    if (typeof showToast === 'function') showToast(link);
  }
}

function bukInit() {
  const createBtn = bukEl('buk-captain-create');
  if (createBtn) createBtn.addEventListener('click', bukCreateChallenge);
  const joinBtn = bukEl('buk-member-join');
  if (joinBtn) joinBtn.addEventListener('click', bukJoinFromBoard);
  const joinSubmit = bukEl('buk-join-submit');
  if (joinSubmit) joinSubmit.addEventListener('click', bukJoinFromScreen);
  const startBtn = bukEl('buk-start-btn');
  if (startBtn) startBtn.addEventListener('click', bukStartChallenge);
  const exitBtn = bukEl('buk-exit');
  if (exitBtn) exitBtn.addEventListener('click', () => { bukExitChallenge(); });
  const linkBtn = bukEl('buk-captain-link');
  if (linkBtn) linkBtn.addEventListener('click', () => {
    const codeEl = bukEl('buk-captain-code');
    const code = codeEl ? codeEl.textContent.trim() : '';
    if (code) bukCopyChallengeLink(code);
  });
  const joinName = bukEl('buk-join-name');
  if (joinName) {
    joinName.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); bukJoinFromScreen(); }
    });
  }
  // The router owns which screen is visible; this screen just has to fill itself in.
  document.addEventListener('app:view', (event) => {
    const detail = event.detail || {};
    if (detail.name !== 'challenge') {
      // Any other route — a link, or the back button — releases the play screen.
      bukLeaveChallengeScreen();
      return;
    }
    bukEnterChallenge(detail.params ? detail.params.code : '');
  });
  const buzzBtn = bukEl('buk-buzz-btn');
  if (buzzBtn) buzzBtn.addEventListener('click', () => { bukBuzz(); });
  const submitBtn = bukEl('buk-answer-submit');
  if (submitBtn) submitBtn.addEventListener('click', bukSubmitTyped);
  const answerInput = bukEl('buk-answer-input');
  if (answerInput) {
    answerInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); bukSubmitTyped(); }
    });
    answerInput.addEventListener('input', () => {
      if (!bukRun.firstKeystrokeAt) bukRun.firstKeystrokeAt = performance.now();
    });
  }
  const nextBtn = bukEl('buk-next-btn');
  if (nextBtn) nextBtn.addEventListener('click', bukNextQuestion);
  const loadBtn = bukEl('buk-board-load');
  if (loadBtn) loadBtn.addEventListener('click', () => {
    const code = (bukEl('buk-board-code') || {}).value || '';
    bukLoadBoard(code.trim());
  });
  const seedBtn = bukEl('buk-board-seed');
  if (seedBtn) seedBtn.addEventListener('click', () => {
    const code = (bukEl('buk-board-code') || {}).value || '';
    bukSeedBoard(code.trim());
  });
  // Keyboard controls (see the run legend): Space buzzes, Enter submits, N goes next.
  document.addEventListener('keydown', (event) => {
    const active = document.activeElement;
    const typing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA');
    if (event.code === 'Space') {
      // Streaming / active clue -> buzz (the Buzz button does the same).
      if (!typing && bukRun.question && !bukRun.answered && (bukRun.windowOpen || bukRun.delivering)) {
        event.preventDefault();
        bukBuzz();
      }
      return;
    }
    if (event.key === 'Enter') {
      // Buzzed -> submit the typed answer (the answer input handles its own Enter too).
      if (!typing && bukRun.answering) {
        event.preventDefault();
        bukSubmitTyped();
      }
      return;
    }
    if (event.key === 'n' || event.key === 'N') {
      // Post-question / lobby -> next question.
      if (!typing && bukRun.awaitingNext) {
        event.preventDefault();
        bukNextQuestion();
      }
    }
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', bukInit);
} else {
  bukInit();
}
