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

  // A buzz in the five seconds AFTER the read is not a buzz on any word: the member heard the
  // whole question, so there is no word to point at. Reported as an 'end' capture with no word
  // attached \u2014 recording the last word would put the buzz back inside the question and count
  // it as an interrupt on the board.
  function snapshotAtEnd() {
    return {
      word: null,
      wordIndex: null,
      charIndex: null,
      wordsHeard: totalWords,
      totalWords,
      captureSource: 'end',
      lastConfirmedWord: totalWords ? words[totalWords - 1].word : null,
      confidence: 'after the last word',
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
    snapshot, snapshotAtEnd, markCancelled, isCancelled, noteNaturalEnd,
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
  streamTimer: null, lockoutTimer: null, windowTimer: null, windowDeadline: 0, speechPoll: null,
  utterance: null, words: [], shownWords: 0, wordSpans: [], revealIndex: -1,
  readText: '',
  sawBoundary: false, fallbackTimer: null, fallbackInterval: null,
  answering: false, awaitingNext: false,
  // The last word the read was allowed to show. A buzz sets it, so the rest of the question
  // cannot appear while the member is answering it.
  frozenAt: null,
  records: [], finished: false,
  // The challenge screen: which stage is showing, and which attempt this browser claimed.
  stage: null, claimedCode: null, joinStatus: null, displayName: '',
};

const BUK_ANSWER_WINDOW_S = 5.0;
const BUK_BONUS_WINDOW_S = 20.0;
// The toss-up read ends with a five-second buzzpoint, not with the last word: the member has
// heard the whole question and may still buzz. Five seconds after the final word the toss-up
// is over and is recorded as expired.
const BUK_TOSSUP_WINDOW_S = 5.0;
// Realistic spoken pace (chars/sec at rate 1) for the reveal fallback when the engine gives
// no word boundaries. Matches the app's measured pace (SOLO_SPOKEN_CHARS_PER_SECOND).
const BUK_SPOKEN_CHARS_PER_SECOND = 12;

// A question this device cannot read aloud is not a question the member can buzz on: the buzz
// is supposed to log the word they HEARD. So the read is refused rather than faked out of the
// revealed text, and the attempt is left exactly where it is.
const BUK_READ_ALOUD_ERROR = 'This device cannot read the question aloud \u2014 open this challenge on a device with speech voices.';

function bukStopSpeech() {
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (bukRun.speechPoll) { clearInterval(bukRun.speechPoll); bukRun.speechPoll = null; }
}

// The buzzpoint is live for exactly one question, and only while that question is being read.
// It opens with the first word of the read and ends with the read: once the question is over
// there is nothing left to buzz on, so the button closes instead of staying armed against a
// question that has already been scored.
function bukOpenBuzzpoint() {
  bukRun.windowOpen = true;
  const buzzBtn = bukEl('buk-buzz-btn');
  if (buzzBtn) buzzBtn.disabled = false;
}

function bukEndBuzzpoint() {
  bukRun.windowOpen = false;
  bukRun.windowDeadline = 0;
  if (bukRun.windowTimer) { clearInterval(bukRun.windowTimer); bukRun.windowTimer = null; }
  const buzzBtn = bukEl('buk-buzz-btn');
  if (buzzBtn) buzzBtn.disabled = true;
  // The countdown belongs to the window, so it leaves with it.
  const timerEl = bukEl('buk-buzz-timer');
  if (timerEl) timerEl.textContent = '';
}

// After the read, the buzzpoint is still open for BUK_TOSSUP_WINDOW_S. The countdown is drawn
// on screen (and in the status line) so the window is visible rather than inferred from a
// button that stays enabled for a few more seconds. At zero the toss-up is over: the buzzpoint
// closes and the question is recorded as expired.
function bukStartBuzzDeadline() {
  bukOpenBuzzpoint();
  const timerEl = bukEl('buk-buzz-timer');
  const deadline = performance.now() + BUK_TOSSUP_WINDOW_S * 1000;
  bukRun.windowDeadline = deadline;
  const tick = () => {
    const remaining = Math.max(0, (deadline - performance.now()) / 1000);
    if (timerEl) timerEl.textContent = `${remaining.toFixed(1)}s left to buzz`;
    bukStatus('buk-run-status', `Read complete \u2014 ${remaining.toFixed(1)}s to buzz.`, '#ffaa00');
    if (remaining <= 0) {
      bukEndBuzzpoint();
      bukSubmitToServer('', null, true, 'expired'); // no buzz in the window: wrong, no answer
    }
  };
  bukRun.windowTimer = setInterval(tick, 100);
  tick();
}

// A fresh question starts with no buzz on record: the previous question's word must not sit
// under a live button and read as if it belonged to the question being read now.
function bukResetBuzzpoint() {
  bukEndBuzzpoint();
  bukRun.pendingBuzz = null;
  bukRun.buzzAt = 0;
  const wordEl = bukEl('buk-buzz-word'); if (wordEl) wordEl.textContent = '';
  const srcEl = bukEl('buk-buzz-source'); if (srcEl) srcEl.textContent = '';
}

function bukStopDelivery() {
  bukRun.delivering = false;
  if (bukRun.streamTimer) { clearInterval(bukRun.streamTimer); bukRun.streamTimer = null; }
  if (bukRun.fallbackTimer) { clearTimeout(bukRun.fallbackTimer); bukRun.fallbackTimer = null; }
  if (bukRun.fallbackInterval) { clearInterval(bukRun.fallbackInterval); bukRun.fallbackInterval = null; }
  bukStopSpeech();
}

// A multiple-choice question is unreadable without its choices: the stem ends in a colon and
// the choices ARE the rest of the question. They are folded into the question text as its last
// lines, so the read is one string with one word list — the speech, the word-by-word reveal and
// the buzz-point tracker all run straight through the choices instead of stopping at the stem.
function bukReadText(question) {
  const stem = String((question && question.text) || '').trimEnd();
  const options = (Array.isArray(question && question.options) ? question.options : [])
    .map((option) => String(option == null ? '' : option).trim())
    .filter(Boolean);
  if (!options.length) return stem;
  // Each choice is its own line. The tokens and the utterance ignore the line breaks, so this
  // formatting changes nothing about what is spoken or how far the read has got.
  return `${stem}\n\n${options.join('\n')}`;
}

// Stream the words on a timer (the deterministic counted source) and speak aloud in
// parallel. A read is over only when BOTH channels are.
function bukStartQuestion(question) {
  bukRun.question = question;
  bukRun.answered = false;
  bukResetBuzzpoint();
  // Buzzable from the first word: the buzzpoint runs for the whole read, choices included.
  if (question.phase !== 'bonus') bukOpenBuzzpoint();
  bukRun.delivering = true;
  bukRun.wordStreamDone = false;
  bukRun.speechDone = false;
  bukRun.firstKeystrokeAt = 0;
  bukRun.buzzAt = 0;
  // The choices are part of the question being read, not a caption under it.
  const readText = bukReadText(question);
  bukRun.readText = readText;
  bukRun.words = buzzTokenizeWords(readText);
  bukRun.shownWords = 0;
  bukRun.frozenAt = null;
  // The captain chose this at creation; the server repeats it on every question payload.
  bukRun.displayMode = question.displayMode === 'audio-only' ? 'audio-only' : 'hybrid';
  bukRun.tracker = createBuzzWordTracker(readText, { wordsPerSecond: 2.6 });

  if (question.phase !== 'bonus') {
    bukStatus('buk-run-status', 'Reading \u2014 buzz any time before the question is over.', '#00f3ff');
  }
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
  bukSpeak(readText);
}

function bukPrepareReveal() {
  const textEl = bukEl('buk-question-text');
  if (textEl) textEl.replaceChildren();
  bukRun.wordSpans = [];
  if (bukRun.displayMode === 'audio-only') {
    // Audio-only: the words are never shown. A status line and the progress bar are the
    // only on-screen record of the read; the tracker still follows the audio for the buzz.
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
  // Each span carries the text between the previous word and itself, so a choice's line break
  // and its label (the "W)" of "W) dxy") are revealed together with that choice's first word.
  // A choice that has not been read yet has neither text nor a line of its own, while the words
  // are still one continuous stream. The block keeps the breaks (white-space: pre-line).
  let previousEnd = 0;
  bukRun.words.forEach((word) => {
    const span = document.createElement('span');
    // The gap is the read's own text between the previous word and this one: the spaces, the
    // punctuation (the ")" in "W) dxy") and the line breaks that begin each choice. Nothing is
    // invented, so the block reads exactly as the question does.
    span.textContent = `${bukRun.readText.slice(previousEnd, word.start)}${word.word}`;
    span.style.visibility = 'hidden';
    if (textEl) textEl.appendChild(span);
    bukRun.wordSpans.push(span);
    previousEnd = word.end;
  });
  // Whatever follows the last word (a closing colon, say) belongs to that word.
  const tail = bukRun.readText.slice(previousEnd);
  if (tail && bukRun.wordSpans.length) {
    bukRun.wordSpans[bukRun.wordSpans.length - 1].textContent += tail;
  }
  bukRun.revealIndex = -1;
  if (!bukRun.words.length) bukRun.wordStreamDone = true;
}

function bukRevealUpTo(index) {
  const words = bukRun.words;
  const last = words.length - 1;
  // A buzzed read stops where it was: whatever asks for more words afterwards — the read
  // settling, or the paced fallback — the rest of the question stays off the screen until the
  // answer is submitted.
  const ceiling = bukRun.frozenAt === null ? last : Math.min(last, bukRun.frozenAt);
  const target = Math.max(-1, Math.min(index, ceiling));
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
      bukRevealUpTo(buzzWordIndexAt(bukRun.words, elapsedSeconds * charsPerSecond));
      if (bukRun.tracker) bukRun.tracker.noteCounted(bukRun.revealIndex);
    }, 120);
  }, 120);
}

