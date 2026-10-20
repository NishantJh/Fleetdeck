"use strict";
// Run command dialog (docs/CONSOLE.md). Only active when the server has the console switched on.
// The unlock token lives in this variable only: never in localStorage or a cookie.

let consoleToken = null;
let consoleInfo = null;
let runPoll = null;
let runId = null;
let runTarget = null;

function mk(tag, props = {}, ...kids) {
  const el = Object.assign(document.createElement(tag), props);
  el.append(...kids);
  return el;
}

// The server's errors are JSON ({"detail": "..."}): show just the text.
function errText(body) {
  try { return JSON.parse(body).detail || body; } catch { return body; }
}

async function consoleApi(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (consoleToken) headers["X-Console-Token"] = consoleToken;
  const r = await fetch(path, { ...opts, headers });
  if (!r.ok) {
    if (r.status === 401) setLocked(true);
    const e = new Error(errText(await r.text()));
    e.status = r.status;
    throw e;
  }
  return r.json();
}

async function initConsole() {
  try {
    const r = await fetch("/api/console");
    if (!r.ok) return;
    consoleInfo = await r.json();
  } catch { return; }
  state.console = consoleInfo;
  render();
}

function setLocked(locked) {
  if (locked) consoleToken = null;
  $("#run-unlock").hidden = !locked;
  $("#run-main").hidden = locked;
  $("#run-lock").hidden = locked;
}

function runWarnings(v) {
  const out = [];
  if (consoleInfo?.warning) out.push(consoleInfo.warning);
  if (consoleInfo && !consoleInfo.allowed) out.push(`The console isn't available from here: ${consoleInfo.reason}`);
  if (v.runs_dashboard) out.push("This is the machine the dashboard runs on: guests and the dashboard depend on it.");
  else if (v.target.platform === "proxmox" && v.target.kind === "host") out.push(`This is ${v.target.name}, a Proxmox node: its guests depend on it.`);
  return out;
}

function openRun(id) {
  const v = state.targets.find((t) => t.target.id === id);
  if (!v) return;
  runTarget = v;
  $("#run-title").textContent = `Run command on ${v.target.name}`;
  $("#run-warn").replaceChildren(...runWarnings(v).map((w) => mk("div", { className: "error-banner" }, w)));
  $("#run-out").textContent = "";
  $("#run-status").textContent = "";
  $("#run-command").value = "";
  $("#run-confirm-row").hidden = true;
  $("#run-confirm").value = "";
  $("#run-password").value = "";
  $("#run-unlock-msg").textContent = "";
  setRunning(false);
  setLocked(!consoleToken);
  updateRunsAs();
  $("#run-dialog").showModal();
  if (consoleToken) loadRunExtras();
}

function updateRunsAs() {
  const c = $("#run-container");
  const name = c.value ? c.selectedOptions[0].textContent.split(" (")[0] : runTarget.target.name;
  $("#run-as").textContent = c.value ? `Runs in the container ${name} on ${runTarget.target.name}` : `Runs as root on ${name}`;
  $("#run-root-row").hidden = !c.value;
}

async function loadRunExtras() {
  const sel = $("#run-container");
  sel.replaceChildren(mk("option", { value: "" }, `${runTarget.target.name} itself`));
  updateRunsAs();
  try {
    const r = await consoleApi(`/api/targets/${encodeURIComponent(runTarget.target.id)}/containers`);
    for (const c of r.containers) sel.append(mk("option", { value: c.id }, `${c.name} (${c.state})`));
  } catch { /* no Docker, or locked: the machine itself still works */ }
  try {
    const r = await consoleApi("/api/console/audit?kind=run&limit=200");
    const mine = r.rows.filter((x) => x.target_id === runTarget.target.id).map((x) => x.command);
    const seen = [...new Set(mine)].slice(0, 20);
    $("#run-history").replaceChildren(
      mk("option", { value: "" }, "History…"),
      ...seen.map((c) => mk("option", { value: c }, c.split("\n")[0].slice(0, 80))),
    );
  } catch { /* history is a convenience */ }
}

