"use strict";
// Settings page forms. Each section reads and saves one part of config.yaml through /api/settings/*.
// Secrets are never sent to the browser: a blank secret field keeps the saved value.
window.FleetdeckSettings = (() => {
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el[k] = v;
      else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
    }
    el.append(...kids.flat().filter((x) => x != null));
    return el;
  };
  const field = (label, input, help) =>
    h("label", { class: "dlg-field" }, label, input, help ? h("span", { class: "muted small" }, help) : null);
  const check = (label, input) => h("label", { class: "check-row" }, input, h("span", {}, label));

  const blank = () => ({
    name: "", previous_name: null, type: "ssh", address: "", ssh_host: "", ssh_port: 22, ssh_user: "root",
    sudo: false, password_set: false, url: "", token_set: false, verify_ssl: true, portainer_endpoint: null,
  });

  function sourceCard(src, api, onRemove) {
    const state = { ...src, previous_name: src.name || null };
    const fields = h("div", { class: "form-grid" });
    const status = h("p", { class: "muted small", role: "status" });
    const trust = h("div", { class: "row", hidden: true });
    const inputs = {};
    const text = (key, label, opts = {}) => {
      const input = h("input", { type: opts.type || "text", value: opts.secret ? "" : (state[key] ?? ""), autocomplete: "off", spellcheck: "false",
        placeholder: opts.secret && state[opts.flag] ? "configured, leave blank to keep" : (opts.placeholder || "") });
      input.oninput = () => { state[key] = opts.number ? (input.value ? Number(input.value) : null) : input.value; };
      if (opts.secret) { state[key] = ""; }
      inputs[key] = input;
      return field(label, input, opts.help);
    };
    const type = h("select", {}, h("option", { value: "ssh" }, "Over SSH (Unraid, TrueNAS, any Docker host)"),
      h("option", { value: "docker-api" }, "Docker API or Portainer (URL and token)"));
    type.value = state.type;
    type.onchange = () => { state.type = type.value; draw(); };
    const draw = () => {
      fields.replaceChildren(
        text("name", "Name", { placeholder: "NAS" }),
        field("Connection", type),
        ...(state.type === "ssh" ? [
          text("ssh_host", "Address", { placeholder: "192.168.1.30", help: "The dashboard's SSH key must be allowed to log in there (public key on the Machines page), or use a password below." }),
          text("ssh_port", "SSH port", { number: true }),
          text("ssh_user", "User", { help: "Needs permission to run docker." }),
          check("Use sudo", Object.assign(h("input", { type: "checkbox" }), { checked: state.sudo, onchange: (e) => { state.sudo = e.target.checked; } })),
          text("password", "Password (optional)", { type: "password", secret: true, flag: "password_set" }),
        ] : [
          text("url", "API address", { placeholder: "https://nas.lan:9443", help: "Portainer, or a Docker socket proxy." }),
          text("token", "API token", { type: "password", secret: true, flag: "token_set", help: "Stored encrypted. Changing the address needs the token again." }),
          text("portainer_endpoint", "Portainer environment ID (Portainer only)", { number: true }),
          check("Verify the TLS certificate", Object.assign(h("input", { type: "checkbox" }), { checked: state.verify_ssl, onchange: (e) => { state.verify_ssl = e.target.checked; } })),
        ]),
        text("address", "Address for links (optional)", { help: "What your browser uses to reach it. Defaults to the address above." }),
      );
    };
    const test = async (fingerprint) => {
      status.textContent = "Testing ...";
      trust.hidden = true;
      try {
        const r = await api("/api/settings/services/test", { method: "POST", body: JSON.stringify({ source: state, trust_fingerprint: fingerprint || null }) });
        if (r.needs_confirm) {
          status.textContent = r.error;
          trust.replaceChildren(h("button", { onclick: () => test(r.fingerprint) }, r.host_key_changed ? "The key changed: trust the new key and test" : "Trust this key and test"));
          trust.hidden = false;
        } else {
          status.textContent = r.ok ? `Works: ${r.containers} containers found.` : `Failed: ${r.error}`;
        }
      } catch (e) { status.textContent = `Failed: ${e.message}`; }
    };
    draw();
    const card = h("div", { class: "source-card" }, fields, status, trust,
      h("div", { class: "row" }, h("button", { type: "button", onclick: () => test() }, "Test"),
        h("button", { type: "button", class: "ghost danger-text", onclick: () => onRemove(card) }, "Remove")));
    card.value = () => state;
    return card;
  }

  async function mountServices(root, api) {
    const data = await api("/api/settings/services");
    const sourcesBox = h("div", { class: "source-list" });
    const status = h("span", { class: "muted", "aria-live": "polite" });
    const enabled = h("input", { type: "checkbox" });
    const stopped = h("input", { type: "checkbox" });
    enabled.checked = data.enabled;
    stopped.checked = data.show_stopped;
    const icons = h("input", { type: "checkbox" });
    icons.checked = data.icons !== false;
    const addresses = h("textarea", { rows: 3, spellcheck: "false", placeholder: "machine name = address your browser uses" });
    addresses.value = Object.entries(data.addresses).map(([k, v]) => `${k} = ${v}`).join("\n");
    const cards = [];
    const add = (src) => {
      const card = sourceCard(src, api, (c) => { cards.splice(cards.indexOf(c), 1); c.remove(); });
      cards.push(card);
      sourcesBox.append(card);
    };
    data.sources.forEach(add);
    const save = h("button", { class: "primary", type: "button", disabled: !data.writable }, "Save services");
    save.onclick = async () => {
      status.textContent = "Saving ...";
      const map = {};
      for (const line of addresses.value.split("\n")) {
        const i = line.indexOf("=");
        if (i > 0) map[line.slice(0, i).trim()] = line.slice(i + 1).trim();
      }
      try {
        await api("/api/settings/services", { method: "PUT", body: JSON.stringify({
          revision: data.revision, enabled: enabled.checked, show_stopped: stopped.checked, icons: icons.checked, addresses: map,
          sources: cards.map((c) => c.value()) }) });
        status.textContent = "Saved. Applies now.";
        root.replaceChildren();
        await mountServices(root, api);
      } catch (e) { status.textContent = e.message; }
    };
    root.replaceChildren(
      h("h2", { id: "services" }, "Services"),
      h("p", { class: "muted" }, "The Services tab lists containers and links. Add a NAS or other Docker host here to list its containers only. It is never updated, checked or backed up from here."),
      check("Show the Services tab", enabled),
      check("Show stopped containers (dimmed)", stopped),
      check("Download app icons (once, from the dashboard-icons set; off shows letters)", icons),
      h("h3", {}, "Other machines (services only)"),
      sourcesBox,
      h("div", { class: "row" }, h("button", { type: "button", onclick: () => add(blank()) }, "Add a machine")),
      field("Link addresses", addresses, "One per line: machine name = the address your browser uses for its port links."),
      h("div", { class: "row" }, save, status),
    );
  }

  async function mountSecrets(root, api) {
    const r = await api("/api/settings/secrets");
    root.hidden = !r.plaintext.length;
    if (!r.plaintext.length) return;
    const status = h("span", { class: "muted", "aria-live": "polite" });
    const button = h("button", { type: "button", class: "primary", disabled: !r.available || !r.writable }, "Move to encrypted store");
    button.onclick = async () => {
      status.textContent = "Moving ...";
      try {
        const m = await api("/api/settings/secrets/migrate", { method: "POST", body: "{}" });
        status.textContent = `Moved ${m.moved.length}.`;
        await mountSecrets(root, api);
      } catch (e) { status.textContent = e.message; }
    };
    root.replaceChildren(
      h("h2", {}, "Secrets in plain text"),
      h("p", { class: "muted" }, `${r.plaintext.length} secret(s) are stored as plain text in config.yaml: ${r.plaintext.join(", ")}. Moving them encrypts them in the state database and leaves a reference in config.yaml. Checked first: the dashboard must read back exactly the same values, or nothing changes.`),
      ...(r.available ? [] : [h("p", { class: "muted" }, "Needs FLEETDECK_STATE (the image sets it).")]),
      h("div", { class: "row" }, button, status),
    );
  }

  // Plain sections (no secrets): one form per section, saved with its revision.
  const SECTIONS = {
    schedule: {
      title: "Schedule",
      intro: "When the dashboard checks your machines on its own.",
      fields: [
        { key: "check_every", type: "text", label: "Check every", placeholder: "6h", help: "For example 30m, 6h or 1d (at least 5 minutes). Blank: only when you click Check." },
        { key: "check_docker", type: "bool", label: "Include Docker containers in scheduled checks" },
        { key: "auto_security_updates", type: "bool", label: "Install security updates automatically after each check", help: "apt only, hosts first, never reboots." },
      ],
    },
    security: {
      title: "Security",
      intro: "Vulnerability scans of Docker images with Trivy. They run in the dashboard, never on your machines.",
      fields: [
        { key: "scan_images", type: "bool", label: "Scan Docker images" },
        { key: "min_severity", type: "select", label: "Show findings at or above", options: ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"] },
        { key: "ignore_unfixed", type: "bool", label: "Ignore vulnerabilities that have no fix yet" },
      ],
    },
    updates: {
      title: "Updates",
      intro: "How upgrades run.",
      fields: [
        { key: "max_parallel", type: "number", label: "Machines updated at the same time", min: 1, max: 64 },
        { key: "snapshot_before_upgrade", type: "bool", label: "Snapshot Proxmox and Incus guests before upgrading", help: "The default of the checkbox in the Update dialog." },
        { key: "snapshot_keep", type: "number", label: "Snapshots to keep per guest", min: 1, max: 100 },
        { key: "community_scripts_update", type: "bool", label: "Update community-scripts containers with their own update", help: "Runs the latest upstream script as root." },
      ],
    },
  };

  async function mountSection(root, api, name) {
    const sec = SECTIONS[name];
    const data = await api(`/api/settings/${name}`);
    const state = {};
    const rows = sec.fields.map((f) => {
      state[f.key] = data[f.key];
      if (f.type === "bool") {
        const input = h("input", { type: "checkbox" });
        input.checked = !!data[f.key];
        input.onchange = () => { state[f.key] = input.checked; };
        return h("div", {}, check(f.label, input), f.help ? h("span", { class: "muted small" }, f.help) : null);
      }
      let input;
      if (f.type === "select") {
        input = h("select", {}, f.options.map((o) => h("option", { value: o }, o)));
        input.value = data[f.key];
      } else {
        input = h("input", { type: f.type === "number" ? "number" : "text", value: data[f.key] ?? "", min: f.min, max: f.max, placeholder: f.placeholder || "", autocomplete: "off", spellcheck: "false" });
      }
      input.oninput = input.onchange = () => { state[f.key] = f.type === "number" ? Number(input.value) : input.value; };
      return field(f.label, input, f.help);
    });
    const status = h("span", { class: "muted", "aria-live": "polite" });
    const save = h("button", { class: "primary", type: "button", disabled: !data.writable }, `Save ${sec.title.toLowerCase()}`);
    save.onclick = async () => {
      status.textContent = "Saving ...";
      try {
        const r = await api(`/api/settings/${name}`, { method: "PUT", body: JSON.stringify({ revision: data.revision, ...state }) });
        status.textContent = r.restart_needed.length ? `Saved. ${r.restart_needed.join(", ")} apply after a restart.` : "Saved. Applies now.";
        const note = status.textContent;
        await mountSection(root, api, name);
        root.querySelector("[aria-live]").textContent = note;
      } catch (e) { status.textContent = e.message; }
    };
    root.replaceChildren(h("h2", {}, sec.title), h("p", { class: "muted" }, sec.intro), ...rows, h("div", { class: "row" }, save, status));
  }

  const NOTIFY_TYPES = {
    ntfy: "ntfy", gotify: "Gotify", discord: "Discord", telegram: "Telegram", webhook: "Generic JSON webhook",
  };

  function targetCard(t, index, api, onRemove) {
    const state = { ...t, previous_index: index, token: "", url: t.url_set && t.type !== "ntfy" && t.type !== "gotify" ? "" : t.url };
    const fields = h("div", { class: "form-grid" });
    const status = h("p", { class: "muted small", role: "status" });
    const text = (key, label, opts = {}) => {
      const secretUrl = key === "url" && opts.secret;
      const input = h("input", { type: opts.secret ? "password" : "text", value: opts.secret ? "" : (state[key] ?? ""), autocomplete: "off", spellcheck: "false",
        placeholder: opts.secret && state[opts.flag] ? "configured, leave blank to keep" : (opts.placeholder || "") });
      input.oninput = () => { state[key] = input.value; };
      return field(label, input, opts.help || (secretUrl ? "Stored encrypted." : undefined));
    };
    const type = h("select", {}, Object.entries(NOTIFY_TYPES).map(([k, v]) => h("option", { value: k }, v)));
    type.value = state.type;
    type.onchange = () => { state.type = type.value; draw(); };
    const draw = () => {
      const secretUrl = state.type === "discord" || state.type === "webhook";
      fields.replaceChildren(
        field("Service", type),
        text("name", "Name (optional)", { placeholder: "Phone" }),
        ...(state.type === "telegram" ? [
          text("token", "Bot token", { secret: true, flag: "token_set", help: "Stored encrypted." }),
          text("chat_id", "Chat ID"),
        ] : secretUrl ? [
          text("url", state.type === "discord" ? "Webhook address" : "Address that accepts a JSON POST", { secret: true, flag: "url_set" }),
        ] : [
          text("url", state.type === "ntfy" ? "Topic address" : "Server address", { placeholder: state.type === "ntfy" ? "https://ntfy.sh/my-topic" : "https://gotify.lan" }),
          text("token", state.type === "ntfy" ? "Access token (optional)" : "Application token", { secret: true, flag: "token_set", help: "Stored encrypted. Changing the address needs the token again." }),
        ]),
      );
    };
    draw();
    const test = async () => {
      status.textContent = "Sending a test message ...";
      try {
        const r = await api("/api/settings/notify/test", { method: "POST", body: JSON.stringify(state) });
        status.textContent = r.ok ? "Sent. Check your device." : `Failed: ${r.error}`;
      } catch (e) { status.textContent = `Failed: ${e.message}`; }
    };
    const card = h("div", { class: "source-card" }, fields, status,
      h("div", { class: "row" }, h("button", { type: "button", onclick: test }, "Send test"),
        h("button", { type: "button", class: "ghost danger-text", onclick: () => onRemove(card) }, "Remove")));
    card.value = () => state;
    return card;
  }

  async function mountNotify(root, api) {
    const data = await api("/api/settings/notify");
    const box = h("div", { class: "source-list" });
    const status = h("span", { class: "muted", "aria-live": "polite" });
    const cards = [];
    const add = (t, i) => {
      const card = targetCard(t, i, api, (c) => { cards.splice(cards.indexOf(c), 1); c.remove(); });
      cards.push(card);
      box.append(card);
    };
    data.targets.forEach((t, i) => add(t, i));
    const save = h("button", { class: "primary", type: "button", disabled: !data.writable }, "Save notifications");
    save.onclick = async () => {
      status.textContent = "Saving ...";
      try {
        await api("/api/settings/notify", { method: "PUT", body: JSON.stringify({ revision: data.revision, targets: cards.map((c) => c.value()) }) });
        root.replaceChildren();
        await mountNotify(root, api);
        root.querySelector("[aria-live]").textContent = "Saved. Applies now.";
      } catch (e) { status.textContent = e.message; }
    };
    root.replaceChildren(
      h("h2", {}, "Notifications"),
      h("p", { class: "muted" }, "Where to send a message when a check finds updates, a critical vulnerability appears, or a guarded command runs. Messages only go out when Schedule has a check interval."),
      box,
      h("div", { class: "row" }, h("button", { type: "button", onclick: () => add({ type: "ntfy", name: "", url: "", url_set: false, token_set: false, chat_id: "" }, null) }, "Add a target")),
      h("div", { class: "row" }, save, status),
    );
  }

  // Console and network: they decide who can reach the dashboard and run commands, so saving asks for the password
  // again, and anything that could lock you out is shown first and saved only after you confirm.
  const GUARDED = {
    console: {
      title: "Run command and terminal",
      intro: "Run commands and open a terminal on a machine from the dashboard (docs/CONSOLE.md). Off by default. Every use is logged and needs the password again.",
      fields: [
        { key: "enabled", type: "bool", label: "Switch the console on", help: "Needs the dashboard login." },
        { key: "terminal", type: "bool", label: "Allow the interactive terminal", help: "Off keeps only Run command." },
        { key: "allow_from", type: "list", label: "Networks allowed to use it", rows: 5, help: "One per line, such as 192.168.0.0/16. Loopback, private networks and Tailscale-style 100.64.0.0/10 by default." },
        { key: "allow_plain_http", type: "bool", label: "Allow over plain http:// from those networks", help: "Passwords and commands travel unencrypted. Prefer https (see the README for Caddy)." },
        { key: "unlock_minutes", type: "number", label: "Stay unlocked for (minutes)", min: 1, max: 60 },
        { key: "keep_days", type: "number", label: "Keep the audit log for (days)", min: 1, max: 3650 },
        { key: "notify", type: "bool", label: "Send a notification when a terminal opens or a guarded command runs" },
        { key: "exclude", type: "list", label: "Machines that never get it", rows: 3, help: "One machine name or id per line." },
      ],
    },
    network: {
      title: "Network",
      intro: "How the dashboard is reached. Mistakes here can lock you out: you are asked to confirm anything risky, and changes apply after a restart.",
      fields: [
        { key: "public_url", type: "text", label: "Dashboard address", placeholder: "http://192.168.1.50:8080", help: "Linked from notifications." },
        { key: "allowed_hosts", type: "list", label: "Allowed host names", rows: 3, help: "One per line, such as 192.168.1.50 or fleetdeck.lan (no port). Any other Host header is refused. Blank allows any." },
        { key: "trusted_proxies", type: "list", label: "Trusted reverse proxies", rows: 3, help: "One address or range per line, such as 172.18.0.0/16. Needed behind Caddy or nginx so each visitor keeps their own address. Blank trusts only 127.0.0.1." },
        { key: "known_hosts", type: "text", label: "SSH known_hosts file", help: "Used by SSH connections that don't name their own. Blank: the dashboard's own file." },
      ],
    },
  };

  async function mountGuarded(root, api, name, note) {
    const spec = GUARDED[name];
    const data = await api(`/api/settings/${name}`);
    const state = {};
    const rows = spec.fields.map((f) => {
      const val = data[f.key];
      state[f.key] = val;
      if (f.type === "bool") {
        const input = h("input", { type: "checkbox" });
        input.checked = !!val;
        input.onchange = () => { state[f.key] = input.checked; };
        return h("div", {}, check(f.label, input), f.help ? h("span", { class: "muted small" }, f.help) : null);
      }
      let input;
      if (f.type === "list") {
        input = h("textarea", { rows: f.rows || 3, spellcheck: "false" });
        input.value = (val || []).join("\n");
        input.oninput = () => { state[f.key] = input.value.split("\n"); };
      } else {
        input = h("input", { type: f.type === "number" ? "number" : "text", value: val ?? "", min: f.min, max: f.max, placeholder: f.placeholder || (f.key === "known_hosts" ? data.default_known_hosts : ""), autocomplete: "off", spellcheck: "false" });
        input.oninput = () => { state[f.key] = f.type === "number" ? Number(input.value) : input.value; };
      }
      return field(f.label, input, f.help);
    });
    const password = h("input", { type: "password", autocomplete: "current-password" });
    const status = h("span", { class: "muted", "aria-live": "polite" });
    const warnBox = h("div", { class: "warn-box", role: "alert", hidden: true });
    const save = h("button", { class: "primary", type: "button", disabled: !data.writable }, `Save ${spec.title.toLowerCase()}`);
    const send = async (confirm) => {
      status.textContent = "Saving ...";
      warnBox.hidden = true;
      try {
        const r = await api(`/api/settings/${name}`, { method: "PUT", body: JSON.stringify({ revision: data.revision, password: password.value, confirm, ...state }) });
        if (!r.saved) {
          warnBox.replaceChildren(h("strong", {}, "This could lock you out or weaken security:"), h("ul", {}, r.warnings.map((w) => h("li", {}, w))),
            h("button", { type: "button", class: "danger", onclick: () => send(true) }, "Save anyway"));
          warnBox.hidden = false;
          status.textContent = "Not saved yet.";
          return;
        }
        const msg = r.restart_needed.length ? `Saved. ${r.restart_needed.join(", ")} apply after a restart.` : "Saved.";
        root.replaceChildren();
        await mountGuarded(root, api, name, msg);
      } catch (e) { status.textContent = e.message; }
    };
    save.onclick = () => send(false);
    const banners = [];
    if (data.plain_http) banners.push(h("p", { class: "warn-box" }, "This page is on plain http://. Anything typed here, including passwords and commands, can be read on the network. Use https where you can."));
    if (name === "console" && !data.has_auth) banners.push(h("p", { class: "warn-box" }, "There is no dashboard login yet, so the console stays off."));
    if (data.restart_pending && (!Array.isArray(data.restart_pending) || data.restart_pending.length)) {
      banners.push(h("p", { class: "muted" }, `Saved changes wait for a restart${Array.isArray(data.restart_pending) ? `: ${data.restart_pending.join(", ")}` : ""}.`));
    }
    status.textContent = note || "";
    root.replaceChildren(h("h2", {}, spec.title), h("p", { class: "muted" }, spec.intro), ...banners, ...rows,
      field("Your password (asked again for this section)", password), warnBox, h("div", { class: "row" }, save, status));
  }

  return { mountServices, mountSecrets, mountSection, mountNotify, mountGuarded };
})();
