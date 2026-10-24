// ============================================================
// BUZZBOARD ANALYTICS — the Analytics station's second data source.
//
// The Buzzpoint Board no longer draws statistics; it downloads a CSV. This view reads
// that CSV (from any number of boards) back in and rebuilds the two pages the captain
// asked for:
//
//   Question Buzzpoints  the packet's questions with every buzz highlighted in place
//   Numerical Data       per-player subject matrix, team coverage meters and the draft
//
// Every value here comes from an uploaded file, so it is untrusted: everything is
// written with createElement / textContent and never as markup.
// ============================================================

const BBX_MAX_FILE_BYTES = 10 * 1024 * 1024;
const BBX_MAX_FILES = 20;
const BBX_SQUAD_SIZE = 4;
const BBX_CORRECT_POINTS = 4;
const BBX_NEG_POINTS = -4;
const BBX_PPTH_CEILING = 4;

const BBX_SUBJECT_ORDER = [
  'Biology', 'Chemistry', 'Physics', 'Math', 'Earth and Space', 'Energy', 'General Science',
];
const BBX_SUBJECT_SHORT = {
  Biology: 'BIO', Chemistry: 'CHM', Physics: 'PHY', Math: 'MTH',
  'Earth and Space': 'ESS', Energy: 'EGY', 'General Science': 'GEN',
};
// Packet categories fold onto the drill's design subjects exactly as the board's own
// subject grid does (mirrors SOLO_SUBJECT_ALIASES in static/js/solo.js).
const BBX_SUBJECT_ALIASES = {
  'life science': 'Biology',
  biology: 'Biology',
  'physical science': 'Physics',
  physics: 'Physics',
  chemistry: 'Chemistry',
  'earth science': 'Earth and Space',
  'earth and space': 'Earth and Space',
  math: 'Math',
  mathematics: 'Math',
  energy: 'Energy',
  'general science': 'General Science',
};
const BBX_TEAM_ACCENTS = { 1: '#00f3ff', 2: '#ff0055', 3: '#00ff66' };
const BBX_MEMBER_COLORS = ['#00f3ff', '#ffaa00', '#00ff66', '#ff0055', '#b026ff', '#2dd4bf', '#f472b6', '#facc15'];

const BBX_TAB_ON = 'px-3 py-1.5 rounded-lg text-[11px] font-mono font-black uppercase border transition bg-neonAmber text-black border-neonAmber';
const BBX_TAB_OFF = 'px-3 py-1.5 rounded-lg text-[11px] font-mono font-black uppercase border transition bg-black text-slate-300 border-borderDark hover:text-white';
const BBX_PAGE_ON = 'px-3 py-1.5 rounded-lg text-[11px] font-mono font-black uppercase border transition bg-neonCyan text-black border-neonCyan';
const BBX_PAGE_OFF = 'px-3 py-1.5 rounded-lg text-[11px] font-mono font-black uppercase border transition bg-black text-slate-300 border-borderDark hover:text-white';

const bbxState = {
  rows: [],
  questions: new Map(),
  players: new Map(),
  files: new Set(),
  subjects: [],
  page: 'questions',
  selectedPlayer: 'ALL',
  showBuzzpoints: true,
  showAnswers: false,
  subjectFilter: 'ALL',
  search: '',
  sortKey: 'ppth',
  sortAsc: false,
};

// ------------------------------------------------------------ small helpers

function bbxEl(id) { return document.getElementById(id); }

function bbxAppend(parent, tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined && text !== null) el.textContent = String(text);
  if (parent) parent.appendChild(el);
  return el;
}

function bbxNumber(value) {
  const parsed = parseFloat(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function bbxRatio(numerator, denominator) {
  if (!denominator) return null;
  return (numerator / denominator) * 100;
}

function bbxPctText(value) {
  return value === null ? '\u2014' : `${Math.round(value)}%`;
}

function bbxStatus(message, tone) {
  const el = bbxEl('bbx-status-text');
  if (!el) return;
  el.textContent = message;
  el.className = 'text-xs font-mono uppercase';
  el.classList.add(tone === 'bad' ? 'text-neonPink' : (tone === 'good' ? 'text-neonGreen' : (tone === 'busy' ? 'text-neonAmber' : 'text-slate-400')));
}

function bbxVerdict(row) {
  const raw = String(row['Verdict'] || '').toLowerCase();
  if (raw.includes('wrong')) return 'wrong';
  if (raw.includes('correct')) return 'correct';
  return '';
}

function bbxSubjectFor(category) {
  const raw = String(category || '').trim();
  return BBX_SUBJECT_ALIASES[raw.toLowerCase()] || raw;
}

// ------------------------------------------------------------ upload

const bbxDropZone = bbxEl('bbx-drop-zone');
const bbxFileInput = bbxEl('bbx-file-input');

if (bbxDropZone) {
  bbxDropZone.addEventListener('dragover', (e) => { e.preventDefault(); bbxDropZone.classList.add('border-neonAmber'); });
  bbxDropZone.addEventListener('dragleave', () => { bbxDropZone.classList.remove('border-neonAmber'); });
  bbxDropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    bbxDropZone.classList.remove('border-neonAmber');
    if (e.dataTransfer && e.dataTransfer.files.length) bbxHandleFiles(e.dataTransfer.files);
  });
}

if (bbxFileInput) {
  bbxFileInput.addEventListener('change', (e) => {
    if (e.target.files && e.target.files.length) bbxHandleFiles(e.target.files);
  });
}

async function bbxHandleFiles(files) {
  if (files.length > BBX_MAX_FILES) {
    bbxStatus(`Select no more than ${BBX_MAX_FILES} buzzboard files at a time.`, 'bad');
    return;
  }
  bbxStatus('PROCESSING BUZZBOARD CSV...', 'busy');
  let accepted = 0;
  for (let i = 0; i < files.length; i += 1) {
    const file = files[i];
    if (!file.name.toLowerCase().endsWith('.csv')) continue;
    if (bbxState.files.has(file.name)) continue;
    if (file.size > BBX_MAX_FILE_BYTES) {
      bbxStatus(`${file.name} is larger than the 10 MB limit.`, 'bad');
      continue;
    }
    try {
      const text = await file.text();
      const added = bbxIngestCsv(text);
      if (added) {
        bbxState.files.add(file.name);
        accepted += 1;
      }
    } catch (error) {
      bbxStatus(`${file.name}: ${error.message}`, 'bad');
    }
  }
  bbxFinishIngest(accepted);
}

// Every ingest ends here — a dropped file, or a board read from its code — so the badge
// and the status line can never disagree with what is on screen. A live refresh comes through
// here too, so one function owns the badge no matter which route filled the rows.
function bbxFinishIngest(accepted) {
  if (accepted) bbxBuildModels();
  bbxRenderAll();
  bbxUpdateCountBadge();
  if (accepted) bbxStatus(`BUZZBOARD ANALYTICS COMPILED \u2014 ${bbxState.rows.length} attempts`, 'good');
}

function bbxUpdateCountBadge() {
  const badge = bbxEl('bbx-count-badge');
  if (!badge) return;
  badge.style.display = bbxState.rows.length ? 'block' : 'none';
  badge.textContent = `${bbxState.rows.length} ROWS LOADED`;
}

