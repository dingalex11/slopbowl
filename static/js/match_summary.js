/* Match summary: a final-score overlay plus a two-line score-by-question chart.
 * The server sends the adjudicated match log with MATCH_COMPLETE; no chart library is
 * needed, and every value drawn into the SVG is a number or a fixed label. */
(function () {
    const SVG_NS = 'http://www.w3.org/2000/svg';
    const screen = document.getElementById('match-end-screen');
    const graph = document.getElementById('match-score-graph');
    let lastPayload = null;
    let lastLog = [];

    function number(value, fallback = 0) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function setText(id, text) {
        const element = document.getElementById(id);
        if (element) element.textContent = String(text);
    }

    function teamName(id, fallback) {
        const element = document.getElementById(id);
        const name = element ? element.textContent.trim() : '';
        return name || fallback;
    }

    function svgNode(tag, attributes = {}, text = '') {
        const node = document.createElementNS(SVG_NS, tag);
        Object.entries(attributes).forEach(([key, value]) => node.setAttribute(key, String(value)));
        if (text) node.textContent = text;
        return node;
    }

    // Build the score history from the log. Rows are chronological, so the last row for a
    // question carries the cumulative score after that question. Missing questions carry
    // the previous score forward; the chart starts at a 0-0 baseline.
    function buildSeries(log, questionCount) {
        const byQuestion = new Map();
        (Array.isArray(log) ? log : []).forEach((row) => {
            const question = Math.round(number(row && row.Q_Num, 0));
            if (question < 1) return;
            const scoreA = Number(row.Score_A);
            const scoreB = Number(row.Score_B);
            if (!Number.isFinite(scoreA) && !Number.isFinite(scoreB)) return;
            const previous = byQuestion.get(question) || { a: 0, b: 0 };
            byQuestion.set(question, {
                a: Number.isFinite(scoreA) ? scoreA : previous.a,
                b: Number.isFinite(scoreB) ? scoreB : previous.b,
            });
        });

        let maxQuestion = Math.max(1, Math.round(number(questionCount, 1)));
        byQuestion.forEach((_, question) => {
            maxQuestion = Math.max(maxQuestion, question);
        });

        const series = [{ q: 0, a: 0, b: 0 }];
        let scoreA = 0;
        let scoreB = 0;
        for (let question = 1; question <= maxQuestion; question += 1) {
            const entry = byQuestion.get(question);
            if (entry) {
                scoreA = entry.a;
                scoreB = entry.b;
            }
            series.push({ q: question, a: scoreA, b: scoreB });
        }
        return { series, maxQuestion };
    }

    function boundsFor(series) {
        let low = 0;
        let high = 10;
        series.forEach((point) => {
            low = Math.min(low, point.a, point.b);
            high = Math.max(high, point.a, point.b);
        });
        const span = high - low;
        const step = span <= 20 ? 5 : span <= 60 ? 10 : span <= 120 ? 20 : 50;
        return {
            low: Math.floor(low / step) * step,
            high: Math.ceil(high / step) * step,
            step,
        };
    }

    function renderGraph(log, questionCount, nameA, nameB) {
        if (!graph) return;
        const { series, maxQuestion } = buildSeries(log, questionCount);
        const bounds = boundsFor(series);
        const width = 760;
        const height = 300;
        const margin = { left: 48, right: 20, top: 22, bottom: 38 };
        const plotWidth = width - margin.left - margin.right;
        const plotHeight = height - margin.top - margin.bottom;

        const x = (question) => margin.left + (question / maxQuestion) * plotWidth;
        const y = (score) => margin.top + ((bounds.high - score) / (bounds.high - bounds.low)) * plotHeight;

        graph.replaceChildren();
        const title = svgNode('title', {}, `${nameA} and ${nameB} scores by question`);
        graph.appendChild(title);

        for (let score = bounds.low; score <= bounds.high; score += bounds.step) {
            const lineY = y(score);
            graph.appendChild(svgNode('line', {
                x1: margin.left,
                x2: margin.left + plotWidth,
                y1: lineY,
                y2: lineY,
                class: score === 0 ? 'match-graph-zero' : 'match-graph-grid',
            }));
            graph.appendChild(svgNode('text', {
                x: margin.left - 9,
                y: lineY + 3.5,
                'text-anchor': 'end',
                class: 'match-graph-label',
            }, String(score)));
        }

        const labelStep = maxQuestion <= 6 ? 1 : Math.ceil(maxQuestion / 6);
        const labels = new Set([0, maxQuestion]);
        for (let question = labelStep; question < maxQuestion; question += labelStep) {
            labels.add(question);
        }
        Array.from(labels).sort((a, b) => a - b).forEach((question) => {
            const anchor = question === 0 ? 'start' : question === maxQuestion ? 'end' : 'middle';
            graph.appendChild(svgNode('text', {
                x: x(question),
                y: height - 14,
                'text-anchor': anchor,
                class: 'match-graph-label',
            }, question === 0 ? 'START' : `Q${question}`));
        });

        graph.appendChild(svgNode('text', {
            x: margin.left + plotWidth / 2,
            y: height - 1,
            'text-anchor': 'middle',
            class: 'match-graph-axis-title',
        }, 'QUESTION'));

        const addSeries = (key, name, cssClass) => {
            const points = series.map((point) => `${x(point.q).toFixed(2)},${y(point[key]).toFixed(2)}`).join(' ');
            graph.appendChild(svgNode('polyline', {
                points,
                class: `match-graph-line ${cssClass}`,
            }));
            series.forEach((point) => {
                const dot = svgNode('circle', {
                    cx: x(point.q),
                    cy: y(point[key]),
                    r: 3.2,
                    class: `match-graph-dot ${cssClass}`,
                });
                dot.appendChild(svgNode('title', {}, `Q${point.q}: ${name} ${point[key]}`));
                graph.appendChild(dot);
            });
        };
        addSeries('a', nameA, 'match-graph-a');
        addSeries('b', nameB, 'match-graph-b');

        graph.setAttribute('aria-label', `${nameA} and ${nameB} scores by question across ${maxQuestion} questions`);
    }

    function show(payload, log) {
        if (!screen) return;
        const safe = payload && typeof payload === 'object' ? payload : {};
        const scoreA = Math.round(number(safe.score_a, 0));
        const scoreB = Math.round(number(safe.score_b, 0));
        const graphLog = Array.isArray(log) ? log : (Array.isArray(safe.log) ? safe.log : []);
        const questionCount = Math.max(0, Math.round(number(
            safe.question_count,
            graphLog.reduce((highest, row) => Math.max(highest, number(row && row.Q_Num, 0)), 0)
        )));

        const nameA = teamName('side-team-a-name', teamName('hud-team-a-name', 'Team A'));
        const nameB = teamName('side-team-b-name', teamName('hud-team-b-name', 'Team B'));
        const result = scoreA === scoreB
            ? `Tie game, ${scoreA}-${scoreB}`
            : `${scoreA > scoreB ? nameA : nameB} wins, ${Math.max(scoreA, scoreB)}-${Math.min(scoreA, scoreB)}`;

        setText('match-end-team-a-name', nameA);
        setText('match-end-team-b-name', nameB);
        setText('match-end-legend-a', nameA);
        setText('match-end-legend-b', nameB);
        setText('match-end-score-a', scoreA);
        setText('match-end-score-b', scoreB);
        setText('match-end-result', result);
        setText('match-end-question-count', `${questionCount} question${questionCount === 1 ? '' : 's'} played`);

        renderGraph(graphLog, questionCount, nameA, nameB);
        lastPayload = {
            score_a: scoreA,
            score_b: scoreB,
            question_count: questionCount,
            teardown: Boolean(safe.teardown),
        };
        lastLog = graphLog;

        const closeFooter = document.getElementById('btn-close-match-end-footer');
        if (closeFooter) closeFooter.textContent = safe.teardown ? 'Close Match' : 'Close';
        screen.classList.remove('hidden');
        screen.setAttribute('aria-hidden', 'false');
        // A natural packet ending can still be reviewed without destroying the room.
        // An explicit End Match hides the now-finished control.
        document.getElementById('btn-end-match')?.classList.toggle('hidden', Boolean(safe.teardown));
        window.setTimeout(() => document.getElementById('btn-close-match-end')?.focus(), 0);
    }

    function hide() {
        if (!screen) return;
        screen.classList.add('hidden');
        screen.setAttribute('aria-hidden', 'true');
    }

    function showLast() {
        if (lastPayload) show(lastPayload, lastLog);
    }

    function requestClose() {
        if (!screen || screen.classList.contains('hidden')) return;
        if (lastPayload && lastPayload.teardown && typeof window.requestMatchRoomClose === 'function') {
            window.requestMatchRoomClose();
            return;
        }
        hide();
    }

    function reset() {
        lastPayload = null;
        lastLog = [];
        hide();
    }

    ['btn-close-match-end', 'btn-close-match-end-footer'].forEach((id) => {
        document.getElementById(id)?.addEventListener('click', requestClose);
    });
    document.addEventListener('keydown', (event) => {
        if (event.key === 'Escape' && screen && !screen.classList.contains('hidden')) requestClose();
    });

    window.MatchSummary = { show, hide, showLast, reset };
})();
