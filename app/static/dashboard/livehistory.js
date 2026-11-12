/* Live Flow, history: the calls, the steps and the keys by model.
 *
 * Loaded after live.js and before app.js. Three tabs under the diagram:
 *
 * - Calls: one row per request, newest first, with filters, pages and a detail
 *   dialog. Loaded from the gateway's call history (kept whether or not this page
 *   is open) and kept up to date by the live events.
 * - Steps: the individual events seen while this page is open, with filters and pages.
 * - Keys by model: for every model, each key that can serve it, how it was used and
 *   how it is doing.
 *
 * Metadata only. Everything rendered goes through textContent, never markup.
 */

const CALLS_PATH = '/operator/v1/calls';
/* Calls loaded on opening (the gateway keeps at most `usage_recent_max`). */
const LIVE_HISTORY_LOAD = 1000;
/* Steps kept in memory while the page is open. */
const LIVE_FEED_KEEP = 500;
const LIVE_KEYS_REFRESH_MS = 30000;

const liveHist = {
  tab: 'calls',
  bar: null,
  tools: null,
  note: null,
  body: null,
  tabButtons: {},
  drawnAt: 0,
  calls: { pager: { page: 1, size: 25 }, search: '', status: '', client: '', provider: '' },
  steps: { pager: { page: 1, size: 25 }, search: '', step: '' },
  keys: { pager: { page: 1, size: 25 }, search: '', provider: '', used: false },
};

const LIVE_TABS = [
  ['calls', 'Calls', 'Every request, newest first. The gateway keeps this history whether or not this page is open.'],
  ['steps', 'Steps', 'The individual steps of the requests, as seen while this page is open.'],
  ['keys', 'Keys by model', 'For every model, each key that can serve it: how many calls it took, how they went, and the key’s state now.'],
];

/* -- small pieces ----------------------------------------------------------- */

function liveRunning(call) {
  return !call.done;
}

function liveCallOk(call) {
  return call.done && liveOk(call.status_code);
}

function liveAttemptLabel(attempt) {
  return providerName(attempt.provider) + ' · ' + liveKey(attempt.key_id) + ' · ' + (attempt.model || 'model?');
}

/* One line per provider try: whether it worked, which key and model, how long. */
function liveTries(call) {
  const box = el('div', 'lf-tries');
  if (!call.attempts.length) {
    box.append(el('span', 'note', call.done ? 'no provider reached' : 'waiting at the gateway'));
    return box;
  }
  for (const attempt of call.attempts) {
    const line = el('div', 'lf-try');
    const mark = attempt.ok === null ? '…' : attempt.ok ? '✓' : '✗';
    const tone = attempt.ok === null ? 'wait' : attempt.ok ? 'ok' : 'bad';
    line.append(
      el('span', 'lf-try-mark lf-try-mark--' + tone, mark),
      el('span', 'lf-try-text', liveAttemptLabel(attempt)),
      el('span', 'lf-try-note', (attempt.ok === false ? (attempt.status_code || 'no answer') + ' · ' : '') + (attempt.ms === null || attempt.ms === undefined ? '' : fmtMs(attempt.ms)))
    );
    box.append(line);
  }
  return box;
}

function liveAskedCell(call) {
  const box = el('div', 'lf-asked');
  box.append(el('span', null, call.requested || '—'));
  if (call.routed_by) {
    box.append(el('span', 'badge badge--suspended', call.routed_by));
  }
  if (call.hops.length) {
    box.append(el('span', 'badge badge--cooldown', plural(call.hops.length, 'move')));
  }
  return box;
}

/* Result, time and tokens in one narrow cell. */
function liveResultCell(call) {
  const box = el('div', 'lf-result');
  if (!call.done) {
    box.append(el('span', 'badge badge--cooldown', 'running'), el('span', 'lf-result-note', fmtMs(Date.now() - call.t0) + ' so far'));
    return box;
  }
  const good = liveOk(call.status_code);
  box.append(
    el('span', 'badge badge--' + (good ? 'active' : 'exhausted'), String(call.status_code) + (good ? '' : ' ' + (call.outcome || ''))),
    el('span', 'lf-result-note', fmtMs(call.latency_ms) + (call.ttft_ms ? ' · first ' + fmtMs(call.ttft_ms) : ''))
  );
  const tokens = liveTokensCell(call);
  if (tokens) {
    box.append(el('span', 'lf-result-note', tokens + ' tokens'));
  }
  return box;
}

