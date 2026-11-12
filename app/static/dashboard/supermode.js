/* Supermode: how it works, what it did, and how it picks a model.
 *
 * Loaded before app.js; it uses the shared helpers declared there and in
 * insights.js (el, mountPage, dataTable, kpiCard, panelSection, apiFetch ...).
 * Everything rendered goes through textContent, never markup.
 */

const SUPERMODE_PATH = '/operator/v1/supermode';
const SUPERMODE_RANKING_PATH = '/operator/v1/supermode/ranking';
const SUPERMODE_NAME = 'palimpsest/supermode';

const supermodePage = {
  panel: null,
  body: null,
  days: '7',
  data: null,
  rankingBox: null,
  needs: { tokens: '2000', vision: false, tools: false },
};

/* The plain-words reason a model was left out of a ranking. */
const SUPERMODE_DROPPED = {
  context: 'Prompt too long for it',
  vision: 'No image support',
  tools: 'No tool support',
  no_key_ready: 'No key ready',
};

/* Why a request moved from one model to the next. */
const SUPERMODE_CAUSES = [
  ['exhausted', 'Every key was spent or paused', 'All keys of the model hit a rate limit, ran out of credits or failed, so the next model took over.'],
  ['context', 'Prompt too long', 'The model refused the prompt as longer than its window; the gateway learned that size.'],
  ['vision', 'No image support', 'The model refused an image; the gateway learned it cannot read them.'],
  ['tools', 'No tool support', 'The model refused tool calls; the gateway learned it cannot make them.'],
  ['request', 'Refused the request', 'The model turned the request down (a parameter it does not take, a "bad request"). Another model served it, so the refusal counted against the first one.'],
];

function smPercent(part, whole) {
  return whole > 0 ? (part / whole) * 100 : 0;
}

/* A count with its share of a total as a bar, like the Usage page. */
function smShare(count, total) {
  const cell = el('div', 'share');
  cell.append(el('span', null, NUMBER_FORMAT.format(count) + ' · ' + fmtPct(total > 0 ? count / total : 0, 0)), meter(smPercent(count, total)));
  return cell;
}

function smSteps(config) {
  const steps = [
    ['A request names the model', 'A client asks for ' + SUPERMODE_NAME + ' instead of a real model. Nothing else changes for the client: same endpoint, same format.'],
    ['The gateway reads what it needs', 'It estimates the prompt size plus the room for the reply, and whether the request carries images or tools. No provider is called to find out.'],
    ['Models that cannot serve are dropped', 'A model is out if its context window is too small, if it cannot read images or call tools when the request needs them, or if none of its keys is ready (paused, spent, throttled).'],
    ['The rest are ranked, quality first', 'Each model has a score: its quality tier minus penalties for failing often, for a recent rate limit, or for refusing a request another model served. A tie goes to the model with the most capacity left.'],
    ['The best one serves the request', 'Its keys are tried by the usual failover. If every key of the model fails, the gateway ranks again with the live state and moves to the next best model, up to ' + config.fallback_models + ' more.'],
    ['It learns from the answer', 'Each answer teaches the gateway: context sizes that work or fail, vision and tool support, how reliable a model is, and a requests-per-minute limit for keys whose provider gives none.'],
  ];
  const grid = el('ol', 'sm-steps');
  steps.forEach(([title, text], index) => {
    const item = el('li', 'sm-step');
    item.append(el('span', 'sm-step-num', String(index + 1)), el('strong', null, title), el('p', 'note', text));
    grid.append(item);
  });
  return grid;
}

function smScoreRules(config) {
  const rows = [
    ['Quality tier', 'start', '0 to 100 from a built-in table matched on the model name (provider prefixes and ":free" are ignored). A model the table does not know starts low.'],
    ['Your override', 'replaces', Object.keys(config.quality_overrides).length ? Object.keys(config.quality_overrides).length + ' model(s) overridden: ' + Object.entries(config.quality_overrides).map(([m, q]) => m + ' = ' + q).join(', ') : 'None set. PALIMPSEST_SUPERMODE_QUALITY replaces the score of a model by its exact name.'],
    ['Failure rate', '− up to 20', 'Once a model has 5 requests of history: 20 points times its share of provider-side failures. Network faults on the gateway side do not count.'],
    ['Recent rate limit', '− 5', 'For 2 minutes after the model got a 429.'],
    ['Refused a request', '− 30', 'For 30 minutes after it turned down a request that another model then served.'],
    ['Same score', 'tie-break', 'The model with an uncapped key first, then the most remaining daily cap, then the name.'],
  ];
  return dataTable(
    [
      { label: 'Part of the score', cell: (row) => row[0] },
      { label: 'Effect', cell: (row) => row[1] },
      { label: 'Details', cell: (row) => row[2] },
    ],
    rows,
    ''
  );
}

