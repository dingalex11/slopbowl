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
const BBX_PAGE_ON = 'px-3.5 py-2 rounded-lg text-[11px] font-mono font-black uppercase tracking-wide border transition bg-neonCyan text-black border-neonCyan flex items-center gap-2';
const BBX_PAGE_OFF = 'px-3.5 py-2 rounded-lg text-[11px] font-mono font-black uppercase tracking-wide border transition bg-black text-slate-400 border-borderDark hover:text-white flex items-center gap-2';

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
  questionSubject: 'ALL',
  search: '',
  sortKey: 'ppth',
  sortAsc: false,
  // The team-builder view follows the template: every candidate's overall PPTH first, the
  // six subject columns on request, and question cards closed until one is opened.
  rawColumns: false,
  openQuestions: new Set(),
  boardCode: '',
};

// A row dragged from the matrix. The drop handlers read the key rather than the data
// transfer alone, because a browser may refuse to expose custom data on dragover.
let bbxDraggedPlayer = null;

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
  // Keep the bar's own spacing: this line sits inside the compact ingestion strip.
  el.className = 'px-3 pb-2 text-[10px] font-mono uppercase tracking-wide';
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
      // A bonus's own answer and target, so a bonus report can be judged on what the member
      // actually typed rather than the toss-up's answer.
      bonusTypedAnswer: row['BonusTypedAnswer'] || '',
      bonusAnswer: row['BonusAnswer'] || '',
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
  bbxRenderBoardIdentity();
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
    bbxState.boardCode = clean;
    bbxLiveStart();          // a no-op while the captain has the poll paused
    bbxRenderLivePill();
    // The code is loaded; the drawer has done its job and yields the page back to the stats.
    bbxToggleDrawer(false);
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
  bbxRenderBoardIdentity();
  bbxRenderReports();
  bbxRenderQuestionSubjectFilters();
  bbxRenderParticipantPills();
  bbxRenderQuestions();
  bbxRenderSubjectFilters();
  bbxRenderHud();
  bbxRenderMatrix();
  bbxRenderDatasetLabel();
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
  bbxRenderDatasetLabel();
}

// The header line the template puts beside the tabs: what this dataset is made of, so the
// captain never has to guess how many questions or candidates are behind the view.
function bbxRenderDatasetLabel() {
  const label = bbxEl('bbx-dataset-label');
  if (!label) return;
  if (!bbxState.rows.length) {
    label.textContent = 'Awaiting board data';
    return;
  }
  const attempts = `${bbxState.rows.length} attempt${bbxState.rows.length === 1 ? '' : 's'}`;
  if (bbxState.page === 'questions') {
    const count = bbxState.questions.size;
    label.textContent = `${count} question${count === 1 ? '' : 's'} \u00b7 ${attempts}`;
  } else {
    const count = bbxState.players.size;
    label.textContent = `${count} candidate${count === 1 ? '' : 's'} \u00b7 ${attempts}`;
  }
}

// The compact bar's identity: the board code (or CSV when the rows were dropped by hand) and
// whether this page is following a live challenge.
function bbxRenderBoardIdentity() {
  const code = bbxEl('bbx-active-code');
  if (code) {
    code.textContent = bbxState.boardCode
      ? bbxState.boardCode.toUpperCase()
      : (bbxState.rows.length ? 'CSV' : '\u2014');
  }
  const label = bbxEl('bbx-connection-label');
  const pill = bbxEl('bbx-connection-pill');
  const dot = bbxEl('bbx-connection-dot');
  if (!label || !pill) return;
  if (bbxLive.boards.size) {
    label.textContent = bbxLive.paused ? 'PAUSED' : `LIVE \u00b7 ${bbxLivePlaying()} PLAYING`;
    pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-black border border-neonGreen/40 text-neonGreen font-semibold tracking-wide';
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-neonGreen animate-pulse';
  } else if (bbxState.rows.length) {
    label.textContent = 'LOADED';
    pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-black border border-neonCyan/40 text-neonCyan font-semibold tracking-wide';
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-neonCyan';
  } else {
    label.textContent = 'STANDBY';
    pill.className = 'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-black border border-borderDark text-slate-400 font-semibold tracking-wide';
    if (dot) dot.className = 'w-2 h-2 rounded-full bg-slate-600';
  }
}

