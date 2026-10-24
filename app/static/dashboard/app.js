/* Operator dashboard shell.
 *
 * The operator key is the credential. It lives in sessionStorage for the life of
 * the tab and travels as a Bearer token on every operator API call, which the
 * API authenticates with its existing operator-realm dependency. There is no
 * second auth system and no server-side session.
 *
 * Every section reaches the API through apiFetch, which is the single place the
 * credential is attached; nothing else calls fetch.
 */

const KEY_STORAGE = 'palimpsest.operator_key';
const POOL_KEYS_PATH = '/v1/operator/pool/keys';
const AVAILABLE_MODELS_PATH = '/v1/operator/pool/available-models';
const UTILIZATION_PATH = '/v1/operator/pool/utilization';
const EVENTS_PATH = '/v1/operator/pool/failover-events';
const EVENTS_PAGE_SIZE = 50;
const ACCOUNTS_PATH = '/operator/v1/accounts';
const CREATE_ACCOUNT_PATH = '/v1/accounts';
const CONFIG_PATH = '/operator/v1/config';

/* The tunables the operator may change, in the order they are shown. Mirrors the
   server's allowlist — anything not listed here is not writable, so the form
   cannot offer a knob the API would refuse. */
const CONFIG_FIELDS = [
  { name: 'verbatim_turns_n', label: 'Verbatim turns', type: 'number', min: 1, step: 1,
    hint: 'Raw turns carried into failover context.' },
  { name: 'retry_budget', label: 'Retry budget', type: 'number', min: 1, step: 1,
    hint: 'Attempts allowed per request.' },
  { name: 'pool_low_water_mark', label: 'Pool low-water mark', type: 'number', min: 0, step: 1,
    hint: 'Throttle below this many free requests. 0 disables.' },
  { name: 'user_daily_cap', label: 'User daily cap', type: 'number', min: 1, step: 1,
    hint: 'Blank means no per-user cap.' },
  { name: 'request_log_prompts', label: 'Log prompts', type: 'checkbox',
    hint: 'Include current-turn messages in request logs.' },
  { name: 'cooldown_seconds', label: 'Cooldown (s)', type: 'number', min: 1, step: 1,
    hint: 'How long a key stays in cooldown after a transient failure.' },
  { name: 'failover_event_log_size', label: 'Failover event log size', type: 'number', min: 1, step: 1,
    hint: 'Events retained in the bounded mirror list.' },
  { name: 'request_log_ttl_hours', label: 'Request log TTL (h)', type: 'number', min: 1, step: 1,
    hint: 'How long request logs are retained.' },
];

const NUMBER_FORMAT = new Intl.NumberFormat();

const PROVIDERS = ['openai', 'anthropic', 'google', 'openrouter'];
const KEY_STATES = ['active', 'exhausted', 'cooldown', 'suspended'];

const POOL_KEY_COLUMNS = [
  { key: 'name', label: 'Name' },
  { key: 'provider', label: 'Provider' },
  { key: 'models', label: 'Models' },
  { key: 'key_hint', label: 'Key hint' },
  { key: 'state', label: 'State' },
  { key: 'daily_cap', label: 'Request cap', numeric: true },
  { key: 'token_cap', label: 'Token cap', numeric: true },
  { key: 'next_reset_at', label: 'Next reset', date: true },
  { key: 'last_modified_by', label: 'Last modified by' },
];

const SECTIONS = [
  {
    id: 'overview',
    label: 'Overview',
    endpoints: ['/operator/v1/overview', '/operator/v1/accounts'],
    render: renderOverview,
  },
  {
    id: 'pool-keys',
    label: 'Pool Keys',
    endpoints: ['/v1/operator/pool/keys'],
    render: renderPoolKeys,
  },
  {
    id: 'utilization-failover',
    label: 'Utilization & Failover',
    endpoints: ['/v1/operator/pool/utilization', '/v1/operator/pool/failover-events'],
    render: renderHealth,
  },
  {
    id: 'users',
    label: 'Users',
    endpoints: ['/operator/v1/accounts'],
    render: renderUsers,
  },
  {
    id: 'usage',
    label: 'Usage',
    endpoints: ['/operator/v1/usage', '/operator/v1/accounts'],
    render: renderUsage,
  },
  {
    id: 'sessions',
    label: 'Sessions',
    endpoints: ['/operator/v1/accounts'],
    render: renderSessions,
  },
  {
    id: 'requests',
    label: 'Requests',
    endpoints: ['/operator/v1/requests', '/operator/v1/accounts'],
    render: renderRequests,
  },
  {
    id: 'performance',
    label: 'Performance',
    endpoints: ['/operator/v1/stats', '/operator/v1/stats/hourly'],
    render: renderPerformance,
  },
  {
    id: 'costs',
    label: 'Costs',
    endpoints: ['/operator/v1/costs', '/operator/v1/pricing'],
    render: renderCosts,
  },
  {
    id: 'alerts',
    label: 'Alerts',
    endpoints: ['/operator/v1/alerts', '/operator/v1/alerts/rules'],
    render: renderAlerts,
  },
  {
    id: 'audit',
    label: 'Audit & Export',
    endpoints: ['/operator/v1/audit', '/operator/v1/export/audit.csv'],
    render: renderAudit,
  },
  {
    id: 'system',
    label: 'System',
    endpoints: ['/operator/v1/system'],
    render: renderSystem,
  },
  {
    id: 'configuration',
    label: 'Configuration',
    endpoints: ['/operator/v1/config'],
    render: renderConfiguration,
  },
];

/* Sidebar grouping by theme; every section id appears exactly once. */
const NAV_GROUPS = [
  { title: 'Monitoring', ids: ['overview', 'performance', 'alerts'] },
  { title: 'Key pool', ids: ['pool-keys', 'utilization-failover'] },
  { title: 'Users', ids: ['users', 'sessions'] },
  { title: 'Activity & costs', ids: ['usage', 'requests', 'costs'] },
  { title: 'Administration', ids: ['audit', 'system', 'configuration'] },
];

/* Stroke icons (24x24 grid) for the sidebar; static markup, never user data. */
const NAV_ICONS = {
  overview: [['rect', {x: '3', y: '3', width: '7', height: '9', rx: '1.5'}], ['rect', {x: '14', y: '3', width: '7', height: '5', rx: '1.5'}], ['rect', {x: '14', y: '12', width: '7', height: '9', rx: '1.5'}], ['rect', {x: '3', y: '16', width: '7', height: '5', rx: '1.5'}]],
  performance: [['path', {d: 'M3 12h4l3-8 4 16 3-8h4'}]],
  alerts: [['path', {d: 'M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9'}], ['path', {d: 'M10.3 21a1.94 1.94 0 0 0 3.4 0'}]],
  'pool-keys': [['circle', {cx: '7.5', cy: '15.5', r: '4.5'}], ['path', {d: 'm10.7 12.3 9.8-9.8M16 7l3 3M14 9l2 2'}]],
  'utilization-failover': [['path', {d: 'M3 3v18h18'}], ['path', {d: 'M7 15v3M12 10v8M17 6v12'}]],
  users: [['circle', {cx: '9', cy: '8', r: '4'}], ['path', {d: 'M2 21v-1a6 6 0 0 1 6-6h2a6 6 0 0 1 6 6v1'}], ['path', {d: 'M16 4.2a4 4 0 0 1 0 7.6M22 21v-1a6 6 0 0 0-4-5.6'}]],
  sessions: [['rect', {x: '3', y: '4', width: '18', height: '12', rx: '2'}], ['path', {d: 'M8 20h8M12 16v4'}]],
  usage: [['path', {d: 'M21 12A9 9 0 1 1 12 3v9z'}], ['path', {d: 'M15 3.5A9 9 0 0 1 20.5 9H15z'}]],
  requests: [['path', {d: 'M4 7h13l-3-3M20 17H7l3 3'}]],
  costs: [['circle', {cx: '12', cy: '12', r: '9'}], ['path', {d: 'M15 9.5C14.5 8.5 13.4 8 12 8c-1.7 0-3 .8-3 2s1.3 1.7 3 2 3 .8 3 2-1.3 2-3 2c-1.4 0-2.5-.5-3-1.5M12 6.5V8m0 8v1.5'}]],
  audit: [['path', {d: 'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z'}], ['path', {d: 'M14 3v5h5M9 13h6M9 17h6'}]],
  system: [['rect', {x: '3', y: '4', width: '18', height: '6', rx: '2'}], ['rect', {x: '3', y: '14', width: '18', height: '6', rx: '2'}], ['path', {d: 'M7 7h.01M7 17h.01'}]],
  configuration: [['path', {d: 'M4 6h10M18 6h2M4 12h2M10 12h10M4 18h12M20 18h0'}], ['circle', {cx: '16', cy: '6', r: '2'}], ['circle', {cx: '8', cy: '12', r: '2'}], ['circle', {cx: '18', cy: '18', r: '2'}]],
};

function navIcon(id) {
  const icon = el('span', 'nav-icon');
  icon.setAttribute('aria-hidden', 'true');
  const ns = 'http:' + '//www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '18');
  svg.setAttribute('height', '18');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.8');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('stroke-linejoin', 'round');
  for (const [tag, attrs] of NAV_ICONS[id] || []) {
    const shape = document.createElementNS(ns, tag);
    for (const [name, value] of Object.entries(attrs)) {
      shape.setAttribute(name, value);
    }
    svg.append(shape);
  }
  icon.append(svg);
  return icon;
}

const els = {
  login: document.getElementById('login'),
  loginForm: document.getElementById('login-form'),
  keyInput: document.getElementById('operator-key'),
  loginSubmit: document.getElementById('login-submit'),
  loginError: document.getElementById('login-error'),
  app: document.getElementById('app'),
  nav: document.getElementById('nav'),
  view: document.getElementById('view'),
  logout: document.getElementById('logout'),
  modal: document.getElementById('modal'),
};

/* The credential stopped working mid-session — every section unwinds on this. */
class SignedOut extends Error {}

/* The API refused a request the operator should see explained. */
class RequestFailed extends Error {
  constructor(status, code, message, param) {
    super(message);
    this.status = status;
    this.code = code;
    /* A 422 names the offending field in `param`; without it the operator is
       left with "Invalid request." and no idea which input to fix. */
    this.param = param || null;
  }
}

/* Storage access throws outright when the browser blocks it (private mode,
   disabled site data); auth degrades to "signed out" rather than breaking. */
function withStorage(apply, fallback = null) {
  try {
    return apply(window.sessionStorage);
  } catch (err) {
    return fallback;
  }
}

function storedKey() {
  return withStorage((storage) => storage.getItem(KEY_STORAGE));
}

function storeKey(key) {
  return withStorage((storage) => {
    storage.setItem(KEY_STORAGE, key);
    return true;
  }, false);
}

