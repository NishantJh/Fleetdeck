"use strict";
// The terminal window (docs/CONSOLE.md). The dashboard page opens it and hands over the unlock token with
// postMessage, so the token is never in a URL, a cookie or storage.

const $ = (sel) => document.querySelector(sel);
let ws = null;
let opened = 0;
let clock = null;

const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
if (location.protocol !== "https:" && !loopback) $("#t-warn").hidden = false;

$("#t-close").onclick = () => { if (ws) ws.close(); window.close(); };

function start(job) {
  $("#t-wait").hidden = true;
  $("#t-title").textContent = job.name + (job.container ? ` · ${job.container}` : "");
  document.title = `${job.name} · Terminal`;
  $("#t-user").textContent = job.container && !job.as_root ? "container user" : "root";

  const term = new Terminal({ cursorBlink: true, fontSize: 13, scrollback: 5000, fontFamily: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open($("#term"));
  fit.fit();

  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/api/console/terminal`);
  ws.binaryType = "arraybuffer";
  const enc = new TextEncoder();
  ws.onopen = () => {
    opened = Date.now();
    clock = setInterval(() => {
      const s = Math.round((Date.now() - opened) / 1000);
      $("#t-time").textContent = `open ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }, 1000);
    // The token goes in the first message, never in the URL.
    ws.send(JSON.stringify({ token: job.token, target: job.target, container: job.container_id || null, as_root: !!job.as_root, cols: term.cols, rows: term.rows }));
    term.focus();
  };
  ws.onmessage = (e) => { if (e.data instanceof ArrayBuffer) term.write(new Uint8Array(e.data)); };
  ws.onclose = (e) => {
    clearInterval(clock);
    term.write(`\r\n\x1b[33m[closed${e.reason ? `: ${e.reason}` : ""}]\x1b[0m\r\n`);
    term.options.disableStdin = true;
  };
  term.onData((d) => { if (ws.readyState === 1) ws.send(enc.encode(d)); });
  term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [cols, rows] })); });
  window.addEventListener("resize", () => fit.fit());
}

let started = false;
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || e.source !== window.opener || started) return;
  if (e.data?.type !== "fleetdeck-terminal-open") return;
  started = true;
  start(e.data);
});

if (window.opener) window.opener.postMessage({ type: "fleetdeck-terminal-ready" }, location.origin);
else $("#t-wait").textContent = "Open a terminal from the dashboard's Run command window.";