// Read the board export into flat attempt records. The parser is the same hardened CSV
// reader the match analytics uses (quotes, embedded commas, newlines, size caps).
function bbxIngestCsv(csvText) {
  const rows = parseCSVRows(csvText);
  if (!rows.length) return 0;
  const headers = rows[0].map((header) => header.trim());
  const required = ['Q_Num', 'Member'];
  for (const name of required) {
    if (!headers.includes(name)) throw new Error(`Not a buzzboard CSV \u2014 missing the ${name} column`);
  }
  let added = 0;
  for (const values of rows.slice(1)) {
    const row = {};
    headers.forEach((header, i) => { row[header] = values[i] === undefined ? '' : String(values[i]).trim(); });
    const qNum = bbxNumber(row['Q_Num']);
    const member = row['Member'] || '';
    if (qNum === null || !member) continue;
    const totalWords = bbxNumber(row['TotalWords']);
    const buzzIndex = bbxNumber(row['BuzzWordIndex']);
    let depth = bbxNumber(row['BuzzDepthPct']);
    if (depth === null && buzzIndex !== null && totalWords) depth = (buzzIndex / totalWords) * 100;
    bbxState.rows.push({
      qNum: Math.round(qNum),
      questionText: row['QuestionText'] || '',
      answer: row['Answer'] || '',
      category: row['Category'] || '',
      subject: row['Subject'] || bbxSubjectFor(row['Category']),
      type: row['Type'] || '',
      member,
      buzzWord: row['BuzzWord'] || '',
      buzzIndex: buzzIndex === null ? null : Math.round(buzzIndex),
      depth,
      wordsHeard: bbxNumber(row['WordsHeard']),
      totalWords,
      captureSource: row['CaptureSource'] || '',
      reactionSeconds: bbxNumber(row['ReactionSeconds']),
      typedAnswer: row['TypedAnswer'] || '',
      verdict: bbxVerdict(row),
      bonusVerdict: String(row['BonusVerdict'] || '').toLowerCase(),
      uid: String(row['RunUid'] || ''),
      reported: String(row['Reported'] || '').toLowerCase(),
      reportReason: String(row['ReportReason'] || ''),
    });
    added += 1;
  }
  return added;
}

// ------------------------------------------------------------ load by code
//
// The captain's board already exists on the server, so this station can read it straight
// from the challenge code: GET /api/challenges/{code}/board returns the runs and the packet,
// and the board's own exporter turns those into the very rows a downloaded buzzboard.csv
// holds. Both routes therefore end in the same parser, and a fetched board cannot analyse
// differently from an uploaded one.
//
// A challenge is live while members are still playing it, so a board read this way is not a
// snapshot: it is re-read on an interval and its rows are replaced in place. Rows carry the
// code that produced them, which is what makes a refresh safe — it can neither double-count a
// run nor disturb a CSV dropped by hand.

async function bbxApi(path, options) {
  const opts = options || {};
  const token = window.getFirebaseIdToken ? await window.getFirebaseIdToken() : null;
  const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  // A board is live data, never a cached document.
  const response = await fetch(path, { ...opts, headers, cache: 'no-store' });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(result.detail || 'Request failed');
    error.status = response.status;
    throw error;
  }
  return result;
}

function bbxBoardError(error, code) {
  const detail = String(error.message || '');
  // The board route answers 403 for both "not the captain" and "no such code"; the server's
  // own wording says which one it was, so it is shown as it came.
  if (error.status === 403 || error.status === 404) {
    return detail || `Only ${code.toUpperCase()}'s captain can load that board.`;
  }
  if (error.status === 401) return 'Sign in as the captain to load a board by code.';
  // 503 covers both an unreachable challenge service and unverifiable auth: the server's
  // wording is the more precise of the two, so it wins.
  if (error.status === 503) return detail || 'The challenge service is unavailable right now.';
  return detail || 'Could not load that board.';
}

// The board's own exporter, so a fetched board is exactly the CSV the download button writes.
function bbxBoardCsvText(board) {
  if (typeof bukBoardCsvText !== 'function') throw new Error('The board exporter is not loaded.');
  return bukBoardCsvText(board.runs || [], board.questions || []);
}

// Attempts the board says are still in play: what the live pill counts as "playing".
function bbxPlayingRuns(board) {
  return (board.runs || []).filter((run) => run.status !== 'finished').length;
}

// The rows one board contributes, tagged with the code that produced them. Tagging is what
// makes a refresh a replacement: only this board's rows are dropped and re-read, so a board
// that appends can never double-count a run and a hand-loaded CSV is never disturbed.
function bbxIngestBoardCsv(code, csvText) {
  const source = `challenge:${code}`;
  const before = bbxState.rows.length;
  const added = bbxIngestCsv(csvText);
  for (let i = before; i < bbxState.rows.length; i += 1) bbxState.rows[i].source = source;
  return added;
}

// ------------------------------------------------------------ live board
//
// While the captain has this station open, a board loaded by code is re-read on an interval so
// the breakdown fills in as members play instead of waiting for a manual reload. The poll is
// deliberately narrow — this station, a visible tab, the buzzboard page — and the pill is the
// whole control: it says what the board is doing and stops or resumes it in one click.

const BBX_LIVE_REFRESH_MS = 15000;
const BBX_LIVE_ON = 'bg-black border border-neonGreen text-neonGreen px-2 py-1 rounded text-[10px] font-mono font-black uppercase tracking-wider transition hover:bg-neonGreen/10 active:scale-95';
const BBX_LIVE_OFF = 'bg-black border border-neonAmber text-neonAmber px-2 py-1 rounded text-[10px] font-mono font-black uppercase tracking-wider transition hover:bg-neonAmber/10 active:scale-95';

const bbxLive = {
  timer: null,
  paused: false,
  updatedAt: 0,
  // code -> { csv, playing }: the last board read, so an unchanged board redraws nothing.
  boards: new Map(),
};

function bbxLivePill() { return bbxEl('bbx-live-toggle'); }

function bbxLivePlaying() {
  let playing = 0;
  bbxLive.boards.forEach((entry) => { playing += entry.playing; });
  return playing;
}

// The pill is written from the live state and nowhere else, so the text can never claim the
// board is following the members when the poll is stopped.
function bbxRenderLivePill() {
  const pill = bbxLivePill();
  if (!pill) return;
  if (!bbxLive.boards.size) {
    pill.classList.add('hidden');
    return;
  }
  pill.classList.remove('hidden');
  const when = bbxLive.updatedAt ? new Date(bbxLive.updatedAt).toLocaleTimeString() : '\u2014';
  if (bbxLive.paused) {
    pill.className = BBX_LIVE_OFF;
    pill.textContent = `Paused \u00b7 updated ${when}`;
    pill.title = 'This board has stopped following the challenge. Click to resume.';
    return;
  }
  pill.className = BBX_LIVE_ON;
  pill.textContent = `Live \u00b7 ${bbxLivePlaying()} playing \u00b7 ${when}`;
  pill.title = `Re-reading the board from the challenge every ${Math.round(BBX_LIVE_REFRESH_MS / 1000)} seconds. Click to pause.`;
}

function bbxLiveStart() {
  if (bbxLive.paused || bbxLive.timer !== null || !bbxLive.boards.size) return;
  bbxLive.timer = setInterval(bbxLiveTick, BBX_LIVE_REFRESH_MS);
  bbxRenderLivePill();
}

function bbxLiveStop() {
  if (bbxLive.timer !== null) { clearInterval(bbxLive.timer); bbxLive.timer = null; }
  bbxRenderLivePill();
}

function bbxLivePause() { bbxLive.paused = true; bbxLiveStop(); }

function bbxLiveResume() {
  bbxLive.paused = false;
  bbxLiveStart();
  bbxLiveTick();  // catch up at once rather than waiting out the interval
}

