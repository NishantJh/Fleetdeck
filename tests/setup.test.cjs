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
  // Linkedom does not implement the native select.value setter used by the setup form.
  Object.defineProperty(window.HTMLSelectElement.prototype, 'value', {
    configurable: true,
    get() { return (this.querySelector('option[selected]') || this.querySelector('option'))?.value || ''; },
    set(value) { for (const option of this.querySelectorAll('option')) option.selected = option.value === value; },
  });
  const storage = new Map();
  const store = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  const calls = [];
  const config = 'docker_enabled: false\nschedule:\n  check_every: 6h\n';
  const state = { first_run: false, writable: true, config_path: 'demo/config.yaml', machines: [], running: [], restart_pending: false, can_restart: true, ...options.state };
  const context = vm.createContext({
    window, document, URLSearchParams, setTimeout: () => 1, clearTimeout: () => {},
    location: { pathname: page, hash: '', href: page }, history: { replaceState() {} },
    localStorage: store, sessionStorage: store,
    fetch: async (url, request = {}) => {
      calls.push({ url, ...request });
      let body;
      if (options.locked) return { ok: false, text: async () => 'setup code missing or out of date' };
      if (url === '/api/setup') body = state;
      else if (url === '/api/setup/config' && request.method === 'PUT') {
        if (options.rejectSave) return { ok: false, text: async () => JSON.stringify({ detail: 'Configuration is invalid' }) };
        state.restart_pending = true; body = { saved: true };
      } else if (url === '/api/setup/config') body = { yaml: config, writable: state.writable };
      else if (url === '/api/setup/script') body = { script: 'simulated script', source: 'simulated source', download: null };
      else throw new Error(`Unexpected API request ${url}`);
      return { ok: true, text: async () => JSON.stringify(body) };
    },
  });
  vm.runInContext(read('format.js'), context);
  vm.runInContext(read('setup.js'), context);
  await flush();
  return { window, document, calls, state, config };
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