function forgetKey() {
  withStorage((storage) => storage.removeItem(KEY_STORAGE));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

/* head and tbody must be children of the table, not siblings beside it. */
function tableOf(...sections) {
  const table = el('table', 'pool-keys-table');
  table.append(...sections);
  return table;
}

/* -- Pagination -------------------------------------------------------------------
 *
 * Long lists show one page at a time. A view keeps a `pager` ({ page, size }); pageOf
 * clamps it (a delete or a refresh can leave it past the end) and returns the slice,
 * and pagerBar draws the controls. The bar sticks to the bottom of the scrolling
 * table, so it stays in reach however long the page is. Grouped lists group within
 * the page being shown.
 */

const TABLE_PAGE_SIZE = 25;
const PAGE_SIZES = [10, 25, 50, 100];

function newPager(size = TABLE_PAGE_SIZE) {
  return { page: 1, size };
}

function pageOf(pager, items) {
  const pages = Math.max(1, Math.ceil(items.length / pager.size));
  pager.page = Math.min(Math.max(1, pager.page), pages);
  const start = (pager.page - 1) * pager.size;
  return items.slice(start, start + pager.size);
}

/* 1 … 4 [5] 6 … 20: the ends, and the neighbours of the current page. */
function pageNumbers(page, pages) {
  const wanted = new Set([1, pages, page - 1, page, page + 1]);
  const numbers = [...wanted].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  const out = [];
  for (const n of numbers) {
    if (out.length && n - out[out.length - 1] > 1) {
      out.push(null);
    }
    out.push(n);
  }
  return out;
}

/* Returns null while everything fits on the smallest page size. */
function pagerBar(pager, total, noun, redraw) {
  if (total <= PAGE_SIZES[0]) {
    return null;
  }
  const pages = Math.max(1, Math.ceil(total / pager.size));
  const go = (page) => {
    pager.page = page;
    redraw();
  };

  const first = (pager.page - 1) * pager.size + 1;
  const last = Math.min(total, pager.page * pager.size);
  const info = el(
    'span',
    'pager-info',
    'Showing ' + NUMBER_FORMAT.format(first) + '–' + NUMBER_FORMAT.format(last) +
      ' of ' + NUMBER_FORMAT.format(total) + ' ' + noun + (total === 1 ? '' : 's')
  );

  const size = pairSelect(PAGE_SIZES.map((n) => [String(n), String(n)]), String(pager.size));
  size.setAttribute('aria-label', 'Rows per page');
  size.addEventListener('change', () => {
    pager.size = Number(size.value);
    go(1);
  });
  const sizeField = el('label', 'pager-size');
  sizeField.append(el('span', null, 'Rows per page'), size);

  const nav = el('nav', 'pager-nav');
  nav.setAttribute('aria-label', 'Pagination');
  const prev = button('‹ Prev', 'ghost btn-sm', () => go(pager.page - 1));
  prev.disabled = pager.page <= 1;
  nav.append(prev);
  for (const n of pageNumbers(pager.page, pages)) {
    if (n === null) {
      nav.append(el('span', 'pager-gap', '…'));
      continue;
    }
    const current = n === pager.page;
    const numeral = button(String(n), current ? 'ghost btn-sm active' : 'ghost btn-sm', () => go(n));
    numeral.setAttribute('aria-label', 'Page ' + n);
    if (current) {
      numeral.setAttribute('aria-current', 'page');
    }
    nav.append(numeral);
  }
  const next = button('Next ›', 'ghost btn-sm', () => go(pager.page + 1));
  next.disabled = pager.page >= pages;
  nav.append(next);

  const bar = el('div', 'pager');
  bar.append(info, sizeField, nav);
  return bar;
}

/* The table with its bar under it, ready for replaceChildren. */
function withPager(table, bar) {
  return bar ? [table, bar] : [table];
}

function button(label, className, onClick) {
  const node = el('button', className, label);
  node.type = 'button';
  if (onClick) {
    node.addEventListener('click', onClick);
  }
  return node;
}

function refreshButton(onClick, label = 'Refresh') {
  const node = button('↻ ' + label, 'ghost', onClick);
  node.title = 'Load the latest data';
  return node;
}

/* Every page has the same frame: title and status line on the left; the page's
   actions on the right, its main action first and Refresh always last; filters
   below; then the content, which scrolls on its own so the actions never leave
   the screen. Returns the content element. */
function mountPage(view, { title, description, status, actions = [], tools = [], content = [] }) {
  const titleBlock = el('div', 'page-title');
  titleBlock.append(el('h2', null, title));
  if (description) {
    titleBlock.append(el('p', 'page-description', description));
  }
  titleBlock.append(status.node);

  const actionBar = el('div', 'page-actions');
  actionBar.append(...actions);

  const head = el('header', 'page-head');
  head.append(titleBlock, actionBar);

  const parts = [head];
  if (tools.length) {
    const toolbar = el('div', 'page-tools');
    toolbar.append(...tools);
    parts.push(toolbar);
  }
  const body = el('div', 'page-body');
  body.append(...content);
  parts.push(body);

  view.replaceChildren(...parts);
  return body;
}

/* What an empty list says, with the action that fills it. */
function emptyState(message, actionLabel, onAction) {
  const box = el('div', 'empty-state');
  box.append(el('p', null, message));
  if (actionLabel) {
    box.append(button(actionLabel, 'primary', onAction));
  }
  return box;
}

/* One dialog serves every form and detail view, so an action opens over the page
   instead of somewhere below it, with its buttons in the same place every time:
   Cancel, then the main action, bottom right. Closing it drops its content, which
   may hold a provider key or a one-time user key.

   `content` is a node, a list of nodes, or a function given the modal handle (for
   forms that report through the modal's own status line). `primary` is
   { label, formId } for a form's submit button, or { label, onClick, danger }. */
function openModal({ title, content, primary = null, cancelLabel = 'Cancel', wide = false }) {
  const dialog = els.modal;
  const status = createStatus();

  const heading = el('h3', null, title);
  heading.id = 'modal-title';
  const dismiss = button('×', 'icon-button', closeModal);
  dismiss.setAttribute('aria-label', 'Close');
  const head = el('header', 'modal-head');
  head.append(heading, dismiss);

  const body = el('div', 'modal-body');
  const buttons = el('div', 'modal-buttons');
  /* cancelLabel null leaves a single button, for dialogs with one way out. */
  const cancel = cancelLabel === null ? null : button(cancelLabel, 'ghost', closeModal);
  if (cancel) {
    buttons.append(cancel);
  }

  let primaryButton = null;
  if (primary) {
    primaryButton = el('button', primary.danger ? 'primary danger' : 'primary', primary.label);
    if (primary.formId) {
      primaryButton.type = 'submit';
      primaryButton.setAttribute('form', primary.formId);
    } else {
      primaryButton.type = 'button';
      primaryButton.addEventListener('click', primary.onClick);
    }
    buttons.append(primaryButton);
  }

  const foot = el('footer', 'modal-foot');
  foot.append(status.node, buttons);

  dialog.className = 'modal' + (wide ? ' modal--wide' : '');
  dialog.replaceChildren(head, body, foot);

  const handle = { status, primaryButton, body, close: closeModal };
  const nodes = typeof content === 'function' ? content(handle) : content;
  body.append(...[].concat(nodes));

  if (!dialog.open) {
    dialog.showModal();
  }
  /* A destructive confirm starts on Cancel, so Enter never deletes by accident. */
  const firstField = body.querySelector('input:not([type=checkbox]), select, textarea');
  (firstField || (primary && primary.danger ? cancel : primaryButton) || cancel || dismiss).focus();
  return handle;
}

function closeModal() {
  if (els.modal.open) {
    els.modal.close();
  }
  els.modal.replaceChildren();
}

/* Escape closes the dialog natively; its content must go with it. */
els.modal.addEventListener('close', () => els.modal.replaceChildren());

/* An in-page replacement for window.confirm: resolves true only on the explicit
   confirm button, false on Cancel, the close button or Escape. */
function confirmAction({ title, message, confirmLabel }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    openModal({
      title,
      content: el('p', 'modal-text', message),
      primary: {
        label: confirmLabel,
        danger: true,
        onClick: () => {
          finish(true);
          closeModal();
        },
      },
    });
    els.modal.addEventListener('close', () => finish(false), { once: true });
  });
}

async function readError(response) {
  let body = null;
  try {
    body = await response.json();
  } catch (err) {
    body = null;
  }
  const error = body && body.error ? body.error : null;
  return {
    code: error ? error.code : null,
    param: error ? error.param : null,
    message:
      error && error.message ? error.message : 'Request failed (' + response.status + ').',
  };
}

async function apiFetch(path, { blob = false, ...init } = {}) {
  const key = storedKey();
  if (!key) {
    showLogin();
    throw new SignedOut();
  }
  const response = await fetch(path, {
    ...init,
    headers: {
      Authorization: 'Bearer ' + key,
      ...(init.headers || {}),
    },
  });

  /* A rejected credential is not a per-request failure: there is nothing left to
     retry with, so drop it and hand the operator back to the login screen. */
  if (response.status === 401 || response.status === 403) {
    forgetKey();
    showLogin('That operator key is no longer accepted. Sign in again.');
    throw new SignedOut();
  }

  if (!response.ok) {
    const failure = await readError(response);
    throw new RequestFailed(response.status, failure.code, failure.message, failure.param);
  }

  if (blob) {
    return response.blob();
  }
  return response.status === 204 ? null : response.json();
}

/* Any operator route answers this question, so the cheapest read-only one is
   the whole of the login decision: 200 is an operator key, 403 is a valid key
   from the wrong realm, 401 is not a key at all. */
async function authenticate(candidate) {
  const response = await fetch('/v1/operator/pool/keys', {
    method: 'GET',
    headers: { Authorization: 'Bearer ' + candidate },
  });

  if (response.ok) {
    return { ok: true };
  }

  const failure = await readError(response);
  if (response.status === 403 || failure.code === 'operator_access_denied') {
    return { ok: false, message: 'That key belongs to a user account, not to the operator realm.' };
  }
  if (response.status === 401) {
    return { ok: false, message: 'Invalid operator key.' };
  }
  return { ok: false, message: 'The gateway could not verify that key (' + response.status + ').' };
}

function setLoginError(message) {
  els.loginError.textContent = message || '';
  els.loginError.hidden = !message;
}

// -- shell ---------------------------------------------------------------------

function renderNav(activeId) {
  const groups = NAV_GROUPS.map((group) => {
    const wrap = el('div', 'nav-group');
    wrap.setAttribute('role', 'group');
    wrap.setAttribute('aria-label', group.title);
    wrap.append(el('div', 'nav-group-title', group.title));
    for (const id of group.ids) {
      const section = SECTIONS.find((candidate) => candidate.id === id);
      const link = el('a', 'nav-link');
      link.append(navIcon(section.id), el('span', 'nav-label', section.label));
      link.href = '#/' + section.id;
      if (section.id === activeId) {
        link.classList.add('is-active');
        link.setAttribute('aria-current', 'page');
      }
      wrap.append(link);
    }
    return wrap;
  });
  els.nav.replaceChildren(...groups);
}

function renderPlaceholder(section) {
  const heading = el('h2', null, section.label);
  const note = el('p', 'note', 'This section is not available yet. It is backed by:');
  const endpoints = el('ul', 'endpoints');
  for (const endpoint of section.endpoints) {
    endpoints.append(el('li', null, endpoint));
  }
  els.view.replaceChildren(heading, note, endpoints);
}

function renderRoute() {
  /* A hash change while signed out (logout itself sets one) must not mount a
     section: it would call the API without a key and report a revoked one. */
  if (els.app.hidden) {
    return;
  }
  const id = window.location.hash.replace(/^#\/?/, '');
  const section = SECTIONS.find((candidate) => candidate.id === id) || SECTIONS[0];
  renderNav(section.id);
  closeModal();
  if (section.render) {
    section.render(els.view);
  } else {
    renderPlaceholder(section);
  }
  els.view.focus();
  refreshAlertBadge();
}

function showLogin(message) {
  /* Drop the rendered section and any open dialog: they may hold a one-time key
     or pool data. */
  closeModal();
  els.view.replaceChildren();
  els.app.hidden = true;
  els.login.hidden = false;
  setLoginError(message);
  els.keyInput.focus();
}

function showApp() {
  els.login.hidden = true;
  els.app.hidden = false;
  renderRoute();
}

els.loginForm.addEventListener('submit', async (event) => {
  event.preventDefault();

  const candidate = els.keyInput.value.trim();
  if (!candidate) {
    setLoginError('Enter an operator key.');
    return;
  }

  els.loginSubmit.disabled = true;
  setLoginError(null);
  try {
    const result = await authenticate(candidate);
    if (!result.ok) {
      setLoginError(result.message);
      return;
    }
    if (!storeKey(candidate)) {
      setLoginError('Browser storage is blocked, so the session cannot be kept.');
      return;
    }
    els.keyInput.value = '';
    showApp();
  } catch (err) {
    setLoginError('The gateway is unreachable.');
  } finally {
    els.loginSubmit.disabled = false;
  }
});

els.logout.addEventListener('click', () => {
  forgetKey();
  poolKeys.records = [];
  users.accounts = [];
  showLogin();
  window.location.hash = '';
});

window.addEventListener('hashchange', renderRoute);

/* -- Pool Keys ----------------------------------------------------------------
 *
 * Client-side only: filtering and sorting run over the already-fetched list, so
 * no interaction here issues a request beyond the one action it performs.
 */

const poolKeys = {
  records: [],
  filters: { state: '', provider: '', query: '' },
  sort: { column: 'id', ascending: true },
  pager: newPager(),
  /* The mounted region of the current section render. Sorting and filtering are
     triggered from table cells the re-render replaces, so they cannot close over
     the elements — they read them back from here. */
  body: null,
  panel: null,
};

function formatDate(value) {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function recordValue(column, record) {
  return column.key === 'name' ? keyName(record) : record[column.key];
}

function formatCell(column, record) {
  const value = recordValue(column, record);
  if (value === null || value === undefined || value === '') {
    return '—';
  }
  if (column.date) {
    return formatDate(value);
  }
  if (column.key === 'provider') {
    return PROVIDER_NAMES[value] || String(value);
  }
  if (Array.isArray(value)) {
    return value.join(', ');
  }
  return String(value);
}

function searchableText(record) {
  return [keyName(record), record.id, record.provider, record.key_hint, record.state, (record.models || []).join(' ')]
    .join(' ')
    .toLowerCase();
}

function compareValues(column, left, right) {
  const missing = (value) => value === null || value === undefined;
  if (missing(left)) {
    return missing(right) ? 0 : -1;
  }
  if (missing(right)) {
    return 1;
  }
  if (column.numeric) {
    return Number(left) - Number(right);
  }
  if (column.date) {
    return new Date(left).getTime() - new Date(right).getTime();
  }
  return String(left).localeCompare(String(right));
}

function visibleKeys() {
  const { state, provider, query } = poolKeys.filters;
  const needle = query.trim().toLowerCase();

  const filtered = poolKeys.records.filter((record) => {
    if (state && record.state !== state) {
      return false;
    }
    if (provider && record.provider !== provider) {
      return false;
    }
    return !needle || searchableText(record).includes(needle);
  });

  const column = POOL_KEY_COLUMNS.find((entry) => entry.key === poolKeys.sort.column);
  const sign = poolKeys.sort.ascending ? 1 : -1;
  return filtered.sort((a, b) => sign * compareValues(column, recordValue(column, a), recordValue(column, b)));
}

function sortBy(columnKey) {
  if (poolKeys.sort.column === columnKey) {
    poolKeys.sort.ascending = !poolKeys.sort.ascending;
  } else {
    poolKeys.sort = { column: columnKey, ascending: true };
  }
  drawPoolKeysTable();
}

function keyActionPath(id, suffix) {
  return POOL_KEYS_PATH + '/' + encodeURIComponent(id) + suffix;
}

/* A section's status line, and the runner every action in that section reports
   through, so messages and outcomes land in the same place. Each section builds
   its own; nothing about the mounted UI is global. */
function createStatus() {
  const node = el('p', 'status');
  node.setAttribute('role', 'status');

  return {
    node,
    report(message, kind) {
      node.textContent = message || '';
      node.className = 'status' + (message ? ' is-' + kind : '');
    },
    async run(action, onSuccess, onError) {
      node.classList.add('is-busy');
      try {
        await onSuccess(await action());
      } catch (err) {
        /* Signing out has already replaced the screen; anything else is the
           operator's to read. */
        if (!(err instanceof SignedOut)) {
          this.report(err.param ? err.param + ': ' + err.message : err.message, 'error');
          if (onError) {
            await onError(err);
          }
        }
      } finally {
        node.classList.remove('is-busy');
      }
    },
  };
}

function replaceRecord(record) {
  const index = poolKeys.records.findIndex((entry) => entry.id === record.id);
  if (index === -1) {
    poolKeys.records.push(record);
  } else {
    poolKeys.records[index] = record;
  }
}

/* A 404 means the key is already gone (removed elsewhere): refetch quietly so the
   stale row disappears, without overwriting the error the operator needs to read. */
async function afterKeyActionError(err) {
  if (err.status === 404) {
    try {
      poolKeys.records = await apiFetch(POOL_KEYS_PATH);
    } catch (refreshErr) {
      /* The action's own error is already on screen. */
    }
  }
  drawPoolKeysTable();
}

/* Row actions are the same small buttons on every table: the state toggle first,
   then the details, and the destructive action last. */
function rowActionCell(record) {
  const cell = el('td', 'row-actions');
  const suspended = record.state === 'suspended';

  const toggle = button(suspended ? 'Resume' : 'Suspend', 'ghost btn-sm', () =>
    poolKeys.panel.run(async () => {
      const updated = await apiFetch(
        keyActionPath(record.id, suspended ? '/resume' : '/suspend'),
        { method: 'POST' }
      );
      replaceRecord(updated);
      poolKeys.panel.report(keyName(record) + (suspended ? ' resumed.' : ' suspended.'), 'ok');
    }, drawPoolKeysTable, afterKeyActionError)
  );
  toggle.title = suspended ? 'Return this key to rotation' : 'Take this key out of rotation';

  const edit = button('Edit', 'ghost btn-sm', () => showEditKey(record));
  const limits = button('Limits', 'ghost btn-sm', () => showProviderLimits(record));

  const remove = button('Remove', 'ghost btn-sm danger', async () => {
    const confirmed = await confirmAction({
      title: 'Remove ' + keyName(record) + '?',
      message: 'The key leaves the pool at once and its counters are deleted. This cannot be undone.',
      confirmLabel: 'Remove key',
    });
    if (!confirmed) {
      return;
    }
    poolKeys.panel.run(async () => {
      await apiFetch(keyActionPath(record.id, ''), { method: 'DELETE' });
      poolKeys.records = poolKeys.records.filter((entry) => entry.id !== record.id);
      poolKeys.panel.report(keyName(record) + ' removed.', 'ok');
    }, drawPoolKeysTable, afterKeyActionError);
  });

  cell.append(toggle, edit, limits, remove);
  return cell;
}

/* Reset schedules. A key's budget counters renew on this schedule. These helpers fill a
   datetime-local field with the next midnight in UTC or in Pacific time. */
function toLocalInput(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return '';
  }
  const pad = (number) => String(number).padStart(2, '0');
  return (
    date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
    'T' + pad(date.getHours()) + ':' + pad(date.getMinutes())
  );
}

function nextMidnightUtc() {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + 1));
}

