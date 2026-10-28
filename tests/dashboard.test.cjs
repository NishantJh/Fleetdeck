// Offline DOM regression tests. All responses and machines are in memory: no browser or host access.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const { test } = require("node:test");
const vm = require("node:vm");
const { parseHTML } = require("linkedom");

const root = resolve(__dirname, "..");
const read = (file) => readFileSync(resolve(root, "fleetdeck/static", file), "utf8");
const now = new Date().toISOString();
const flush = () => new Promise((done) => setImmediate(done));

function machine(id, options = {}) {
  const parent = options.parent || id;
  return {
    target: { id, name: id, platform: "proxmox", source: "demo", parent: options.guest ? parent : null,
      kind: options.guest ? "vm" : "host", status: "running", updatable: true, check_only: false,
      can_snapshot: !!options.guest, can_backup: !options.guest, ...options.target },
    check: { os: "Debian", packages: [], notes: [], checked_at: now, ...options.check },
    hidden: false, blocked: null, active_job: null, ...options.view,
  };
}

async function dashboard({ docker = false, targets = [], rememberedView = "machines", schedule = null } = {}) {
  const { window, document } = parseHTML(read("index.html"));
  const calls = [], errors = [], storage = new Map([["dashboardView", JSON.stringify(rememberedView)]]);
  let activeElement = document.body;
  Object.defineProperty(document, "activeElement", { get: () => activeElement });
  window.HTMLElement.prototype.focus = function () { activeElement = this; };
  window.HTMLElement.prototype.getBoundingClientRect = () => ({ right: 900, bottom: 200, top: 175 });
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.HTMLElement.prototype.showModal = function () { this.open = true; };
  window.HTMLElement.prototype.close = function () { this.open = false; };
  const data = { targets, errors: {}, settings: { docker_enabled: docker, snapshot_before_upgrade: true } };
  const sandbox = {
    window, document, URL, innerWidth: 1200, innerHeight: 800,
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) },
    setTimeout: () => 1, clearTimeout: () => {},
    console: { error: (err) => errors.push(err) },
    alert: (text) => errors.push(text), confirm: () => true,
    fetch: async (path, options = {}) => {
      calls.push({ path, ...options });
      let body;
      if (path.startsWith("/api/targets")) body = data;
      else if (path === "/api/jobs") body = [];
      else if (path === "/api/schedule") body = schedule || { check_every: null };
      else if (path === "/api/actions") body = { jobs: [], rejected: {} };
      else if (path === "/api/session") body = { login: false };
      else throw new Error(`Unexpected request: ${path}`);
      return { ok: true, json: async () => body };
    },
    location: { hostname: "localhost", protocol: "http:", pathname: "/" },
    matchMedia: () => ({ matches: false }), addEventListener: window.addEventListener.bind(window),
  };
  const context = vm.createContext(sandbox);
  vm.runInContext(read("theme.js"), context);
  window.dispatchEvent(new window.Event("DOMContentLoaded"));
  vm.runInContext(read("app.js"), context);
  vm.runInContext("globalThis.ui = {state, render, load, openMachineMenu, setView}", context);
  await flush();
  assert.deepEqual(errors, [], "dashboard starts without runtime errors");
  const click = (selector) => {
    const el = document.querySelector(selector);
    assert.ok(el, `Missing control ${selector}`);
    el.dispatchEvent(new window.Event("click", { bubbles: true }));
    return el;
  };
  return { ...sandbox.ui, document, window, calls, errors, click, data };
}

test("OS-only default hides all Docker UI, including an old remembered Containers view", async () => {
  const pending = machine("pve1", { check: { packages: [{ name: "curl", current: "1", available: "2", security: true }], docker_installed: true },
    view: { stacks: { docker: "yes", stacks: [] } } });
  const ui = await dashboard({ targets: [pending, machine("pve2")], rememberedView: "containers" });
  const { document: doc } = ui;
  assert.equal(doc.querySelector(".view-tabs").hidden, true);
  assert.equal(doc.querySelector("#groups").hidden, false);
  assert.equal(doc.querySelector("#containers").hidden, true);
  assert.equal(doc.querySelector("#containers").innerHTML, "");
  assert.equal(doc.querySelector('[data-label="Containers"]'), null);
  assert.doesNotMatch(doc.querySelector("#summary").textContent, /Docker|stacks|images/);
  assert.equal(doc.querySelectorAll('[data-act="upgrade"]').length, 1);
  ui.click('[data-machine-menu="pve1"]');
  assert.doesNotMatch(doc.querySelector("#machine-actions").textContent, /Docker/);
  ui.click("#check-all");
  await flush();
  const actions = ui.calls.filter((call) => call.path === "/api/actions");
  assert.equal(actions.length, 1);
  assert.deepEqual(JSON.parse(actions[0].body).target_ids, ["pve1", "pve2"]);
  assert.equal(JSON.parse(actions[0].body).action, "check");
  assert.deepEqual(ui.errors, []);
});

