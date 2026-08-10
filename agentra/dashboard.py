"""TASK-015: the observability dashboard's HTML, served as one self-contained
page (no build step, no CDN dependency) from GET / in server.py. All the
actual data comes from server.py's own JSON APIs (GET /system/status,
/apps, /runs, /signals) via plain fetch() polling -- this module is just
markup/CSS/JS, no server-side templating, so the same page works identically
whether it's fetched from a local `agentra serve` or the deployed Cloud Run
service.
"""

DASHBOARD_HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agentra</title>
<style>
  :root {
    color-scheme: light dark;
    --bg: #0b0d12;
    --panel: #12151c;
    --border: #262b36;
    --text: #e6e9ef;
    --muted: #8b93a3;
    --accent: #5b8cff;
    --good: #37c98f;
    --bad: #ef5a6f;
    --warn: #e3b341;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 24px;
    border-bottom: 1px solid var(--border);
  }
  header h1 { font-size: 16px; margin: 0; font-weight: 600; letter-spacing: 0.02em; }
  header h1 span { color: var(--muted); font-weight: 400; }
  #status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 600;
    border: 1px solid var(--border);
  }
  #status-pill.running { color: var(--good); }
  #status-pill.paused { color: var(--bad); }
  #status-pill .dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; }
  main { padding: 24px; max-width: 1100px; margin: 0 auto; display: grid; gap: 20px; }
  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 16px 18px;
  }
  .panel h2 {
    font-size: 13px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
    margin: 0 0 12px 0;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  button {
    background: var(--accent);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12px;
    cursor: pointer;
    font-weight: 600;
  }
  button.danger { background: var(--bad); }
  button.ghost { background: transparent; border: 1px solid var(--border); color: var(--text); }
  button:disabled { opacity: 0.5; cursor: default; }
  input[type=text] {
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 13px;
    width: 100%;
  }
  form.register-form { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; align-items: end; }
  form.register-form label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
  form.register-form .full { grid-column: 1 / -1; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; }
  .badge.completed { background: rgba(55,201,143,0.15); color: var(--good); }
  .badge.failed { background: rgba(239,90,111,0.15); color: var(--bad); }
  .badge.running, .badge.queued { background: rgba(227,179,65,0.15); color: var(--warn); }
  #msg { font-size: 12px; color: var(--muted); min-height: 16px; margin-top: 8px; }
  code { color: var(--accent); }
  .standup-entry { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .standup-entry:last-child { border-bottom: none; }
  .standup-entry .head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
  .standup-entry .head strong { font-size: 13px; }
  .standup-entry .date { color: var(--muted); font-size: 11px; }
  .standup-entry .body { white-space: pre-wrap; font-size: 13px; color: var(--text); }
  .standup-entry .body.none { color: var(--muted); font-style: italic; }
</style>
</head>
<body>

<header>
  <h1>agentra <span>&mdash; orchestrator dashboard</span></h1>
  <div style="display:flex; align-items:center; gap:12px;">
    <span id="status-pill"><span class="dot"></span><span id="status-text">loading…</span></span>
    <button id="pause-btn" class="danger">Pause</button>
    <button id="resume-btn" class="ghost" style="display:none;">Resume</button>
  </div>
</header>

<main>

  <div class="panel">
    <h2>Register a repo</h2>
    <form class="register-form" id="register-form">
      <div>
        <label>App name</label>
        <input type="text" name="name" placeholder="my-app" required>
      </div>
      <div>
        <label>Branch</label>
        <input type="text" name="branch" placeholder="main" value="main">
      </div>
      <div class="full">
        <label>GitHub repo URL</label>
        <input type="text" name="repo_url" placeholder="https://github.com/org/repo.git" required>
      </div>
      <div class="full">
        <label>Objective (optional)</label>
        <input type="text" name="objective" placeholder="what should agentra work toward for this repo?">
      </div>
      <div class="full">
        <button type="submit">Register &amp; clone</button>
      </div>
    </form>
    <div id="msg"></div>
  </div>

  <div class="panel">
    <h2>Registered apps</h2>
    <table id="apps-table">
      <thead><tr><th>App</th><th>Objective</th><th>Shipped</th><th>Known bugs</th><th></th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="apps-empty" style="display:none;">No apps registered yet.</div>
  </div>

  <div class="panel">
    <h2>Daily standup</h2>
    <div id="standup-list"></div>
    <div class="empty" id="standup-empty" style="display:none;">No apps registered yet.</div>
  </div>

  <div class="panel">
    <h2>Runs (this instance)</h2>
    <table id="runs-table">
      <thead><tr><th>App</th><th>Source</th><th>Status</th><th>Started</th><th>Detail</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="runs-empty" style="display:none;">No runs yet.</div>
  </div>

  <div class="panel">
    <h2>Recent signals</h2>
    <table id="signals-table">
      <thead><tr><th>Time</th><th>Source</th><th>Detail</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="empty" id="signals-empty" style="display:none;">No signals logged yet.</div>
  </div>

</main>

<script>
const $ = (sel) => document.querySelector(sel);
const esc = (s) => (s ?? "").toString().replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtTime = (epochSeconds) => epochSeconds ? new Date(epochSeconds * 1000).toLocaleString() : "";

async function jsonFetch(url, opts) {
  const res = await fetch(url, opts);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || res.statusText);
  return body;
}

async function refreshStatus() {
  const data = await jsonFetch("/system/status");
  const pill = $("#status-pill"), text = $("#status-text");
  if (data.paused) {
    pill.className = "paused";
    text.textContent = "PAUSED" + (data.pause_record?.reason ? " — " + data.pause_record.reason : "");
    $("#pause-btn").style.display = "none";
    $("#resume-btn").style.display = "inline-block";
  } else {
    pill.className = "running";
    text.textContent = "RUNNING";
    $("#pause-btn").style.display = "inline-block";
    $("#resume-btn").style.display = "none";
  }
}

async function refreshApps() {
  const data = await jsonFetch("/apps");
  const rows = Object.entries(data.apps);
  $("#apps-empty").style.display = rows.length ? "none" : "block";
  $("#apps-table tbody").innerHTML = rows.map(([name, info]) => `
    <tr>
      <td><strong>${esc(name)}</strong></td>
      <td>${esc(info.objective) || "<span class=\\"empty\\">none set</span>"}</td>
      <td>${info.shipped_count}</td>
      <td>${info.known_bugs}</td>
      <td><button class="ghost run-now" data-app="${esc(name)}">Run now</button></td>
    </tr>
  `).join("");
}

async function refreshStandups() {
  const appsData = await jsonFetch("/apps");
  const names = Object.keys(appsData.apps);
  $("#standup-empty").style.display = names.length ? "none" : "block";
  const entries = await Promise.all(names.map(async (name) => {
    const data = await jsonFetch(`/apps/${encodeURIComponent(name)}/standup/latest`).catch(() => ({standup: null}));
    return { name, standup: data.standup };
  }));
  $("#standup-list").innerHTML = entries.map(({name, standup}) => `
    <div class="standup-entry">
      <div class="head">
        <strong>${esc(name)}</strong>
        <span>
          <span class="date">${standup ? esc(standup.date) : "no standup yet"}</span>
          <button class="ghost standup-now" data-app="${esc(name)}" style="margin-left:8px;">Generate now</button>
        </span>
      </div>
      <div class="body ${standup ? "" : "none"}">${standup ? esc(standup.content) : "No standup generated for this project yet."}</div>
    </div>
  `).join("");
}

async function refreshRuns() {
  const data = await jsonFetch("/runs");
  $("#runs-empty").style.display = data.runs.length ? "none" : "block";
  $("#runs-table tbody").innerHTML = data.runs.map((r) => `
    <tr>
      <td>${esc(r.app)}</td>
      <td>${esc(r.source)}</td>
      <td><span class="badge ${esc(r.status)}">${esc(r.status)}</span></td>
      <td>${fmtTime(r.started_at)}</td>
      <td>${r.error ? esc(r.error) : (r.result ? "cost $" + (r.result.cost_usd ?? 0).toFixed(4) : "")}</td>
    </tr>
  `).join("");
}

async function refreshSignals() {
  const data = await jsonFetch("/signals");
  $("#signals-empty").style.display = data.signals.length ? "none" : "block";
  $("#signals-table tbody").innerHTML = data.signals.map((s) => `
    <tr>
      <td>${esc(s.ts)}</td>
      <td>${esc(s.source)}</td>
      <td>${esc(s.message)}</td>
    </tr>
  `).join("");
}

async function refreshAll() {
  await Promise.all([refreshStatus(), refreshApps(), refreshStandups(), refreshRuns(), refreshSignals()]).catch(() => {});
}

$("#pause-btn").addEventListener("click", async () => {
  const reason = prompt("Reason for pausing (optional):") || null;
  await jsonFetch("/system/pause", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({reason}) });
  refreshAll();
});