/* The next midnight in an IANA zone: read the zone's wall clock now, and the zone's
   offset from UTC falls out of the difference. */
function nextMidnightIn(timeZone) {
  const now = new Date();
  const parts = {};
  for (const part of new Intl.DateTimeFormat('en-CA', {
    timeZone,
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).formatToParts(now)) {
    parts[part.type] = Number(part.value);
  }
  const wallAsUtc = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
  const offset = wallAsUtc - Math.floor(now.getTime() / 1000) * 1000;
  return new Date(Date.UTC(parts.year, parts.month - 1, parts.day + 1) - offset);
}

/* Two buttons that set a reset field to the common provider schedules. */
function scheduleButtons(resetAtInput) {
  const row = el('div', 'schedule-buttons');
  const make = (label, compute) => {
    const button = el('button', 'ghost', label);
    button.type = 'button';
    button.addEventListener('click', () => {
      resetAtInput.value = toLocalInput(compute());
      resetAtInput.dispatchEvent(new Event('input', { bubbles: true }));
    });
    return button;
  };
  row.append(
    el('span', 'note', 'Set to the next:'),
    make('Midnight UTC', nextMidnightUtc),
    make('Midnight Pacific', () => nextMidnightIn('America/Los_Angeles'))
  );
  return row;
}

/* Edit a key in place: name, models and the budgets, and when its counters renew. The
   provider and the key itself are fixed at creation. */
function showEditKey(record) {
  openModal({
    title: 'Edit ' + keyName(record),
    content: (modal) => buildEditForm(record, modal),
    primary: { label: 'Save changes', formId: 'edit-key-form' },
    wide: true,
  });
}

function buildEditForm(record, modal) {
  const form = el('form', 'form-grid');
  form.id = 'edit-key-form';
  form.noValidate = true;

  const name = el('input');
  name.type = 'text';
  name.maxLength = 60;
  name.placeholder = 'e.g. Groq main';
  name.value = record.label || '';

  const models = el('input');
  models.type = 'text';
  models.value = (record.models || []).join(', ');
  models.required = true;
  models.placeholder = 'gpt-4o-mini, gpt-4o';

  const cap = el('input');
  cap.type = 'number';
  cap.min = '0';
  cap.placeholder = 'unlimited';
  cap.value = record.daily_cap === null || record.daily_cap === undefined ? '' : String(record.daily_cap);

  const tokens = el('input');
  tokens.type = 'number';
  tokens.min = '1';
  tokens.placeholder = 'unlimited';
  tokens.value = record.token_cap === null || record.token_cap === undefined ? '' : String(record.token_cap);

  const resetAt = el('input');
  resetAt.type = 'datetime-local';
  resetAt.value = toLocalInput(record.next_reset_at);
  const resetAtStart = resetAt.value;
  const period = el('input');
  period.type = 'number';
  period.min = '1';
  period.max = '720';
  period.value = String(record.reset_period_hours || 24);
  /* Only what the operator actually changed is sent, so opening the form and saving
     never moves a key's schedule by accident. */
  const resetChanges = () => {
    const changes = {};
    if (resetAt.value && resetAt.value !== resetAtStart) {
      changes.reset_at = new Date(resetAt.value).toISOString();
    }
    if (period.value !== '' && Number(period.value) !== record.reset_period_hours) {
      changes.reset_period_hours = Number(period.value);
    }
    return changes;
  };

  const picker = buildModelPicker(models, () =>
    apiFetch(keyActionPath(record.id, '/available-models'))
  );

  form.append(
    labelled('Name (empty = provider and key hint)', name),
    labelled('Models (comma separated)', models),
    picker,
    labelled('Daily cap (empty = unlimited)', cap),
    labelled('Token cap per window (empty = unlimited)', tokens),
    labelled('Next reset (your local time)', resetAt),
    scheduleButtons(resetAt),
    labelled('Window length (hours)', period)
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const list = models.value.split(',').map((model) => model.trim()).filter(Boolean);
    if (!list.length) {
      modal.status.report('Enter at least one model.', 'error');
      return;
    }
    const capText = cap.value.trim();
    modal.primaryButton.disabled = true;
    modal.status.run(
      async () => {
        const updated = await apiFetch(keyActionPath(record.id, ''), {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            ...resetChanges(),
            label: name.value.trim() === '' ? null : name.value.trim(),
            models: list,
            daily_cap: capText === '' ? null : Number(capText),
            token_cap: tokens.value.trim() === '' ? null : Number(tokens.value),
          }),
        });
        replaceRecord(updated);
        closeModal();
        poolKeys.panel.report(keyName(updated) + ' updated. It applies from the next request.', 'ok');
      },
      drawPoolKeysTable
    ).finally(() => {
      if (modal.primaryButton) {
        modal.primaryButton.disabled = false;
      }
    });
  });

  return form;
}

/* What the provider itself reports about a key's limits: its live answer where it
   has an endpoint for that, plus the rate-limit headers from the key's last request.
   Shown as the provider sent them, so a field the provider omits is simply absent. */
function limitRows(object) {
  return Object.entries(object).map(([name, value]) => ({
    name,
    value: value !== null && typeof value === 'object' ? JSON.stringify(value) : String(value),
  }));
}

function drawProviderLimits(record, info) {
  const columns = [
    { label: 'Field', cell: (row) => row.name },
    { label: 'Value', cell: (row) => row.value },
  ];
  const parts = [];

  parts.push(el('h4', null, 'Reported now by the provider'));
  if (info.live) {
    const free = info.live.free_model_daily_requests;
    if (free && free.limit !== undefined) {
      parts.push(
        el('p', 'note', 'Free-model requests today: ' + free.used + ' of ' + free.limit +
          ' used (' + free.remaining + ' left).')
      );
    }
    parts.push(dataTable(columns, limitRows(info.live), 'The provider returned nothing.'));
  } else if (info.live_error) {
    parts.push(el('p', 'note', info.live_error));
  } else {
    parts.push(el('p', 'note', 'This provider has no endpoint that reports a key\u2019s limits.'));
  }

  parts.push(el('h4', null, 'Seen on the last request'));
  if (info.observed) {
    parts.push(el('p', 'note', 'Captured ' + formatDate(info.observed.updated_at) + '.'));
    parts.push(dataTable(columns, limitRows(info.observed.headers), 'No headers.'));
  } else {
    parts.push(
      el('p', 'note', 'No rate-limit headers captured yet. They appear after the next request ' +
        'this key serves, if the provider sends them.')
    );
  }
  openModal({
    title: 'Provider limits for ' + keyName(record),
    content: parts,
    cancelLabel: 'Close',
    wide: true,
  });
}

async function showProviderLimits(record) {
  poolKeys.panel.report('Asking the provider…', 'pending');
  poolKeys.panel.run(
    () => apiFetch(keyActionPath(record.id, '/provider-limits')),
    (info) => {
      poolKeys.panel.report('', 'ok');
      drawProviderLimits(record, info);
    }
  );
}

async function drawPoolKeysTable() {
  const body = poolKeys.body;
  if (!body) {
    return;
  }
  
  // Show loading state
  body.replaceChildren(el('p', 'note', 'Loading...'));
  
  try {
    // Fetch all keys (we'll paginate on the client side)
    // Build query parameters for filtering and sorting
    const params = new URLSearchParams();
    
    // Add filters
    if (poolKeys.filters.state) {
      params.append('state', poolKeys.filters.state);
    }
    if (poolKeys.filters.provider) {
      params.append('provider', poolKeys.filters.provider);
    }
    if (poolKeys.filters.query) {
      params.append('search', poolKeys.filters.query);
    }
    
    // Note: The API doesn't support offset-based pagination, so we fetch all and paginate client-side
    // For sorting, we'll also need to do it client-side since the API sorting might not work with our pagination
    const records = await apiFetch(POOL_KEYS_PATH + '?' + params.toString());
    poolKeys.records = records || [];
    
    // Apply sorting client-side
    if (poolKeys.sort.column) {
      const column = POOL_KEY_COLUMNS.find(col => col.key === poolKeys.sort.column);
      if (column) {
        poolKeys.records.sort((a, b) => {
          const valA = recordValue(column, a);
          const valB = recordValue(column, b);
          return poolKeys.sort.ascending 
            ? compareValues(column, valA, valB) 
            : -compareValues(column, valA, valB);
        });
      }
    }
    
    const totalRecords = poolKeys.records.length;
    const paginatedRecords = pageOf(poolKeys.pager, poolKeys.records);
    
    if (!paginatedRecords.length) {
      body.replaceChildren(
        totalRecords
          ? emptyState('No keys match the current filters.')
          : emptyState(
            'No provider keys yet. Add one to start routing requests.',
            '+ Add your first key',
            openAddKey
          )
      );
      return;
    }
    
    const head = el('thead');
    const headRow = el('tr');
    for (const column of POOL_KEY_COLUMNS) {
      const th = el('th', null);
      th.scope = 'col';
      const active = poolKeys.sort.column === column.key;
      if (active) {
        th.setAttribute('aria-sort', poolKeys.sort.ascending ? 'ascending' : 'descending');
      }
      const button = el(
        'button',
        'sort-button',
        column.label + (active ? (poolKeys.sort.ascending ? ' ▲' : ' ▼') : '')
      );
      button.type = 'button';
      button.addEventListener('click', () => sortBy(column.key));
      th.append(button);
      headRow.append(th);
    }
    const actionsHead = el('th', 'actions-col', 'Actions');
    actionsHead.scope = 'col';
    headRow.append(actionsHead);
    head.append(headRow);
    
    const rowFor = (record) => {
      const tr = el('tr');
      for (const column of POOL_KEY_COLUMNS) {
        const cell = el('td');
        if (column.key === 'state') {
          cell.append(el('span', 'badge badge--' + record.state, STATE_LABELS[record.state] || record.state));
        } else {
          const formatted = formatCell(column, record);
          if (column.key === 'models') {
            // One line: first model, "+N" for the rest, full list on hover.
            const list = record.models || [];
            cell.classList.add('models-column');
            cell.append(el('span', 'models-text', list.length ? list[0] : formatted));
            if (list.length > 1) {
              cell.append(el('span', 'models-more', '+' + (list.length - 1)));
            }
            cell.title = list.join('\n');
            if (list.length) {
              const copy = el('button', 'models-copy', 'Copy');
              copy.type = 'button';
              copy.title = 'Copy all model names';
              copy.addEventListener('click', (event) => {
                event.stopPropagation();
                navigator.clipboard.writeText(list.join(', ')).then(() => {
                  copy.textContent = 'Copied';
                  setTimeout(() => { copy.textContent = 'Copy'; }, 1200);
                });
              });
              cell.append(copy);
            }
          } else {
            cell.textContent = formatted;
          }
        }
        tr.append(cell);
      }
      tr.append(rowActionCell(record));
      return tr;
    };
    
    const tbody = el('tbody');
    appendGrouped(
      tbody,
      paginatedRecords,
      keyGrouping(poolKeys.group, (members) => {
        const available = members.filter((key) => key.state === 'active').length;
        return plural(members.length, 'key') + ' · ' + available + ' available';
      }),
      rowFor,
      POOL_KEY_COLUMNS.length + 1
    );
    
    body.replaceChildren(
      ...withPager(tableOf(head, tbody), pagerBar(poolKeys.pager, totalRecords, 'key', drawPoolKeysTable))
    );
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      body.replaceChildren(el('p', 'note', 'Error loading keys: ' + err.message));
    }
  }
}