test("host panels and guest rows keep stable order across inventory refreshes", async () => {
  const ui = await dashboard({ targets: [machine("pve2"), machine("guest2", { guest: true, parent: "pve1" }), machine("pve1"), machine("guest1", { guest: true, parent: "pve1" })] });
  const labels = () => [...ui.document.querySelectorAll("#groups .group-head")].map((el) => el.textContent.trim());
  assert.deepEqual(labels(), ["Proxmox pve1", "Proxmox pve2"]);
  const names = [...ui.document.querySelectorAll("#groups [data-machine-menu]")].map((el) => el.dataset.machineMenu);
  assert.deepEqual(names, ["pve1", "guest1", "guest2", "pve2"]);
  assert.equal(ui.document.querySelector('[data-machine-row="pve1"]').classList.contains("has-guests"), true);
  assert.equal(ui.document.querySelector('[data-machine-row="guest1"]').classList.contains("first-guest"), true);
  assert.equal(ui.document.querySelector('[data-machine-row="guest2"]').classList.contains("last-guest"), true);
  const trigger = ui.click('[data-machine-menu="pve1"]');
  ui.state.targets.reverse();
  ui.render();
  assert.deepEqual(labels(), ["Proxmox pve1", "Proxmox pve2"]);
  assert.ok(ui.document.querySelector('[data-machine-menu="pve1"]') === trigger, "polling preserves the menu trigger");
  assert.equal(ui.document.querySelector("#machine-actions").hidden, false);
  ui.document.dispatchEvent(Object.assign(new ui.window.Event("keydown"), { key: "Escape" }));
  assert.equal(ui.document.querySelector("#machine-actions").hidden, true);
  assert.ok(ui.document.activeElement === trigger, "Escape restores focus to the menu trigger");
});

test("bulk actions appear only after selection; updates still require confirmation", async () => {
  const ui = await dashboard({ targets: [machine("guest", { guest: true, parent: "pve1", check: { packages: [{ name: '<img src=x onerror="alert(1)">', current: "1", available: "2" }] } }),
    machine("monitor", { target: { check_only: true } })] });
  const doc = ui.document;
  assert.equal(doc.querySelector("#bulk-toolbar").hidden, true);
  assert.equal(doc.querySelector('[data-select="monitor"]').disabled, true);
  const checkbox = doc.querySelector('[data-select="guest"]');
  checkbox.checked = true;
  checkbox.dispatchEvent(new ui.window.Event("change", { bubbles: true }));
  assert.equal(doc.querySelector("#bulk-toolbar").hidden, false);
  assert.equal(doc.querySelector('[data-machine-row="guest"]').classList.contains("is-selected"), true);
  ui.click('[data-toggle="guest"]');
  assert.equal(doc.querySelector(".pkgs img"), null);
  assert.match(doc.querySelector(".pkgs").textContent, /<img src=x/);
  assert.equal(doc.querySelector(".pkgs td[colspan]").getAttribute("colspan"), "10");
  ui.click('[data-act="upgrade"]');
  assert.equal(doc.querySelector("#confirm").open, true);
  assert.equal(doc.querySelector("#confirm-snapshot-row").hidden, false);
  assert.equal(ui.calls.some((call) => call.path === "/api/actions"), false);
  ui.click("#clear-selected");
  assert.equal(doc.querySelector("#bulk-toolbar").hidden, true);
  assert.deepEqual(ui.errors, []);
});