// A device with no speech engine at all cannot play this mode. Voices that have not loaded yet
// are NOT grounds to refuse: Chrome and iOS report an empty list until speech is first used, so
// the read itself is the real test (see the poll in bukSpeak and bukReadUnavailable).
function bukCanReadAloud() {
  return Boolean(window.speechSynthesis) && typeof window.SpeechSynthesisUtterance === 'function';
}

// The read could not be spoken. Nothing is revealed, no buzz window opens and no answer is
// recorded \u2014 the run stays on this question, so the member can resume it on a device that
// speaks. Measured before this existed: the client declared the read complete 1.5s in, put the
// whole question on screen and then recorded a timeout against a question nobody had heard.
function bukReadUnavailable() {
  bukStopDelivery();
  if (bukRun.lockoutTimer) { clearInterval(bukRun.lockoutTimer); bukRun.lockoutTimer = null; }
  bukEndBuzzpoint();
  bukRun.answering = false;
  bukRun.awaitingNext = false;
  const panel = bukEl('buk-answer-panel');
  if (panel) panel.classList.add('hidden');
  const feedback = bukEl('buk-feedback');
  if (feedback) { feedback.textContent = BUK_READ_ALOUD_ERROR; feedback.style.color = '#ff4d4d'; }
  bukStatus('buk-run-status', 'Read refused \u2014 nothing was recorded. Your attempt is untouched: open this challenge again on a device with speech voices.', '#ff4d4d');
  return true;
}