function drawOptions(select, values, selected, labels = {}) {
  select.replaceChildren(
    ...values.map((value) => {
      const option = el('option', null, labels[value] || value);
      option.value = value;
      return option;
    })
  );
  select.value = selected;
}

/* Filters carry an "All" escape hatch; the add-key provider picker must not —
   "All" is not a provider, and resetting the form would select it. */
function drawFilterOptions(select, values, selected, labels) {
  const all = el('option', null, 'All');
  all.value = '';
  drawOptions(select, values, selected, labels);
  select.replaceChildren(all, ...select.options);
  select.value = selected;
}

function buildFilters() {
  const stateSelect = el('select');
  stateSelect.id = 'filter-state';
  drawFilterOptions(stateSelect, KEY_STATES, poolKeys.filters.state, STATE_LABELS);
stateSelect.addEventListener('change', () => {
    poolKeys.filters.state = stateSelect.value;
    poolKeys.pager.page = 1; // Reset to first page when filters change
    drawPoolKeysTable();
});

  const providerSelect = el('select');
  providerSelect.id = 'filter-provider';
  drawFilterOptions(providerSelect, PROVIDERS, poolKeys.filters.provider, PROVIDER_NAMES);
providerSelect.addEventListener('change', () => {
    poolKeys.filters.provider = providerSelect.value;
    poolKeys.pager.page = 1; // Reset to first page when filters change
    drawPoolKeysTable();
});

  const search = el('input', 'search');
  search.type = 'search';
  search.id = 'filter-search';
  search.placeholder = 'Search id, hint, model…';
  search.value = poolKeys.filters.query;
  search.addEventListener('input', () => {
    poolKeys.filters.query = search.value;
    drawPoolKeysTable();
  });

  const groupSelect = pairSelect(KEY_GROUPS, poolKeys.group);
  groupSelect.addEventListener('change', () => {
    poolKeys.group = groupSelect.value;
    saveChoice('group.poolKeys', poolKeys.group);
    poolKeys.pager.page = 1; // Reset to first page when filters change
    drawPoolKeysTable();
  });

  const filters = el('div', 'filters');
  filters.append(
    labelled('Search', search),
    labelled('State', stateSelect),
    labelled('Provider', providerSelect),
    labelled('Group by', groupSelect)
  );
  return filters;
}

function labelled(text, control) {
  const wrapper = el('label', 'field');
  if (control.id) {
    wrapper.htmlFor = control.id;
  }
  wrapper.append(el('span', null, text), control);
  return wrapper;
}

/* Reads the named controls directly rather than going through the form's named
   property access, so the payload does not depend on implicit DOM behaviour. */
function addKeyPayload(fields) {
  const payload = {
    provider: fields.provider.value,
    models: fields.models.value
      .split(',')
      .map((model) => model.trim())
      .filter(Boolean),
    key: fields.key.value.trim(),
  };
  if (fields.label.value.trim() !== '') {
    payload.label = fields.label.value.trim();
  }

  const dailyCap = fields.dailyCap.value.trim();
  if (dailyCap !== '') {
    payload.daily_cap = Number(dailyCap);
  }
  const tokenCap = fields.tokenCap.value.trim();
  if (tokenCap !== '') {
    payload.token_cap = Number(tokenCap);
  }
  if (fields.resetAt.value) {
    const resetAt = new Date(fields.resetAt.value);
    if (Number.isNaN(resetAt.getTime())) {
      throw new RequestFailed(422, 'invalid_request', 'Enter a valid reset date.', 'reset_at');
    }
    payload.reset_at = resetAt.toISOString();
  }
  const resetHours = fields.resetPeriodHours.value.trim();
  if (resetHours !== '') {
    payload.reset_period_hours = Number(resetHours);
  }

  return payload;
}

/* Model picker. The text input stays the source of truth (so typing a model by
   hand still works); this lists what the provider offers and ticks or unticks
   entries in that same input. What is ticked is what the key serves, and so what
   /v1/models shows users. `load` returns [{id, name, free}] from the provider. */
function parseModelList(input) {
  return input.value.split(',').map((model) => model.trim()).filter(Boolean);
}

function buildModelPicker(input, load) {
  const wrap = el('div', 'model-picker');
  const status = createStatus();
  const fetch = el('button', 'ghost', 'Choose from provider\u2019s models');
  fetch.type = 'button';

  const filter = el('input');
  filter.type = 'search';
  filter.placeholder = 'Filter models\u2026';
  const freeOnly = el('input');
  freeOnly.type = 'checkbox';
  const freeLabel = el('label', 'picker-free');
  freeLabel.append(freeOnly, el('span', null, 'Free only'));
  const tools = el('div', 'picker-tools');
  tools.append(filter, freeLabel);
  tools.hidden = true;

  const list = el('div', 'picker-list');
  list.hidden = true;

  let catalog = [];
  const rows = new Map();

  const syncChecks = () => {
    const chosen = new Set(parseModelList(input));
    for (const [id, row] of rows) {
      row.box.checked = chosen.has(id);
    }
    summary();
  };
  const summary = () => {
    if (catalog.length) {
      status.report(
        parseModelList(input).length + ' selected of ' + catalog.length + ' offered.',
        'ok'
      );
    }
  };
  const applyFilter = () => {
    const needle = filter.value.trim().toLowerCase();
    for (const [id, row] of rows) {
      const hay = (id + ' ' + (row.name || '')).toLowerCase();
      row.node.hidden = (needle && !hay.includes(needle)) || (freeOnly.checked && !row.free);
    }
  };

  const draw = () => {
    rows.clear();
    list.replaceChildren();
    for (const model of catalog) {
      const node = el('label', 'picker-row');
      const box = el('input');
      box.type = 'checkbox';
      const text = el('span', null, model.id);
      node.append(box, text);
      if (model.name && model.name !== model.id) {
        node.title = model.name;
      }
      box.addEventListener('change', () => {
        const chosen = parseModelList(input).filter((id) => id !== model.id);
        if (box.checked) {
          chosen.push(model.id);
        }
        input.value = chosen.join(', ');
        summary();
      });
      rows.set(model.id, { node, box, name: model.name, free: Boolean(model.free) });
      list.append(node);
    }
    freeLabel.hidden = !catalog.some((model) => model.free);
    tools.hidden = false;
    list.hidden = false;
    syncChecks();
    applyFilter();
  };

  fetch.addEventListener('click', () => {
    fetch.disabled = true;
    status.run(
      async () => {
        status.report('Asking the provider\u2026', 'pending');
        catalog = (await load()).data;
      },
      () => {
        if (!catalog.length) {
          status.report('The provider returned no models.', 'error');
          return;
        }
        draw();
      }
    ).finally(() => {
      fetch.disabled = false;
    });
  });

  filter.addEventListener('input', applyFilter);
  freeOnly.addEventListener('change', applyFilter);
  input.addEventListener('input', syncChecks);

  /* Called when the surrounding form is reset, so a new key starts with no list. */
  wrap.clear = () => {
    catalog = [];
    rows.clear();
    list.replaceChildren();
    list.hidden = true;
    tools.hidden = true;
    status.report('', 'ok');
  };

  wrap.append(fetch, status.node, tools, list);
  return wrap;
}

function openAddKey() {
  openModal({
    title: 'Add provider key',
    content: (modal) => buildAddForm(modal),
    primary: { label: 'Add key', formId: 'add-key-form' },
    wide: true,
  });
}

function buildAddForm(modal) {
  const form = el('form', 'add-key');
  form.id = 'add-key-form';
  form.noValidate = true;

  /* Controls are addressed by name — the form's named property access is what
     addKeyPayload reads, and a name is what a submitted form would send. */
  const control = (tag, name, type) => {
    const node = el(tag);
    node.name = name;
    node.id = 'add-key-' + name;
    if (type) {
      node.type = type;
    }
    return node;
  };

  const provider = control('select', 'provider');
  drawOptions(provider, PROVIDERS, 'openai', PROVIDER_NAMES);

  const label = control('input', 'label', 'text');
  label.maxLength = 60;
  label.placeholder = 'e.g. Groq main (optional)';

  const models = control('input', 'models', 'text');
  models.placeholder = 'gpt-4o-mini, gpt-4o';
  models.required = true;

  /* The operator pastes the provider key once. Nothing echoes it back — the
     record the API returns carries a hint only, and the field is cleared on
     submit so the material does not linger in the form. */
  const key = control('input', 'key', 'password');
  key.className = 'key-material';
  key.autocomplete = 'off';
  key.spellcheck = false;
  key.required = true;

  const dailyCap = control('input', 'dailyCap', 'number');
  dailyCap.min = '0';
  dailyCap.placeholder = 'unlimited';

  const tokenCap = control('input', 'tokenCap', 'number');
  tokenCap.min = '1';
  tokenCap.placeholder = 'unlimited';

  const resetAt = control('input', 'resetAt', 'datetime-local');

  const resetPeriodHours = control('input', 'resetPeriodHours', 'number');
  resetPeriodHours.min = '1';
  resetPeriodHours.max = '720';
  resetPeriodHours.placeholder = '24';

  /* Before the key exists in the pool, the provider is asked with the key the
     operator just pasted; it is used for that one request and not kept. */
  const picker = buildModelPicker(models, async () => {
    if (!key.value.trim()) {
      throw new RequestFailed(422, 'invalid_request', 'Paste the provider key first.', 'key');
    }
    return apiFetch(AVAILABLE_MODELS_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: provider.value, key: key.value.trim() }),
    });
  });
  provider.addEventListener('change', () => picker.clear());
  form.addEventListener('reset', () => picker.clear());

  /* The required fields come first; limits and schedule are optional. */
  const grid = el('div', 'form-grid');
  grid.append(
    labelled('Provider', provider),
    labelled('Provider key', key),
    labelled('Name (optional)', label),
    labelled('Models (comma separated)', models),
    picker
  );
  const limitsGrid = el('div', 'form-grid');
  limitsGrid.append(
    labelled('Daily cap', dailyCap),
    labelled('Token cap per window', tokenCap),
    labelled('Resets at', resetAt),
    labelled('Reset period (hours)', resetPeriodHours),
    scheduleButtons(resetAt)
  );

  form.append(grid, el('h4', 'form-section', 'Limits (optional)'), limitsGrid);

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    let payload;
    try {
      payload = addKeyPayload({
        provider,
        label,
        models,
        key,
        dailyCap,
        tokenCap,
        resetAt,
        resetPeriodHours,
      });
    } catch (err) {
      modal.status.report(err.param ? err.param + ': ' + err.message : err.message, 'error');
      return;
    }

    if (!payload.key) {
      modal.status.report('Paste the provider key.', 'error');
      key.focus();
      return;
    }
    if (!payload.models.length) {
      modal.status.report('List at least one model.', 'error');
      models.focus();
      return;
    }

    modal.primaryButton.disabled = true;
    modal.status.run(async () => {
      const created = await apiFetch(POOL_KEYS_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      /* Drop the pasted material the moment it is accepted — the record that
         comes back carries a hint, and nothing else should hold the secret. */
      form.reset();
      closeModal();
      replaceRecord(created);
      poolKeys.panel.report(keyName(created) + ' added. It is routable now.', 'ok');
    }, drawPoolKeysTable).finally(() => {
      if (modal.primaryButton) {
        modal.primaryButton.disabled = false;
      }
    });
  });

  return form;
}

async function loadKeys() {
  poolKeys.panel.report('Loading keys…', 'pending');
  // Reset pagination when loading new data
  poolKeys.pager.page = 1;
  try {
    poolKeys.records = await apiFetch(POOL_KEYS_PATH);
    poolKeys.panel.report(poolKeys.records.length + ' key(s) in the pool.', 'ok');
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      poolKeys.panel.report(err.message, 'error');
    }
  }
  drawPoolKeysTable();
}

function renderPoolKeys(view) {
  poolKeys.records = [];
  poolKeys.filters = { state: '', provider: '', query: '' };
  poolKeys.group = savedChoice('group.poolKeys', 'provider', KEY_GROUPS.map((entry) => entry[0]));
  poolKeys.sort = { column: 'name', ascending: true };
  poolKeys.panel = createStatus();
  poolKeys.body = el('div', 'table-wrap');

  mountPage(view, {
    title: 'Pool Keys',
    status: poolKeys.panel,
    actions: [button('+ Add key', 'primary', openAddKey), refreshButton(() => loadKeys())],
    tools: [buildFilters()],
    content: [poolKeys.body],
  });
  loadKeys();
}

/* -- Utilization & Failover ------------------------------------------------------
 *
 * Read-only views of utilization and failover events. The seamless-failover rate
 * is computed from the events currently loaded, so paging in more events moves it.
 */

const health = {
  utilization: null,
  events: [],
  nextBefore: null,
  panel: null,
  summary: null,
  keyTable: null,
  eventTable: null,
  loadMore: null,
};

function humanizeSeconds(total) {
  if (total === null || total === undefined) {
    return '—';
  }
  const seconds = Math.max(0, Math.round(total));
  if (seconds < 60) {
    return seconds + 's';
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return minutes + 'm';
  }
  const hours = Math.floor(minutes / 60);
  if (hours < 24) {
    return hours + 'h ' + (minutes % 60) + 'm';
  }
  return Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
}

