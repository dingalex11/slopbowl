const csvDropZone = document.getElementById('csv-drop-zone');
const csvFileInput = document.getElementById('csv-file-input');
const teamStatsBody = document.getElementById('team-stats-body');
const playerStatsBody = document.getElementById('player-stats-body');
const csvStatusText = document.getElementById('csv-status-text');
const csvCountBadge = document.getElementById('csv-count-badge');

let globalTeamStats = {};
let globalPlayerStats = {};
let gamesProcessed = new Set();

if(csvDropZone) {
    csvDropZone.addEventListener('dragover', (e) => { e.preventDefault(); csvDropZone.classList.add('dragover'); });
    csvDropZone.addEventListener('dragleave', () => { csvDropZone.classList.remove('dragover'); });
    csvDropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        csvDropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length) handleCSVFiles(e.dataTransfer.files);
    });
}

if(csvFileInput) {
    csvFileInput.addEventListener('change', (e) => {
        if (e.target.files.length) handleCSVFiles(e.target.files);
    });
}

function resetAnalytics() {
    globalTeamStats = {};
    globalPlayerStats = {};
    gamesProcessed.clear();
    if(teamStatsBody) teamStatsBody.innerHTML = '<tr><td colspan="7" class="py-4 text-center text-slate-600">Awaiting data...</td></tr>';
    if(playerStatsBody) playerStatsBody.innerHTML = '<tr><td colspan="7" class="py-4 text-center text-slate-600">Awaiting data...</td></tr>';
    if(csvCountBadge) csvCountBadge.style.display = "none";
    if(csvStatusText) {
        csvStatusText.innerText = "SYSTEM READY // WAITING FOR MATCH CSV";
        csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-slate-400";
    }
}

async function handleCSVFiles(files) {
    if (files.length > 20) {
        if (csvStatusText) csvStatusText.innerText = "Select no more than 20 match files at a time.";
        return;
    }
    if (csvStatusText) {
        csvStatusText.innerText = "PROCESSING CSV DATA...";
        csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-amber-500 font-bold";
    }
    let accepted = 0;
    for (let i = 0; i < files.length; i++) {
        const file = files[i];
        if (!file.name.toLowerCase().endsWith('.csv')) continue;
        
        if (file.size > 10 * 1024 * 1024) {
            if (csvStatusText) csvStatusText.innerText = `${file.name} is larger than the 10 MB limit.`;
            continue;
        }
        try {
            const text = await file.text();
            processCSVData(text, file.name);
            accepted += 1;
        } catch (error) {
            if (csvStatusText) csvStatusText.innerText = `${file.name}: ${error.message}`;
        }
    }

    renderAnalyticsTables();
    
    if (csvCountBadge) {
        csvCountBadge.style.display = "block";
        csvCountBadge.innerText = `${gamesProcessed.size} GAMES PROCESSED`;
    }
    if (csvStatusText && accepted > 0) {
        csvStatusText.innerText = "ANALYTICS COMPILED SUCCESSFULLY";
        csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-teamA font-bold";
    }
}

function parseCSVRows(csvText) {
    if (csvText.length > 10 * 1024 * 1024) throw new Error('CSV exceeds the 10 MB limit');
    const rows = [];
    let row = [];
    let field = '';
    let quoted = false;
    for (let i = 0; i < csvText.length; i++) {
        const char = csvText[i];
        if (quoted) {
            if (char === '"' && csvText[i + 1] === '"') { field += '"'; i++; }
            else if (char === '"') quoted = false;
            else field += char;
        } else if (char === '"') quoted = true;
        else if (char === ',') { row.push(field); field = ''; }
        else if (char === '\n') {
            row.push(field.replace(/\r$/, ''));
            rows.push(row);
            if (rows.length > 100000) throw new Error('CSV exceeds the 100,000 row limit');
            row = [];
            field = '';
        }
        else field += char;
    }
    if (quoted) throw new Error('Malformed CSV: unclosed quoted field');
    if (field.length || row.length) { row.push(field.replace(/\r$/, '')); rows.push(row); }
    return rows;
}