$("#resume-btn").addEventListener("click", async () => {
  await jsonFetch("/system/resume", { method: "POST" });
  refreshAll();
});

$("#register-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const payload = Object.fromEntries(new FormData(form).entries());
  if (!payload.objective) delete payload.objective;
  const msg = $("#msg");
  msg.textContent = "Cloning…";
  try {
    const result = await jsonFetch("/apps", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload) });
    msg.textContent = `Registered ${result.name} at ${result.repo_path}`;
    form.reset();
    form.branch.value = "main";
    refreshApps();
  } catch (err) {
    msg.textContent = "Error: " + err.message;
  }
});

document.addEventListener("click", async (e) => {
  if (e.target.classList.contains("run-now")) {
    const appName = e.target.dataset.app;
    e.target.disabled = true;
    e.target.textContent = "Starting…";
    try {
      await jsonFetch(`/apps/${encodeURIComponent(appName)}/run`, { method: "POST" });
    } finally {
      e.target.disabled = false;
      e.target.textContent = "Run now";
      refreshRuns();
    }
  } else if (e.target.classList.contains("standup-now")) {
    const appName = e.target.dataset.app;
    e.target.disabled = true;
    e.target.textContent = "Generating…";
    try {
      await jsonFetch(`/apps/${encodeURIComponent(appName)}/standup`, { method: "POST" });
    } finally {
      refreshStandups();
    }
  }
});

refreshAll();
setInterval(refreshAll, 5000);
</script>

</body>
</html>
"""