function liveTokensCell(call) {
  if (call.prompt_tokens === null || call.prompt_tokens === undefined) {
    return null;
  }
  return NUMBER_FORMAT.format(call.prompt_tokens) + ' / ' + NUMBER_FORMAT.format(call.completion_tokens || 0);
}

/* -- the call dialog -------------------------------------------------------- */

/* The steps of a call in order, rebuilt from its list of tries: when each began
   and ended counts the time the tries took one after the other. */
function liveCallSteps(call) {
  const first = call.hops.length ? call.hops[0].from : call.attempts.length ? call.attempts[0].model : call.model;
  const steps = [{ at: 0, ev: { kind: 'request', requested: call.requested, model: first, routed_by: call.routed_by, stream: call.stream, tools: call.tools } }];
  let cursor = 0;
  call.attempts.forEach((attempt, index) => {
    steps.push({ at: cursor, ev: { kind: 'attempt', n: index + 1, key_id: attempt.key_id, provider: attempt.provider, model: attempt.model } });
    cursor += attempt.ms || 0;
    if (attempt.ok !== null) {
      steps.push({
        at: cursor,
        ev: { kind: 'result', ok: attempt.ok, key_id: attempt.key_id, provider: attempt.provider, status_code: attempt.status_code, cause: attempt.classification, ms: attempt.ms || 0 },
      });
    }
    const next = call.attempts[index + 1];
    const hop = next ? call.hops.find((entry) => entry.from === attempt.model && entry.to === next.model) : null;
    if (hop) {
      steps.push({ at: cursor, ev: { kind: 'hop', from: hop.from, to: hop.to, cause: hop.cause } });
    }
  });
  if (call.done) {
    steps.push({ at: call.latency_ms || cursor, ev: { kind: 'done', status_code: call.status_code, outcome: call.outcome, latency_ms: call.latency_ms, ttft_ms: call.ttft_ms, prompt_tokens: call.prompt_tokens, completion_tokens: call.completion_tokens } });
  }
  return steps;
}

function liveShowCall(rid) {
  const call = livePage.calls.get(rid);
  if (!call) {
    return;
  }
  const total = Math.max(1, call.latency_ms || Date.now() - call.t0);
  const fields = [
    ['Request id', call.rid],
    ['Started', formatDate(call.t0)],
    ['Client', liveClient(call.account_id)],
    ['Asked for', call.requested],
    ['Routing', call.routed_by ? call.routed_by + ' → ' + call.model : 'the model asked for'],
    ['Result', call.done ? call.status_code + ' ' + (call.outcome || '') : 'still running'],
    ['Total time', call.done ? fmtMs(call.latency_ms) : fmtMs(Date.now() - call.t0) + ' so far'],
    ['First chunk', call.ttft_ms ? fmtMs(call.ttft_ms) : null],
    ['Tokens in / out', liveTokensCell(call)],
    ['Streamed', call.stream ? 'yes' : 'no'],
    ['With tools', call.tools ? 'yes' : 'no'],
  ];
  const summary = dataTable(
    [
      { label: 'Field', cell: (field) => field[0] },
      { label: 'Value', cell: (field) => field[1] },
    ],
    fields,
    ''
  );
  const rows = liveCallSteps(call).map((step) => {
    const [label, tone, text] = liveDescribe(step.ev, call.account_id);
    return { at: step.at, label, tone, text };
  });
  const steps = dataTable(
    [
      { label: 'After', cell: (row) => '+' + fmtMs(row.at) },
      { label: 'Step', cell: (row) => el('span', 'badge badge--' + row.tone, row.label) },
      { label: 'What happened', cell: (row) => el('span', 'lf-wrap', row.text) },
      { label: 'When in the request', cell: (row) => meter(Math.min(100, (row.at / total) * 100)) },
    ],
    rows,
    ''
  );
  const body = el('div', 'stack');
  body.append(summary, el('h3', 'lf-modal-title', 'Steps (times add up the tries one after the other)'), steps, el('h3', 'lf-modal-title', 'Keys and models tried'), liveTries(call));
  openModal({ title: 'Call details', wide: true, cancelLabel: 'Close', content: body });
}

function liveCallButton(rid, label = 'Details') {
  return button(label, 'ghost btn-sm', () => liveShowCall(rid));
}

/* -- the Calls tab ---------------------------------------------------------- */

function liveCallText(call) {
  const parts = [call.rid, call.requested, call.model, liveClient(call.account_id), call.status_code, call.outcome];
  for (const attempt of call.attempts) {
    parts.push(attempt.provider, liveKey(attempt.key_id), attempt.model);
  }
  return parts.filter((part) => part !== null && part !== undefined).join(' ').toLowerCase();
}