function smStatCards(stats) {
  const t = stats.totals;
  const summary = el('div', 'summary overview-summary');
  const answered = t.requests ? t.ok / t.requests : null;
  summary.append(
    kpiCard('Requests', fmtCompact(t.requests), 'last ' + plural(stats.days, 'day')),
    kpiCard('Answered', answered === null ? '—' : fmtPct(answered), NUMBER_FORMAT.format(t.ok) + ' answered · ' + NUMBER_FORMAT.format(t.failed) + ' not'),
    kpiCard('First pick answered', t.ok ? fmtPct(t.first_pick_served / t.ok) : '—', 'the best-ranked model did the work'),
    kpiCard('Moved to another model', NUMBER_FORMAT.format(t.hopped), NUMBER_FORMAT.format(t.hops) + ' move(s) in total'),
    kpiCard('Average latency', fmtMs(t.avg_latency_ms), 'answered requests'),
    kpiCard('No model could serve', NUMBER_FORMAT.format(t.no_model), 'nothing fit or nothing was ready'),
    kpiCard('Needed images', NUMBER_FORMAT.format(t.needed_vision), 'requests with an image'),
    kpiCard('Needed tools', NUMBER_FORMAT.format(t.needed_tools), 'requests with tools')
  );
  return summary;
}

function smDistribution(stats) {
  const answered = stats.totals.ok;
  const models = dataTable(
    [
      { label: 'Model', cell: (row) => row.model },
      { label: 'Answered', cell: (row) => smShare(row.served, answered) },
      { label: 'First pick', cell: (row) => NUMBER_FORMAT.format(row.first_pick) },
    ],
    stats.models,
    'No supermode request in this range yet.'
  );
  const providers = dataTable(
    [
      { label: 'Provider', cell: (row) => providerName(row.provider) },
      { label: 'Answered', cell: (row) => smShare(row.served, answered) },
    ],
    stats.providers,
    'No supermode request in this range yet.'
  );
  const accounts = dataTable(
    [
      { label: 'Client', cell: (row) => idCell(accountLabel(row.account_id), row.account_id) },
      { label: 'Requests', cell: (row) => smShare(row.requests, stats.totals.requests) },
    ],
    stats.accounts,
    'No supermode request in this range yet.'
  );
  const grid = el('div', 'sm-columns');
  grid.append(panelSection('By model', models), panelSection('By provider', providers), panelSection('By client', accounts));
  return grid;
}

function smCauses(stats) {
  const total = stats.totals.hops;
  const rows = SUPERMODE_CAUSES.map(([key, label, text]) => ({ label, text, count: stats.causes[key] || 0 }));
  return dataTable(
    [
      { label: 'Why it moved on', cell: (row) => row.label },
      { label: 'Times', cell: (row) => smShare(row.count, total) },
      { label: 'Meaning', cell: (row) => row.text },
    ],
    rows,
    ''
  );
}

function smLearnedText(learned) {
  const parts = [];
  if (learned.context_proven !== null) {
    parts.push('holds ≥ ' + fmtCompact(learned.context_proven));
  }
  if (learned.context_refused_at !== null) {
    parts.push('refused at ' + fmtCompact(learned.context_refused_at));
  }
  if (learned.ok || learned.failed) {
    parts.push(learned.ok + ' ok / ' + learned.failed + ' failed');
  }
  const now = Date.now() / 1000;
  if (learned.throttled_at && now - learned.throttled_at < 120) {
    parts.push('throttled just now');
  }
  if (learned.declined_at && now - learned.declined_at < 1800) {
    parts.push('declined a request');
  }
  return parts.join(' · ');
}