// Re-read one board and replace that board's own rows. The rest of the screen is left alone:
// the captain's team draft, the open page, the filters and any hand-loaded CSVs survive, and
// a board that has not changed is not redrawn at all.
async function bbxRefreshChallengeBoard(code) {
  const entry = bbxLive.boards.get(code);
  if (!entry) return false;
  try {
    const board = await bbxApi(`/api/challenges/${encodeURIComponent(code)}/board`);
    bbxLive.updatedAt = Date.now();
    entry.playing = bbxPlayingRuns(board);
    const csv = bbxBoardCsvText(board);
    if (csv === entry.csv) {
      // Nobody has played since the last read: leave the screen exactly as it is.
      bbxRenderLivePill();
      return false;
    }
    entry.csv = csv;
    const source = `challenge:${code}`;
    const before = bbxState.rows.length;
    bbxState.rows = bbxState.rows.filter((row) => row.source !== source);
    const replaced = before - bbxState.rows.length;
    const added = bbxIngestBoardCsv(code, csv);
    bbxBuildModels();
    bbxRenderAll();
    bbxUpdateCountBadge();
    const fresh = added - replaced;
    bbxStatus(fresh > 0
      ? `Board ${code.toUpperCase()} updated \u2014 ${fresh} new attempt${fresh === 1 ? '' : 's'}.`
      : `Board ${code.toUpperCase()} updated.`, 'good');
    bbxRenderLivePill();
    return true;
  } catch (error) {
    // A board that can no longer be read stops following rather than hammering the service.
    bbxLive.paused = true;
    bbxLiveStop();
    bbxStatus(bbxBoardError(error, code), 'bad');
    return false;
  }
}

async function bbxLiveTick() {
  // Only the station that is showing the board polls: another screen, a hidden tab, or the
  // match tab of this same station has nothing to redraw.
  if (bbxLive.paused || !bbxLive.boards.size) return;
  if (document.visibilityState === 'hidden') return;
  if (document.body.getAttribute('data-view') !== 'analytics') return;
  const panel = bbxEl('analytics-panel-buzzboard');
  if (panel && panel.classList.contains('hidden')) return;
  for (const code of [...bbxLive.boards.keys()]) {
    await bbxRefreshChallengeBoard(code);
  }
}

async function bbxLoadChallengeBoard(code) {
  const clean = String(code || '').trim().toLowerCase();
  // The server's own rule for a challenge code (see ui_service._CHALLENGE_CODE_RE).
  if (!/^[a-z0-9]{4,8}$/.test(clean)) {
    bbxStatus('Enter a challenge code (4-8 letters or digits).', 'bad');
    return 0;
  }
  const key = `challenge:${clean}`;
  if (bbxState.files.has(key)) {
    bbxStatus(`Board ${clean.toUpperCase()} is already loaded.`, 'busy');
    return 0;
  }
  bbxStatus(`FETCHING BOARD ${clean.toUpperCase()}...`, 'busy');
  try {
    const board = await bbxApi(`/api/challenges/${encodeURIComponent(clean)}/board`);
    const csv = bbxBoardCsvText(board);
    const added = bbxIngestBoardCsv(clean, csv);
    bbxState.files.add(key);
    // From here the board follows the challenge. The code is registered even with no runs yet,
    // so the first member to finish appears without anyone reloading the page.
    bbxLive.boards.set(clean, { csv: csv, playing: bbxPlayingRuns(board) });
    bbxLive.updatedAt = Date.now();
    bbxLiveStart();          // a no-op while the captain has the poll paused
    bbxRenderLivePill();
    if (!added) {
      bbxRenderAll();
      bbxStatus(`Board ${clean.toUpperCase()} has no runs yet \u2014 it will fill in as members play.`, 'busy');
    } else {
      bbxFinishIngest(added);
    }
    // The address remembers which board is open, so the view can be linked and refreshed.
    if (typeof window.routerReplace === 'function') {
      window.routerReplace(`#/analytics/${encodeURIComponent(clean)}`);
    }
    return added;
  } catch (error) {
    bbxStatus(bbxBoardError(error, clean), 'bad');
    return 0;
  }
}

const bbxLoadBoardBtn = bbxEl('bbx-load-board');
const bbxChallengeCode = bbxEl('bbx-challenge-code');
const bbxLiveBtn = bbxLivePill();

if (bbxLoadBoardBtn) {
  bbxLoadBoardBtn.addEventListener('click', () => {
    bbxLoadChallengeBoard(bbxChallengeCode ? bbxChallengeCode.value : '');
  });
}

if (bbxChallengeCode) {
  bbxChallengeCode.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      bbxLoadChallengeBoard(bbxChallengeCode.value);
    }
  });
}

if (bbxLiveBtn) {
  bbxLiveBtn.addEventListener('click', () => {
    if (bbxLive.paused) bbxLiveResume(); else bbxLivePause();
    bbxRenderLivePill();
  });
}

// A tab that was in the background comes back current: catch up at once instead of waiting out
// the rest of the interval.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') bbxLiveTick();
});

// The router owns the address; a link like #/analytics/ab12cd opens this page on that board,
// which is what makes a breakdown shareable. Leaving this station stops the polling but keeps
// the loaded boards, so coming back resumes following them.
document.addEventListener('app:view', (event) => {
  const detail = event.detail || {};
  if (detail.name !== 'analytics') {
    bbxLiveStop();
    return;
  }
  const code = (detail.params && detail.params.code) || '';
  // The buzzboard panel (not the match tables), on its first page.
  showAnalyticsView('buzzboard');
  bbxShowPage('questions');
  if (bbxChallengeCode && code) bbxChallengeCode.value = code;
  bbxLiveStart();
  if (!code) return;
  if (bbxState.files.has(`challenge:${code}`)) return;  // already on screen
  bbxLoadChallengeBoard(code);
});

// ------------------------------------------------------------ models