/* Pool key and request ids are UUIDv7, whose leading hex is a millisecond
   timestamp — keys created together share a prefix, so showing only the head
   renders a whole batch of them identically. The discriminating suffix is what
   gets displayed; the full id stays on hover for copying. */
function shortId(value) {
  return value ? String(value).slice(-8) : '—';
}

/* Failover events settle milliseconds apart and carry nanosecond `ts`, so second
   resolution would print a burst of events on one indistinguishable timestamp. */
/* Fractional seconds belong inside the locale's own time format: appending ".ms"
   to toLocaleString() lands after "PM" in 12-hour locales. */
const EVENT_TIME_FORMAT = new Intl.DateTimeFormat(undefined, {
  year: 'numeric', month: 'short', day: 'numeric',
  hour: 'numeric', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3,
  timeZoneName: 'short',
});

function formatEventTime(value) {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  return EVENT_TIME_FORMAT.format(parsed);
}

/* Only ever receives a number computed above, never operator-supplied text. */
function meter(fraction) {
  const track = el('div', 'meter');
  const fill = el('div', 'meter-fill');
  fill.style.width = Math.max(0, Math.min(100, Number(fraction) || 0)) + '%';
  track.append(fill);
  return track;
}

function metricCard(label, value, detail, fraction) {
  const card = el('div', 'metric');
  card.append(el('span', 'metric-label', label), el('span', 'metric-value', value));
  if (fraction !== null && fraction !== undefined) {
    card.append(meter(fraction));
  }
  card.append(el('span', 'metric-detail', detail));
  return card;
}

function seamlessFailoverRate() {
  const total = health.events.length;
  const successful = health.events.filter((event) => event.outcome === 'success').length;
  return {
    total,
    successful,
    pct: total ? (successful / total) * 100 : null,
  };
}

function drawSummary() {
  const aggregate = health.utilization ? health.utilization.aggregate : null;
  const rate = seamlessFailoverRate();

  const utilizationPct = aggregate ? aggregate.utilization_pct : null;
  const totalKeys = health.utilization ? health.utilization.keys.length : 0;

  health.summary.replaceChildren(
    metricCard(
      'Pool utilization',
      utilizationPct === null ? '—' : utilizationPct.toFixed(1) + '%',
      utilizationPct === null
        ? 'no capped keys to divide by'
        : NUMBER_FORMAT.format(aggregate.total_remaining) +
          ' left of ' +
          NUMBER_FORMAT.format(aggregate.total_cap),
      utilizationPct
    ),
    metricCard(
      'Requests served',
      aggregate ? NUMBER_FORMAT.format(aggregate.total_used) : '—',
      'this reset window, across ' + totalKeys + ' key(s)'
    ),
    metricCard(
      'Tokens used',
      aggregate ? NUMBER_FORMAT.format(aggregate.tokens_used) : '\u2014',
      aggregate && aggregate.token_cap_total
        ? NUMBER_FORMAT.format(aggregate.tokens_remaining_total) +
          ' left of ' +
          NUMBER_FORMAT.format(aggregate.token_cap_total) +
          ' on keys with a token cap'
        : 'this reset window; no token caps set'
    ),
    metricCard(
      'Keys by state',
      NUMBER_FORMAT.format(totalKeys),
      aggregate
        ? aggregate.active_keys +
          ' active · ' +
          aggregate.exhausted_keys +
          ' exhausted · ' +
          aggregate.cooldown_keys +
          ' cooldown · ' +
          aggregate.suspended_keys +
          ' suspended'
        : '—'
    ),
    metricCard(
      'Seamless failover',
      rate.pct === null ? '—' : rate.pct.toFixed(1) + '%',
      rate.total
        ? rate.successful + ' of ' + rate.total + ' loaded event(s)'
        : 'no failover events loaded',
      rate.pct
    )
  );
}

/* The operator's grouping choice per page, kept for the tab's session so a reload does
   not undo it. Session storage, like the sign-in key: nothing here is written to
   persistent storage. When storage is blocked the page starts from its default. */
function savedChoice(name, fallback, allowed) {
  const stored = withStorage((storage) => storage.getItem('palimpsest.' + name));
  return stored !== null && allowed.includes(stored) ? stored : fallback;
}

function saveChoice(name, value) {
  withStorage((storage) => storage.setItem('palimpsest.' + name, value));
}

/* Grouping. Rows gather under a header line that says how many there are and whatever
   totals the page wants; a click on the header folds the group. `group` is
   { key(item), label(key), summary(items) }; pass null for a flat list. */
function appendGrouped(tbody, items, group, rowFor, colspan) {
  if (!group) {
    for (const item of items) {
      tbody.append(rowFor(item));
    }
    return;
  }
  const buckets = new Map();
  for (const item of items) {
    const key = group.key(item);
    if (!buckets.has(key)) {
      buckets.set(key, []);
    }
    buckets.get(key).push(item);
  }
  const ordered = [...buckets.entries()].sort((a, b) =>
    group.label(a[0]).localeCompare(group.label(b[0]))
  );
  for (const [key, members] of ordered) {
    const rowEls = members.map(rowFor);
    const label = group.label(key);
    const toggle = el('button', 'group-toggle', '\u25be ' + label);
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', 'true');
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      toggle.textContent = (open ? '\u25be ' : '\u25b8 ') + label;
      for (const row of rowEls) {
        row.hidden = !open;
      }
    });
    const cell = el('td');
    cell.colSpan = colspan;
    cell.append(toggle, el('span', 'group-summary', ' \u00b7 ' + group.summary(members)));
    const header = el('tr', 'group-row');
    header.append(cell);
    tbody.append(header, ...rowEls);
  }
}

function plural(count, noun) {
  return NUMBER_FORMAT.format(count) + ' ' + (count === 1 ? noun : noun + 's');
}

const KEY_GROUPS = [
  ['', 'No grouping'],
  ['provider', 'By provider'],
  ['state', 'By status'],
];

/* Grouping for lists of pool keys, whether the pool or the utilization view. */
function keyGrouping(mode, summarize) {
  if (mode === 'provider') {
    return {
      key: (key) => key.provider || 'unknown',
      label: (provider) => PROVIDER_NAMES[provider] || provider,
      summary: summarize,
    };
  }
  if (mode === 'state') {
    return {
      key: (key) => key.state,
      label: (state) => STATE_LABELS[state] || state,
      summary: summarize,
    };
  }
  return null;
}

/* How a key is named everywhere in the dashboard: the operator's own label if there
   is one, otherwise the provider and the tail of the key, which is what the provider's
   own console shows. An internal id never carries meaning for a person. */
const PROVIDER_NAMES = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  openrouter: 'OpenRouter',
};

function keyName(key) {
  if (key.label) {
    return key.label;
  }
  const provider = PROVIDER_NAMES[key.provider] || key.provider || 'Key';
  return key.key_hint ? provider + ' ' + key.key_hint : provider;
}

const STATE_LABELS = {
  active: 'Available',
  exhausted: 'Out of quota',
  cooldown: 'Paused',
  suspended: 'Suspended',
};

/* When the key's counters start again: the countdown, and the moment it points at. */
function renewCell(key) {
  const cell = el('td');
  const line = el('div', 'cell-line');
  line.append(el('span', null, humanizeSeconds(key.seconds_to_reset)));
  if (key.next_reset_at) {
    line.append(el('span', 'note', 'at ' + formatDate(key.next_reset_at)));
  }
  cell.append(line);
  return cell;
}

/* "7 of 100 used · 93 left" with a bar, or "7 used · no limit set". */
function budgetCell(used, cap, unit) {
  const cell = el('td', 'usage-cell');
  const line = el('div', 'cell-line');
  cell.append(line);
  const hasCap = cap !== null && cap !== undefined;
  if (hasCap) {
    line.append(meter((used / cap) * 100));
  }
  const spent = NUMBER_FORMAT.format(used || 0);
  /* "1 request", "2 requests": the noun follows the number it comes after. */
  const noun = (count) => (count === 1 ? unit.replace(/s$/, '') : unit);
  line.append(
    el(
      'span',
      null,
      hasCap
        ? spent + ' of ' + NUMBER_FORMAT.format(cap) + ' ' + noun(cap) + ' used \u00b7 ' +
            NUMBER_FORMAT.format(Math.max(0, cap - (used || 0))) + ' left'
        : spent + ' ' + noun(used || 0) + ' used \u00b7 no limit set'
    )
  );
  return cell;
}

function drawUtilizationKeys() {
  const keys = health.utilization ? health.utilization.keys : [];
  if (!keys.length) {
    health.keyTable.replaceChildren(
      el('p', 'note', health.utilization ? 'No provider keys in the pool.' : 'Not loaded.')
    );
    return;
  }

  const head = el('thead');
  const headRow = el('tr');
  for (const label of ['Key', 'Status', 'Requests this period', 'Tokens this period', 'Renews in']) {
    const th = el('th', null, label);
    th.scope = 'col';
    if (label === 'Renews in') {
      th.title =
        'When this key\u2019s own request and token budgets start again (default: midnight UTC). ' +
        'It does not predict the provider\u2019s quota; change it in Pool Keys, Edit.';
    }
    headRow.append(th);
  }
  head.append(headRow);

  const rowFor = (key) => {
    const name = el('td');
    name.textContent = keyName(key);
    name.title = 'Key ' + key.key_id;

    /* Everything about a key stays on its one row, notes included. */
    const status = el('td');
    const statusLine = el('div', 'cell-line');
    status.append(statusLine);
    statusLine.append(el('span', 'badge badge--' + key.state, STATE_LABELS[key.state] || key.state));
    if (key.state === 'cooldown' && key.cooldown_remaining_s) {
      statusLine.append(el('span', 'note', 'back in ' + humanizeSeconds(key.cooldown_remaining_s)));
    }
    /* A model can be paused on its own while the key keeps serving the others. */
    for (const cooling of key.cooling_models || []) {
      statusLine.append(
        el('span', 'note', cooling.model + ' paused, back in ' + humanizeSeconds(cooling.seconds_remaining))
      );
    }

    const tr = el('tr');
    tr.append(
      name,
      status,
      budgetCell(key.used, key.daily_cap, 'requests'),
      budgetCell(key.tokens_used, key.token_cap, 'tokens'),
      renewCell(key)
    );
    return tr;
  };

  const summarize = (members) =>
    plural(members.length, 'key') +
    ' \u00b7 ' +
    plural(members.reduce((sum, key) => sum + (key.used || 0), 0), 'request') +
    ' \u00b7 ' +
    plural(members.reduce((sum, key) => sum + (key.tokens_used || 0), 0), 'token');

  const tbody = el('tbody');
  appendGrouped(
    tbody,
    pageOf(health.keyPager, keys),
    keyGrouping(health.group, summarize),
    rowFor,
    5
  );

  health.keyTable.replaceChildren(
    el(
      'p',
      'note',
      'A period is each key\u2019s own reset window; both counters restart when it renews. Tokens ' +
        'count only replies where the provider reported them. Change limits in Pool Keys \u2192 Edit.'
    ),
    ...withPager(tableOf(head, tbody), pagerBar(health.keyPager, keys.length, 'key', drawUtilizationKeys))
  );
}

const OUTCOME_TONES = {
  success: 'active',
  exhausted_503: 'cooldown',
  error_5xx: 'exhausted',
  error_4xx: 'suspended',
};

/* A failover event only stores key ids; the utilization view, loaded alongside, knows
   which key each one is. A key removed since then falls back to a short id. */
function failoverKeyName(id) {
  const known = health.utilization ? health.utilization.keys.find((key) => key.key_id === id) : null;
  return known ? keyName(known) : shortId(id);
}

function drawEvents() {
  if (!health.events.length) {
    health.eventTable.replaceChildren(
      el(
        'p',
        'note',
        health.eventsLoaded ? 'No failover events recorded.' : 'No failover events loaded.'
      )
    );
    // Hide load more button when no events
    if (health.loadMore) {
      health.loadMore.hidden = true;
    }
    return;
  }

  const head = el('thead');
  const headRow = el('tr');
  for (const label of ['Time', 'Outcome', 'Attempts', 'Duration', 'Keys', 'Status', 'Request']) {
    const th = el('th', null, label);
    th.scope = 'col';
    headRow.append(th);
  }
  head.append(headRow);

  const tbody = el('tbody');
  for (const event of pageOf(health.eventPager, health.events)) {
    const outcome = el('td');
    outcome.append(
      el('span', 'badge badge--' + (OUTCOME_TONES[event.outcome] || 'suspended'), event.outcome)
    );

    const keys = el('td');
    keys.textContent = failoverKeyName(event.source_key_id) + ' → ' + failoverKeyName(event.target_key_id);
    keys.title = [event.source_key_id, event.target_key_id].filter(Boolean).join(' → ');

    const request = el('td');
    request.textContent = shortId(event.request_id);
    request.title = event.request_id;

    const tr = el('tr');
    tr.append(
      el('td', null, formatEventTime(event.ts)),
      outcome,
      el('td', null, String(event.attempts)),
      el('td', null, NUMBER_FORMAT.format(event.duration_ms) + ' ms'),
      keys,
      el('td', null, String(event.status)),
      request
    );
    tbody.append(tr);
  }

  health.eventTable.replaceChildren(
    ...withPager(
      tableOf(head, tbody),
      pagerBar(health.eventPager, health.events.length, 'event', drawEvents)
    )
  );

  // Update load more button state
  if (health.loadMore) {
    const hasMore = health.nextBefore !== null;
    health.loadMore.hidden = !hasMore;
    health.loadMore.disabled = false;
    
    // Update button text to show how many events loaded
    health.loadMore.textContent = hasMore 
      ? `Load older events (${health.events.length} loaded so far)`
      : `All events loaded (${health.events.length} total)`;
  }
}