function bbxToggleDrawer(force) {
  const drawer = bbxEl('bbx-ingestion-drawer');
  if (!drawer) return;
  const show = force === undefined ? drawer.classList.contains('hidden') : Boolean(force);
  drawer.classList.toggle('hidden', !show);
  const button = bbxEl('bbx-toggle-drawer');
  if (button) button.textContent = show ? 'Hide ingest' : 'Change board / Ingest CSV';
}

function bbxSetRawColumns(checked) {
  bbxState.rawColumns = Boolean(checked);
  bbxRenderMatrix();
}

// Accordion cards remember which ones were opened, so a render (a filter click, a live
// refresh) does not close the question the captain is reading.
function bbxToggleQuestion(key, body, arrow) {
  if (!body) return;
  const opening = body.classList.contains('hidden');
  body.classList.toggle('hidden', !opening);
  if (arrow) arrow.style.transform = opening ? 'rotate(180deg)' : 'rotate(0deg)';
  if (opening) bbxState.openQuestions.add(key);
  else bbxState.openQuestions.delete(key);
}

function bbxExpandAllQuestions(expand) {
  bbxState.openQuestions = new Set();
  if (expand) {
    [...bbxState.questions.values()].forEach((question) => bbxState.openQuestions.add(question.key));
  }
  bbxRenderQuestions();
}

function bbxRenderQuestionSubjectFilters() {
  const holder = bbxEl('bbx-question-subject-filters');
  if (!holder) return;
  holder.replaceChildren();
  const subjects = new Set();
  [...bbxState.questions.values()].forEach((question) => {
    const subject = question.subject || question.category;
    if (subject) subjects.add(subject);
  });
  const options = ['ALL', ...subjects];
  options.forEach((subject) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = subject === 'ALL' ? 'All subjects' : subject;
    btn.className = `px-2.5 py-1 rounded text-[10px] font-mono uppercase border transition ${
      bbxState.questionSubject === subject
        ? 'bg-neonCyan text-black border-neonCyan font-bold'
        : 'bg-black text-slate-400 border-borderDark hover:text-white'
    }`;
    btn.addEventListener('click', () => {
      bbxState.questionSubject = subject;
      bbxRenderQuestionSubjectFilters();
      bbxRenderQuestions();
    });
    holder.appendChild(btn);
  });
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
  bbxState.questionSubject = 'ALL';
  bbxState.search = '';
  bbxState.boardCode = '';
  bbxState.rawColumns = false;
  bbxState.openQuestions = new Set();
  const search = bbxEl('bbx-search');
  if (search) search.value = '';
  const raw = bbxEl('bbx-raw-columns');
  if (raw) raw.checked = false;
  const badge = bbxEl('bbx-count-badge');
  if (badge) badge.style.display = 'none';
  bbxToggleDrawer(false);
  bbxShowPage('questions');
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
    btn.className = `px-3 py-1 rounded border text-[10px] font-mono uppercase whitespace-nowrap transition ${
      active
        ? 'bg-neonCyan text-black border-neonCyan font-bold'
        : 'bg-black text-slate-300 border-borderDark hover:border-slate-500 hover:text-white'
    }`;
    btn.addEventListener('click', () => {
      bbxState.selectedPlayer = key;
      bbxRenderParticipantPills();
      bbxRenderQuestions();
    });
    holder.appendChild(btn);
  };
  make('All buzzers', 'ALL', bbxState.selectedPlayer === 'ALL');
  players.forEach((player) => make(player.name, player.key, bbxState.selectedPlayer === player.key));
}

function bbxToggleBuzzpoints() {
  bbxState.showBuzzpoints = !bbxState.showBuzzpoints;
  const label = bbxEl('bbx-toggle-buzz');
  if (label) label.textContent = bbxState.showBuzzpoints ? 'Hide buzzpoints' : 'Show buzzpoints';
  bbxRenderQuestions();
}