function setRunning(running) {
  $("#run-go").disabled = running;
  $("#run-stop").hidden = !running;
}

async function startRun(confirmName = null) {
  const command = $("#run-command").value.trim();
  if (!command) return;
  const body = {
    target: runTarget.target.id,
    command,
    timeout: Math.max(1, Math.min(3600, parseInt($("#run-timeout").value, 10) || 60)),
    container: $("#run-container").value || null,
    as_root: $("#run-root").checked,
    confirm: confirmName,
  };
  $("#run-out").textContent = "";
  $("#run-status").textContent = "";
  try {
    const r = await consoleApi("/api/console/run", { method: "POST", body: JSON.stringify(body) });
    runId = r.id;
    $("#run-confirm-row").hidden = true;
    setRunning(true);
    pollRun(0);
  } catch (e) {
    if (e.status === 409) {
      $("#run-confirm-text").textContent = e.message;
      $("#run-confirm-row").hidden = false;
      $("#run-confirm").focus();
    } else {
      $("#run-status").textContent = e.message;
    }
  }
}

async function pollRun(offset) {
  clearTimeout(runPoll);
  const id = runId;
  try {
    const r = await consoleApi(`/api/console/runs/${id}?since=${offset}`);
    if (id !== runId) return;
    const out = $("#run-out");
    if (r.lines.length) {
      const stick = out.scrollTop + out.clientHeight >= out.scrollHeight - 8;
      out.textContent += r.lines.join("\n") + "\n";
      if (stick) out.scrollTop = out.scrollHeight;
    }
    if (r.status === "running") {
      runPoll = setTimeout(() => pollRun(r.next), 700);
      return;
    }
    setRunning(false);
    const word = { done: "finished", failed: "failed", stopped: "stopped", timeout: "timed out" }[r.status] || r.status;
    const rc = r.rc != null ? `, exit code ${r.rc}` : "";
    const secs = r.duration != null ? `, ${r.duration} s` : "";
    $("#run-status").textContent = `${word}${rc}${secs}${r.error ? `: ${r.error}` : ""}`;
    loadRunExtras();
  } catch (e) {
    setRunning(false);
    $("#run-status").textContent = e.message;
  }
}

$("#run-unlock-go").onclick = async () => {
  $("#run-unlock-msg").textContent = "";
  try {
    const r = await consoleApi("/api/console/unlock", { method: "POST", body: JSON.stringify({ password: $("#run-password").value }) });
    consoleToken = r.token;
    $("#run-password").value = "";
    setLocked(false);
    loadRunExtras();
  } catch (e) {
    $("#run-unlock-msg").textContent = e.message;
  }
};
$("#run-password").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); $("#run-unlock-go").click(); }
});
$("#run-go").onclick = () => startRun(null);
$("#run-confirm-go").onclick = () => startRun($("#run-confirm").value);
$("#run-command").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); startRun(null); }
});
$("#run-stop").onclick = () => consoleApi(`/api/console/runs/${runId}/stop`, { method: "POST" }).catch(() => {});
$("#run-container").onchange = updateRunsAs;
$("#run-history").onchange = (e) => {
  if (e.target.value) $("#run-command").value = e.target.value;
  e.target.value = "";
};
$("#run-copy").onclick = () => navigator.clipboard?.writeText($("#run-out").textContent);
$("#run-lock").onclick = async () => {
  await consoleApi("/api/console/lock", { method: "POST" }).catch(() => {});
  setLocked(true);
};
$("#run-close").onclick = () => $("#run-dialog").close();
$("#run-dialog").addEventListener("close", () => { clearTimeout(runPoll); runId = null; });

document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-run]");
  if (el) openRun(el.dataset.run);
});

initConsole();