function bukSpeak(text) {
  if (!window.speechSynthesis) {
    bukReadUnavailable();
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
  // A buzz cancels the read, and Chromium reports a cancelled utterance as an error
  // ('interrupted') rather than an end — so settling here would reveal the whole question the
  // instant a member buzzed. The cancelled read is refused on both channels.
  utterance.onerror = () => {
    if (bukRun.tracker && bukRun.tracker.isCancelled()) return;
    // An error before a single word came out is this device failing to speak, not a read that
    // finished, so it is refused instead of settled.
    if (!spoke) { bukReadUnavailable(); return; }
    settle();
  };
  const begin = performance.now();
  bukRun.speechPoll = setInterval(() => {
    if (settled) { clearInterval(bukRun.speechPoll); bukRun.speechPoll = null; return; }
    if (window.speechSynthesis.speaking || window.speechSynthesis.pending) { spoke = true; return; }
    // It spoke and stopped: the read is over even when no 'end' event arrives.
    if (spoke) { settle(); return; }
    // It never spoke at all: the engine accepted the utterance and nothing came of it.
    if (performance.now() - begin > 1500) bukReadUnavailable();
  }, 200);
  window.speechSynthesis.speak(utterance);
}

function bukFinishDelivery() {
  if (bukRun.answered || !bukRun.delivering) return;
  if (!bukRun.wordStreamDone || !bukRun.speechDone) return;
  bukRun.delivering = false;
  if (bukRun.question && bukRun.question.phase === 'bonus') {
    // A bonus is not buzzable, so its read hands straight over to the 20-second answer window.
    bukEndBuzzpoint();
    bukStatus('buk-run-status', `Read complete \u2014 ${BUK_BONUS_WINDOW_S.toFixed(0)}s to type your bonus answer.`, '#00f3ff');
    bukOpenAnswerPanel(BUK_BONUS_WINDOW_S, 'bonus');
  } else {
    // A toss-up is still buzzable for five seconds after the last word.
    bukStartBuzzDeadline();
  }
}

function bukBuzz() {
  if (!bukRun.question || bukRun.answered) return false;
  if (!bukRun.windowOpen && !bukRun.delivering) return false;
  if (bukRun.question.phase === 'bonus') return false;

  // A buzz in the post-read window was heard against the whole question, so it is captured
  // at the END: no word, capture source 'end'. Buzzing before the read ends still snapshots
  // the word the member was on.
  const atEnd = !bukRun.delivering && bukRun.wordStreamDone && bukRun.speechDone;
  // THE rule: snapshot the word BEFORE cancelling the read.
  const snapshot = bukRun.tracker
    ? (atEnd ? bukRun.tracker.snapshotAtEnd() : bukRun.tracker.snapshot())
    : null;
  if (bukRun.tracker && !atEnd) bukRun.tracker.markCancelled();
  // Freeze before the read is stopped: the words on screen now are the ones the member heard.
  bukRun.frozenAt = bukRun.revealIndex;
  bukStopDelivery();
  bukEndBuzzpoint();
  bukRun.buzzAt = performance.now();
  const reactionSeconds = (bukRun.buzzAt - bukRun.readStart) / 1000;

  bukRun.pendingBuzz = { ...snapshot, reactionSeconds: Number(reactionSeconds.toFixed(3)) };
  const wordEl = bukEl('buk-buzz-word');
  if (wordEl) {
    wordEl.textContent = snapshot && snapshot.captureSource === 'end'
      ? '[end]'
      : (snapshot && snapshot.word ? snapshot.word : '(no word)');
  }
  const srcEl = bukEl('buk-buzz-source');
  if (srcEl) srcEl.textContent = snapshot ? `${snapshot.captureSource} \u2022 ${snapshot.wordsHeard}/${snapshot.totalWords} words` : '';
  bukStatus('buk-run-status', atEnd
    ? `Buzzed at the end of the question \u2014 answer now`
    : `Locked out at ${reactionSeconds.toFixed(2)}s on "${snapshot ? snapshot.word : ''}" \u2014 answer now`, '#ffaa00');
  bukOpenAnswerPanel(BUK_ANSWER_WINDOW_S, 'tossup');
  return true;
}

// The answer is in, so the rest of the question is no longer a secret: a buzzed read reveals
// what it had not reached yet, exactly as a completed read shows its text.
function bukRevealFullQuestion() {
  if (bukRun.frozenAt === null) return;
  bukRun.frozenAt = null;
  bukRevealUpTo((bukRun.words || []).length - 1);
}

function bukOpenAnswerPanel(seconds, phase) {
  const panel = bukEl('buk-answer-panel');
  if (panel) panel.classList.remove('hidden');
  const input = bukEl('buk-answer-input');
  if (input) { input.value = ''; input.focus(); }
  bukRun.answerPhase = phase;
  bukRun.answering = true;
  const title = bukEl('buk-answer-title');
  if (title) title.textContent = phase === 'bonus' ? `Bonus answer \u2014 ${seconds.toFixed(0)}s` : `Type your answer \u2014 ${seconds.toFixed(0)}s`;
  const label = bukEl('buk-answer-timer');
  // Show the full window at once: the label used to say 5.0s until the first tick, so a bonus
  // opened looking like a toss-up answer window.
  if (label) label.textContent = `${seconds.toFixed(1)}s`;
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
  // The answer closes the question: no buzz may start on it after this point, and the button
  // must not stay live while the next question is being fetched.
  bukRun.answered = true;
  bukEndBuzzpoint();
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
    bukRevealFullQuestion();
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
    const w = document.createElement('span'); w.className = 'text-neonCyan';
    // A buzz after the read is marked at the end, not on the question's last word.
    w.textContent = rec.buzz && rec.buzz.captureSource === 'end'
      ? '[end]'
      : (rec.buzz && rec.buzz.word ? rec.buzz.word : '\u2014');
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
      // The exported text is the text that was read: choices included, as their own lines.
      // Analytics rebuilds the card from this, so a buzz that landed in a choice still has a
      // word to sit on instead of pointing past the end of a stem-only question.
      const questionText = bukReadText({ text: question.tossup_text || '', options: question.tossup_options || [] });
      const depth = (typeof r.buzzWordIndex === 'number' && r.totalWords)
        ? Math.round(((r.buzzWordIndex + 1) / r.totalWords) * 100)
        : '';
      rows.push([
        r.qIndex + 1,
        r.subject || bukSubjectFor(r.category),
        r.category || question.category || '',
        r.type || question.type || '',
        questionText,
        question.tossup_answer || '',
        run.displayName || run.uid,
        // An end-of-question buzz has no word: it is marked [end] with no index, so the
        // captain's export cannot read it as a buzz on the final word.
        r.captureSource === 'end' ? '[end]' : (r.buzzWord == null ? '' : r.buzzWord),
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
  // Definitively unable to speak: refuse before the packet begins, so nothing is served and the
  // member's attempt is untouched.
  if (!bukCanReadAloud()) {
    bukStatus('buk-start-status', BUK_READ_ALOUD_ERROR, '#ff4d4d');
    if (btn) btn.disabled = false;
    return;
  }
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
  bukEndBuzzpoint();
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
    // A hidden answer input keeps focus after its panel closes; it cannot receive typing, so
    // it must not swallow the Space that buzzes on the next question either.
    const typing = active && (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')
      && active.offsetParent !== null;
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