function bbxToggleAnswers() {
  bbxState.showAnswers = !bbxState.showAnswers;
  const label = bbxEl('bbx-toggle-ans');
  if (label) label.textContent = bbxState.showAnswers ? 'Hide answers' : 'Show answers';
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
      item.className = 'border-t border-borderDark first:border-t-0 py-3';
      const top = bbxAppend(item, 'div', 'flex flex-wrap items-start justify-between gap-3');

      const info = bbxAppend(top, 'div', 'min-w-0 flex-1 space-y-1.5');
      const line = bbxAppend(info, 'div', 'flex flex-wrap items-center gap-2 text-[11px] font-mono');
      bbxAppend(line, 'span', 'font-black text-neonPink', `\u2691 Q${row.qNum}`);
      bbxAppend(line, 'span', 'font-bold text-slate-200', row.member || 'Unknown member');
      bbxAppend(line, 'span', 'px-1.5 py-0.5 rounded bg-panel text-slate-400 uppercase tracking-wider', phase === 'bonus' ? 'Bonus' : 'Toss-up');
      const current = bbxPhaseVerdict(row, phase);
      bbxAppend(line, 'span', `font-bold ${current === 'correct' ? 'text-neonGreen' : (current === 'wrong' ? 'text-neonPink' : 'text-slate-500')}`,
        current === 'correct' ? 'Currently correct' : (current === 'wrong' ? 'Currently incorrect' : 'Not judged'));

      // The question itself, so the captain can judge without opening the card.
      bbxAppend(info, 'p', 'text-[11px] font-sans leading-snug text-slate-400 max-h-16 overflow-y-auto pr-1',
        row.questionText || 'This export did not carry the question text.');

      // What the member actually typed, next to what the packet expected. A bare verdict
      // is not enough to override the grader; a near-miss and a blank look identical there.
      const answers = bbxAppend(info, 'div', 'flex flex-wrap items-center gap-2 text-[10px] font-mono');
      const answerCell = (label, value, tone) => {
        const cell = bbxAppend(answers, 'div', 'bg-panel border border-borderDark rounded px-2 py-1 min-w-0');
        bbxAppend(cell, 'span', 'text-slate-500 uppercase tracking-wider mr-1', `${label}:`);
        bbxAppend(cell, 'span', `font-bold ${tone} break-words`, value);
      };
      const typed = phase === 'bonus' ? row.bonusTypedAnswer : row.typedAnswer;
      const target = phase === 'bonus' ? row.bonusAnswer : row.answer;
      const hasTyped = Boolean(typed && String(typed).trim());
      answerCell('answered', hasTyped ? typed : '(blank)', hasTyped ? 'text-neonCyan' : 'text-slate-500');
      answerCell('target', target || '\u2014', 'text-neonGreen');

      if (phase !== 'bonus' && (row.buzzWord || row.captureSource === 'end')) {
        const depth = (row.depth === null || row.depth === undefined) ? '' : ` at ${Math.round(row.depth)}%`;
        const where = row.captureSource === 'end'
          ? 'Buzzed after the read'
          : `Buzzed on \u201c${row.buzzWord}\u201d${depth}`;
        bbxAppend(info, 'p', 'text-[10px] font-mono text-slate-500', where);
      }
      if (row.reportReason) bbxAppend(info, 'p', 'text-[10px] font-mono text-slate-500', `Why: ${row.reportReason}`);

      const actions = bbxAppend(top, 'div', 'flex flex-wrap items-center gap-2');
      // The strip is the report, even when this dataset cannot act on it: a dropped CSV must
      // still show what was flagged and the answers behind it.
      list.appendChild(item);
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
  const questions = [...bbxState.questions.values()]
    .filter((question) => bbxState.questionSubject === 'ALL'
      || (question.subject || question.category) === bbxState.questionSubject)
    .sort((a, b) => (a.qNum - b.qNum) || a.text.localeCompare(b.text));
  if (!questions.length) {
    if (bbxState.questions.size) {
      bbxAppend(holder, 'p', 'text-xs font-mono text-slate-500', 'No questions match this subject filter.');
      return;
    }
    bbxAppend(holder, 'p', 'text-xs font-mono text-slate-500', 'No buzzboard data yet \u2014 drop a CSV exported from the Buzzpoint Board, or load a board by its challenge code above.');
    return;
  }
  questions.forEach((question) => holder.appendChild(bbxQuestionCard(question)));
}