function liveFilteredCalls() {
  const f = liveHist.calls;
  const needle = f.search.trim().toLowerCase();
  return [...livePage.calls.values()]
    .filter((call) => {
      if (f.client && call.account_id !== f.client) {
        return false;
      }
      if (f.provider && !call.attempts.some((attempt) => attempt.provider === f.provider)) {
        return false;
      }
      if (f.status === 'ok' && !liveCallOk(call)) {
        return false;
      }
      if (f.status === 'failed' && !(call.done && !liveOk(call.status_code))) {
        return false;
      }
      if (f.status === 'running' && call.done) {
        return false;
      }
      if (f.status === 'retried' && !(call.attempts.length > 1 || call.hops.length)) {
        return false;
      }
      return !needle || liveCallText(call).includes(needle);
    })
    .sort((a, b) => b.t0 - a.t0);
}

function liveDrawCalls() {
  const all = livePage.calls.size;
  const list = liveFilteredCalls();
  liveHist.body.replaceChildren(
    el('p', 'note', list.length === all ? NUMBER_FORMAT.format(all) + ' call(s).' : NUMBER_FORMAT.format(list.length) + ' of ' + NUMBER_FORMAT.format(all) + ' call(s) match the filters.'),
    dataTable(
      [
        { label: 'Time', cell: (call) => liveWhen(call.t0) },
        { label: 'Client', cell: (call) => idCell(liveClient(call.account_id), call.account_id || '') },
        { label: 'Asked for', cell: liveAskedCell },
        { label: 'Served by (provider · key · model)', cell: liveTries },
        { label: 'Result · time · tokens in / out', cell: liveResultCell },
        { label: '', cell: (call) => liveCallButton(call.rid) },
      ],
      list,
      all ? 'No call matches these filters.' : 'No call yet. Send a request through the gateway.',
      null,
      { pager: liveHist.calls.pager, noun: 'call', redraw: liveDrawHistory }
    )
  );
}