function smRankCell(row) {
  if (row.position) {
    return el('strong', null, '#' + row.position);
  }
  return el('span', 'badge badge--suspended', SUPERMODE_DROPPED[row.dropped_because] || row.dropped_because);
}

function smScoreCell(row) {
  const quality = row.override !== null ? row.override : row.base_quality;
  const cell = el('span', null, String(row.score));
  cell.title = 'quality ' + quality + (row.override !== null ? ' (your override)' : ' (table)') + ' − penalty ' + row.penalty;
  return cell;
}

function drawSupermodeRanking(data) {
  const needs = data.needs;
  const lead = el(
    'p',
    'note',
    'For a request of about ' + NUMBER_FORMAT.format(needs.tokens) + ' tokens' + (needs.vision ? ' with an image' : '') + (needs.tools ? ' with tools' : '') + ': ' + data.ranked + ' model(s) can serve, ' + data.dropped + ' dropped. The top one is picked; the next ones are the order it moves on in.'
  );
  const table = dataTable(
    [
      { label: 'Rank', cell: smRankCell },
      { label: 'Model', cell: (row) => row.model },
      { label: 'Provider', cell: (row) => row.providers.map(providerName).join(', ') },
      { label: 'Score', cell: smScoreCell },
      { label: 'Context', cell: (row) => (row.context === null ? 'unknown' : fmtCompact(row.context)) },
      { label: 'Images', cell: (row) => (row.vision ? 'yes' : 'no') },
      { label: 'Tools', cell: (row) => (row.tools === null ? 'unknown' : row.tools ? 'yes' : 'no') },
      { label: 'Keys ready', cell: (row) => String(row.keys_ready) },
      { label: 'Capacity', cell: (row) => (row.position ? (row.capacity === null ? 'unlimited' : NUMBER_FORMAT.format(row.capacity) + ' left') : '—') },
      { label: 'Learned', cell: (row) => smLearnedText(row.learned) },
    ],
    data.models,
    'No model is served by the pool yet. Add a key with models first.'
  );
  supermodePage.rankingBox.replaceChildren(lead, table);
}

async function loadSupermodeRanking() {
  const needs = supermodePage.needs;
  const tokens = Math.max(1, Math.floor(Number(needs.tokens) || 0));
  const query = '?tokens=' + tokens + '&vision=' + needs.vision + '&tools=' + needs.tools;
  supermodePage.rankingBox.replaceChildren(el('p', 'note', 'Ranking the models…'));
  try {
    drawSupermodeRanking(await apiFetch(SUPERMODE_RANKING_PATH + query));
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      supermodePage.rankingBox.replaceChildren(el('p', 'note', err.message));
    }
  }
}

function smRankingTool() {
  const tokens = el('input');
  tokens.type = 'number';
  tokens.min = '1';
  tokens.max = '10000000';
  tokens.value = supermodePage.needs.tokens;
  tokens.id = 'sm-tokens';
  const vision = el('input');
  vision.type = 'checkbox';
  vision.checked = supermodePage.needs.vision;
  const tools = el('input');
  tools.type = 'checkbox';
  tools.checked = supermodePage.needs.tools;
  const run = button('Show ranking', 'primary', () => {
    supermodePage.needs = { tokens: tokens.value, vision: vision.checked, tools: tools.checked };
    loadSupermodeRanking();
  });
  run.type = 'button';
  const form = el('div', 'filters sm-tool');
  form.append(labelled('Prompt plus reply (tokens)', tokens), labelled('Includes an image', vision), labelled('Uses tools', tools), run);
  supermodePage.rankingBox = el('div', 'stack');
  const box = el('div', 'stack');
  box.append(form, supermodePage.rankingBox);
  return box;
}

function smPath(event) {
  if (!event.picked) {
    return 'no model could take it';
  }
  return event.picked + event.hops.map((hop) => ' → ' + hop.to + ' (' + hop.cause + ')').join('');
}