function bbxQuestionCard(question) {
  const card = document.createElement('article');
  card.className = 'bg-panel border border-borderDark rounded-xl overflow-hidden';
  const open = bbxState.openQuestions.has(question.key);

  // Header: a one-line telemetry summary, as in the template. The whole strip is the
  // accordion control, so a card opens without hunting for a small caret.
  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'w-full text-left px-3.5 py-2.5 flex flex-wrap items-center justify-between gap-3 hover:bg-panelHover transition select-none';
  card.appendChild(head);

  const chips = bbxAppend(head, 'div', 'flex flex-wrap items-center gap-2');
  bbxAppend(chips, 'span', 'px-2 py-0.5 rounded bg-black border border-borderDark text-neonGreen font-mono text-[10px] font-bold uppercase', question.category || question.subject || 'General');
  bbxAppend(chips, 'span', 'px-2 py-0.5 rounded bg-black border border-borderDark text-slate-300 font-mono text-[10px] uppercase', question.type ? `Tossup \u00b7 ${question.type}` : 'Tossup');
  bbxAppend(chips, 'span', 'text-neonAmber font-mono font-bold text-xs', `Q${question.qNum}`);
  bbxAppend(chips, 'span', 'text-slate-500 font-mono text-[10px]', `${question.attempts} attempt${question.attempts === 1 ? '' : 's'}`);
  if (question.reportCount) {
    bbxAppend(chips, 'span', 'px-2 py-0.5 rounded text-[10px] font-mono font-black uppercase border border-neonPink/60 bg-black text-neonPink', `\u2691 ${question.reportCount} reported`);
  }

  const summary = bbxAppend(head, 'div', 'flex flex-wrap items-center gap-3 text-[10px] font-mono');
  const stats = new Map(bbxQuestionStats(question));
  const pair = (label, key, tone) => {
    const item = bbxAppend(summary, 'div', 'flex items-center gap-1');
    bbxAppend(item, 'span', 'text-slate-500 uppercase', `${label}:`);
    bbxAppend(item, 'span', `font-bold ${tone}`, stats.get(key));
  };
  pair('plays', 'PLAYS', 'text-slate-200');
  pair('buzz', 'BUZZES', 'text-slate-200');
  pair('conv', 'CONVERSION', 'text-neonCyan');
  pair('acc', 'ACCURACY', 'text-neonGreen');
  pair('avg', 'AVG BUZZ', 'text-slate-300');
  pair('celerity', 'CELERITY', 'text-slate-300');
  const arrow = bbxAppend(summary, 'span', 'text-slate-500 transition-transform duration-200', '\u25be');
  if (open) arrow.style.transform = 'rotate(180deg)';

  const body = bbxAppend(card, 'div', `border-t border-borderDark p-4 space-y-3 bg-black/40 ${open ? '' : 'hidden'}`);

  // Stats ribbon: the full telemetry, only once the card is open.
  const ribbon = bbxAppend(body, 'div', 'border border-borderDark rounded-lg bg-black px-3 py-2');
  const grid = bbxAppend(ribbon, 'div', 'grid grid-cols-3 sm:grid-cols-5 lg:grid-cols-10 gap-2 text-center font-mono');
  bbxQuestionStats(question).forEach(([label, value]) => {
    const cell = bbxAppend(grid, 'div', 'flex flex-col items-center');
    bbxAppend(cell, 'span', 'text-[9px] font-mono text-slate-500 uppercase tracking-widest border-b border-dotted border-slate-700 pb-0.5', label);
    bbxAppend(cell, 'span', 'text-xs font-bold text-slate-100 mt-1', value);
  });

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
      span.style.background = correct ? 'rgba(0,255,102,0.16)' : 'rgba(255,0,85,0.16)';
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
  const endBadge = bbxAppend(footLeft, 'span', 'px-2 py-0.5 rounded text-[11px] font-mono font-medium border border-neonPink/50 bg-black text-neonPink', `END \u00b7 ${endBuzzes.length}`);
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
    bbxAppend(footLeft, 'span', 'px-2 py-0.5 rounded text-[11px] font-mono text-slate-500 border border-borderDark bg-black', `NO BUZZ \u00b7 ${question.noBuzz}`);
  }
  const answerBtn = document.createElement('button');
  answerBtn.type = 'button';
  answerBtn.className = 'inline-flex items-center text-slate-400 hover:text-white font-medium';
  answerBtn.textContent = bbxState.showAnswers ? 'Hide answer' : 'Show answer';
  footLeft.appendChild(answerBtn);
  bbxAppend(foot, 'span', 'text-slate-500 font-mono text-[11px]', `Target: ${question.answer || '\u2014'}`);

  const answerBox = bbxAppend(body, 'div', 'bg-black border border-borderDark rounded-lg p-2.5 text-xs font-mono text-slate-200 hidden');
  bbxAppend(answerBox, 'span', 'font-bold text-neonGreen', 'Answer: ');
  bbxAppend(answerBox, 'span', null, question.answer || '\u2014');
  if (bbxState.showAnswers) answerBox.classList.remove('hidden');
  answerBtn.addEventListener('click', () => {
    const hidden = answerBox.classList.toggle('hidden');
    answerBtn.textContent = hidden ? 'Show answer' : 'Hide answer';
  });

  head.addEventListener('click', () => bbxToggleQuestion(question.key, body, arrow));
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
    btn.textContent = subject === 'ALL' ? 'All subjects' : subject;
    btn.className = `px-2.5 py-1 rounded text-[10px] font-mono uppercase border transition ${
      bbxState.subjectFilter === subject
        ? 'bg-neonCyan text-black border-neonCyan font-bold'
        : 'bg-black text-slate-400 border-borderDark hover:text-white'
    }`;
    btn.addEventListener('click', () => {
      bbxState.subjectFilter = subject;
      bbxRenderSubjectFilters();
      bbxRenderMatrix();
    });
    holder.appendChild(btn);
  });
}