test("update columns separate security, packages and phased updates with only relevant actions", async () => {
  const ui = await dashboard({
    schedule: { check_every: 21600, next_run: new Date(Date.now() + 2 * 3600 * 1000).toISOString(), notify_targets: 1 },
    targets: [
      machine("pve", { target: { can_backup: true }, check: { reboot_required: true, packages: [
        { name: "openssl", current: "1", available: "2", security: true },
        { name: "systemd", current: "1", available: "2" },
        { name: "tzdata", current: "1", available: "2", phased: true },
      ] } }),
      machine("phased", { guest: true, parent: "pve", check: { reboot_required: false, packages: [
        { name: "firmware", current: "1", available: "2", phased: true },
      ] } }),
      machine("failed", { guest: true, parent: "pve", check: { error: "timeout", packages: [] } }),
      machine("current", { guest: true, parent: "pve", check: { reboot_required: false, packages: [] } }),
    ],
  });
  const doc = ui.document;
  const headers = [...doc.querySelectorAll(".machine-table > thead th")].map((el) => el.textContent.trim());
  assert.deepEqual(headers, ["", "Machine", "Type", "OS", "Security", "Package", "Phased", "Reboot", "Last check", "Actions", "More"]);
  assert.equal(doc.querySelector('[data-machine-row="pve"] [data-label="Type"]').textContent.trim(), "Node");
  assert.equal(doc.querySelector('[data-machine-row="pve"] [data-act="upgrade"] span').textContent, "Update");
  assert.equal(doc.querySelector('[data-machine-row="pve"] [data-act="reboot"] span').textContent, "Reboot");
  assert.equal(doc.querySelector('[data-machine-row="pve"] .reboot-status').textContent, "Required");
  assert.ok(doc.querySelector('[data-machine-row="pve"] .reboot-status .ui-icon'));
  assert.equal(doc.querySelector('[data-machine-row="phased"] [data-act="upgrade"]'), null);
  assert.equal(doc.querySelector('[data-machine-row="phased"] .update-metric.phased').textContent.trim(), "1");
  assert.equal(doc.querySelector('[data-machine-row="failed"] [data-act="check"] span').textContent, "Retry check");
  assert.equal(doc.querySelector('[data-machine-row="current"] .actions button'), null);
  assert.doesNotMatch(doc.querySelector("#groups").textContent, /—/);
  assert.match(doc.querySelector("#summary").textContent, /1Machine needing updates/);
  assert.match(doc.querySelector("#schedule-status").textContent, /Next check in 2 h/);

  ui.click('[data-machine-row="pve"] .update-metric.security');
  assert.deepEqual([...doc.querySelectorAll("#packages-pve .package-type")].map((el) => el.textContent.trim()), ["Security", "Package", "Phased"]);
  ui.click('[data-machine-menu="pve"]');
  assert.doesNotMatch(doc.querySelector("#machine-actions").textContent, /Reboot/);
  assert.match(doc.querySelector("#machine-actions").textContent, /Check updates/);
  assert.match(doc.querySelector("#machine-actions").textContent, /Back up configuration/);
  assert.equal(doc.querySelectorAll("#machine-actions .ui-icon").length >= 3, true);
  assert.doesNotMatch(doc.querySelector("#machine-actions").textContent, /…/);
  assert.deepEqual(ui.errors, []);
});

test("optional Docker status is separate, honest about failed checks, and never auto-pulls", async () => {
  const stacks = { docker: "yes", compose: "v2", checked_at: now, stacks: [
    { name: "unreachable", state: "running", error: "registry unavailable", services: [], update_available: true },
  ] };
  const ui = await dashboard({ docker: true, targets: [machine("docker-vm", { check: { docker_installed: true }, view: { stacks } }), machine("plain", { check: { docker_installed: false } })] });
  assert.equal(ui.document.querySelector(".view-tabs").hidden, false);
  assert.equal(ui.document.querySelectorAll("[data-open-docker]").length, 1);
  ui.click('[data-open-docker="docker-vm"]');
  assert.equal(ui.document.querySelector("#groups").hidden, true);
  assert.match(ui.document.querySelector("#containers").textContent, /unknown · check failed/);
  assert.equal(ui.document.querySelectorAll("[data-stack]").length, 0);
  assert.equal(ui.calls.some((call) => call.path === "/api/actions"), false);
  ui.click("[data-fetch-docker]");
  assert.match(ui.document.querySelector("#confirm-text").textContent, /Downloads \(pulls\)/);
  assert.equal(ui.calls.some((call) => call.path === "/api/actions"), false);
});

test("favicon and clickable Fleetdeck logo are present", async () => {
  const ui = await dashboard();
  assert.equal(ui.document.querySelector('link[rel="icon"]').getAttribute("href"), "/static/favicon.svg");
  assert.equal(ui.document.querySelector("a.brand").getAttribute("href"), "/");
  assert.equal(ui.document.querySelector("a.brand").getAttribute("aria-label"), "Fleetdeck dashboard");
});
