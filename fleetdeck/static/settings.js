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
          revision: data.revision, enabled: enabled.checked, show_stopped: stopped.checked, addresses: map,
          sources: cards.map((c) => c.value()) }) });
        status.textContent = "Saved. Applies now.";
        root.replaceChildren();
        await mountServices(root, api);
      } catch (e) { status.textContent = e.message; }
    };
    root.replaceChildren(
      h("h2", {}, "Services"),
      h("p", { class: "muted" }, "The Services tab lists containers and links. Add a NAS or other Docker host here to list its containers only. It is never updated, checked or backed up from here."),
      check("Show the Services tab", enabled),
      check("Show stopped containers (dimmed)", stopped),
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

  return { mountServices, mountSecrets };
})();