// ------------------------------------------------------------ the team draft
// The template's right-hand column: three persistent team cards, each with subject coverage
// and four slots. A challenge's runs are individual, so teams are a manual draft the captain
// builds here; the stats underneath never change when a member moves.

function bbxRenderHud() {
  const holder = bbxEl('bbx-hud');
  if (!holder) return;
  holder.replaceChildren();
  if (!bbxState.players.size) {
    bbxAppend(holder, 'div', 'bg-panel border border-dashed border-borderDark rounded-xl p-4 text-center text-[11px] font-mono text-slate-500',
      'Load a board to draft teams.');
    return;
  }

  [1, 2, 3].forEach((teamId) => {
    const accent = BBX_TEAM_ACCENTS[teamId];
    const members = bbxTeamMembers(teamId);
    const card = bbxAppend(holder, 'div', 'bg-panel rounded-xl border-2 p-3 space-y-2.5 transition');
    card.style.borderColor = `${accent}55`;
    const rest = () => { card.style.borderColor = `${accent}55`; card.style.background = ''; };
    card.addEventListener('dragover', (event) => {
      event.preventDefault();
      card.style.borderColor = accent;
      card.style.background = `${accent}14`;
    });
    card.addEventListener('dragleave', rest);
    card.addEventListener('drop', (event) => {
      event.preventDefault();
      rest();
      const key = event.dataTransfer.getData('text/plain') || bbxDraggedPlayer;
      bbxDraggedPlayer = null;
      if (key) bbxAssignToTeam(key, teamId);
    });

    const head = bbxAppend(card, 'div', 'flex items-center justify-between');
    const left = bbxAppend(head, 'div', 'flex items-center gap-2');
    const dot = bbxAppend(left, 'span', 'w-2.5 h-2.5 rounded-full');
    dot.style.background = accent;
    bbxAppend(left, 'span', 'text-[11px] font-mono font-bold text-white uppercase', `Team ${teamId}`);
    bbxAppend(left, 'span', 'text-[10px] font-mono text-slate-400', `(${members.length}/${BBX_SQUAD_SIZE})`);
    const covered = bbxState.subjects.filter((subject) => members.some((member) => member.subjects[subject] && member.subjects[subject].attempts));
    const overall = bbxState.subjects.length ? Math.round((covered.length / bbxState.subjects.length) * 100) : 0;
    const cov = bbxAppend(head, 'span', 'text-[10px] font-mono font-bold', `${overall}% cov`);
    cov.style.color = accent;

    const meters = bbxAppend(card, 'div', 'grid grid-cols-6 gap-1 text-[9px] font-mono text-center text-slate-500 bg-black/50 rounded px-1.5 py-1');
    bbxState.subjects.forEach((subject) => {
      const cell = bbxAppend(meters, 'div', null);
      bbxAppend(cell, 'div', null, BBX_SUBJECT_SHORT[subject] || subject.slice(0, 3).toUpperCase());
      let best = null;
      members.forEach((member) => {
        const ppth = bbxPlayerSubjectPpth(member, subject);
        if (ppth !== null && (best === null || ppth > best)) best = ppth;
      });
      const pct = best === null ? 0 : Math.max(0, Math.min(100, Math.round((best / BBX_PPTH_CEILING) * 100)));
      const track = bbxAppend(cell, 'div', 'w-full bg-panel h-1 rounded-full overflow-hidden mt-0.5');
      const fill = bbxAppend(track, 'div', 'h-full rounded-full');
      fill.style.width = `${pct}%`;
      fill.style.background = pct > 0 ? accent : '#1e293b';
      bbxAppend(cell, 'div', `mt-0.5 ${pct >= 50 ? 'text-slate-200 font-bold' : 'text-slate-600'}`, `${pct}%`);
    });

    const slots = bbxAppend(card, 'div', 'space-y-1.5 min-h-[96px]');
    members.forEach((member) => {
      const row = bbxAppend(slots, 'div', 'flex items-center justify-between gap-2 bg-black border border-borderDark rounded-lg px-2 py-1.5 text-[10px] font-mono');
      const who = bbxAppend(row, 'div', 'min-w-0 flex items-center gap-2');
      const nameDot = bbxAppend(who, 'span', 'w-1.5 h-1.5 rounded-full shrink-0');
      nameDot.style.background = accent;
      const name = bbxAppend(who, 'span', 'font-bold text-white truncate', member.name);
      name.title = member.name;
      bbxAppend(who, 'span', 'text-slate-500 truncate', bbxPrimarySpecialty(member) || 'untested');
      const right = bbxAppend(row, 'div', 'flex items-center gap-1.5 shrink-0');
      const points = bbxPlayerPoints(member);
      const pointsEl = bbxAppend(right, 'span', 'font-bold', `${points} pts`);
      pointsEl.style.color = points >= 0 ? '#00ff66' : '#ff0055';
      const remove = bbxAppend(right, 'button', 'text-slate-500 hover:text-neonPink transition px-0.5', '\u00d7');
      remove.type = 'button';
      remove.title = `Remove ${member.name} from Team ${teamId}`;
      remove.addEventListener('click', () => bbxRemoveFromTeam(member.key, teamId));
    });
    for (let slot = members.length; slot < BBX_SQUAD_SIZE; slot += 1) {
      bbxAppend(slots, 'div', 'border border-dashed border-borderDark/80 rounded-lg py-2 text-center text-[10px] font-mono text-slate-600', `+ Slot ${slot + 1} open`);
    }

    const foot = bbxAppend(card, 'div', 'flex items-center justify-between gap-2 text-[9px] font-mono text-slate-500 pt-1.5 border-t border-borderDark');
    bbxAppend(foot, 'span', 'truncate', members.length
      ? `${covered.length}/${bbxState.subjects.length} subjects covered`
      : 'Drop a candidate here');
    const clear = bbxAppend(foot, 'button', 'text-slate-500 hover:text-neonPink uppercase tracking-wide transition', 'Clear');
    clear.type = 'button';
    clear.addEventListener('click', () => {
      members.forEach((member) => { member.team = 0; });
      bbxRenderHud();
      bbxRenderMatrix();
    });
  });
}