function smRecent(events) {
  return dataTable(
    [
      { label: 'Time', cell: (row) => formatDate(row.ts) },
      { label: 'Client', cell: (row) => (row.account_id ? idCell(accountLabel(row.account_id), row.account_id) : '—') },
      {
        label: 'Needed',
        cell: (row) => {
          const needs = row.needs || {};
          return ['~' + fmtCompact(needs.tokens || 0) + ' tokens', needs.vision ? 'image' : '', needs.tools ? 'tools' : ''].filter(Boolean).join(' · ');
        },
      },
      { label: 'Picked → answered by', cell: (row) => el('span', 'sm-path', smPath(row)) },
      { label: 'Provider', cell: (row) => (row.provider ? providerName(row.provider) : '—') },
      { label: 'Result', cell: (row) => el('span', 'badge badge--' + (row.served ? 'active' : 'exhausted'), row.served ? 'answered' : String(row.status_code)) },
      { label: 'Latency', cell: (row) => fmtMs(row.latency_ms) },
    ],
    events,
    'No supermode request yet. Send one with the model "' + SUPERMODE_NAME + '".'
  );
}

function smLimits(limits) {
  if (!limits.length) {
    return el('p', 'note', 'None yet. A limit is learned when a key is throttled (429) by a provider that sends no rate-limit headers.');
  }
  return dataTable(
    [
      { label: 'Key', cell: (row) => idCell(row.label || shortId(row.key_id), row.key_id) },
      { label: 'Provider', cell: (row) => providerName(row.provider) },
      { label: 'Learned limit', cell: (row) => row.requests_per_minute + ' requests / minute' },
    ],
    limits,
    ''
  );
}

function drawSupermode(data) {
  const config = data.config;
  const stats = data.stats;
  const nodes = [];
  if (config.bypassed) {
    const warning = el('div', 'alert-card');
    warning.append(
      el('strong', null, 'Supermode is bypassed'),
      el('span', null, 'The master model "' + config.master_model + '" is set, so every request goes to it, including those for ' + SUPERMODE_NAME + '. Clear it on the Configuration page to use supermode.')
    );
    nodes.push(warning);
  }
  nodes.push(
    smStatCards(stats),
    panelSection('How supermode works', smSteps(config)),
    panelSection('How a model is selected', el('p', 'note', 'The score decides the order. These are the parts of it.'), smScoreRules(config)),
    panelSection('What it would pick right now', el('p', 'note', 'The live ranking from the current pool state, for a request of the size and kind you choose.'), smRankingTool()),
    panelSection(
      'Requests per day',
      trendChart(
        stats.daily.map((day) => day.day.slice(5)),
        [
          { label: 'Answered', values: stats.daily.map((day) => day.ok) },
          { label: 'Not answered', values: stats.daily.map((day) => day.failed) },
        ],
        { kind: 'bar' }
      )
    ),
    smDistribution(stats),
    panelSection('Why requests moved to another model', smCauses(stats)),
    panelSection('Learned request limits', el('p', 'note', 'Keys the gateway limited itself, from what it saw. Keys at their limit are tried last.'), smLimits(data.learned_limits)),
    panelSection('Recent supermode requests', smRecent(data.recent))
  );
  supermodePage.body.replaceChildren(...nodes);
  loadSupermodeRanking();
}

async function loadSupermode() {
  await runLoad(supermodePage, 'Loading supermode…', async () => {
    const [data] = await Promise.all([apiFetch(SUPERMODE_PATH + '?days=' + supermodePage.days), loadAccountNames().catch(() => null)]);
    supermodePage.data = data;
    const total = data.stats.totals.requests;
    supermodePage.panel.report(total ? plural(total, 'supermode request') + ' in the last ' + plural(data.stats.days, 'day') + '.' : 'No supermode request in this range yet.', 'ok');
    drawSupermode(data);
  });
}

function renderSupermode(view) {
  supermodePage.panel = createStatus();
  supermodePage.body = el('div', 'stack');
  mountPage(view, {
    title: 'Supermode',
    description: 'Request the model ' + SUPERMODE_NAME + ' and the gateway picks the best model that can serve it right now.',
    status: supermodePage.panel,
    actions: [refreshButton(() => loadSupermode())],
    tools: [rangeSelect(supermodePage, loadSupermode)],
    content: [supermodePage.body],
  });
  loadSupermode();
}