function bbxBuildModels() {
  // A later upload must not throw away the draft the captain already made.
  const drafts = new Map([...bbxState.players.values()].map((player) => [player.key, player.team]));
  bbxState.questions = new Map();
  bbxState.players = new Map();
  const subjects = new Set();

  for (const row of bbxState.rows) {
    const key = `${row.qNum}\u0000${row.questionText}`;
    let question = bbxState.questions.get(key);
    if (!question) {
      question = {
        key,
        qNum: row.qNum,
        text: row.questionText,
        answer: row.answer,
        category: row.category,
        subject: row.subject,
        type: row.type,
        tokens: bbxTokenize(row.questionText),
        buzzes: [],
        attempts: 0,
        correct: 0,
        wrong: 0,
        noBuzz: 0,
        reportCount: 0,
        depthSum: 0,
        depthCount: 0,
        correctDepthSum: 0,
        correctDepthCount: 0,
      };
      bbxState.questions.set(key, question);
    }
    question.attempts += 1;
    if (row.reported) question.reportCount += 1;
    if (row.verdict === 'correct') question.correct += 1;
    if (row.verdict === 'wrong') question.wrong += 1;
    // An 'end' buzz has no word index but is still a buzz: the member heard the whole
    // question and buzzed in the five seconds after it. Only a run with no buzz at all
    // counts as a no-buzz on the END line.
    if (row.buzzIndex === null && row.captureSource !== 'end') {
      question.noBuzz += 1;
    } else {
      question.buzzes.push(row);
      if (row.depth !== null) {
        question.depthSum += row.depth;
        question.depthCount += 1;
        if (row.verdict === 'correct') {
          question.correctDepthSum += row.depth;
          question.correctDepthCount += 1;
        }
      }
    }

    let player = bbxState.players.get(row.member);
    if (!player) {
      player = {
        key: row.member,
        name: row.member,
        team: 0,
        attempts: 0,
        buzzes: 0,
        correct: 0,
        incorrect: 0,
        negs: 0,
        depthSum: 0,
        depthCount: 0,
        subjects: {},
      };
      bbxState.players.set(row.member, player);
    }
    player.attempts += 1;
    const subjectKey = row.subject || 'General Science';
    subjects.add(subjectKey);
    if (!player.subjects[subjectKey]) player.subjects[subjectKey] = { attempts: 0, correct: 0, negs: 0 };
    const bucket = player.subjects[subjectKey];
    bucket.attempts += 1;
    if (row.verdict === 'correct') { player.correct += 1; bucket.correct += 1; }
    if (row.buzzIndex !== null || row.captureSource === 'end') {
      player.buzzes += 1;
      if (row.depth !== null) { player.depthSum += row.depth; player.depthCount += 1; }
      // A wrong answer after the full read is just incorrect, not a neg: only an interrupt
      // carries the neg and its penalty.
      if (row.verdict === 'wrong' && row.captureSource === 'end') {
        player.incorrect += 1;
      } else if (row.verdict === 'wrong') {
        player.negs += 1;
        bucket.negs += 1;
      }
    }
  }

  bbxState.players.forEach((player) => {
    if (drafts.has(player.key)) player.team = drafts.get(player.key);
  });

  bbxState.subjects = [...subjects].sort((a, b) => {
    const ia = BBX_SUBJECT_ORDER.indexOf(a);
    const ib = BBX_SUBJECT_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

// Tokenize the packet text with the board's own tokenizer when it is present, so the
// highlighted word indices line up with what the member's buzz recorded.
function bbxTokenize(text) {
  const raw = String(text || '');
  if (typeof window.buzzTokenizeWords === 'function') {
    return window.buzzTokenizeWords(raw).map((token) => token.word);
  }
  return raw.split(/\s+/).filter(Boolean);
}

// Points per tossup heard: +4 for a correct buzz, -4 for a neg, averaged over the
// tossups that member heard in that subject (the same weighting as the match log).
function bbxPlayerSubjectPpth(player, subject) {
  const bucket = player.subjects[subject];
  if (!bucket || !bucket.attempts) return null;
  const points = bucket.correct * BBX_CORRECT_POINTS + bucket.negs * BBX_NEG_POINTS;
  return points / bucket.attempts;
}

// Overall points for the run: a correct toss-up is +4 and a neg is -4, the same weighting
// the per-subject PPTH columns use.
function bbxPlayerPoints(player) {
  return player.correct * BBX_CORRECT_POINTS + player.negs * BBX_NEG_POINTS;
}

function bbxPlayerPpth(player) {
  if (!player.attempts) return null;
  return bbxPlayerPoints(player) / player.attempts;
}

function bbxPlayerSpeed(player) {
  if (!player.depthCount) return null;
  return player.depthSum / player.depthCount;
}

function bbxPlayerAccuracy(player) {
  return bbxRatio(player.correct, player.buzzes);
}

function bbxPrimarySpecialty(player) {
  let best = null;
  let bestPpth = -Infinity;
  let bestAttempts = -Infinity;
  for (const subject of bbxState.subjects) {
    const bucket = player.subjects[subject];
    if (!bucket || !bucket.attempts) continue;
    const ppth = bbxPlayerSubjectPpth(player, subject);
    if (ppth > bestPpth || (ppth === bestPpth && bucket.attempts > bestAttempts)) {
      best = subject;
      bestPpth = ppth;
      bestAttempts = bucket.attempts;
    }
  }
  return best;
}

// ------------------------------------------------------------ rendering: shell

function bbxRenderAll() {
  bbxRenderReports();
  bbxRenderParticipantPills();
  bbxRenderQuestions();
  bbxRenderSubjectFilters();
  bbxRenderHud();
  bbxRenderMatrix();
}

function bbxShowPage(page) {
  bbxState.page = page === 'numbers' ? 'numbers' : 'questions';
  const questionsPanel = bbxEl('bbx-panel-questions');
  const numbersPanel = bbxEl('bbx-panel-numbers');
  const questionsBtn = bbxEl('bbx-page-questions');
  const numbersBtn = bbxEl('bbx-page-numbers');
  if (questionsPanel) questionsPanel.classList.toggle('hidden', bbxState.page !== 'questions');
  if (numbersPanel) numbersPanel.classList.toggle('hidden', bbxState.page !== 'numbers');
  if (questionsBtn) questionsBtn.className = bbxState.page === 'questions' ? BBX_PAGE_ON : BBX_PAGE_OFF;
  if (numbersBtn) numbersBtn.className = bbxState.page === 'numbers' ? BBX_PAGE_ON : BBX_PAGE_OFF;
}

function showAnalyticsView(view) {
  const match = view !== 'buzzboard';
  const matchPanel = bbxEl('analytics-panel-match');
  const buzzPanel = bbxEl('analytics-panel-buzzboard');
  const matchTab = bbxEl('analytics-tab-match');
  const buzzTab = bbxEl('analytics-tab-buzzboard');
  if (matchPanel) matchPanel.classList.toggle('hidden', !match);
  if (buzzPanel) buzzPanel.classList.toggle('hidden', match);
  if (matchTab) matchTab.className = match ? BBX_TAB_ON : BBX_TAB_OFF;
  if (buzzTab) buzzTab.className = match ? BBX_TAB_OFF : BBX_TAB_ON;
}

function resetBuzzboardAnalytics() {
  // Clearing the data also stops the boards being followed, so nothing re-loads behind the
  // captain's back: a code that was cleared stays forgotten until it is loaded again.
  bbxLiveStop();
  bbxLive.boards.clear();
  bbxLive.paused = false;
  bbxLive.updatedAt = 0;
  bbxRenderLivePill();
  bbxState.rows = [];
  bbxState.questions = new Map();
  bbxState.players = new Map();
  bbxState.files = new Set();
  bbxState.subjects = [];
  bbxState.selectedPlayer = 'ALL';
  bbxState.subjectFilter = 'ALL';
  bbxState.search = '';
  const search = bbxEl('bbx-search');
  if (search) search.value = '';
  const badge = bbxEl('bbx-count-badge');
  if (badge) badge.style.display = 'none';
  bbxRenderAll();
  bbxStatus('SYSTEM READY // WAITING FOR A BUZZBOARD CSV OR CHALLENGE CODE');
}
// ------------------------------------------------------------ page 1: question buzzpoints

function bbxRenderParticipantPills() {
  const holder = bbxEl('bbx-participant-pills');
  if (!holder) return;
  holder.replaceChildren();
  const players = [...bbxState.players.values()].sort((a, b) => a.name.localeCompare(b.name));
  const make = (label, key, active) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = label;
    btn.className = `px-2 py-0.5 rounded text-[11px] font-medium transition ${
      active ? 'bg-neonCyan text-black font-bold' : 'bg-black border border-borderDark text-slate-300 hover:text-white'
    }`;
    btn.addEventListener('click', () => {
      bbxState.selectedPlayer = key;
      bbxRenderParticipantPills();
      bbxRenderQuestions();
    });
    holder.appendChild(btn);
  };
  make('All Buzzers', 'ALL', bbxState.selectedPlayer === 'ALL');
  players.forEach((player) => make(player.name, player.key, bbxState.selectedPlayer === player.key));
}

function bbxToggleBuzzpoints() {
  bbxState.showBuzzpoints = !bbxState.showBuzzpoints;
  const label = bbxEl('bbx-toggle-buzz');
  if (label) label.textContent = bbxState.showBuzzpoints ? 'Hide Buzzpoints' : 'Show Buzzpoints';
  bbxRenderQuestions();
}

function bbxToggleAnswers() {
  bbxState.showAnswers = !bbxState.showAnswers;
  const label = bbxEl('bbx-toggle-ans');
  if (label) label.textContent = bbxState.showAnswers ? 'Hide Answers' : 'Show Answers';
  bbxRenderQuestions();
}

// ------------------------------------------------------------ reported questions
// The member's flag is a request, not a verdict. This queue is where the captain answers
// it: each open report names the attempt and offers the two judgements, and the server
// records who overrode what. Only a board loaded by code carries the run uid needed to
// correct it; a CSV dropped by hand is a snapshot, so it says so instead of pretending.

function bbxReportPhases(row) {
  return String(row.reported || '')
    .split('+')
    .filter((phase) => phase === 'tossup' || phase === 'bonus');
}

function bbxPhaseVerdict(row, phase) {
  const raw = phase === 'bonus' ? row.bonusVerdict : row.verdict;
  return bbxVerdict({ Verdict: raw });
}

function bbxRenderReports() {
  const holder = bbxEl('bbx-report-queue');
  const list = bbxEl('bbx-report-list');
  if (!holder || !list) return;
  list.replaceChildren();
  const reported = bbxState.rows.filter((row) => bbxReportPhases(row).length);
  holder.classList.toggle('hidden', !reported.length);
  if (!reported.length) return;

  reported.forEach((row) => {
    bbxReportPhases(row).forEach((phase) => {
      const item = document.createElement('div');
      item.className = 'flex flex-wrap items-center justify-between gap-2 border-t border-borderDark first:border-t-0 py-2';
      const info = bbxAppend(item, 'div', 'min-w-0');
      const line = bbxAppend(info, 'div', 'flex flex-wrap items-center gap-2 text-[11px] font-mono');
      bbxAppend(line, 'span', 'font-black text-neonPink', `\u2691 Q${row.qNum}`);
      bbxAppend(line, 'span', 'font-bold text-slate-200', row.member || 'Unknown member');
      bbxAppend(line, 'span', 'px-1.5 py-0.5 rounded bg-panel text-slate-400 uppercase tracking-wider', phase === 'bonus' ? 'Bonus' : 'Toss-up');
      const current = bbxPhaseVerdict(row, phase);
      bbxAppend(line, 'span', `font-bold ${current === 'correct' ? 'text-neonGreen' : (current === 'wrong' ? 'text-neonPink' : 'text-slate-500')}`,
        current === 'correct' ? 'Currently correct' : (current === 'wrong' ? 'Currently incorrect' : 'Not judged'));
      if (row.reportReason) bbxAppend(info, 'p', 'mt-1 text-[10px] font-mono text-slate-500', row.reportReason);

      const actions = bbxAppend(item, 'div', 'flex items-center gap-2');
      const fixable = Boolean(row.uid) && String(row.source || '').startsWith('challenge:');
      if (!fixable) {
        bbxAppend(actions, 'span', 'text-[10px] font-mono text-slate-500', 'Load this board by code to fix it');
        return;
      }
      const make = (label, correct, tone) => {
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = `px-2.5 py-1 rounded border text-[10px] font-mono font-black uppercase transition active:scale-95 disabled:opacity-40 disabled:cursor-not-allowed ${
          tone === 'correct'
            ? 'bg-black border-neonGreen/70 text-neonGreen hover:bg-neonGreen/10'
            : 'bg-black border-neonPink/70 text-neonPink hover:bg-neonPink/10'
        }`;
        btn.textContent = label;
        btn.disabled = (correct && current === 'correct') || (!correct && current === 'wrong');
        btn.addEventListener('click', () => bbxJudgeReport(row, phase, correct));
        actions.appendChild(btn);
      };
      make('Mark correct', true, 'correct');
      make('Mark incorrect', false, 'wrong');
      list.appendChild(item);
    });
  });
}

async function bbxJudgeReport(row, phase, correct) {
  const source = String(row.source || '');
  const code = source.startsWith('challenge:') ? source.slice('challenge:'.length) : '';
  if (!code || !row.uid) {
    bbxStatus('Load this board by code to correct a reported attempt.', 'bad');
    return;
  }
  const label = correct ? 'correct' : 'incorrect';
  bbxStatus(`Marking Q${row.qNum} ${phase} ${label}\u2026`, 'busy');
  try {
    await bbxApi(`/api/challenges/${encodeURIComponent(code)}/judge`, {
      method: 'POST',
      body: JSON.stringify({ uid: row.uid, qIndex: row.qNum - 1, phase, correct }),
    });
    bbxStatus(`Q${row.qNum} ${phase} marked ${label}.`, 'good');
    const refreshed = await bbxRefreshChallengeBoard(code);
    if (!refreshed && !bbxLive.boards.has(code)) {
      const board = await bbxApi(`/api/challenges/${encodeURIComponent(code)}/board`);
      const csv = bbxBoardCsvText(board);
      bbxState.rows = bbxState.rows.filter((item) => item.source !== source);
      bbxIngestBoardCsv(code, csv);
      bbxBuildModels();
      bbxRenderAll();
      bbxUpdateCountBadge();
    }
  } catch (error) {
    bbxRenderReports();
    bbxStatus(bbxBoardError(error, code), 'bad');
  }
}

function bbxRenderQuestions() {
  const holder = bbxEl('bbx-questions-container');
  if (!holder) return;
  holder.replaceChildren();
  const questions = [...bbxState.questions.values()].sort((a, b) => (a.qNum - b.qNum) || a.text.localeCompare(b.text));
  if (!questions.length) {
    bbxAppend(holder, 'p', 'text-xs font-mono text-slate-500', 'No buzzboard data yet \u2014 drop a CSV exported from the Buzzpoint Board, or load a board by its challenge code above.');
    return;
  }
  questions.forEach((question) => holder.appendChild(bbxQuestionCard(question)));
}

function bbxQuestionCard(question) {
  const card = document.createElement('article');
  card.className = 'bg-black border border-borderDark rounded-xl overflow-hidden';

  // Header: the question's own labels plus the scoreline.
  const head = bbxAppend(card, 'div', 'px-4 py-2.5 flex flex-wrap items-center justify-between gap-2 border-b border-borderDark bg-panel');
  const chips = bbxAppend(head, 'div', 'flex flex-wrap items-center gap-2');
  bbxAppend(chips, 'span', 'px-2.5 py-0.5 rounded text-xs font-semibold bg-panelHover text-neonGreen', question.category || question.subject || 'GENERAL');
  bbxAppend(chips, 'span', 'px-2.5 py-0.5 rounded text-xs font-medium bg-panelHover text-neonCyan', question.type ? `Tossup \u00b7 ${question.type}` : 'Tossup');
  bbxAppend(chips, 'span', 'text-xs text-neonAmber font-mono font-bold', `Q${question.qNum}`);
  if (question.reportCount) {
    bbxAppend(chips, 'span', 'px-2 py-0.5 rounded text-[10px] font-mono font-black uppercase border border-neonPink/60 bg-panel text-neonPink', `\u2691 ${question.reportCount} reported`);
  }
  bbxAppend(head, 'span', 'text-[11px] font-mono text-slate-500', `${question.attempts} attempt${question.attempts === 1 ? '' : 's'}`);

  const body = bbxAppend(card, 'div', 'p-4 space-y-3');

  // Stats ribbon.
  const ribbon = bbxAppend(body, 'div', 'border border-borderDark rounded bg-panel px-3 py-2');
  const grid = bbxAppend(ribbon, 'div', 'grid grid-cols-5 sm:grid-cols-10 gap-2 text-center font-mono');
  const stats = bbxQuestionStats(question);
  for (const [label, value] of stats) {
    const cell = bbxAppend(grid, 'div', 'flex flex-col items-center');
    bbxAppend(cell, 'span', 'text-[10px] font-mono text-slate-500 uppercase tracking-widest border-b border-dotted border-slate-600', label);
    bbxAppend(cell, 'span', 'text-xs font-bold text-slate-100 mt-1', value);
  }

  // Prose with every buzz highlighted at the word it was captured on.
  const prose = bbxAppend(body, 'div', 'text-[15px] leading-relaxed text-slate-300 font-sans pt-1 select-text');
  if (question.tokens.length) {
    question.tokens.forEach((word, index) => {
      const hits = question.buzzes.filter((buzz) => buzz.buzzIndex !== null && buzz.buzzIndex - 1 === index);
      const showHits = bbxState.showBuzzpoints ? hits : [];
      if (!showHits.length) {
        const span = bbxAppend(prose, 'span', null, `${word} `);
        span.style.color = '#cbd5e1';
        return;
      }
      const primary = showHits[0];
      const matches = bbxState.selectedPlayer === 'ALL' || showHits.some((hit) => hit.member === bbxState.selectedPlayer);
      const span = bbxAppend(prose, 'span', null, `${word} `);
      const correct = showHits.some((hit) => hit.verdict === 'correct');
      span.style.background = correct ? 'rgba(0,255,102,0.18)' : 'rgba(255,0,85,0.18)';
      span.style.borderBottom = `2.5px solid ${correct ? '#00ff66' : '#ff0055'}`;
      span.style.borderRadius = '2px';
      span.style.padding = '0 3px';
      span.style.cursor = 'pointer';
      if (!matches) span.style.opacity = '0.25';
      // The names and verdicts live in the styled tooltip; a native title would open a second,
      // unpainted tooltip on top of it with the same list.
      span.setAttribute('aria-label', showHits.map((hit) => `${hit.member}: ${hit.verdict === 'correct' ? 'Correct' : 'Neg'}`).join(', '));
      span.addEventListener('mouseenter', () => bbxShowTooltip(span, showHits, correct));
      span.addEventListener('mouseleave', bbxHideTooltip);
      span.addEventListener('click', () => {
        bbxState.selectedPlayer = primary.member;
        bbxRenderParticipantPills();
        bbxRenderQuestions();
      });
    });
  } else {
    bbxAppend(prose, 'span', 'text-slate-500', 'This export carried no question text \u2014 re-download the board CSV to include it.');
  }

  // Footer: who buzzed at the END, how many never buzzed, and the answer on request.
  const foot = bbxAppend(body, 'div', 'pt-2 flex flex-wrap items-center justify-between gap-2 text-xs border-t border-borderDark');
  const footLeft = bbxAppend(foot, 'div', 'flex flex-wrap items-center gap-2');
  // An end buzz has no word to hover, so the END badge carries the whole list in its tooltip
  // rather than repeating every member inline.
  const endBuzzes = question.buzzes.filter((buzz) => buzz.captureSource === 'end');
  const endBadge = bbxAppend(footLeft, 'span', 'px-2 py-0.5 rounded text-[11px] font-mono font-medium border border-neonPink/50 bg-panel text-neonPink', `END \u00b7 ${endBuzzes.length}`);
  if (endBuzzes.length) {
    endBadge.className += ' cursor-help';
    endBadge.setAttribute('aria-label', `${endBuzzes.length} end buzz${endBuzzes.length === 1 ? '' : 'es'}`);
    endBadge.addEventListener('mouseenter', () => {
      bbxShowTooltip(endBadge, endBuzzes, endBuzzes.some((hit) => hit.verdict === 'correct'));
    });
    endBadge.addEventListener('mouseleave', bbxHideTooltip);
  }
  // A run that ended without any buzz is not an end buzz: it keeps its own count.
  if (question.noBuzz) {
    bbxAppend(footLeft, 'span', 'px-2 py-0.5 rounded text-[11px] font-mono text-slate-500 border border-borderDark bg-panel', `NO BUZZ \u00b7 ${question.noBuzz}`);
  }
  const answerBtn = document.createElement('button');
  answerBtn.type = 'button';
  answerBtn.className = 'inline-flex items-center text-slate-400 hover:text-white font-medium';
  answerBtn.textContent = 'Show Answer';
  answerBtn.addEventListener('click', () => {
    if (answerBox) answerBox.classList.toggle('hidden');
  });
  footLeft.appendChild(answerBtn);
  bbxAppend(foot, 'span', 'text-slate-500 font-mono text-[11px]', `Target: ${question.answer || '\u2014'}`);

  const answerBox = bbxAppend(body, 'div', 'bg-panel border border-borderDark rounded p-2.5 text-xs font-mono text-slate-200 hidden');
  bbxAppend(answerBox, 'span', 'font-bold text-neonGreen', 'Answer: ');
  bbxAppend(answerBox, 'span', null, question.answer || '\u2014');
  if (bbxState.showAnswers) answerBox.classList.remove('hidden');

  return card;
}

function bbxQuestionStats(question) {
  const buzzes = question.buzzes.length;
  const interrupts = question.buzzes.filter((b) => b.wordsHeard !== null && b.totalWords !== null && b.wordsHeard < b.totalWords).length;
  // Only interrupts are negs; an end buzz answered wrong is incorrect but not a neg, and an
  // expired toss-up was never buzzed at all.
  const negs = question.buzzes.filter((b) => b.verdict === 'wrong' && b.captureSource !== 'end').length;
  const avgBuzz = question.depthCount ? question.depthSum / question.depthCount : null;
  const celerity = question.correctDepthCount ? question.correctDepthSum / question.correctDepthCount : null;
  const power = question.buzzes.filter((b) => b.verdict === 'correct' && b.depth !== null && b.depth <= 25).length;
  return [
    ['PLAYS', question.attempts],
    ['BUZZES', buzzes],
    ['CORRECT', question.correct],
    ['INTERRUPTS', interrupts],
    ['CONVERSION', buzzes ? `${Math.round((question.correct / buzzes) * 100)}%` : '\u2014'],
    ['ACCURACY', bbxPctText(bbxRatio(question.correct, question.attempts))],
    ['POWER', power || '\u2014'],
    ['NEG', buzzes ? `${Math.round((negs / buzzes) * 100)}%` : '\u2014'],
    ['AVG BUZZ', avgBuzz === null ? '\u2014' : `@${Math.round(avgBuzz)}%`],
    ['CELERITY', celerity === null ? '\u2014' : `@${Math.round(celerity)}%`],
  ];
}

// ------------------------------------------------------------ tooltip

function bbxShowTooltip(anchor, hits, correct) {
  const tip = bbxEl('bbx-tooltip');
  if (!tip) return;
  tip.replaceChildren();
  tip.style.borderColor = correct ? '#00ff66' : '#ff0055';
  // Just the member and whether they were right. The word already shows the buzz position,
  // the verdict already implies the answer, and the points follow from the verdict.
  hits.forEach((hit, index) => {
    if (index > 0) bbxAppend(tip, 'div', 'border-t border-white/20 my-1.5');
    const hitCorrect = hit.verdict === 'correct';
    const row = bbxAppend(tip, 'div', 'flex items-center gap-1.5');
    const dot = bbxAppend(row, 'span', 'inline-block w-2.5 h-2.5 rotate-45 border');
    dot.style.borderColor = hitCorrect ? '#00ff66' : '#ff0055';
    dot.style.background = hitCorrect ? 'rgba(0,255,102,0.3)' : 'rgba(255,0,85,0.3)';
    bbxAppend(row, 'span', 'font-bold text-white tracking-wide', hit.member);
    // A wrong answer after the question was read in full is just incorrect; a neg is an
    // interrupt, which by definition happens before the read finishes.
    const verdictText = hitCorrect ? 'Correct' : (hit.captureSource === 'end' ? 'Incorrect' : 'Neg');
    const verdict = bbxAppend(row, 'span', 'text-[10px] font-black uppercase tracking-wider', verdictText);
    verdict.style.color = hitCorrect ? '#00ff66' : '#ff0055';
  });
  tip.classList.remove('hidden');
  const rect = anchor.getBoundingClientRect();
  const width = tip.offsetWidth || 300;
  const height = tip.offsetHeight || 120;
  let left = rect.left + (rect.width / 2) - (width / 2);
  let top = rect.bottom + 7;
  if (top + height > window.innerHeight) top = rect.top - height - 7;
  tip.style.left = `${Math.max(10, Math.min(left, window.innerWidth - width - 10))}px`;
  // A list of several buzzers is a tall panel; keep it on screen when it has to flip above
  // an anchor near the bottom.
  tip.style.top = `${Math.max(10, Math.min(top, window.innerHeight - height - 10))}px`;
}

function bbxHideTooltip() {
  const tip = bbxEl('bbx-tooltip');
  if (tip) tip.classList.add('hidden');
}

// ------------------------------------------------------------ page 2: numerical data

function bbxTeamMembers(teamId) {
  return [...bbxState.players.values()].filter((player) => player.team === teamId);
}

function bbxRenderSubjectFilters() {
  const holder = bbxEl('bbx-subject-filters');
  if (!holder) return;
  holder.replaceChildren();
  const options = ['ALL', ...bbxState.subjects];
  options.forEach((subject) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = subject === 'ALL' ? 'All Subjects' : subject;
    btn.className = `px-2.5 py-1 rounded text-[11px] font-mono border transition ${
      bbxState.subjectFilter === subject
        ? 'bg-neonCyan text-black border-neonCyan font-bold'
        : 'bg-black text-slate-300 border-borderDark hover:text-white'
    }`;
    btn.addEventListener('click', () => {
      bbxState.subjectFilter = subject;
      bbxRenderSubjectFilters();
      bbxRenderMatrix();
    });
    holder.appendChild(btn);
  });
}

function bbxRenderHud() {
  const holder = bbxEl('bbx-hud');
  if (!holder) return;
  holder.replaceChildren();
  if (!bbxState.players.size) {
    bbxAppend(holder, 'p', 'text-xs font-mono text-slate-500 col-span-3', 'Assign candidates once a board CSV is loaded.');
    return;
  }
  [1, 2, 3].forEach((teamId) => {
    const accent = BBX_TEAM_ACCENTS[teamId];
    const members = bbxTeamMembers(teamId);
    const card = bbxAppend(holder, 'div', 'border border-borderDark bg-black rounded-lg p-2');
    const head = bbxAppend(card, 'div', 'flex items-center justify-between mb-1.5');
    const left = bbxAppend(head, 'div', 'flex items-center gap-1.5');
    const dot = bbxAppend(left, 'span', 'w-2.5 h-2.5 rounded-full');
    dot.style.background = accent;
    const title = bbxAppend(left, 'span', 'font-bold text-xs font-mono tracking-tight text-slate-100', `TEAM ${teamId}`);
    bbxAppend(left, 'span', 'text-[11px] font-mono font-semibold', `(${members.length}/${BBX_SQUAD_SIZE})`).style.color = accent;
    const covered = bbxState.subjects.filter((subject) => members.some((m) => m.subjects[subject] && m.subjects[subject].attempts));
    const overall = bbxState.subjects.length ? Math.round((covered.length / bbxState.subjects.length) * 100) : 0;
    const right = bbxAppend(head, 'div', 'flex items-center gap-1');
    bbxAppend(right, 'span', 'font-mono text-xs font-bold', `${overall}%`).style.color = accent;
    bbxAppend(right, 'span', 'text-[10px] font-mono uppercase text-slate-500', 'cov');

    const barTrack = bbxAppend(card, 'div', 'w-full bg-panel rounded-full h-1.5 overflow-hidden');
    const bar = bbxAppend(barTrack, 'div', 'h-full rounded-full');
    bar.style.width = `${overall}%`;
    bar.style.background = accent;

    const meters = bbxAppend(card, 'div', 'grid grid-cols-5 gap-1 pt-2');
    bbxState.subjects.forEach((subject) => {
      const cell = bbxAppend(meters, 'div', 'flex flex-col items-center');
      bbxAppend(cell, 'span', 'text-[9px] font-mono text-slate-500 uppercase', BBX_SUBJECT_SHORT[subject] || subject.slice(0, 3).toUpperCase());
      let best = null;
      members.forEach((member) => {
        const ppth = bbxPlayerSubjectPpth(member, subject);
        if (ppth !== null && (best === null || ppth > best)) best = ppth;
      });
      const pct = best === null ? 0 : Math.max(0, Math.min(100, Math.round((best / BBX_PPTH_CEILING) * 100)));
      const track = bbxAppend(cell, 'div', 'w-full bg-panel h-1 rounded-full overflow-hidden mt-0.5');
      const fill = bbxAppend(track, 'div', 'h-full rounded-full');
      fill.style.width = `${pct}%`;
      fill.style.background = accent;
      const label = bbxAppend(cell, 'span', 'text-[9px] font-mono mt-0.5', `${pct}%`);
      label.style.color = pct > 0 ? '#cbd5e1' : '#475569';
    });

    const status = bbxAppend(card, 'div', 'mt-1.5 flex items-center justify-between text-[10px] font-mono pt-1 border-t border-borderDark');
    bbxAppend(status, 'span', 'text-slate-400 truncate max-w-[190px]',
      members.length ? members.map((m) => m.name).join(', ') : 'None assigned');
    if (!members.length) {
      bbxAppend(status, 'span', 'px-1 rounded bg-panel text-slate-400', `${BBX_SQUAD_SIZE} slots open`);
    } else if (overall === 100) {
      bbxAppend(status, 'span', 'px-1 rounded bg-panel text-neonGreen', 'Full coverage');
    } else {
      const missing = bbxState.subjects.filter((subject) => !covered.includes(subject));
      bbxAppend(status, 'span', 'px-1 rounded bg-panel text-neonAmber',
        `Missing: ${missing.map((s) => BBX_SUBJECT_SHORT[s] || s).join(', ')}`);
    }
  });
}

function bbxCycleTeam(memberKey, reverse) {
  const player = bbxState.players.get(memberKey);
  if (!player) return;
  player.team = reverse ? 0 : (player.team + 1) % 4;
  bbxRenderHud();
  bbxRenderMatrix();
}

function bbxResetTeams() {
  bbxState.players.forEach((player) => { player.team = 0; });
  bbxRenderHud();
  bbxRenderMatrix();
}

function bbxExportRosters(button) {
  const line = (teamId) => {
    const members = bbxTeamMembers(teamId);
    return members.length ? members.map((m) => `${m.name} (${bbxPrimarySpecialty(m) || 'untested'})`).join(', ') : 'None';
  };
  const text = `Buzzboard lineups:\n\u2022 Team 1: ${line(1)}\n\u2022 Team 2: ${line(2)}\n\u2022 Team 3: ${line(3)}`;
  const copy = (value) => {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(value);
    const area = document.createElement('textarea');
    area.value = value;
    document.body.appendChild(area);
    area.select();
    document.execCommand('copy');
    area.remove();
    return Promise.resolve();
  };
  copy(text).then(() => {
    if (!button) return;
    const previous = button.textContent;
    button.textContent = 'Copied!';
    setTimeout(() => { button.textContent = previous; }, 1500);
  }).catch(() => bbxStatus('Clipboard access was refused \u2014 copy the rosters manually.', 'bad'));
}

function bbxSetSearch(value) {
  bbxState.search = String(value || '').toLowerCase().trim();
  bbxRenderMatrix();
}

function bbxSortBy(key) {
  if (bbxState.sortKey === key) bbxState.sortAsc = !bbxState.sortAsc;
  else { bbxState.sortKey = key; bbxState.sortAsc = false; }
  bbxRenderMatrix();
}

function bbxSortValue(player, key) {
  if (key === 'name') return player.name;
  if (key === 'acc') return bbxPlayerAccuracy(player);
  if (key === 'correct') return player.correct;
  if (key === 'incorrect') return player.incorrect;
  if (key === 'pts') return bbxPlayerPoints(player);
  if (key === 'speed') return bbxPlayerSpeed(player);
  if (key === 'negs') return player.negs;
  if (key === 'ppth') return bbxPlayerPpth(player);
  return bbxPlayerSubjectPpth(player, key);
}

function bbxRenderMatrix() {
  const table = bbxEl('bbx-matrix');
  if (!table) return;
  table.replaceChildren();

  const head = bbxAppend(table, 'thead');
  const headRow = bbxAppend(head, 'tr', 'border-b border-borderDark text-[10px] text-slate-500 uppercase select-none');
  const th = (parent, label, title, onClick, extra) => {
    const cell = bbxAppend(parent, 'th', `py-2 px-2 ${extra || ''} ${onClick ? 'cursor-pointer hover:text-white' : ''}`, label);
    if (title) cell.title = title;
    if (onClick) cell.addEventListener('click', onClick);
    return cell;
  };
  th(headRow, 'Team', 'Click to cycle Unassigned \u2192 T1 \u2192 T2 \u2192 T3. Shift-click clears.', null, 'text-center px-3');
  th(headRow, 'Candidate', 'Sorted by name', () => bbxSortBy('name'), 'text-left px-3');
  bbxState.subjects.forEach((subject) => {
    th(headRow, `${BBX_SUBJECT_SHORT[subject] || subject.slice(0, 3).toUpperCase()} PPTH`,
      'Points per tossup heard: +4 correct, -4 neg',
      () => bbxSortBy(subject), 'text-center');
  });
  th(headRow, 'Acc %', 'Accuracy: correct \u00f7 buzzes', () => bbxSortBy('acc'), 'text-center');
  th(headRow, 'Correct', 'Tossups answered correctly', () => bbxSortBy('correct'), 'text-center');
  th(headRow, 'Incorrect', 'Wrong answers after the question was fully read \u2014 incorrect, not a neg', () => bbxSortBy('incorrect'), 'text-center');
  th(headRow, 'Negs', 'Interrupts answered wrong, which carry the neg penalty', () => bbxSortBy('negs'), 'text-center');
  th(headRow, 'Pts', `Overall points: +${BBX_CORRECT_POINTS} per correct tossup, ${BBX_NEG_POINTS} per neg`, () => bbxSortBy('pts'), 'text-center');
  th(headRow, 'Speed', 'Average buzz depth: how far into the question the buzz landed (higher = later)', () => bbxSortBy('speed'), 'text-center');
  th(headRow, 'Primary Specialty', 'Best subject by PPTH', null, 'text-right px-3');

  const body = bbxAppend(table, 'tbody', 'divide-y divide-borderDark');
  let list = [...bbxState.players.values()];
  if (bbxState.subjectFilter !== 'ALL') {
    list = list.filter((player) => player.subjects[bbxState.subjectFilter] && player.subjects[bbxState.subjectFilter].attempts);
  }
  if (bbxState.search) {
    list = list.filter((player) => {
      const specialty = bbxPrimarySpecialty(player) || '';
      return player.name.toLowerCase().includes(bbxState.search)
        || specialty.toLowerCase().includes(bbxState.search);
    });
  }
  list.sort((a, b) => {
    const va = bbxSortValue(a, bbxState.sortKey);
    const vb = bbxSortValue(b, bbxState.sortKey);
    if (typeof va === 'string' || typeof vb === 'string') {
      const sa = String(va === null ? '' : va);
      const sb = String(vb === null ? '' : vb);
      return bbxState.sortAsc ? sa.localeCompare(sb) : sb.localeCompare(sa);
    }
    const na = va === null ? -Infinity : va;
    const nb = vb === null ? -Infinity : vb;
    return bbxState.sortAsc ? na - nb : nb - na;
  });

  const indicator = bbxEl('bbx-count-indicator');
  if (indicator) indicator.textContent = `${list.length} candidate${list.length === 1 ? '' : 's'}`;

  if (!list.length) {
    const row = bbxAppend(body, 'tr');
    bbxAppend(row, 'td', 'py-4 text-center text-slate-600', 'Awaiting data...')
      .setAttribute('colspan', String(9 + bbxState.subjects.length));
    return;
  }

  list.forEach((player) => {
    const accent = BBX_TEAM_ACCENTS[player.team] || null;
    const row = bbxAppend(body, 'tr', 'transition-colors');
    if (accent) row.style.background = `${accent}12`;

    const teamCell = bbxAppend(row, 'td', 'py-1.5 px-3 text-center');
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.textContent = player.team ? `T${player.team}` : '\u2014';
    badge.className = 'w-8 h-5 rounded text-[10.5px] font-mono inline-flex items-center justify-center cursor-pointer transition';
    badge.title = 'Click to cycle T1 \u2192 T2 \u2192 T3. Shift-click to unassign.';
    if (player.team) {
      badge.style.background = accent;
      badge.style.color = '#04060a';
      badge.style.fontWeight = '700';
    } else {
      badge.style.border = '1px dashed #475569';
      badge.style.color = '#64748b';
    }
    badge.addEventListener('click', (event) => bbxCycleTeam(player.key, event.shiftKey));
    badge.addEventListener('contextmenu', (event) => { event.preventDefault(); bbxCycleTeam(player.key, true); });
    teamCell.appendChild(badge);

    bbxAppend(row, 'td', 'py-1.5 px-3').appendChild(bbxNameCell(player));
    bbxState.subjects.forEach((subject) => {
      const ppth = bbxPlayerSubjectPpth(player, subject);
      const cell = bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono');
      if (ppth === null) {
        bbxAppend(cell, 'span', 'text-slate-600', '\u2014');
      } else {
        bbxAppend(cell, 'span', ppth >= 2 ? 'font-bold text-slate-100' : 'text-slate-300', ppth.toFixed(1));
      }
    });

    const acc = bbxPlayerAccuracy(player);
    const accCell = bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono');
    bbxAppend(accCell, 'span', acc !== null && acc >= 80 ? 'font-bold text-neonGreen' : 'text-slate-300', bbxPctText(acc));

    bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-200', player.correct);

    bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-400', player.incorrect);

    bbxAppend(row, 'td', `py-1.5 px-2 text-center font-mono ${player.negs >= 4 ? 'text-neonPink font-bold' : 'text-slate-400'}`, player.negs);

    const points = bbxPlayerPoints(player);
    const pointsCell = bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono font-bold', `${points > 0 ? '+' : ''}${points}`);
    pointsCell.style.color = points > 0 ? '#00ff66' : (points < 0 ? '#ff0055' : '#94a3b8');

    const speed = bbxPlayerSpeed(player);
    bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-400', speed === null ? '\u2014' : `${Math.round(speed)}%`);

    const specialty = bbxPrimarySpecialty(player);
    bbxAppend(row, 'td', 'py-1.5 px-3 text-right')
      .appendChild(bbxAppend(null, 'span', 'px-1.5 py-0.5 rounded text-[10px] bg-panelHover text-slate-300 border border-borderDark', specialty || '\u2014'));
  });
}

function bbxNameCell(player) {
  const wrap = document.createElement('div');
  wrap.className = 'flex items-center gap-1.5';
  const name = bbxAppend(wrap, 'span', 'font-semibold', player.name);
  name.style.color = BBX_MEMBER_COLORS[[...bbxState.players.keys()].indexOf(player.key) % BBX_MEMBER_COLORS.length];
  bbxAppend(wrap, 'span', 'text-slate-500 font-mono text-[10.5px]', `${player.attempts} heard \u00b7 ${player.buzzes} buzzed`);
  return wrap;
}

// ------------------------------------------------------------ wiring

bbxShowPage('questions');
showAnalyticsView('match');