function processCSVData(csvText, fileName) {
    const rows = parseCSVRows(csvText);
    if (!rows.length) return;
    if (gamesProcessed.has(fileName)) return;
    gamesProcessed.add(fileName);
    const headers = rows[0].map((header) => header.trim());

    rows.slice(1).forEach((values) => {
        const row = {};
        headers.forEach((header, i) => { row[header] = values[i] ? values[i].trim() : ''; });
        const eventType = row['Event_Type'];
        const team = row['Team'];
        const player = row['Player'];
        if (!team || !eventType) return;

        const points = parseInt(row['Points']) || 0;
        const isCorrect = row['Correct'] === 'True';
        const buzzTime = parseFloat(row['Buzz_Time']) || 0.0;
        const buzzpoint = row['Buzzpoint'] || "";

        if (!globalTeamStats[team]) {
            globalTeamStats[team] = { games: new Set(), total_points: 0, tossup_points: 0, bonus_points: 0, correct: 0, negs: 0, interrupts: 0 };
        }
        globalTeamStats[team].games.add(fileName);
        globalTeamStats[team].total_points += points;

        if (eventType === 'TOSSUP' || eventType === 'TOSSUP_REBOUND') {
            globalTeamStats[team].tossup_points += points;
            if (isCorrect) globalTeamStats[team].correct += 1;
            if (points === -4) { globalTeamStats[team].negs += 1; globalTeamStats[team].interrupts += 1; }
        } else if (eventType === 'BONUS') {
            globalTeamStats[team].bonus_points += points;
        }

        if (player && (eventType === 'TOSSUP' || eventType === 'TOSSUP_REBOUND')) {
            const pKey = `${team} - ${player}`;
            if (!globalPlayerStats[pKey]) {
                globalPlayerStats[pKey] = { team: team, games: new Set(), buzzes: 0, points: 0, correct: 0, negs: 0, int_attempts: 0, int_correct: 0, total_speed: 0.0 };
            }
            
            let p = globalPlayerStats[pKey];
            p.games.add(fileName);
            p.buzzes += 1;
            p.points += points;
            if (buzzTime > 0) p.total_speed += buzzTime;

            const isInterrupt = (buzzpoint.toLowerCase() !== "full read" && buzzpoint !== "") || points === -4;
            
            if (isCorrect) p.correct += 1;
            if (points === -4) p.negs += 1;

            if (isInterrupt) {
                p.int_attempts += 1;
                if (isCorrect) p.int_correct += 1;
            }
        }
    });
}

