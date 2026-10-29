// Isolated administration-page tests: configuration and connection APIs are synthetic.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const { parseHTML } = require('linkedom');
const read = name => fs.readFileSync(path.join(__dirname, '../fleetdeck/static', name), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

async function administration(page, options = {}) {
  const { window, document } = parseHTML(read('setup.html'));
  window.HTMLElement.prototype.focus = function () {};
  window.HTMLElement.prototype.showModal = function () { this.open = true; };
  window.HTMLElement.prototype.close = function () { this.open = false; };
  // Linkedom does not implement the native select.value setter used by the setup form.
  Object.defineProperty(window.HTMLSelectElement.prototype, 'value', {
    configurable: true,
    get() { return (this.querySelector('option[selected]') || this.querySelector('option'))?.value || ''; },
    set(value) { for (const option of this.querySelectorAll('option')) option.selected = option.value === value; },
  });
  const storage = new Map();
  const store = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  const calls = [], alerts = [];
  const config = 'docker_enabled: false\nschedule:\n  check_every: 6h\n';
  const state = { first_run: false, writable: true, config_path: 'demo/config.yaml', machines: [], running: [], restart_pending: false, can_restart: true, ...options.state };
  const context = vm.createContext({
    window, document, URLSearchParams, setTimeout: () => 1, clearTimeout: () => {}, alert: message => alerts.push(message),
    FileReader: class {
      readAsDataURL() { this.result = 'data:application/octet-stream;base64,ZGVtbw=='; this.onload(); }
    },
    location: { pathname: page, hash: '', href: page }, history: { replaceState() {} },
    localStorage: store, sessionStorage: store,
    fetch: async (url, request = {}) => {
      calls.push({ url, ...request });
      let body;
      if (options.locked) return { ok: false, text: async () => 'setup code missing or out of date' };
      if (url === '/api/schedule') body = { notify_targets: 1 };
      else if (url === '/api/notify/test') body = { sent: 1, errors: {} };
      else if (url === '/api/setup') body = state;
      else if (url === '/api/setup/config' && request.method === 'PUT') {
        if (options.rejectSave) return { ok: false, text: async () => JSON.stringify({ detail: 'Configuration is invalid' }) };
        state.restart_pending = true; body = { saved: true };
      } else if (url === '/api/setup/config') body = { yaml: config, writable: state.writable };
      else if (url === '/api/setup/script') body = { script: 'simulated script', source: 'simulated source', download: null };
      else if (url === '/api/backup/restore') body = JSON.parse(request.body).confirm ? { restarting: false } : {
        backup: { created_at: '2026-10-29T12:00:00Z', username: 'demo', machines: 1, history: false, files: ['config.yaml'] },
      };
      else throw new Error(`Unexpected API request ${url}`);
      return { ok: true, text: async () => JSON.stringify(body), json: async () => body };
    },
  });
  vm.runInContext(read('format.js'), context);
  vm.runInContext(read('tools.js'), context);
  vm.runInContext(read('setup.js'), context);
  await flush();
  return { window, document, calls, state, config, alerts };
}

test('Machines has permanent connection security above cards and does not load the YAML editor', async () => {
  const ui = await administration('/machines');
  const doc = ui.document;
  assert.equal(doc.querySelector('#machines').hidden, false);
  assert.equal(doc.querySelector('#advanced').hidden, true);
  const security = doc.querySelector('#connection-security');
  assert.equal(security.tagName, 'SECTION');
  assert.equal(security.hidden, false);
  assert.ok([...doc.querySelector('main').children].indexOf(security) < [...doc.querySelector('main').children].indexOf(doc.querySelector('#machines')));
  assert.equal(ui.calls.some(call => call.url === '/api/setup/config'), false);
  const card = doc.querySelector('#cards .card');
  card.querySelector('.f-name').value = 'demo';
  card.querySelector('.f-address').value = '192.0.2.10';
  doc.querySelector('#allow-from').value = '192.0.2.50';
  await card.querySelector('.b-script').onclick();
  const request = ui.calls.find(call => call.url === '/api/setup/script');
  assert.equal(JSON.parse(request.body).allow_from, '192.0.2.50');
  assert.equal(ui.calls.some(call => call.url === '/api/setup/connect'), false);
});

test('Settings loads the full configuration, saves the edited text and keeps apply separate', async () => {
  const ui = await administration('/settings');
  const doc = ui.document;
  assert.equal(doc.querySelector('#page-title').textContent, 'Settings');
  assert.equal(doc.querySelector('#machines').hidden, true);
  assert.equal(doc.querySelector('#connection-security').hidden, true);
  assert.equal(doc.querySelector('#cards').children.length, 0);
  assert.equal(doc.querySelector('#advanced').hidden, false);
  assert.equal(doc.querySelector('#config-text').value, ui.config);
  const edited = ui.config + 'max_parallel: 2\n';
  doc.querySelector('#config-text').value = edited;
  await doc.querySelector('#config-save').onclick();
  const request = ui.calls.find(call => call.method === 'PUT');
  assert.equal(JSON.parse(request.body).yaml, edited);
  assert.equal(doc.querySelector('#apply').hidden, false);
  assert.equal(doc.querySelector('#config-text').value, edited, 'render after save must not replace the editor with an older response');
  assert.equal(ui.calls.some(call => call.url === '/api/setup/apply'), false);
});

test('Settings retains read-only handling and validation errors', async () => {
  const readonly = await administration('/settings', { state: { writable: false } });
  assert.equal(readonly.document.querySelector('#config-text').readOnly, true);
  assert.equal(readonly.document.querySelector('#config-save').disabled, true);
  const rejected = await administration('/settings', { rejectSave: true });
  rejected.document.querySelector('#config-text').value = 'invalid: [';
  await rejected.document.querySelector('#config-save').onclick();
  assert.match(rejected.document.querySelector('#config-status').textContent, /invalid/);
  assert.equal(rejected.document.querySelector('#apply').hidden, true);
});

test('Legacy setup keeps first-run login and setup-code gates', async () => {
  const first = await administration('/setup', { state: { first_run: true } });
  assert.equal(first.document.querySelector('#admin').hidden, false);
  assert.equal(first.document.querySelector('#machines').hidden, true);
  assert.equal(first.document.querySelector('#advanced').hidden, true);
  assert.equal(first.document.querySelector('#setup-navigation').hidden, true);
  const locked = await administration('/setup', { locked: true });
  assert.equal(locked.document.querySelector('#unlock').hidden, false);
  assert.equal(locked.document.querySelector('#machines').hidden, true);
});

for (const page of ['/machines', '/settings']) test(`${page} includes dashboard icon, backup and explicit notification actions`, async () => {
  const ui = await administration(page);
  const doc = ui.document;
  assert.ok(doc.querySelector('#to-dashboard svg'));
  assert.equal(doc.querySelector('#admin-test-notify').hidden, false);
  assert.ok(doc.querySelector('#admin-backup'));
  assert.equal(ui.calls.some(call => call.method === 'POST'), false, 'loading a page must not trigger a notification or backup');
  const ids = [...doc.querySelectorAll('[id]')].map(el => el.id);
  assert.equal(new Set(ids).size, ids.length, 'shared dialog IDs must not collide with first-run restore fields');
  doc.querySelector('#restore-pass').value = 'wizard-demo';
  doc.querySelector('#admin-backup').onclick();
  assert.equal(doc.querySelector('#admin-backup-dialog').open, true);
  assert.equal(doc.querySelector('#restore-pass').value, 'wizard-demo');
  await doc.querySelector('#admin-backup-download').onclick();
  assert.match(doc.querySelector('#admin-backup-msg').textContent, /at least 12/);
  assert.equal(ui.calls.some(call => call.url === '/api/backup'), false);
  await doc.querySelector('#admin-test-notify').onclick();
  assert.equal(ui.calls.filter(call => call.url === '/api/notify/test' && call.method === 'POST').length, 1);
  assert.match(ui.alerts.at(-1), /Sent to 1/);
});

test('shared restore requires opening the file and explicit confirmation before replacing data', async () => {
  const ui = await administration('/settings');
  const doc = ui.document;
  doc.querySelector('#admin-backup').onclick();
  doc.querySelector('#admin-restore-file').files = [{ name: 'demo.hlmbackup' }];
  doc.querySelector('#admin-restore-pass').value = 'demo passphrase';
  await doc.querySelector('#admin-restore-check').onclick();
  assert.equal(doc.querySelector('#admin-restore-preview').hidden, false);
  assert.equal(doc.querySelector('#admin-restore-go').disabled, true);
  await doc.querySelector('#admin-restore-go').onclick();
  assert.equal(ui.calls.filter(call => call.url === '/api/backup/restore').length, 1);
  doc.querySelector('#admin-restore-ack').checked = true;
  doc.querySelector('#admin-restore-ack').onchange({ target: doc.querySelector('#admin-restore-ack') });
  await doc.querySelector('#admin-restore-go').onclick();
  const restore = ui.calls.filter(call => call.url === '/api/backup/restore');
  assert.equal(restore.length, 2);
  assert.equal(JSON.parse(restore[1].body).confirm, true);
  assert.match(doc.querySelector('#admin-restore-msg').textContent, /Restored/);
});