function bbxAssignToTeam(memberKey, teamId) {
  const player = bbxState.players.get(memberKey);
  if (!player) return;
  if (player.team !== teamId && bbxTeamMembers(teamId).length >= BBX_SQUAD_SIZE) {
    bbxStatus(`Team ${teamId} is full (${BBX_SQUAD_SIZE}/${BBX_SQUAD_SIZE}) \u2014 remove someone first.`, 'bad');
    return;
  }
  player.team = teamId;
  bbxRenderHud();
  bbxRenderMatrix();
  bbxStatus(`${player.name} \u2192 Team ${teamId}.`, 'good');
}

function bbxRemoveFromTeam(memberKey, teamId) {
  const player = bbxState.players.get(memberKey);
  if (!player || player.team !== teamId) return;
  player.team = 0;
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

  const head = bbxAppend(table, 'thead', 'sticky top-0 z-10 bg-panel');
  const headRow = bbxAppend(head, 'tr', 'border-b border-borderDark text-[10px] text-slate-500 uppercase select-none');
  const th = (label, title, onClick, extra) => {
    const cell = bbxAppend(headRow, 'th', `py-2 px-2 ${extra || ''} ${onClick ? 'cursor-pointer hover:text-white' : ''}`, label);
    if (title) cell.title = title;
    if (onClick) cell.addEventListener('click', onClick);
    return cell;
  };
  th('Candidate / Buzzes', 'Sorted by name', () => bbxSortBy('name'), 'text-left px-3');
  th('Overall PPTH', 'Points per tossup heard: +4 correct, -4 neg', () => bbxSortBy('ppth'), 'text-center');
  th('Specialty', 'Best subject by PPTH', null, 'text-center');
  bbxState.subjects.forEach((subject) => {
    th(BBX_SUBJECT_SHORT[subject] || subject.slice(0, 3).toUpperCase(),
      'Points per tossup heard in this subject: +4 correct, -4 neg',
      () => bbxSortBy(subject),
      `text-center ${bbxState.rawColumns ? '' : 'hidden'} bbx-raw-col`);
  });
  th('Acc %', 'Accuracy: correct \u00f7 buzzes', () => bbxSortBy('acc'), 'text-center');
  th('Correct', 'Tossups answered correctly', () => bbxSortBy('correct'), 'text-center');
  th('Incorrect', 'Wrong answers after the question was fully read \u2014 incorrect, not a neg', () => bbxSortBy('incorrect'), 'text-center');
  th('Pts', `Overall points: +${BBX_CORRECT_POINTS} per correct tossup, ${BBX_NEG_POINTS} per neg`, () => bbxSortBy('pts'), 'text-center');
  th('Negs', 'Interrupts answered wrong, which carry the neg penalty', () => bbxSortBy('negs'), 'text-center');
  th('Speed', 'Average buzz depth: how far into the question the buzz landed (higher = later)', () => bbxSortBy('speed'), 'text-center');
  th('Quick Assign', 'Add this candidate to a team', null, 'text-center');

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
  const note = bbxEl('bbx-matrix-note');
  if (note) note.textContent = `${list.length} shown \u00b7 ${bbxState.players.size} total`;

  if (!list.length) {
    const row = bbxAppend(body, 'tr');
    bbxAppend(row, 'td', 'py-4 text-center text-slate-600', 'Awaiting data...')
      .setAttribute('colspan', String(10 + bbxState.subjects.length));
    return;
  }

  list.forEach((player) => {
    const accent = BBX_TEAM_ACCENTS[player.team] || null;
    const row = bbxAppend(body, 'tr', 'transition-colors hover:bg-panelHover cursor-grab');
    if (accent) row.style.background = `${accent}10`;
    row.draggable = true;
    row.title = 'Drag into a team card on the right';
    row.addEventListener('dragstart', (event) => {
      bbxDraggedPlayer = player.key;
      event.dataTransfer.setData('text/plain', player.key);
      event.dataTransfer.effectAllowed = 'move';
      row.style.opacity = '0.4';
    });
    row.addEventListener('dragend', () => {
      row.style.opacity = '';
      bbxDraggedPlayer = null;
    });

    bbxAppend(row, 'td', 'py-2 px-3').appendChild(bbxNameCell(player));

    const ppth = bbxPlayerPpth(player);
    const ppthCell = bbxAppend(row, 'td', 'py-2 px-2 text-center');
    bbxAppend(ppthCell, 'span',
      `px-2 py-0.5 rounded font-bold ${ppth !== null && ppth >= 2 ? 'bg-panelHover text-neonCyan border border-neonCyan/40' : 'text-slate-300'}`,
      ppth === null ? '\u2014' : ppth.toFixed(2));

    const specialty = bbxPrimarySpecialty(player);
    bbxAppend(row, 'td', 'py-2 px-2 text-center')
      .appendChild(bbxAppend(null, 'span', 'px-1.5 py-0.5 rounded text-[10px] bg-panelHover text-slate-300 border border-borderDark', specialty || '\u2014'));

    bbxState.subjects.forEach((subject) => {
      const subjectPpth = bbxPlayerSubjectPpth(player, subject);
      const cell = bbxAppend(row, 'td', `py-2 px-2 text-center font-mono ${bbxState.rawColumns ? '' : 'hidden'} bbx-raw-col`);
      if (subjectPpth === null) {
        bbxAppend(cell, 'span', 'text-slate-600', '\u2014');
      } else {
        bbxAppend(cell, 'span', subjectPpth >= 2 ? 'font-bold text-slate-100' : (subjectPpth < 0 ? 'text-neonPink' : 'text-slate-300'), subjectPpth.toFixed(1));
      }
    });

    const acc = bbxPlayerAccuracy(player);
    bbxAppend(row, 'td', 'py-2 px-2 text-center font-mono')
      .appendChild(bbxAppend(null, 'span', acc !== null && acc >= 80 ? 'font-bold text-neonGreen' : 'text-slate-300', bbxPctText(acc)));

    bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-200', player.correct);

    bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono text-slate-400', player.incorrect);

    const points = bbxPlayerPoints(player);
    const pointsCell = bbxAppend(row, 'td', 'py-1.5 px-2 text-center font-mono font-bold', `${points > 0 ? '+' : ''}${points}`);
    pointsCell.style.color = points > 0 ? '#00ff66' : (points < 0 ? '#ff0055' : '#94a3b8');

    bbxAppend(row, 'td', `py-2 px-2 text-center font-mono ${player.negs >= 4 ? 'text-neonPink font-bold' : 'text-slate-400'}`, player.negs);

    const speed = bbxPlayerSpeed(player);
    bbxAppend(row, 'td', 'py-2 px-2 text-center font-mono text-slate-400', speed === null ? '\u2014' : `${Math.round(speed)}%`);

    const quick = bbxAppend(row, 'td', 'py-2 px-2 text-center');
    const buttons = bbxAppend(quick, 'div', 'flex items-center justify-center gap-1');
    [1, 2, 3].forEach((teamId) => {
      const btn = bbxAppend(buttons, 'button', 'px-1.5 py-0.5 rounded bg-panel border border-borderDark text-[9px] font-mono font-bold transition', `+T${teamId}`);
      btn.type = 'button';
      btn.title = `Add ${player.name} to Team ${teamId}`;
      btn.style.color = BBX_TEAM_ACCENTS[teamId];
      if (player.team === teamId) {
        btn.style.background = BBX_TEAM_ACCENTS[teamId];
        btn.style.borderColor = BBX_TEAM_ACCENTS[teamId];
        btn.style.color = '#04060a';
      }
      btn.addEventListener('click', () => bbxAssignToTeam(player.key, teamId));
    });
  });
}

function bbxNameCell(player) {
  const wrap = document.createElement('div');
  wrap.className = 'flex items-center gap-1.5 min-w-0';
  const name = bbxAppend(wrap, 'span', 'font-semibold truncate', player.name);
  name.style.color = BBX_MEMBER_COLORS[[...bbxState.players.keys()].indexOf(player.key) % BBX_MEMBER_COLORS.length];
  bbxAppend(wrap, 'span', 'text-slate-500 font-mono text-[10px]', `${player.attempts} heard \u00b7 ${player.buzzes} buzzed`);
  return wrap;
}

// ------------------------------------------------------------ wiring

bbxShowPage('questions');
showAnalyticsView('match');