function renderAnalyticsTables() {
    if(teamStatsBody) teamStatsBody.innerHTML = "";
    const sortedTeams = Object.entries(globalTeamStats).sort((a, b) => (b[1].total_points / b[1].games.size) - (a[1].total_points / a[1].games.size));
    
    sortedTeams.forEach(([team, data]) => {
        const gp = data.games.size;
        const ppg = (data.total_points / gp).toFixed(2);
        const ppb = data.correct > 0 ? (data.bonus_points / data.correct).toFixed(2) : "0.00";

        const tr = document.createElement('tr');
        const teamCell = document.createElement('td');
        teamCell.className = 'py-2 px-3 font-bold';
        teamCell.textContent = team;
        tr.append(teamCell);
        for (const [value, className] of [
            [gp, 'py-2 px-3'], [data.total_points, 'py-2 px-3'], [ppg, 'py-2 px-3 text-teamA'],
            [ppb, 'py-2 px-3 text-purple-300 font-bold'], [data.correct, 'py-2 px-3'], [data.negs, 'py-2 px-3 text-red-400']
        ]) {
            const cell = document.createElement('td');
            cell.className = className;
            cell.textContent = String(value);
            tr.append(cell);
        }
        if(teamStatsBody) teamStatsBody.appendChild(tr);
    });

    if(playerStatsBody) playerStatsBody.innerHTML = "";
    const sortedPlayers = Object.entries(globalPlayerStats).sort((a, b) => b[1].points - a[1].points);

    sortedPlayers.forEach(([pKey, p]) => {
        const gp = p.games.size;
        const ppg = (p.points / gp).toFixed(2);
        const ppa = p.buzzes > 0 ? (p.points / p.buzzes).toFixed(2) : "0.00";
        const intRate = p.buzzes > 0 ? ((p.int_attempts / p.buzzes) * 100).toFixed(1) : "0.0";
        const intAcc = p.int_attempts > 0 ? ((p.int_correct / p.int_attempts) * 100).toFixed(1) : "0.0";
        const avgSpd = p.buzzes > 0 && p.total_speed > 0 ? (p.total_speed / p.buzzes).toFixed(3) : "N/A";

        const tr = document.createElement('tr');
        const identityCell = document.createElement('td');
        identityCell.className = 'py-2 px-3 font-bold';
        const playerName = document.createElement('span');
        playerName.className = 'block';
        playerName.textContent = pKey.split('-').slice(1).join('-').trim();
        const teamName = document.createElement('span');
        teamName.className = 'text-[9px] text-slate-500';
        teamName.textContent = p.team;
        identityCell.append(playerName, teamName);
        tr.append(identityCell);
        for (const [value, className] of [
            [p.points, 'py-2 px-3'], [ppg, 'py-2 px-3'], [ppa, 'py-2 px-3 text-amber-300 font-bold'],
            [`${intAcc}%`, `py-2 px-3 ${intAcc > 50 ? 'text-teamA' : 'text-slate-400'}`],
            [`${intRate}%`, 'py-2 px-3 text-red-400'], [avgSpd, 'py-2 px-3 text-cyan-400']
        ]) {
            const cell = document.createElement('td');
            cell.className = className;
            cell.textContent = String(value);
            tr.append(cell);
        }
        if(playerStatsBody) playerStatsBody.appendChild(tr);
    });
}
// ... [Existing analytics.js code] ...

const btnCloudAnalytics = document.getElementById('btn-cloud-analytics');

if (btnCloudAnalytics) {
    btnCloudAnalytics.addEventListener('click', async () => {
        if (!window.loadMatchLogsFromCloud) return;
        
        if (csvStatusText) {
            csvStatusText.innerText = "FETCHING CLOUD LOGS...";
            csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-amber-500 font-bold";
        }
        
        try {
            const logs = await window.loadMatchLogsFromCloud();
            
            if (logs.length === 0) {
                alert("No match logs found in your cloud profile.");
                resetAnalytics();
                return;
            }
            
            // Standard CSV Headers expected by your processCSVData function
            const headers = ["Q_Num", "Event_Type", "Team", "Player", "Correct", "Points", "Buzzpoint", "Buzz_Time", "Score_A", "Score_B"];
            
            logs.forEach(cloudDoc => {
                const fileName = `cloud_${cloudDoc.roomId}_${cloudDoc.updatedAt}.csv`;
                let csvContent = headers.join(",") + "\n";
                
                // Convert the JSON payload array back to a raw CSV string
                cloudDoc.log.forEach(row => {
                    const values = headers.map(h => {
                        let val = row[h] !== undefined ? row[h] : "";
                        // Wrap in quotes to prevent internal commas from breaking the parser
                        return `"${String(val).replaceAll('"', '""')}"`; 
                    });
                    csvContent += values.join(",") + "\n";
                });
                
                // Feed the generated CSV string directly into your existing parser!
                processCSVData(csvContent, fileName);
            });
            
            renderAnalyticsTables();
            
            if (csvCountBadge) {
                csvCountBadge.style.display = "block";
                csvCountBadge.innerText = `${gamesProcessed.size} GAMES PROCESSED`;
            }
            if (csvStatusText) {
                csvStatusText.innerText = "CLOUD ANALYTICS COMPILED SUCCESSFULLY";
                csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-teamA font-bold";
            }
            
        } catch (err) {
            console.error("Error loading cloud analytics:", err);
            if (csvStatusText) {
                csvStatusText.innerText = "ERROR FETCHING CLOUD LOGS";
                csvStatusText.className = "text-xs font-mono mt-1 block uppercase text-red-500 font-bold";
            }
            alert("Failed to load cloud logs. Check console for details.");
        }
    });
}