function drawHealth() {
  drawSummary();
  drawUtilizationKeys();
  drawEvents();
  health.loadMore.hidden = health.nextBefore === null;
}

/* The cursor is an ISO timestamp ending in "+00:00"; interpolated raw, the "+"
   is read as a space and the next page comes back rejected. */
function eventsPath(before) {
  const query = 'limit=' + EVENTS_PAGE_SIZE;
  return before
    ? EVENTS_PATH + '?' + query + '&before=' + encodeURIComponent(before)
    : EVENTS_PATH + '?' + query;
}

async function loadEventsPage(before) {
  const page = await apiFetch(eventsPath(before));
  health.events = before ? health.events.concat(page.events) : page.events;
  health.nextBefore = page.next_before === undefined ? null : page.next_before;
  health.eventsLoaded = true;
  
  // Update button state after loading
  if (health.loadMore) {
    const hasMore = health.nextBefore !== null;
    health.loadMore.hidden = !hasMore;
    health.loadMore.disabled = false;
    
    // Update button text to show how many events loaded
    health.loadMore.textContent = hasMore 
      ? `Load older events (${health.events.length} loaded so far)`
      : `All events loaded (${health.events.length} total)`;
  }
}

async function loadHealth() {
  health.panel.report('Loading…', 'pending');
  health.eventsLoaded = false;

  /* The two reads are independent: a failed utilization read must not leave the
     events list stale on Refresh (and vice versa). */
  const failures = [];
  try {
    health.utilization = await apiFetch(UTILIZATION_PATH);
  } catch (err) {
    if (err instanceof SignedOut) {
      return;
    }
    failures.push(err.message);
  }
  try {
    await loadEventsPage(null);
  } catch (err) {
    if (err instanceof SignedOut) {
      return;
    }
    failures.push(err.message);
  }

  if (failures.length) {
    health.panel.report(failures.join(' · '), 'error');
  } else {
    health.panel.report(
      health.utilization.keys.length + ' key(s) · ' + health.events.length + ' failover event(s).',
      'ok'
    );
  }
  drawHealth();
}

function renderHealth(view) {
  health.utilization = null;
  health.events = [];
  health.nextBefore = null;
  health.eventsLoaded = false;
  health.keyPager = newPager();
  health.eventPager = newPager();
  health.panel = createStatus();

  health.loadMore = el('button', 'ghost load-more', 'Load older events');
  health.loadMore.type = 'button';
  health.loadMore.hidden = true;
  health.loadMore.addEventListener('click', () => {
    /* One page request at a time: a double click would fetch the same cursor
       twice and append the page twice. */
    health.loadMore.disabled = true;
    health.panel
      .run(
        () => loadEventsPage(health.nextBefore),
        () => {
          health.panel.report(health.events.length + ' failover event(s) loaded.', 'ok');
          drawHealth();
        }
      )
      .finally(() => {
        health.loadMore.disabled = false;
      });
  });

  health.summary = el('div', 'summary');
  health.keyTable = el('div', 'table-wrap');
  health.group = savedChoice('group.keys', 'provider', KEY_GROUPS.map((entry) => entry[0]));
  health.eventTable = el('div', 'table-wrap');

  const groupBy = pairSelect(KEY_GROUPS, health.group);
  groupBy.addEventListener('change', () => {
    health.group = groupBy.value;
    health.keyPager.page = 1;
    saveChoice('group.keys', health.group);
    drawUtilizationKeys();
  });
  const filters = el('div', 'filters');
  filters.append(labelled('Group keys', groupBy));

  mountPage(view, {
    title: 'Utilization & Failover',
    status: health.panel,
    actions: [refreshButton(() => loadHealth())],
    tools: [filters],
    content: [
      health.summary,
      el('h3', null, 'Utilization by key'),
      health.keyTable,
      el('h3', null, 'Failover events'),
      health.eventTable,
      health.loadMore,
    ],
  });
  loadHealth();
}

/* -- Users ----------------------------------------------------------------------
 *
 * Operator view over the user realm. Creating an account stays on the
 * self-service endpoint, which hands back the first key exactly once — the only
 * moment any user key material reaches a browser, and it is never stored.
 */

const users = {
  accounts: [],
  panel: null,
  table: null,
};

function accountStatusPath(id, action) {
  return ACCOUNTS_PATH + '/' + encodeURIComponent(id) + '/' + action;
}

function accountKeysPath(id) {
  return accountStatusPath(id, 'keys');
}

function openCreateUser() {
  openModal({
    title: 'New user',
    content: (modal) => buildCreateForm(modal),
    primary: { label: 'Create user', formId: 'create-account-form' },
  });
}

function buildCreateForm(modal) {
  const form = el('form', 'form-grid');
  form.id = 'create-account-form';
  form.noValidate = true;

  const displayName = el('input');
  displayName.type = 'text';
  displayName.name = 'displayName';
  displayName.id = 'new-account-name';
  displayName.maxLength = 200;
  displayName.placeholder = 'e.g. Research team';

  form.append(
    labelled('Display name', displayName),
    el('p', 'note', 'The user’s first API key is shown once, right after creation.')
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    modal.primaryButton.disabled = true;
    modal.status
      .run(
        () => apiFetch(CREATE_ACCOUNT_PATH, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ display_name: displayName.value.trim() }),
        }),
        async (created) => {
          form.reset();
          /* The one-time key gets its own dismissible block rather than a status
             line, so it cannot be left sitting in a message the operator walks
             away from — and it is dropped the moment they dismiss it. Shown
             before the list refresh: if that fails, the key (visible exactly
             once) must not be lost with it. */
          showIssuedKey(created);
          await loadAccounts();
        }
      )
      .finally(() => {
        if (modal.primaryButton) {
          modal.primaryButton.disabled = false;
        }
      });
  });

  return form;
}

/* The one-time key replaces the create form in the same dialog, so it cannot be
   left in a status line the operator walks away from, and it is dropped the
   moment the dialog closes. */
function showIssuedKey(created) {
  const key = created.keys && created.keys[0] ? created.keys[0].key : null;
  if (!key) {
    closeModal();
    users.panel.report('Account created, but the gateway returned no key for it.', 'error');
    return;
  }

  const value = el('code', 'issued-key-value', key);
  const modal = openModal({
    title: 'User ' + created.display_name + ' created',
    content: [
      el('p', 'issued-key-warning', 'Copy this API key now. It is shown once and never again.'),
      value,
    ],
    cancelLabel: null,
    primary: { label: 'Done, I saved it', onClick: closeModal },
  });

  if (navigator.clipboard) {
    const copy = button('Copy key', 'ghost btn-sm', () =>
      navigator.clipboard.writeText(key).then(
        () => modal.status.report('Copied to the clipboard.', 'ok'),
        () => modal.status.report('Copy failed; select the key and copy it.', 'error')
      )
    );
    modal.body.append(copy);
  }
}

function accountRowActions(account) {
  const cell = el('td', 'row-actions');
  const disabled = account.status === 'disabled';

  const toggle = button(disabled ? 'Enable' : 'Disable', 'ghost btn-sm', () => {
    const action = disabled ? 'enable' : 'disable';
    users.panel.run(
      async () => {
        const result = await apiFetch(accountStatusPath(account.id, action), { method: 'POST' });
        account.status = result.status;
        users.panel.report(
          'Account ' + account.display_name + ' ' + (disabled ? 'enabled.' : 'disabled.'),
          'ok'
        );
      },
      drawUsers
    );
  });

  toggle.title = disabled ? 'Let this user’s keys authenticate again' : 'Block this user’s keys';

  const details = button('Details', 'ghost btn-sm', () => showUserDetail(account.id));
  const keys = button('Keys', 'ghost btn-sm', () => showAccountKeys(account));

  const remove = button('Delete', 'ghost btn-sm danger', async () => {
    const name = account.display_name || shortId(account.id);
    const confirmed = await confirmAction({
      title: 'Delete ' + name + '?',
      message:
        'Its API keys stop working and its sessions are removed. This cannot be undone. ' +
        'To block it without losing anything, use Disable instead.',
      confirmLabel: 'Delete user',
    });
    if (!confirmed) {
      return;
    }
    users.panel.run(
      async () => {
        await apiFetch(ACCOUNTS_PATH + '/' + encodeURIComponent(account.id), { method: 'DELETE' });
        users.accounts = users.accounts.filter((candidate) => candidate.id !== account.id);
        users.panel.report('Account ' + name + ' deleted.', 'ok');
      },
      drawUsers
    );
  });

  cell.append(details, toggle, keys, remove);
  return cell;
}

function drawUsers() {
  if (!users.accounts.length) {
    users.table.replaceChildren(
      emptyState('No user accounts yet.', '+ Create the first user', openCreateUser)
    );
    return;
  }

  const head = el('thead');
  const headRow = el('tr');
  for (const label of ['Account', 'Created', 'Active keys', 'Status', 'Actions']) {
    const th = el('th', label === 'Actions' ? 'actions-col' : null, label);
    th.scope = 'col';
    headRow.append(th);
  }
  head.append(headRow);

  const tbody = el('tbody');
  for (const account of pageOf(users.pager, users.accounts)) {
    const name = el('td');
    name.append(el('span', null, account.display_name || '(unnamed)'));
    const id = el('span', 'cell-id', shortId(account.id));
    id.title = account.id;
    name.append(id);

    const status = el('td');
    status.append(
      el(
        'span',
        'badge badge--' + (account.status === 'disabled' ? 'suspended' : 'active'),
        account.status === 'disabled' ? 'Disabled' : account.status === 'active' ? 'Active' : account.status
      )
    );

    const tr = el('tr');
    tr.append(
      name,
      el('td', null, formatDate(account.created_at)),
      el('td', null, NUMBER_FORMAT.format(account.active_key_count)),
      status,
      accountRowActions(account)
    );
    tbody.append(tr);
  }

  users.table.replaceChildren(
    ...withPager(tableOf(head, tbody), pagerBar(users.pager, users.accounts.length, 'account', drawUsers))
  );
}

async function showAccountKeys(account) {
  users.panel.run(
    () => apiFetch(accountKeysPath(account.id)),
    (keys) => {
      const show = (content) =>
        openModal({
          title: 'Keys for ' + (account.display_name || shortId(account.id)),
          content,
          cancelLabel: 'Close',
          wide: true,
        });

      if (!keys.length) {
        show(el('p', 'note', 'This account has no keys.'));
        return;
      }

      const wrap = el('div', 'table-wrap');
      const pager = newPager(10);
      const draw = () => {
        const head = el('thead');
        const headRow = el('tr');
        for (const label of ['Prefix', 'Status', 'Created', 'Last used']) {
          const th = el('th', null, label);
          th.scope = 'col';
          headRow.append(th);
        }
        head.append(headRow);

        const tbody = el('tbody');
        for (const key of pageOf(pager, keys)) {
          const tr = el('tr');
          const status = el('td');
          status.append(
            el('span', 'badge badge--' + (key.status === 'active' ? 'active' : 'suspended'), key.status)
          );
          tr.append(
            el('td', null, key.prefix),
            status,
            el('td', null, formatDate(key.created_at)),
            el('td', null, key.last_used_at ? formatDate(key.last_used_at) : '—')
          );
          tbody.append(tr);
        }
        wrap.replaceChildren(...withPager(tableOf(head, tbody), pagerBar(pager, keys.length, 'key', draw)));
      };
      draw();
      show(wrap);
    }
  );
}

async function loadAccounts() {
  users.accounts = await apiFetch(ACCOUNTS_PATH);
  users.panel.report(
    users.accounts.length + ' account(s), ' +
      users.accounts.reduce((total, account) => total + account.active_key_count, 0) +
      ' active key(s).',
    'ok'
  );
  drawUsers();
}

function renderUsers(view) {
  users.accounts = [];
  users.pager = newPager();
  users.panel = createStatus();
  users.table = el('div', 'table-wrap');

  mountPage(view, {
    title: 'Users',
    status: users.panel,
    actions: [button('+ New user', 'primary', openCreateUser), refreshButton(() => loadUsers())],
    content: [users.table],
  });
  loadUsers();
}

async function loadUsers() {
  users.panel.report('Loading accounts…', 'pending');
  try {
    await loadAccounts();
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      users.panel.report(err.message, 'error');
    }
  }
}

/* -- Usage, Sessions and Requests ---------------------------------------------------
 *
 * Operator statistics. Everything here is metadata: ids, model strings, counts and
 * timings. Conversation content is never requested and never shown.
 */

const USAGE_PATH = '/operator/v1/usage';
const REQUESTS_PATH = '/operator/v1/requests';

const accountNames = {};

async function loadAccountNames() {
  const accounts = await apiFetch(ACCOUNTS_PATH);
  for (const account of accounts) {
    accountNames[account.id] = account.display_name || '(unnamed) ' + shortId(account.id);
  }
  return accounts;
}

function accountLabel(id) {
  return accountNames[id] || shortId(id);
}

/* Value/label pairs, unlike drawOptions, which uses one string for both. */
function pairSelect(pairs, selected) {
  const select = el('select');
  for (const [value, label] of pairs) {
    const option = el('option', null, label);
    option.value = value;
    select.append(option);
  }
  select.value = selected;
  return select;
}

function cellOf(content) {
  const td = el('td');
  if (content instanceof Node) {
    td.append(content);
  } else {
    td.textContent = content === null || content === undefined || content === '' ? '—' : content;
  }
  return td;
}