function liveCallTools() {
  const f = liveHist.calls;
  const calls = [...livePage.calls.values()];
  const clients = [...new Set(calls.map((call) => call.account_id).filter(Boolean))];
  const providers = [...new Set(calls.flatMap((call) => call.attempts.map((attempt) => attempt.provider)))].sort();
  const search = el('input');
  search.type = 'search';
  search.placeholder = 'request id, model, key, client…';
  search.value = f.search;
  search.addEventListener('input', () => {
    f.search = search.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const status = pairSelect(
    [['', 'All results'], ['ok', 'Answered'], ['failed', 'Failed'], ['running', 'Running now'], ['retried', 'Retried or moved on']],
    f.status
  );
  status.addEventListener('change', () => {
    f.status = status.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const client = pairSelect([['', 'All clients'], ...clients.map((id) => [id, liveClient(id)])], f.client);
  client.addEventListener('change', () => {
    f.client = client.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const provider = pairSelect([['', 'All providers'], ...providers.map((id) => [id, providerName(id)])], f.provider);
  provider.addEventListener('change', () => {
    f.provider = provider.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  return [labelled('Search', search), labelled('Result', status), labelled('Client', client), labelled('Provider', provider)];
}

/* -- the Steps tab ---------------------------------------------------------- */

const LIVE_STEP_LABELS = ['1 request in', '2 sent out', '3 answer', '3 refused', 'moves on', '4 response', '4 failed'];

function liveDrawSteps() {
  const f = liveHist.steps;
  const needle = f.search.trim().toLowerCase();
  const list = livePage.feed.filter((row) => (!f.step || row.step === f.step) && (!needle || (row.ev.rid + ' ' + row.text).toLowerCase().includes(needle)));
  liveHist.body.replaceChildren(
    el('p', 'note', list.length === livePage.feed.length ? NUMBER_FORMAT.format(list.length) + ' step(s), the last ' + LIVE_FEED_KEEP + ' kept.' : NUMBER_FORMAT.format(list.length) + ' of ' + NUMBER_FORMAT.format(livePage.feed.length) + ' step(s) match the filters.'),
    dataTable(
      [
        { label: 'Time', cell: (row) => liveClock(row.ev.t) },
        {
          label: 'Request',
          cell: (row) => {
            const link = el('button', 'link-button', row.ev.rid.slice(-6));
            link.type = 'button';
            link.title = 'Open this call';
            link.addEventListener('click', () => liveShowCall(row.ev.rid));
            return link;
          },
        },
        { label: 'Step', cell: (row) => el('span', 'badge badge--' + row.tone, row.step) },
        { label: 'What happened', cell: (row) => el('span', 'lf-wrap', row.text) },
      ],
      list,
      livePage.feed.length ? 'No step matches these filters.' : 'No step yet. Steps appear as requests go through while this page is open.',
      null,
      { pager: f.pager, noun: 'step', redraw: liveDrawHistory }
    )
  );
}

function liveStepTools() {
  const f = liveHist.steps;
  const search = el('input');
  search.type = 'search';
  search.placeholder = 'request id, model, key, words…';
  search.value = f.search;
  search.addEventListener('input', () => {
    f.search = search.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const step = pairSelect([['', 'All steps'], ...LIVE_STEP_LABELS.map((label) => [label, label])], f.step);
  step.addEventListener('change', () => {
    f.step = step.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  return [labelled('Search', search), labelled('Step', step)];
}

/* -- the Keys by model tab -------------------------------------------------- */

/* One row per model and key: from the keys that can serve the model and from
   every try seen in the calls. */
function liveTargets() {
  const rows = new Map();
  const row = (model, keyId) => {
    const id = model + '|' + keyId;
    let entry = rows.get(id);
    if (!entry) {
      const record = livePage.keyRecords.find((key) => key.id === keyId);
      entry = { model, key_id: keyId, provider: record ? record.provider : '', state: record ? record.state : 'removed', serves: false, calls: 0, ok: 0, fail: 0, ms: 0, msN: 0, active: 0, last: 0, lastOk: null };
      rows.set(id, entry);
    }
    return entry;
  };
  for (const record of livePage.keyRecords) {
    for (const model of record.models || []) {
      row(model, record.id).serves = true;
    }
  }
  for (const call of livePage.calls.values()) {
    for (const attempt of call.attempts) {
      if (!attempt.model) {
        continue;
      }
      const entry = row(attempt.model, attempt.key_id);
      if (!entry.provider) {
        entry.provider = attempt.provider;
      }
      entry.calls += 1;
      if (attempt.ok === true) {
        entry.ok += 1;
        if (attempt.ms !== null && attempt.ms !== undefined) {
          entry.ms += attempt.ms;
          entry.msN += 1;
        }
      } else if (attempt.ok === false) {
        entry.fail += 1;
      } else {
        entry.active += 1;
      }
      if (call.t0 >= entry.last) {
        entry.last = call.t0;
        entry.lastOk = attempt.ok;
      }
    }
  }
  return [...rows.values()];
}

function liveKeyGrouping() {
  return {
    key: (entry) => entry.model,
    label: (model) => model,
    summary: (members) => {
      const calls = members.reduce((sum, entry) => sum + entry.calls, 0);
      const ok = members.reduce((sum, entry) => sum + entry.ok, 0);
      return plural(members.length, 'key') + ' · ' + plural(calls, 'call') + (calls ? ' · ' + fmtPct(ok / calls, 0) + ' answered' : '');
    },
  };
}

function liveDrawKeys() {
  const f = liveHist.keys;
  const needle = f.search.trim().toLowerCase();
  const all = liveTargets();
  const list = all
    .filter((entry) => (!f.provider || entry.provider === f.provider) && (!f.used || entry.calls > 0) && (!needle || (entry.model + ' ' + liveKey(entry.key_id) + ' ' + entry.provider).toLowerCase().includes(needle)))
    .sort((a, b) => a.model.localeCompare(b.model) || b.calls - a.calls || liveKey(a.key_id).localeCompare(liveKey(b.key_id)));
  liveHist.body.replaceChildren(
    el('p', 'note', NUMBER_FORMAT.format(list.length) + ' key and model pair(s)' + (list.length === all.length ? '' : ' of ' + NUMBER_FORMAT.format(all.length)) + '. Counts come from the calls above; a pair with no call is a key that can serve the model but has not been asked.'),
    dataTable(
      [
        { label: 'Key', cell: (entry) => idCell(liveKey(entry.key_id) + (entry.provider ? ' · ' + providerName(entry.provider) : ''), entry.key_id) },
        { label: 'Key state', cell: (entry) => el('span', 'badge badge--' + (STATE_LABELS[entry.state] ? entry.state : 'suspended'), STATE_LABELS[entry.state] || entry.state) },
        { label: 'Calls', cell: (entry) => String(entry.calls) },
        { label: 'Answered', cell: (entry) => String(entry.ok) },
        { label: 'Failed', cell: (entry) => String(entry.fail) },
        {
          label: 'Success',
          cell: (entry) => {
            const done = entry.ok + entry.fail;
            const cell = el('div', 'share');
            cell.append(el('span', null, done ? fmtPct(entry.ok / done, 0) : '—'), meter(done ? (entry.ok / done) * 100 : 0));
            return cell;
          },
        },
        { label: 'Average answer', cell: (entry) => (entry.msN ? fmtMs(entry.ms / entry.msN) : null) },
        { label: 'In flight', cell: (entry) => (entry.active ? String(entry.active) : '—') },
        { label: 'Last used', cell: (entry) => (entry.last ? liveWhen(entry.last) : '—') },
        { label: 'Last result', cell: (entry) => (entry.lastOk === null ? null : el('span', 'badge badge--' + (entry.lastOk ? 'active' : 'exhausted'), entry.lastOk ? 'answered' : 'failed')) },
      ],
      list,
      all.length ? 'No key matches these filters.' : 'No key serves any model yet.',
      liveKeyGrouping(),
      { pager: f.pager, noun: 'key and model pair', redraw: liveDrawHistory }
    )
  );
}

function liveKeyTools() {
  const f = liveHist.keys;
  const providers = [...new Set(livePage.keyRecords.map((key) => key.provider))].sort();
  const search = el('input');
  search.type = 'search';
  search.placeholder = 'model or key…';
  search.value = f.search;
  search.addEventListener('input', () => {
    f.search = search.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const provider = pairSelect([['', 'All providers'], ...providers.map((id) => [id, providerName(id)])], f.provider);
  provider.addEventListener('change', () => {
    f.provider = provider.value;
    f.pager.page = 1;
    liveDrawHistory();
  });
  const used = el('input');
  used.type = 'checkbox';
  used.checked = f.used;
  used.addEventListener('change', () => {
    f.used = used.checked;
    f.pager.page = 1;
    liveDrawHistory();
  });
  return [labelled('Search', search), labelled('Provider', provider), labelled('Only keys that were used', used)];
}

/* -- the panel -------------------------------------------------------------- */

function liveDrawHistory() {
  if (!liveHist.body) {
    return;
  }
  liveHist.drawnAt = Date.now();
  if (liveHist.tab === 'steps') {
    liveDrawSteps();
  } else if (liveHist.tab === 'keys') {
    liveDrawKeys();
  } else {
    liveDrawCalls();
  }
  const counts = { calls: livePage.calls.size, steps: livePage.feed.length };
  for (const [id, label] of LIVE_TABS.map((tab) => [tab[0], tab[1]])) {
    liveHist.tabButtons[id].textContent = label + (id in counts ? ' (' + NUMBER_FORMAT.format(counts[id]) + ')' : '');
  }
}

function liveSelectTab(id) {
  liveHist.tab = id;
  saveChoice('live.tab', id);
  for (const [tabId, button_] of Object.entries(liveHist.tabButtons)) {
    button_.classList.toggle('is-active', tabId === id);
    button_.setAttribute('aria-selected', String(tabId === id));
  }
  liveHist.note.textContent = LIVE_TABS.find((tab) => tab[0] === id)[2];
  liveHist.tools.replaceChildren(...(id === 'steps' ? liveStepTools() : id === 'keys' ? liveKeyTools() : liveCallTools()));
  liveDrawHistory();
}

function liveHistoryPanel() {
  liveHist.tab = savedChoice('live.tab', 'calls', LIVE_TABS.map((tab) => tab[0]));
  liveHist.bar = el('div', 'lf-tabs');
  liveHist.bar.setAttribute('role', 'tablist');
  liveHist.tabButtons = {};
  for (const [id, label] of LIVE_TABS) {
    const tab = el('button', 'lf-tab', label);
    tab.type = 'button';
    tab.setAttribute('role', 'tab');
    tab.addEventListener('click', () => liveSelectTab(id));
    liveHist.tabButtons[id] = tab;
    liveHist.bar.append(tab);
  }
  liveHist.note = el('p', 'note');
  liveHist.tools = el('div', 'filters lf-tools');
  liveHist.body = el('div', 'lf-history-body');
  for (const part of [liveHist.calls, liveHist.steps, liveHist.keys]) {
    part.pager.page = 1;
  }
  const panel = el('section', 'panel');
  panel.append(el('h3', null, 'History'), liveHist.bar, liveHist.note, liveHist.tools, liveHist.body);
  liveSelectTab(liveHist.tab);
  return panel;
}
