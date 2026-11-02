"use strict";

// Shared dashboard tools. Bind only controls; no backup or notification request runs on page load.
(() => {
  async function api(path, options = {}) {
    const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
    if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
    return response.json();
  }

  async function testNotification() {
    try {
      const r = await api("/api/notify/test", { method: "POST" });
      const failed = Object.entries(r.errors);
      alert(failed.length ? `Sent to ${r.sent}. Failed:\n${failed.map(([n, e]) => `${n}: ${e}`).join("\n")}` : `Sent to ${r.sent} target(s).`);
    } catch (e) {
      alert(`Could not send: ${e.message}`);
    }
  }

  function bindBackup(trigger, dialog, prefix = "") {
    const $ = selector => dialog.querySelector(selector.replace(/^#/, `#${prefix}`));
    // Backup: an encrypted file of the config, keys and history; restoring one restarts the app.
    const backup = { data: null };
    trigger.onclick = () => {
      for (const id of ["backup-pass", "backup-pass2", "restore-file", "restore-pass"]) $(`#${id}`).value = "";
      for (const id of ["backup-msg", "restore-msg"]) $(`#${id}`).textContent = "";
      resetRestore();
      dialog.showModal();
    };

    function resetRestore() {
      backup.data = null;
      $("#restore-preview").hidden = true;
      $("#restore-ack").checked = false;
      $("#restore-go").hidden = true;
      $("#restore-go").disabled = true;
      $("#restore-check").hidden = false;
    }

    function say(id, text, bad = false) {
      const el = $(`#${id}`);
      el.textContent = text;
      el.className = bad ? "bad-text" : "muted";
    }

    $("#backup-download").onclick = async () => {
      const pass = $("#backup-pass").value;
      if (pass.length < 12) return say("backup-msg", "Use a passphrase of at least 12 characters.", true);
      if (pass !== $("#backup-pass2").value) return say("backup-msg", "The two passphrases don't match.", true);
      say("backup-msg", "Encrypting…");
      try {
        const r = await fetch("/api/backup", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ passphrase: pass }) });
        if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
        const name = /filename="([^"]+)"/.exec(r.headers.get("Content-Disposition") || "")?.[1] || "homelab-backup.hlmbackup";
        const url = URL.createObjectURL(await r.blob());
        const a = Object.assign(document.createElement("a"), { href: url, download: name });
        document.body.append(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        say("backup-msg", `Downloaded ${name}. Keep it somewhere safe, away from this server, and remember the passphrase.`);
      } catch (e) {
        say("backup-msg", `Could not make the backup: ${e.message}`, true);
      }
    };

    const readBase64 = (file) => new Promise((resolve, reject) => {
      const fr = new FileReader();
      fr.onload = () => resolve(String(fr.result).split(",", 2)[1] || "");
      fr.onerror = () => reject(fr.error);
      fr.readAsDataURL(file);
    });

    $("#restore-file").onchange = resetRestore;
    $("#restore-pass").oninput = resetRestore;
    $("#restore-ack").onchange = (e) => { $("#restore-go").disabled = !e.target.checked; };

    $("#restore-check").onclick = async () => {
      const file = $("#restore-file").files[0];
      if (!file) return say("restore-msg", "Choose a backup file first.", true);
      if (!$("#restore-pass").value) return say("restore-msg", "Enter the backup's passphrase.", true);
      say("restore-msg", "Opening the backup…");
      try {
        backup.data = await readBase64(file);
        const r = await api("/api/backup/restore", { method: "POST", body: JSON.stringify({ passphrase: $("#restore-pass").value, data: backup.data }) });
        const b = r.backup;
        const made = b.created_at ? window.FleetdeckDates.dateTime(b.created_at) : "an unknown date";
        $("#restore-summary").textContent = `Made ${made}. Login: ${b.username || "none"}. ${b.machines} machine${b.machines === 1 ? "" : "s"} in the config` +
          `${b.history ? ", with job history" : ""}. ${b.files.length} file${b.files.length === 1 ? "" : "s"}.`;
        $("#restore-preview").hidden = false;
        $("#restore-check").hidden = true;
        $("#restore-go").hidden = false;
        say("restore-msg", "");
      } catch (e) {
        backup.data = null;
        say("restore-msg", e.message.replace(/^400 /, "").replace(/^\{"detail":"(.*)"\}$/, "$1"), true);
      }
    };

    $("#restore-go").onclick = async () => {
      if (!backup.data || !$("#restore-ack").checked) return;
      $("#restore-go").disabled = true;
      say("restore-msg", "Restoring…");
      try {
        const r = await api("/api/backup/restore", { method: "POST", body: JSON.stringify({ passphrase: $("#restore-pass").value, data: backup.data, confirm: true }) });
        if (!r.restarting) return say("restore-msg", "Restored. Restart the container to use it.");
        say("restore-msg", "Restored. Restarting with it; this page reloads when it's back (log in with the backup's login).");
        await new Promise((ok) => setTimeout(ok, 1500));
        for (let i = 0; i < 60; i++) {
          // Any answer means it is back; 401 if the restored login differs from the one in this browser.
          try { await fetch("/api/schedule"); break; } catch { await new Promise((ok) => setTimeout(ok, 1000)); }
        }
        location.reload();
      } catch (e) {
        $("#restore-go").disabled = false;
        say("restore-msg", `Could not restore: ${e.message}`, true);
      }
    };

  }
  window.FleetdeckTools = Object.freeze({ bindBackup, testNotification });
})();