/* `paging` is { pager, noun, redraw } for a list that can be long; the result is then a
   fragment holding the table and its bar. */
function dataTable(columns, rows, emptyMessage, group = null, paging = null) {
  if (!rows.length) {
    return el('p', 'note', emptyMessage);
  }
  const total = rows.length;
  if (paging) {
    rows = pageOf(paging.pager, rows);
  }
  const headRow = el('tr');
  for (const column of columns) {
    const th = el('th', null, column.label);
    th.scope = 'col';
    headRow.append(th);
  }
  const head = el('thead');
  head.append(headRow);

  const body = el('tbody');
  appendGrouped(
    body,
    rows,
    group,
    (row) => {
      const tr = el('tr');
      for (const column of columns) {
        tr.append(cellOf(column.cell(row)));
      }
      return tr;
    },
    columns.length
  );
  const table = tableOf(head, body);
  if (!paging) {
    return table;
  }
  const fragment = document.createDocumentFragment();
  fragment.append(...withPager(table, pagerBar(paging.pager, total, paging.noun, paging.redraw)));
  return fragment;
}

function tokens(value) {
  return value === null || value === undefined ? null : NUMBER_FORMAT.format(value);
}

function idCell(label, id) {
  const span = el('span', null, label);
  span.title = id;
  return span;
}

/* Usage */

const USAGE_RANGES = [
  ['1', 'Today'],
  ['7', 'Last 7 days'],
  ['30', 'Last 30 days'],
  ['90', 'Last 90 days'],
];
const USAGE_GROUPS = [
  ['model', 'By model'],
  ['account', 'By user'],
  ['provider', 'By provider'],
  ['provider_model', 'By provider and model'],
  ['account_model', 'By user and model'],
  ['account_provider', 'By user and provider'],
];
/* Which row fields each grouping keeps, in column order. */
const USAGE_DIMENSIONS = {
  model: ['model'],
  account: ['account_id'],
  provider: ['provider'],
  provider_model: ['provider', 'model'],
  account_model: ['account_id', 'model'],
  account_provider: ['account_id', 'provider'],
};
const USAGE_DIMENSION_COLUMNS = {
  account_id: {
    label: 'User',
    cell: (row) => idCell(accountLabel(row.account_id), row.account_id),
  },
  provider: {
    label: 'Provider',
    cell: (row) => (row.provider === 'unknown' ? 'Unknown' : PROVIDER_NAMES[row.provider] || row.provider),
  },
  model: { label: 'Model', cell: (row) => row.model },
};

const usage = {
  panel: null,
  summary: null,
  table: null,
  daily: null,
  data: null,
  days: '7',
  group: 'model',
  pager: newPager(),
  dailyPager: newPager(),
};

function groupUsage(rows, mode) {
  const dimensions = USAGE_DIMENSIONS[mode] || USAGE_DIMENSIONS.model;
  const groups = new Map();
  for (const row of rows) {
    const key = dimensions.map((dimension) => row[dimension]).join('\u0000');
    let group = groups.get(key);
    if (!group) {
      group = {
        account_id: null,
        provider: null,
        model: null,
        requests: 0,
        ok: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        total_tokens: 0,
        requests_without_usage: 0,
      };
      for (const dimension of dimensions) {
        group[dimension] = row[dimension];
      }
      groups.set(key, group);
    }
    for (const field of ['requests', 'ok', 'prompt_tokens', 'completion_tokens', 'total_tokens', 'requests_without_usage']) {
      group[field] += row[field];
    }
  }
  return [...groups.values()].sort((a, b) => b.total_tokens - a.total_tokens);
}

function shareCell(fraction) {
  const cell = el('div', 'share');
  cell.append(meter(fraction * 100), el('span', null, (fraction * 100).toFixed(1) + '%'));
  return cell;
}

function drawUsage() {
  const data = usage.data;
  const rows = data.rows;
  const total = rows.reduce((sum, row) => sum + row.total_tokens, 0);
  const prompt = rows.reduce((sum, row) => sum + row.prompt_tokens, 0);
  const completion = rows.reduce((sum, row) => sum + row.completion_tokens, 0);
  const requests = rows.reduce((sum, row) => sum + row.requests, 0);
  const unknown = rows.reduce((sum, row) => sum + row.requests_without_usage, 0);

  usage.summary.replaceChildren(
    metricCard('Total tokens', NUMBER_FORMAT.format(total), 'since ' + data.since),
    metricCard('Prompt tokens', NUMBER_FORMAT.format(prompt), 'sent to providers'),
    metricCard('Completion tokens', NUMBER_FORMAT.format(completion), 'returned by providers'),
    metricCard(
      'Requests served',
      NUMBER_FORMAT.format(requests),
      unknown ? NUMBER_FORMAT.format(unknown) + ' without provider token counts' : 'all with token counts'
    )
  );

  const columns = (USAGE_DIMENSIONS[usage.group] || USAGE_DIMENSIONS.model).map(
    (dimension) => USAGE_DIMENSION_COLUMNS[dimension]
  );
  columns.push(
    { label: 'Requests', cell: (row) => tokens(row.requests) },
    { label: 'Prompt tokens', cell: (row) => tokens(row.prompt_tokens) },
    { label: 'Completion tokens', cell: (row) => tokens(row.completion_tokens) },
    { label: 'Total tokens', cell: (row) => tokens(row.total_tokens) },
    { label: 'Share', cell: (row) => shareCell(total ? row.total_tokens / total : 0) }
  );
  usage.table.replaceChildren(
    dataTable(columns, groupUsage(rows, usage.group), 'No usage recorded in this range yet.', null, {
      pager: usage.pager,
      noun: 'row',
      redraw: drawUsage,
    })
  );

  const peak = Math.max(0, ...data.daily.map((day) => day.prompt_tokens + day.completion_tokens));
  usage.daily.replaceChildren(
    el('h3', null, 'Tokens per day'),
    dataTable(
      [
        { label: 'Day (UTC)', cell: (day) => day.day },
        {
          label: 'Tokens',
          cell: (day) => {
            const cell = el('div', 'share');
            const count = day.prompt_tokens + day.completion_tokens;
            cell.append(meter(peak ? (count / peak) * 100 : 0), el('span', null, NUMBER_FORMAT.format(count)));
            return cell;
          },
        },
        { label: 'Requests', cell: (day) => tokens(day.requests) },
      ],
      data.daily,
      'No data.',
      null,
      { pager: usage.dailyPager, noun: 'day', redraw: drawUsage }
    )
  );
}

async function loadUsage() {
  usage.panel.report('Loading usage…', 'pending');
  try {
    const [data] = await Promise.all([
      apiFetch(USAGE_PATH + '?days=' + encodeURIComponent(usage.days)),
      loadAccountNames(),
    ]);
    usage.data = data;
    usage.panel.report('Token counts as reported by the providers.', 'ok');
    drawUsage();
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      usage.panel.report(err.message, 'error');
    }
  }
}

function renderUsage(view) {
  usage.panel = createStatus();
  usage.summary = el('div', 'summary');
  usage.table = el('div', 'table-wrap');
  usage.daily = el('div', 'table-wrap');
  usage.group = savedChoice('group.usage', 'model', USAGE_GROUPS.map((entry) => entry[0]));

  const range = pairSelect(USAGE_RANGES, usage.days);
  range.addEventListener('change', () => {
    usage.days = range.value;
    usage.pager.page = 1;
    usage.dailyPager.page = 1;
    loadUsage();
  });
  const group = pairSelect(USAGE_GROUPS, usage.group);
  group.addEventListener('change', () => {
    usage.group = group.value;
    usage.pager.page = 1;
    saveChoice('group.usage', usage.group);
    if (usage.data) {
      drawUsage();
    }
  });
  const filters = el('div', 'filters');
  filters.append(labelled('Range', range), labelled('Group', group));

  mountPage(view, {
    title: 'Usage',
    status: usage.panel,
    actions: [refreshButton(() => loadUsage())],
    tools: [filters],
    content: [usage.summary, usage.table, usage.daily],
  });
  loadUsage();
}

/* Sessions */

const sessions = { panel: null, table: null, picker: null, accountId: '', list: [], pager: newPager(), search: '' };

function accountSessionsPath(id) {
  return accountStatusPath(id, 'sessions');
}

function drawSessions(fullList = sessions.list) {
  sessions.list = fullList;
  const needle = sessions.search.trim().toLowerCase();
  const list = needle
    ? fullList.filter((row) => [row.id, row.last_model].some((field) => field && String(field).toLowerCase().includes(needle)))
    : fullList;
  sessions.table.replaceChildren(
    dataTable(
      [
        { label: 'Session', cell: (row) => idCell(shortId(row.id), row.id) },
        { label: 'Started', cell: (row) => formatDate(row.created_at) },
        { label: 'Last active', cell: (row) => formatDate(row.updated_at) },
        { label: 'Turns', cell: (row) => tokens(row.turn_count) },
        { label: 'Prompt tokens', cell: (row) => tokens(row.prompt_tokens) },
        { label: 'Completion tokens', cell: (row) => tokens(row.completion_tokens) },
        { label: 'Total tokens', cell: (row) => tokens(row.prompt_tokens + row.completion_tokens) },
        { label: 'Last model', cell: (row) => row.last_model },
      ],
      list,
      'This user has no live sessions. Sessions expire after a period of inactivity.',
      null,
      { pager: sessions.pager, noun: 'session', redraw: () => drawSessions() }
    )
  );
}

async function loadSessions() {
  if (!sessions.accountId) {
    sessions.table.replaceChildren(emptyState('Choose a user above to see their sessions.'));
    sessions.panel.report('', 'ok');
    return;
  }
  sessions.panel.report('Loading sessions…', 'pending');
  try {
    const body = await apiFetch(accountSessionsPath(sessions.accountId));
    sessions.panel.report(
      body.data.length + ' live session(s). Conversation content is not shown here.',
      'ok'
    );
    drawSessions(body.data);
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      sessions.panel.report(err.message, 'error');
    }
  }
}

function renderSessions(view) {
  sessions.panel = createStatus();
  sessions.table = el('div', 'table-wrap');
  sessions.picker = el('div', 'filters');

  mountPage(view, {
    title: 'Sessions',
    status: sessions.panel,
    actions: [refreshButton(() => loadSessions())],
    tools: [sessions.picker],
    content: [sessions.table],
  });

  sessions.panel.run(
    async () => loadAccountNames(),
    (accounts) => {
      if (!accounts.some((account) => account.id === sessions.accountId)) {
        sessions.accountId = '';
      }
      const select = pairSelect(
        [['', 'Choose a user…'], ...accounts.map((account) => [account.id, accountLabel(account.id)])],
        sessions.accountId
      );
      select.addEventListener('change', () => {
        sessions.accountId = select.value;
        sessions.pager.page = 1;
        loadSessions();
      });
      const search = el('input');
      search.type = 'search';
      search.placeholder = 'session id or model…';
      search.value = sessions.search;
      search.addEventListener('input', () => {
        sessions.search = search.value;
        sessions.pager.page = 1;
        drawSessions();
      });
      sessions.picker.replaceChildren(labelled('User', select), labelled('Search', search));
      return loadSessions();
    }
  );
}

/* Requests */

const REQUEST_LIMITS = [
  ['50', 'Latest 50'],
  ['100', 'Latest 100'],
  ['500', 'Latest 500'],
];

const requests = {
  panel: null,
  table: null,
  picker: null,
  accountId: '',
  limit: '100',
  group: '',
  list: [],
  pager: newPager(),
  search: '',
  outcome: '',
  from: '',
  to: '',
};

const REQUEST_GROUPS = [
  ['', 'No grouping'],
  ['account', 'By user'],
  ['provider', 'By provider'],
  ['model', 'By model'],
  ['outcome', 'By outcome'],
];

function providerName(provider) {
  return provider ? PROVIDER_NAMES[provider] || provider : 'Unknown';
}

function requestGrouping(mode) {
  const summarize = (members) => {
    const tokensTotal = members.reduce(
      (sum, row) => sum + (row.prompt_tokens || 0) + (row.completion_tokens || 0),
      0
    );
    const latency = Math.round(members.reduce((sum, row) => sum + row.latency_ms, 0) / members.length);
    return (
      plural(members.length, 'request') +
      ' \u00b7 ' +
      plural(tokensTotal, 'token') +
      ' \u00b7 avg ' +
      NUMBER_FORMAT.format(latency) +
      ' ms'
    );
  };
  if (mode === 'account') {
    return { key: (row) => row.account_id, label: (id) => accountLabel(id), summary: summarize };
  }
  if (mode === 'provider') {
    return { key: (row) => row.provider || '', label: providerName, summary: summarize };
  }
  if (mode === 'model') {
    return { key: (row) => row.model, label: (model) => model, summary: summarize };
  }
  if (mode === 'outcome') {
    return { key: (row) => row.outcome, label: (outcome) => outcome, summary: summarize };
  }
  return null;
}

function outcomeBadge(outcome) {
  const ok = outcome === 'success' || outcome === 'success_persist_failed';
  return el('span', 'badge badge--' + (ok ? 'active' : 'suspended'), outcome);
}

/* "429 transient" per failed provider try: why a failover happened. The status is
   the provider's HTTP code; the class is how the gateway treated it (transient =
   short cooldown, exhausted = out until reset, rejected = key refused). */
function failureSummary(failures) {
  if (!failures || !failures.length) {
    return null;
  }
  return failures
    .map((failure) => (failure.status_code || 'no response') + ' ' + (failure.classification || ''))
    .join(', ');
}

/* Client-side filters over the loaded list: text, outcome class and a time window. */
function filteredRequests(list) {
  const needle = requests.search.trim().toLowerCase();
  const from = requests.from ? new Date(requests.from).getTime() : null;
  const to = requests.to ? new Date(requests.to).getTime() : null;
  return list.filter((row) => {
    const ok = row.outcome === 'success' || row.outcome === 'success_persist_failed';
    if (requests.outcome === 'ok' && !ok) {
      return false;
    }
    if (requests.outcome === 'failed' && ok) {
      return false;
    }
    const at = new Date(row.timestamp).getTime();
    if ((from !== null && at < from) || (to !== null && at > to)) {
      return false;
    }
    if (!needle) {
      return true;
    }
    return [row.request_id, row.session_id, row.model, row.provider, row.outcome, accountLabel(row.account_id)]
      .filter(Boolean)
      .some((field) => String(field).toLowerCase().includes(needle));
  });
}

function showRequestDetail(row) {
  const fields = [
    ['Request id', row.request_id],
    ['Time', formatDate(row.timestamp)],
    ['User', accountLabel(row.account_id)],
    ['Session', row.session_id],
    ['Provider', row.provider ? providerName(row.provider) : null],
    ['Model', row.model],
    ['Outcome', row.outcome],
    ['HTTP status', row.status_code],
    ['Latency', NUMBER_FORMAT.format(row.latency_ms) + ' ms'],
    ['First token', row.ttft_ms === null || row.ttft_ms === undefined ? null : NUMBER_FORMAT.format(row.ttft_ms) + ' ms'],
    ['Streamed', row.stream ? 'yes' : 'no'],
    ['Provider tries', row.attempts],
    ['Failed tries', failureSummary(row.failures)],
    ['Prompt tokens', tokens(row.prompt_tokens)],
    ['Completion tokens', tokens(row.completion_tokens)],
  ];
  openModal({
    title: 'Request details',
    cancelLabel: 'Close',
    content: dataTable(
      [
        { label: 'Field', cell: (field) => field[0] },
        { label: 'Value', cell: (field) => (field[1] === null || field[1] === undefined ? null : String(field[1])) },
      ],
      fields,
      ''
    ),
  });
}

function drawRequests(fullList = requests.list) {
  const list = filteredRequests(fullList);
  requests.table.replaceChildren(
    dataTable(
      [
        { label: 'Time', cell: (row) => formatDate(row.timestamp) },
        { label: 'User', cell: (row) => idCell(accountLabel(row.account_id), row.account_id) },
        { label: 'Session', cell: (row) => (row.session_id ? idCell(shortId(row.session_id), row.session_id) : null) },
        { label: 'Provider', cell: (row) => (row.provider ? providerName(row.provider) : null) },
        { label: 'Model', cell: (row) => row.model },
        { label: 'Outcome', cell: (row) => outcomeBadge(row.outcome) },
        { label: 'Status', cell: (row) => String(row.status_code) },
        { label: 'Latency', cell: (row) => NUMBER_FORMAT.format(row.latency_ms) + ' ms' },
        { label: 'Prompt tokens', cell: (row) => tokens(row.prompt_tokens) },
        { label: 'Completion tokens', cell: (row) => tokens(row.completion_tokens) },
        { label: 'Tries', cell: (row) => tokens(row.attempts) },
        { label: 'Failed tries', cell: (row) => failureSummary(row.failures) },
        { label: 'Stream', cell: (row) => (row.stream ? 'yes' : 'no') },
        { label: '', cell: (row) => button('Details', 'ghost btn-sm', () => showRequestDetail(row)) },
      ],
      list,
      fullList.length ? 'No request matches these filters.' : 'No requests recorded yet.',
      requestGrouping(requests.group),
      { pager: requests.pager, noun: 'request', redraw: () => drawRequests() }
    )
  );
}

async function loadRequests() {
  requests.panel.report('Loading requests…', 'pending');
  try {
    const query =
      '?limit=' +
      encodeURIComponent(requests.limit) +
      (requests.accountId ? '&account_id=' + encodeURIComponent(requests.accountId) : '');
    const [body] = await Promise.all([apiFetch(REQUESTS_PATH + query), loadAccountNames()]);
    requests.panel.report(
      body.data.length + ' request(s). Metadata only; the list is capped and older entries drop off.',
      'ok'
    );
    requests.list = body.data;
    drawRequests(body.data);
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      requests.panel.report(err.message, 'error');
    }
  }
}

function renderRequests(view) {
  requests.panel = createStatus();
  requests.table = el('div', 'table-wrap');
  requests.picker = el('div', 'filters');
  requests.group = savedChoice('group.requests', '', REQUEST_GROUPS.map((entry) => entry[0]));

  const limit = pairSelect(REQUEST_LIMITS, requests.limit);
  limit.addEventListener('change', () => {
    requests.limit = limit.value;
    requests.pager.page = 1;
    loadRequests();
  });
  mountPage(view, {
    title: 'Requests',
    status: requests.panel,
    actions: [
      button('Export CSV', 'ghost', () =>
        requests.panel.run(
          () =>
            downloadFile(
              '/operator/v1/export/requests.csv' +
                '?limit=' +
                encodeURIComponent(requests.limit) +
                (requests.accountId ? '&account_id=' + encodeURIComponent(requests.accountId) : ''),
              'requests.csv'
            ),
          () => requests.panel.report('requests.csv downloaded.', 'ok')
        )
      ),
      refreshButton(() => loadRequests()),
    ],
    tools: [requests.picker],
    content: [requests.table],
  });

  requests.panel.run(
    async () => loadAccountNames(),
    (accounts) => {
      if (!accounts.some((account) => account.id === requests.accountId)) {
        requests.accountId = '';
      }
      const select = pairSelect(
        [['', 'All users'], ...accounts.map((account) => [account.id, accountLabel(account.id)])],
        requests.accountId
      );
      select.addEventListener('change', () => {
        requests.accountId = select.value;
        requests.pager.page = 1;
        loadRequests();
      });
      const groupBy = pairSelect(REQUEST_GROUPS, requests.group);
      groupBy.addEventListener('change', () => {
        requests.group = groupBy.value;
        requests.pager.page = 1;
        saveChoice('group.requests', requests.group);
        drawRequests(requests.list);
      });
      const refilter = () => {
        requests.pager.page = 1;
        drawRequests(requests.list);
      };
      const search = el('input');
      search.type = 'search';
      search.placeholder = 'id, model, user…';
      search.value = requests.search;
      search.addEventListener('input', () => {
        requests.search = search.value;
        refilter();
      });
      const outcome = pairSelect(
        [['', 'All outcomes'], ['ok', 'Succeeded'], ['failed', 'Failed']],
        requests.outcome
      );
      outcome.addEventListener('change', () => {
        requests.outcome = outcome.value;
        refilter();
      });
      const windowInput = (name, label) => {
        const input = el('input');
        input.type = 'datetime-local';
        input.value = requests[name];
        input.addEventListener('change', () => {
          requests[name] = input.value;
          refilter();
        });
        return labelled(label, input);
      };
      const dateRange = el('div', 'field date-range');
      dateRange.append(
        el('span', null, 'Date range'),
        (() => {
          const wrap = el('div', 'date-range-inputs');
          const fromInput = el('input');
          fromInput.type = 'datetime-local';
          fromInput.value = requests.from;
          fromInput.addEventListener('change', () => {
            requests.from = fromInput.value;
            refilter();
          });
          const toInput = el('input');
          toInput.type = 'datetime-local';
          toInput.value = requests.to;
          toInput.addEventListener('change', () => {
            requests.to = toInput.value;
            refilter();
          });
          wrap.append(labelled('From', fromInput), labelled('To', toInput));
          return wrap;
        })()
      );
      requests.picker.replaceChildren(
        labelled('User', select),
        labelled('Show', limit),
        labelled('Group by', groupBy),
        labelled('Search', search),
        labelled('Outcome', outcome)
      );
      const dateRangeRow = el('div', 'filters date-range-row');
      dateRangeRow.append(dateRange);
      requests.picker.parentElement.insertBefore(dateRangeRow, requests.picker.nextSibling);
      return loadRequests();
    }
  );
}

/* -- Configuration ----------------------------------------------------------------
 *
 * Every field is editable and every field is tunable: the API returns the
 * operator-tunable subset only, so credentials and infrastructure never reach
 * this view to be shown, and there is nothing read-only to render alongside.
 */

const configuration = {
  values: {},
  panel: null,
  form: null,
  save: null,
};

function configControl(field) {
  if (field.type === 'checkbox') {
    const box = el('input');
    box.type = 'checkbox';
    box.name = field.name;
    box.id = 'config-' + field.name;
    return box;
  }

  const input = el('input');
  input.type = field.type;
  input.name = field.name;
  input.id = 'config-' + field.name;
  if (field.min !== undefined) {
    input.min = String(field.min);
  }
  if (field.max !== undefined) {
    input.max = String(field.max);
  }
  if (field.step !== undefined) {
    input.step = String(field.step);
  }
  return input;
}

/* Only fields that differ from the last server state are sent: an untouched
   field would otherwise become a persisted override pinning today's value, and
   would overwrite a change another operator made since this form loaded. */
function readConfigForm(form, baseline = {}) {
  const patch = {};
  for (const field of CONFIG_FIELDS) {
    const control = form.elements.namedItem(field.name);
    let value;
    if (field.type === 'checkbox') {
      value = control.checked;
    } else if (control.value.trim() !== '') {
      value = field.type === 'number' ? Number(control.value) : control.value.trim();
    } else if (field.name === 'user_daily_cap') {
      /* Blank is the documented way to clear an optional cap; the other
         numerics are required and the server refuses an empty one. */
      value = null;
    } else {
      continue;
    }
    const before = baseline[field.name] === undefined ? null : baseline[field.name];
    if (value !== before) {
      patch[field.name] = value;
    }
  }
  return patch;
}

function fillConfigForm(form, values) {
  for (const field of CONFIG_FIELDS) {
    const control = form.elements[field.name];
    const value = values[field.name];
    if (field.type === 'checkbox') {
      control.checked = value === true;
    } else {
      control.value = value === null || value === undefined ? '' : String(value);
    }
  }
}

/* Save is enabled only while the form differs from what the server holds. */
function syncConfigSave() {
  const loaded = Object.keys(configuration.values || {}).length > 0;
  const dirty = loaded && Object.keys(readConfigForm(configuration.form, configuration.values)).length > 0;
  configuration.save.disabled = !dirty;
  configuration.save.textContent = dirty ? 'Save changes' : 'No changes';
}

function buildConfigForm(save) {
  const form = el('form', 'config-form');
  form.id = 'config-form';
  form.noValidate = true;

  const grid = el('div', 'form-grid');
  for (const field of CONFIG_FIELDS) {
    const wrapper = labelled(field.label, configControl(field));
    wrapper.append(el('span', 'field-hint', field.hint));
    grid.append(wrapper);
  }

  form.append(grid);
  form.addEventListener('input', syncConfigSave);
  form.addEventListener('change', syncConfigSave);

  const clearInvalid = () => {
    for (const control of form.querySelectorAll('.is-invalid')) {
      control.classList.remove('is-invalid');
    }
  };

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    clearInvalid();
    if (!Object.keys(configuration.values || {}).length) {
      /* Nothing loaded: a blank form would read as "clear the cap, stop logging". */
      configuration.panel.report('Configuration has not loaded. Refresh first.', 'error');
      return;
    }
    const changes = readConfigForm(form, configuration.values);
    if (!Object.keys(changes).length) {
      configuration.panel.report('No changes to save.', 'ok');
      return;
    }
    save.disabled = true;
    configuration.panel.run(
      () => apiFetch(CONFIG_PATH, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(changes),
      }),
      (updated) => {
        /* Re-render from the server's response, not from what was typed: it is
           the authoritative post-validation state, so a clamped or coerced
           value can never look saved when it was not. */
        configuration.values = updated;
        fillConfigForm(form, updated);
        configuration.panel.report('Configuration saved. Effective on the next request.', 'ok');
      },
      (err) => {
        const control = err.param ? form.elements.namedItem(err.param) : null;
        if (control && control.classList) {
          control.classList.add('is-invalid');
          control.focus();
        }
      }
    ).finally(syncConfigSave);
  });

  return form;
}

async function loadConfiguration() {
  configuration.panel.report('Loading configuration…', 'pending');
  try {
    configuration.values = await apiFetch(CONFIG_PATH);
    fillConfigForm(configuration.form, configuration.values);
    configuration.panel.report('Changes apply from the next request.', 'ok');
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      configuration.panel.report(err.message, 'error');
    }
  }
  syncConfigSave();
}

function renderConfiguration(view) {
  configuration.values = {};
  configuration.panel = createStatus();
  configuration.save = el('button', 'primary', 'No changes');
  configuration.save.type = 'submit';
  configuration.save.setAttribute('form', 'config-form');
  configuration.save.disabled = true;
  configuration.form = buildConfigForm(configuration.save);

  mountPage(view, {
    title: 'Configuration',
    description: 'Runtime settings. Credentials and infrastructure are not shown here.',
    status: configuration.panel,
    actions: [configuration.save, refreshButton(() => loadConfiguration(), 'Reload')],
    content: [configuration.form],
  });
  loadConfiguration();
}

/* A reload keeps the key but not its validity — re-check it before showing an
   authenticated shell, so a revoked key signs out instead of dead-ending. */
async function boot() {
  const key = storedKey();
  if (!key) {
    showLogin();
    return;
  }

  let result;
  try {
    result = await authenticate(key);
  } catch (err) {
    /* Unreachable is not revoked: keep the key so a retry after the gateway
       is back does not demand a fresh sign-in. */
    showLogin('The gateway is unreachable. Reload to try again.');
    return;
  }
  if (!result.ok) {
    forgetKey();
    showLogin(result.message);
    return;
  }
  showApp();
}

boot();
