/* Analytics sections: Overview, Performance, Costs, Alerts, Audit & Export, System,
 * plus the per-user detail dialog.
 *
 * Loaded before app.js and used only through it: every helper here (el, apiFetch,
 * mountPage, ...) is defined in app.js and resolved when a section renders, never
 * while this file loads. Charts are inline SVG built with the DOM, so the strict
 * Content-Security-Policy (no inline styles or scripts) is respected.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const OVERVIEW_PATH = '/operator/v1/overview';
const STATS_PATH = '/operator/v1/stats';
const HOURLY_PATH = '/operator/v1/stats/hourly';
const PRICING_PATH = '/operator/v1/pricing';
const COSTS_PATH = '/operator/v1/costs';
const ALERTS_PATH = '/operator/v1/alerts';
const ALERT_RULES_PATH = '/operator/v1/alerts/rules';
const AUDIT_PATH = '/operator/v1/audit';
const SYSTEM_PATH = '/operator/v1/system';
const EXPORT_PATH = '/operator/v1/export/';
const UTILIZATION_VIEW_PATH = '/v1/operator/pool/utilization';

const RANGE_OPTIONS = [
  ['1', 'Today'],
  ['7', 'Last 7 days'],
  ['30', 'Last 30 days'],
  ['90', 'Last 90 days'],
];

// -- formatting ------------------------------------------------------------------

function fmtCompact(value) {
  if (value === null || value === undefined) {
    return '—';
  }
  const abs = Math.abs(value);
  if (abs >= 1e9) {
    return (value / 1e9).toFixed(1) + 'B';
  }
  if (abs >= 1e6) {
    return (value / 1e6).toFixed(1) + 'M';
  }
  if (abs >= 1e4) {
    return (value / 1e3).toFixed(1) + 'k';
  }
  return NUMBER_FORMAT.format(Math.round(value));
}

function fmtMs(value) {
  if (value === null || value === undefined) {
    return '—';
  }
  return value >= 1000 ? (value / 1000).toFixed(2) + ' s' : Math.round(value) + ' ms';
}

function fmtPct(fraction, digits = 1) {
  return fraction === null || fraction === undefined ? '—' : (fraction * 100).toFixed(digits) + '%';
}

function fmtMoney(value, currency) {
  if (value === null || value === undefined) {
    return '—';
  }
  const digits = Math.abs(value) < 1 ? 4 : 2;
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency: currency || 'USD',
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    }).format(value);
  } catch (err) {
    return value.toFixed(digits) + ' ' + (currency || '');
  }
}

/* "▲ 12% vs previous" — `goodWhenUp` decides the colour; the text always states it. */
function deltaNote(current, previous, goodWhenUp = true) {
  const node = el('span', 'delta');
  if (!previous) {
    node.textContent = current ? 'no earlier data to compare' : '';
    return node;
  }
  const change = ((current - previous) / previous) * 100;
  if (Math.abs(change) < 0.5) {
    node.textContent = 'flat vs previous period';
    return node;
  }
  const up = change > 0;
  node.className = 'delta ' + (up === goodWhenUp ? 'delta--good' : 'delta--bad');
  node.textContent = (up ? '▲ ' : '▼ ') + Math.abs(change).toFixed(0) + '% vs previous period';
  return node;
}

function kpiCard(label, value, detail) {
  const card = el('div', 'metric');
  card.append(el('span', 'metric-label', label), el('span', 'metric-value', value));
  if (detail instanceof Node) {
    card.append(detail);
  } else {
    card.append(el('span', 'metric-detail', detail || ''));
  }
  return card;
}

function panelSection(title, ...nodes) {
  const box = el('section', 'panel');
  box.append(el('h3', null, title), ...nodes);
  return box;
}

// -- charts ----------------------------------------------------------------------

function svgEl(tag, attrs = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attrs)) {
    node.setAttribute(name, String(value));
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

function niceMax(value) {
  if (value <= 0) {
    return 1;
  }
  const power = Math.pow(10, Math.floor(Math.log10(value)));
  const unit = value / power;
  return (unit <= 1 ? 1 : unit <= 2 ? 2 : unit <= 5 ? 5 : 10) * power;
}

function chartLegend(series) {
  const legend = el('div', 'chart-legend');
  series.forEach((entry, index) => {
    const item = el('span', 'legend-item');
    item.append(el('span', 'swatch swatch--' + (index % 6)), document.createTextNode(entry.label));
    legend.append(item);
  });
  return legend;
}

/* A line/area chart (kind 'line') or stacked columns (kind 'bar'). `series` is
   [{ label, values }]; `labels` is the x axis, one per value. */
function trendChart(labels, series, { kind = 'line', height = 190, format = fmtCompact } = {}) {
  const box = el('figure', 'chart');
  const aspectWidth = 800;
  const aspectHeight = height;
  const pad = { left: 46, right: 10, top: 10, bottom: 24 };
  const svg = svgEl('svg', { viewBox: '0 0 ' + aspectWidth + ' ' + aspectHeight, class: 'chart-svg', role: 'img' });
  const innerW = aspectWidth - pad.left - pad.right;
  const innerH = aspectHeight - pad.top - pad.bottom;
  const count = labels.length;

  const totals = labels.map((_, i) =>
    kind === 'bar'
      ? series.reduce((sum, entry) => sum + (entry.values[i] || 0), 0)
      : Math.max(0, ...series.map((entry) => entry.values[i] || 0))
  );
  const max = niceMax(Math.max(0, ...totals));
  const y = (value) => pad.top + innerH - (value / max) * innerH;
  const x = (index) =>
    kind === 'bar'
      ? pad.left + (innerW / Math.max(count, 1)) * (index + 0.5)
      : pad.left + (count <= 1 ? innerW / 2 : (innerW / (count - 1)) * index);

  for (let step = 0; step <= 4; step += 1) {
    const value = (max / 4) * step;
    svg.append(
      svgEl('line', { x1: pad.left, x2: aspectWidth - pad.right, y1: y(value), y2: y(value), class: 'chart-grid' }),
      svgEl('text', { x: pad.left - 6, y: y(value) + 4, class: 'chart-axis', 'text-anchor': 'end' }, format(value))
    );
  }
  const labelEvery = Math.max(1, Math.ceil(count / 7));
  labels.forEach((label, index) => {
    if (index % labelEvery === 0 || index === count - 1) {
      svg.append(
        svgEl('text', { x: x(index), y: height - 6, class: 'chart-axis', 'text-anchor': 'middle' }, label)
      );
    }
  });

  if (kind === 'bar') {
    const barW = Math.max(2, Math.min(28, (innerW / Math.max(count, 1)) * 0.7));
    labels.forEach((label, index) => {
      let base = 0;
      series.forEach((entry, s) => {
        const value = entry.values[index] || 0;
        if (value <= 0) {
          return;
        }
        const bar = svgEl('rect', {
          x: x(index) - barW / 2,
          y: y(base + value),
          width: barW,
          height: Math.max(1, y(base) - y(base + value)),
          class: 'chart-bar chart-fill--' + (s % 6),
        });
        bar.append(svgEl('title', {}, label + ' · ' + entry.label + ': ' + NUMBER_FORMAT.format(value)));
        svg.append(bar);
        base += value;
      });
    });
  } else {
    series.forEach((entry, s) => {
      const points = entry.values.map((value, index) => [x(index), y(value || 0)]);
      if (points.length > 1) {
        const line = points.map(([px, py]) => px.toFixed(1) + ',' + py.toFixed(1)).join(' ');
        const area = line + ' ' + points[points.length - 1][0].toFixed(1) + ',' + y(0) + ' ' + points[0][0].toFixed(1) + ',' + y(0);
        svg.append(
          svgEl('polygon', { points: area, class: 'chart-area chart-fill--' + (s % 6) }),
          svgEl('polyline', { points: line, class: 'chart-line chart-stroke--' + (s % 6) })
        );
      }
      points.forEach(([px, py], index) => {
        const dot = svgEl('circle', { cx: px, cy: py, r: count > 40 ? 1.5 : 3, class: 'chart-dot chart-fill--' + (s % 6) });
        dot.append(svgEl('title', {}, labels[index] + ' · ' + entry.label + ': ' + NUMBER_FORMAT.format(entry.values[index] || 0)));
        svg.append(dot);
      });
    });
  }
  box.append(svg);
  if (series.length > 1 || kind === 'bar') {
    box.append(chartLegend(series));
  }
  if (!totals.some((value) => value > 0)) {
    box.append(el('p', 'note', 'No traffic in this range yet.'));
  }
  return box;
}

/* Latency histogram from the stored bucket counts. */
function latencyHistogram(buckets, edges) {
  const labels = buckets.map((_, index) =>
    index < edges.length ? '<' + (edges[index] >= 1000 ? edges[index] / 1000 + 's' : edges[index]) : '≥' + edges[edges.length - 1] / 1000 + 's'
  );
  return trendChart(labels, [{ label: 'Requests', values: buckets }], { kind: 'bar', height: 170 });
}

/* Weekday x hour-of-day (UTC) traffic heat map from hourly rows. */
function trafficHeatmap(hourly) {
  const grid = Array.from({ length: 7 }, () => Array(24).fill(0));
  for (const row of hourly) {
    const moment = new Date(row.hour);
    grid[moment.getUTCDay()][moment.getUTCHours()] += row.requests;
  }
  const max = Math.max(1, ...grid.flat());
  const cell = 22;
  const left = 34;
  const top = 16;
  const svg = svgEl('svg', {
    viewBox: '0 0 ' + (left + 24 * cell) + ' ' + (top + 7 * cell + 4),
    class: 'chart-svg heatmap',
    role: 'img',
  });
  const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  for (let hour = 0; hour < 24; hour += 3) {
    svg.append(svgEl('text', { x: left + hour * cell + cell / 2, y: 11, class: 'chart-axis', 'text-anchor': 'middle' }, String(hour)));
  }
  grid.forEach((row, day) => {
    svg.append(svgEl('text', { x: left - 6, y: top + day * cell + cell * 0.65, class: 'chart-axis', 'text-anchor': 'end' }, days[day]));
    row.forEach((value, hour) => {
      const rect = svgEl('rect', {
        x: left + hour * cell + 1,
        y: top + day * cell + 1,
        width: cell - 2,
        height: cell - 2,
        rx: 3,
        class: 'heat-cell',
        'fill-opacity': value ? 0.12 + 0.88 * (value / max) : 0.05,
      });
      rect.append(svgEl('title', {}, days[day] + ' ' + hour + ':00 UTC · ' + NUMBER_FORMAT.format(value) + ' requests'));
      svg.append(rect);
    });
  });
  const box = el('figure', 'chart');
  box.append(svg);
  return box;
}

function sparkline(values) {
  const width = 90;
  const height = 24;
  const svg = svgEl('svg', { viewBox: '0 0 ' + width + ' ' + height, class: 'sparkline', role: 'img' });
  const max = Math.max(1, ...values);
  if (values.length > 1) {
    const points = values
      .map((value, index) => ((width / (values.length - 1)) * index).toFixed(1) + ',' + (height - 2 - (value / max) * (height - 4)).toFixed(1))
      .join(' ');
    svg.append(svgEl('polyline', { points, class: 'chart-line chart-stroke--0' }));
  }
  return svg;
}

// -- shared page plumbing --------------------------------------------------------

function rangeSelect(state, reload) {
  const select = pairSelect(RANGE_OPTIONS, state.days);
  select.addEventListener('change', () => {
    state.days = select.value;
    reload();
  });
  const filters = el('div', 'filters');
  filters.append(labelled('Range', select));
  return filters;
}

async function runLoad(state, message, work) {
  state.panel.report(message, 'pending');
  try {
    await work();
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      state.panel.report(err.message, 'error');
    }
  }
}

async function downloadFile(path, filename) {
  const blob = await apiFetch(path, { blob: true });
  const url = URL.createObjectURL(blob);
  const link = el('a');
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function exportButton(label, path, filename, state) {
  return button(label, 'ghost', () =>
    state.panel.run(
      () => downloadFile(path, filename),
      () => state.panel.report(filename + ' downloaded.', 'ok')
    )
  );
}

// -- Overview --------------------------------------------------------------------

const overview = { panel: null, body: null, days: '7', data: null };

function drawOverview() {
  const data = overview.data;
  const t = data.totals;
  const prev = data.previous;
  const errors = t.errors_4xx + t.errors_5xx;
  const prevErrors = prev.errors_4xx + prev.errors_5xx;
  const tokensNow = t.prompt_tokens + t.completion_tokens;
  const tokensPrev = prev.prompt_tokens + prev.completion_tokens;
  const pool = data.pool;
  const nodes = [];

  if (data.alerts.count) {
    const banner = el('a', 'alert-banner' + (data.alerts.critical ? ' alert-banner--critical' : ''));
    banner.href = '#/alerts';
    banner.textContent =
      plural(data.alerts.count, 'alert') + ' firing' + (data.alerts.critical ? ' (' + data.alerts.critical + ' critical)' : '') + ' — open Alerts';
    nodes.push(banner);
  }

  const summary = el('div', 'summary overview-summary');
  summary.append(
    kpiCard('Requests', fmtCompact(t.requests), deltaNote(t.requests, prev.requests)),
    kpiCard('Success rate', fmtPct(t.success_rate), t.requests ? NUMBER_FORMAT.format(t.ok) + ' of ' + NUMBER_FORMAT.format(t.requests) + ' clean' : 'no traffic'),
    kpiCard('Errors', fmtCompact(errors), deltaNote(errors, prevErrors, false)),
    kpiCard('Latency p50 / p95', fmtMs(t.p50_ms) + ' / ' + fmtMs(t.p95_ms), 'avg ' + fmtMs(t.avg_latency_ms)),
    kpiCard('Tokens', fmtCompact(tokensNow), deltaNote(tokensNow, tokensPrev)),
    kpiCard(
      'Estimated cost',
      data.cost.total || data.cost.previous ? fmtMoney(data.cost.total, data.cost.currency) : '—',
      data.cost.unpriced_tokens
        ? fmtCompact(data.cost.unpriced_tokens) + ' tokens have no price — see Costs'
        : deltaNote(data.cost.total, data.cost.previous, false)
    ),
    kpiCard('Active users', NUMBER_FORMAT.format(data.active_users), 'made at least one request'),
    kpiCard(
      'Pool',
      pool.active_keys + ' active',
      pool.exhausted_keys + ' exhausted · ' + pool.cooldown_keys + ' cooldown · ' + pool.suspended_keys + ' suspended'
    )
  );
  nodes.push(summary);

  const labels = data.daily.map((day) => day.day.slice(5));
  nodes.push(
    panelSection(
      'Requests and errors per day',
      trendChart(labels, [
        { label: 'Requests', values: data.daily.map((day) => day.requests) },
        { label: 'Errors', values: data.daily.map((day) => day.errors_4xx + day.errors_5xx) },
      ])
    ),
    panelSection(
      'Tokens per day',
      trendChart(
        labels,
        [
          { label: 'Prompt', values: data.daily.map((day) => day.prompt_tokens) },
          { label: 'Completion', values: data.daily.map((day) => day.completion_tokens) },
        ],
        { kind: 'bar' }
      )
    )
  );

  if (pool.utilization_pct !== null && pool.utilization_pct !== undefined) {
    const strip = el('div', 'pool-strip');
    strip.append(
      el('span', null, 'Capped pool utilization ' + pool.utilization_pct.toFixed(1) + '%'),
      meter(pool.utilization_pct),
      el('span', 'metric-detail', NUMBER_FORMAT.format(pool.total_remaining) + ' requests left of ' + NUMBER_FORMAT.format(pool.total_cap))
    );
    nodes.push(panelSection('Pool capacity', strip));
  }

  nodes.push(
    panelSection(
      'Top users',
      dataTable(
        [
          { label: 'User', cell: (row) => idCell(accountLabel(row.account_id), row.account_id) },
          { label: 'Requests', cell: (row) => tokens(row.requests) },
          { label: 'Tokens', cell: (row) => tokens(row.tokens) },
          { label: 'Error rate', cell: (row) => fmtPct(row.error_rate) },
          {
            label: '',
            cell: (row) => button('Details', 'ghost btn-sm', () => showUserDetail(row.account_id)),
          },
        ],
        data.top_users,
        'No user has made a request in this range.'
      )
    )
  );
  overview.body.replaceChildren(...nodes);
}

async function loadOverview() {
  await runLoad(overview, 'Loading overview…', async () => {
    const [data] = await Promise.all([apiFetch(OVERVIEW_PATH + '?days=' + overview.days), loadAccountNames()]);
    overview.data = data;
    overview.panel.report('Compared with the ' + overview.days + ' day(s) before.', 'ok');
    drawOverview();
  });
}

function renderOverview(view) {
  overview.panel = createStatus();
  overview.body = el('div', 'stack');
  mountPage(view, {
    title: 'Overview',
    status: overview.panel,
    actions: [refreshButton(() => loadOverview())],
    tools: [rangeSelect(overview, loadOverview)],
    content: [overview.body],
  });
  loadOverview();
}

// -- Performance -----------------------------------------------------------------

const performance_ = { panel: null, body: null, days: '7', stats: null, hourly: null, util: null };

function reliabilityTable(entries, nameColumn) {
  return dataTable(
    [
      nameColumn,
      { label: 'Requests', cell: (row) => tokens(row.requests) },
      { label: 'Success', cell: (row) => fmtPct(row.success_rate) },
      { label: 'Error rate', cell: (row) => fmtPct(row.error_rate) },
      { label: 'p50', cell: (row) => fmtMs(row.p50_ms) },
      { label: 'p95', cell: (row) => fmtMs(row.p95_ms) },
      { label: 'p99', cell: (row) => fmtMs(row.p99_ms) },
      { label: 'Avg first token', cell: (row) => fmtMs(row.avg_ttft_ms) },
      { label: 'Tokens / request', cell: (row) => (row.requests ? tokens(Math.round((row.prompt_tokens + row.completion_tokens) / row.requests)) : null) },
      { label: 'After failover', cell: (row) => tokens(row.failovers) },
    ],
    entries,
    'No traffic in this range yet.'
  );
}

function keyForecast(keyStats, utilKey) {
  if (!utilKey || utilKey.remaining === null || utilKey.remaining === undefined) {
    return '—';
  }
  if (utilKey.remaining === 0) {
    return 'out now';
  }
  const today = new Date().toISOString().slice(0, 10);
  const todayRow = keyStats.days.find((day) => day.day === today);
  const hoursElapsed = Math.max(1, new Date().getUTCHours() + new Date().getUTCMinutes() / 60);
  const burn = todayRow ? todayRow.requests / hoursElapsed : 0;
  if (!burn) {
    return 'no usage today';
  }
  return '~' + humanizeSeconds((utilKey.remaining / burn) * 3600) + ' at today’s pace';
}

function drawPerformance() {
  const stats = performance_.stats;
  const t = stats.totals;
  const utilByKey = {};
  for (const key of (performance_.util && performance_.util.keys) || []) {
    utilByKey[key.key_id] = key;
  }
  const nodes = [];

  const summary = el('div', 'summary');
  summary.append(
    kpiCard('Latency p50', fmtMs(t.p50_ms), 'median request'),
    kpiCard('Latency p95', fmtMs(t.p95_ms), 'p99 ' + fmtMs(t.p99_ms)),
    kpiCard('Avg first token', fmtMs(t.avg_ttft_ms), 'streaming requests only'),
    kpiCard('Error rate', fmtPct(t.error_rate), NUMBER_FORMAT.format(t.errors_4xx) + ' client · ' + NUMBER_FORMAT.format(t.errors_5xx) + ' server'),
    kpiCard('Pool exhausted', NUMBER_FORMAT.format(t.exhausted), 'requests answered 503'),
    kpiCard('Served after failover', NUMBER_FORMAT.format(t.failovers), t.requests ? fmtPct(t.failovers / t.requests) + ' of requests' : '')
  );
  nodes.push(summary);

  nodes.push(panelSection('Latency distribution', latencyHistogram(t.latency_buckets, stats.latency_edges_ms)));

  const classes = Object.keys(stats.error_classes).sort();
  nodes.push(
    panelSection(
      'Failed provider tries by cause',
      classes.length
        ? trendChart(
            stats.daily.map((day) => day.day.slice(5)),
            classes.map((name) => ({ label: name, values: stats.daily.map((day) => day.error_classes[name] || 0) })),
            { kind: 'bar' }
          )
        : el('p', 'note', 'No failed provider tries in this range.')
    )
  );

  nodes.push(
    panelSection(
      'By provider',
      reliabilityTable(
        Object.entries(stats.providers)
          .map(([name, row]) => ({ name, ...row }))
          .sort((a, b) => b.requests - a.requests),
        { label: 'Provider', cell: (row) => providerName(row.name) }
      )
    ),
    panelSection(
      'By model',
      reliabilityTable(stats.models, { label: 'Model', cell: (row) => row.model })
    )
  );

  nodes.push(
    panelSection(
      'Pool keys: usage history and reliability',
      dataTable(
        [
          { label: 'Key', cell: (row) => idCell(keyDisplay(row.key_id, utilByKey), row.key_id) },
          { label: 'Requests', cell: (row) => tokens(row.requests) },
          { label: 'Tokens', cell: (row) => tokens(row.prompt_tokens + row.completion_tokens) },
          { label: 'Failed tries', cell: (row) => tokens(row.failed_tries) },
          { label: 'Failure rate', cell: (row) => fmtPct(row.failure_rate) },
          { label: 'p95', cell: (row) => fmtMs(row.p95_ms) },
          { label: 'Daily requests', cell: (row) => sparkline(row.days.map((day) => day.requests)) },
          {
            label: 'Left',
            cell: (row) => {
              const key = utilByKey[row.key_id];
              return key && key.remaining !== null ? NUMBER_FORMAT.format(key.remaining) : null;
            },
          },
          { label: 'Runs out in', cell: (row) => keyForecast(row, utilByKey[row.key_id]) },
        ],
        stats.keys,
        'No pool key served traffic in this range.'
      )
    )
  );

  const hourly = performance_.hourly.hourly;
  nodes.push(
    panelSection(
      'Last 48 hours',
      trendChart(
        hourly.slice(-48).map((row) => new Date(row.hour).getUTCHours() + 'h'),
        [
          { label: 'Requests', values: hourly.slice(-48).map((row) => row.requests) },
          { label: 'Errors', values: hourly.slice(-48).map((row) => row.errors_4xx + row.errors_5xx) },
        ]
      )
    ),
    panelSection('Busiest hours (last 14 days, UTC)', trafficHeatmap(hourly))
  );
  performance_.body.replaceChildren(...nodes);
}

function keyDisplay(keyId, utilByKey) {
  const key = utilByKey[keyId];
  if (key) {
    return key.label || (PROVIDER_NAMES[key.provider] || key.provider) + ' …' + (key.key_hint || '').slice(-4);
  }
  return shortId(keyId) + ' (removed)';
}

async function loadPerformance() {
  await runLoad(performance_, 'Loading performance…', async () => {
    const [stats, hourly, util] = await Promise.all([
      apiFetch(STATS_PATH + '?days=' + performance_.days),
      apiFetch(HOURLY_PATH + '?hours=336'),
      apiFetch(UTILIZATION_VIEW_PATH),
    ]);
    performance_.stats = stats;
    performance_.hourly = hourly;
    performance_.util = util;
    performance_.panel.report('Latency percentiles are estimated from fixed-width histogram buckets.', 'ok');
    drawPerformance();
  });
}

function renderPerformance(view) {
  performance_.panel = createStatus();
  performance_.body = el('div', 'stack');
  mountPage(view, {
    title: 'Performance',
    status: performance_.panel,
    actions: [refreshButton(() => loadPerformance())],
    tools: [rangeSelect(performance_, loadPerformance)],
    content: [performance_.body],
  });
  loadPerformance();
}

// -- Costs -----------------------------------------------------------------------

const costs = { panel: null, body: null, days: '30', data: null, pricing: null };

function priceRow(model = '', input = '', output = '') {
  const row = el('div', 'price-row');
  const fields = [
    ['Model (prefix matches dated variants)', 'text', model, 'price-model'],
    ['Input / 1M tokens', 'number', input, 'price-in'],
    ['Output / 1M tokens', 'number', output, 'price-out'],
  ].map(([label, type, value, name]) => {
    const input_ = el('input');
    input_.type = type;
    input_.name = name;
    input_.value = value;
    if (type === 'number') {
      input_.min = '0';
      input_.step = 'any';
    } else {
      input_.maxLength = 128;
      input_.setAttribute('aria-label', label);
    }
    input_.placeholder = label;
    return input_;
  });
  const remove = button('Remove', 'ghost btn-sm danger', () => row.remove());
  row.append(...fields, remove);
  return row;
}

function openPricing(extraModels = []) {
  const pricing = costs.pricing || { currency: 'USD', prices: {}, monthly_budget: null };
  openModal({
    title: 'Model prices',
    wide: true,
    primary: { label: 'Save prices', formId: 'pricing-form' },
    content: (modal) => {
      const form = el('form', 'stack');
      form.id = 'pricing-form';
      form.noValidate = true;
      const currency = el('input');
      currency.value = pricing.currency;
      currency.maxLength = 8;
      const budget = el('input');
      budget.type = 'number';
      budget.min = '0';
      budget.step = 'any';
      budget.value = pricing.monthly_budget === null ? '' : pricing.monthly_budget;
      const top = el('div', 'filters');
      top.append(labelled('Currency', currency), labelled('Monthly budget (optional)', budget));
      const rows = el('div', 'stack');
      for (const [name, price] of Object.entries(pricing.prices)) {
        rows.append(priceRow(name, price.input, price.output));
      }
      for (const name of extraModels) {
        rows.append(priceRow(name));
      }
      if (!rows.children.length) {
        rows.append(priceRow());
      }
      const add = button('+ Add model', 'ghost', () => rows.append(priceRow()));
      form.append(
        el('p', 'note', 'Prices per 1M tokens, set by you — nothing is bundled. Costs are estimates from provider-reported token counts.'),
        top,
        rows,
        add
      );
      form.addEventListener('submit', (event) => {
        event.preventDefault();
        const prices = {};
        for (const row of rows.children) {
          const name = row.querySelector('[name=price-model]').value.trim();
          if (!name) {
            continue;
          }
          prices[name] = {
            input: Number(row.querySelector('[name=price-in]').value || 0),
            output: Number(row.querySelector('[name=price-out]').value || 0),
          };
        }
        modal.status.run(
          () =>
            apiFetch(PRICING_PATH, {
              method: 'PUT',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                currency: currency.value.trim() || 'USD',
                prices,
                monthly_budget: budget.value === '' ? null : Number(budget.value),
              }),
            }),
          () => {
            closeModal();
            return loadCosts();
          }
        );
      });
      return form;
    },
  });
}

function drawCosts() {
  const data = costs.data;
  const money = (value) => fmtMoney(value, data.currency);
  const month = data.month;
  const nodes = [];

  const summary = el('div', 'summary');
  summary.append(
    kpiCard('Spend in range', money(data.total), 'last ' + data.days + ' day(s)'),
    kpiCard('Month to date', money(month.cost), month.month),
    kpiCard('Projected this month', money(month.projected), 'at the current pace'),
    kpiCard(
      'Budget',
      month.budget ? month.budget_used_pct.toFixed(0) + '% used' : 'not set',
      month.budget ? money(month.cost) + ' of ' + money(month.budget) : 'set one under Edit prices'
    )
  );
  nodes.push(summary);
  if (month.budget) {
    nodes.push(meter(Math.min(100, month.budget_used_pct)));
    if (month.projected > month.budget) {
      nodes.push(el('p', 'note note--warn', 'Projected spend (' + money(month.projected) + ') is above the monthly budget.'));
    }
  }

  if (data.unpriced.length) {
    const box = el('div', 'note note--warn');
    box.append(
      document.createTextNode(
        'No price for: ' + data.unpriced.map((entry) => entry.model + ' (' + fmtCompact(entry.tokens) + ' tokens)').join(', ') + '. '
      ),
      button('Set prices', 'ghost btn-sm', () => openPricing(data.unpriced.map((entry) => entry.model)))
    );
    nodes.push(box);
  }

  nodes.push(
    panelSection(
      'Spend per day',
      trendChart(
        data.daily.map((day) => day.day.slice(5)),
        [{ label: 'Cost', values: data.daily.map((day) => day.cost) }],
        { kind: 'bar', format: (value) => (value >= 1 ? value.toFixed(0) : value.toFixed(3)) }
      )
    ),
    panelSection(
      'By model',
      dataTable(
        [
          { label: 'Provider', cell: (row) => providerName(row.provider) },
          { label: 'Model', cell: (row) => row.model },
          { label: 'Requests', cell: (row) => tokens(row.requests) },
          { label: 'Tokens', cell: (row) => tokens(row.tokens) },
          { label: 'Cost', cell: (row) => money(row.cost) },
          { label: 'Share', cell: (row) => shareCell(data.total ? row.cost / data.total : 0) },
        ],
        data.models,
        'No usage in this range.'
      )
    ),
    panelSection(
      'By user',
      dataTable(
        [
          { label: 'User', cell: (row) => idCell(accountLabel(row.account_id), row.account_id) },
          { label: 'Requests', cell: (row) => tokens(row.requests) },
          { label: 'Tokens', cell: (row) => tokens(row.tokens) },
          { label: 'Cost', cell: (row) => money(row.cost) },
          { label: 'Share', cell: (row) => shareCell(data.total ? row.cost / data.total : 0) },
        ],
        data.accounts,
        'No usage in this range.'
      )
    )
  );
  costs.body.replaceChildren(...nodes);
}

async function loadCosts() {
  await runLoad(costs, 'Loading costs…', async () => {
    const [data, pricing] = await Promise.all([
      apiFetch(COSTS_PATH + '?days=' + costs.days),
      apiFetch(PRICING_PATH),
      loadAccountNames(),
    ]);
    costs.data = data;
    costs.pricing = pricing;
    costs.panel.report('Estimated from provider-reported tokens and the prices you set.', 'ok');
    drawCosts();
  });
}

function renderCosts(view) {
  costs.panel = createStatus();
  costs.body = el('div', 'stack');
  mountPage(view, {
    title: 'Costs',
    status: costs.panel,
    actions: [button('Edit prices', 'primary', () => openPricing()), refreshButton(() => loadCosts())],
    tools: [rangeSelect(costs, loadCosts)],
    content: [costs.body],
  });
  loadCosts();
}

// -- Alerts ----------------------------------------------------------------------

const alertsPage = { panel: null, active: null, rules: null };
const alertBadge = { count: 0, at: 0 };

function alertCard(alertItem) {
  const card = el('div', 'alert-card alert-card--' + alertItem.severity);
  card.append(el('strong', null, alertItem.title), el('span', null, alertItem.detail));
  return card;
}

function ruleNumber(name, label, value, min, max, step = 1) {
  const input = el('input');
  input.type = 'number';
  input.name = name;
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = value === null || value === undefined ? '' : value;
  input.placeholder = 'off';
  return labelled(label, input);
}

function drawAlertRules(rules) {
  const form = el('form', 'stack');
  form.noValidate = true;
  const enabled = el('input');
  enabled.type = 'checkbox';
  enabled.checked = rules.enabled;
  const problems = el('input');
  problems.type = 'checkbox';
  problems.checked = rules.key_problems;
  const url = el('input');
  url.type = 'url';
  url.name = 'webhook_url';
  url.placeholder = 'https://hooks.example.com/… (Slack, Discord, any JSON webhook)';
  url.value = rules.webhook_url || '';
  const grid = el('div', 'filters');
  grid.append(
    ruleNumber('pool_remaining_pct', 'Pool capacity left below (%)', rules.pool_remaining_pct, 0, 100),
    ruleNumber('error_rate_pct', 'Error rate above (%, this hour)', rules.error_rate_pct, 0, 100),
    ruleNumber('error_rate_min_requests', 'Min requests for error rule', rules.error_rate_min_requests, 1, 100000),
    ruleNumber('user_cap_pct', 'User at (%) of daily cap', rules.user_cap_pct, 1, 100)
  );
  const toggles = el('div', 'filters');
  toggles.append(labelled('Alerts enabled', enabled), labelled('Key suspended / exhausted', problems));
  const save = button('Save rules', 'primary');
  save.type = 'submit';
  const test = button('Send test message', 'ghost', () =>
    alertsPage.panel.run(
      () => apiFetch(ALERTS_PATH + '/test', { method: 'POST' }),
      () => alertsPage.panel.report('Test message sent.', 'ok')
    )
  );
  form.append(
    el('p', 'note', 'Delivered to the webhook at most once an hour per alert. The gateway evaluates rules every minute.'),
    toggles,
    grid,
    labelled('Webhook URL', url),
    (() => {
      const row = el('div', 'filters');
      row.append(save, test);
      return row;
    })()
  );
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const num = (name) => {
      const value = form.querySelector('[name=' + name + ']').value;
      return value === '' ? null : Number(value);
    };
    alertsPage.panel.run(
      () =>
        apiFetch(ALERT_RULES_PATH, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            enabled: enabled.checked,
            key_problems: problems.checked,
            webhook_url: url.value.trim() || null,
            pool_remaining_pct: num('pool_remaining_pct'),
            error_rate_pct: num('error_rate_pct'),
            error_rate_min_requests: num('error_rate_min_requests') || 20,
            user_cap_pct: num('user_cap_pct'),
          }),
        }),
      () => {
        alertsPage.panel.report('Alert rules saved.', 'ok');
        return loadAlerts();
      }
    );
  });
  return form;
}

async function loadAlerts() {
  await runLoad(alertsPage, 'Loading alerts…', async () => {
    const [active, rules] = await Promise.all([apiFetch(ALERTS_PATH), apiFetch(ALERT_RULES_PATH)]);
    alertsPage.panel.report(
      active.data.length ? plural(active.data.length, 'alert') + ' firing.' : 'Nothing is firing.',
      active.data.length ? 'error' : 'ok'
    );
    alertsPage.active.replaceChildren(
      ...(active.data.length ? active.data.map(alertCard) : [el('p', 'note', 'All clear: no rule is firing right now.')])
    );
    alertsPage.rules.replaceChildren(drawAlertRules(rules));
    alertBadge.count = active.data.length;
    alertBadge.at = Date.now();
    paintAlertBadge();
  });
}

function renderAlerts(view) {
  alertsPage.panel = createStatus();
  alertsPage.active = el('div', 'stack');
  alertsPage.rules = el('div', 'stack');
  mountPage(view, {
    title: 'Alerts',
    status: alertsPage.panel,
    actions: [refreshButton(() => loadAlerts())],
    content: [panelSection('Firing now', alertsPage.active), panelSection('Rules and webhook', alertsPage.rules)],
  });
  loadAlerts();
}

/* The nav link carries the count of firing alerts, refreshed at most every 30 s. */
function paintAlertBadge() {
  const link = els.nav.querySelector('a[href="#/alerts"]');
  if (!link) {
    return;
  }
  const old = link.querySelector('.nav-badge');
  if (old) {
    old.remove();
  }
  if (alertBadge.count) {
    link.append(el('span', 'nav-badge', String(alertBadge.count)));
  }
}

async function refreshAlertBadge() {
  paintAlertBadge();
  if (Date.now() - alertBadge.at < 30000) {
    return;
  }
  alertBadge.at = Date.now();
  try {
    alertBadge.count = (await apiFetch(ALERTS_PATH)).data.length;
    paintAlertBadge();
  } catch (err) {
    /* The badge is a convenience: a failure here must not disturb the page. */
  }
}

// -- Audit & export --------------------------------------------------------------

const auditPage = { panel: null, table: null, days: '30', action: '', pager: null };

const AUDIT_FILTERS = [
  ['', 'All actions'],
  ['account', 'Users'],
  ['pool_key', 'Pool keys'],
  ['config', 'Configuration'],
  ['pricing', 'Prices'],
  ['alerts', 'Alerts'],
  ['operator_key', 'Operator keys'],
];

function describeDetail(detail) {
  if (!detail || !Object.keys(detail).length) {
    return null;
  }
  return Object.entries(detail)
    .map(([key, value]) => key + ': ' + (typeof value === 'object' && value !== null ? JSON.stringify(value) : value))
    .join(' · ');
}

async function loadAudit() {
  auditPage.pager = auditPage.pager || newPager();
  await runLoad(auditPage, 'Loading audit log…', async () => {
    const query = '?limit=1000' + (auditPage.action ? '&action=' + encodeURIComponent(auditPage.action) : '');
    const [body] = await Promise.all([apiFetch(AUDIT_PATH + query), loadAccountNames()]);
    auditPage.panel.report(plural(body.data.length, 'action') + ' recorded. Key material is never logged.', 'ok');
    auditPage.table.replaceChildren(
      dataTable(
        [
          { label: 'When', cell: (row) => formatDate(row.ts) },
          { label: 'Operator', cell: (row) => idCell(shortId(row.operator_id), row.operator_id) },
          { label: 'Action', cell: (row) => row.action },
          { label: 'Target', cell: (row) => (row.target ? idCell(accountNames[row.target] || shortId(row.target), row.target) : null) },
          { label: 'Detail', cell: (row) => describeDetail(row.detail) },
        ],
        body.data,
        'Nothing recorded yet. Changes made from now on appear here.',
        null,
        { pager: auditPage.pager, noun: 'action', redraw: loadAudit }
      )
    );
  });
}

function renderAudit(view) {
  auditPage.panel = createStatus();
  auditPage.table = el('div', 'table-wrap');
  const filter = pairSelect(AUDIT_FILTERS, auditPage.action);
  filter.addEventListener('change', () => {
    auditPage.action = filter.value;
    loadAudit();
  });
  const range = pairSelect(RANGE_OPTIONS.slice(1), auditPage.days);
  range.addEventListener('change', () => {
    auditPage.days = range.value;
  });
  const exports = el('div', 'filters');
  exports.append(
    labelled('Usage export range', range),
    button('Download usage CSV', 'ghost', () =>
      auditPage.panel.run(
        () => downloadFile(EXPORT_PATH + 'usage.csv?days=' + auditPage.days, 'usage-' + auditPage.days + 'd.csv'),
        () => auditPage.panel.report('usage CSV downloaded.', 'ok')
      )
    ),
    exportButton('Download requests CSV', EXPORT_PATH + 'requests.csv?limit=10000', 'requests.csv', auditPage),
    exportButton('Download audit CSV', EXPORT_PATH + 'audit.csv', 'audit.csv', auditPage)
  );
  const filters = el('div', 'filters');
  filters.append(labelled('Show', filter));
  mountPage(view, {
    title: 'Audit & Export',
    status: auditPage.panel,
    actions: [refreshButton(() => loadAudit())],
    tools: [filters],
    content: [panelSection('Export data', exports), panelSection('Operator audit log', auditPage.table)],
  });
  loadAudit();
}

// -- System ----------------------------------------------------------------------

const systemPage = { panel: null, body: null };

function fmtBytes(value) {
  if (value === null || value === undefined) {
    return '—';
  }
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return size.toFixed(unit ? 1 : 0) + ' ' + units[unit];
}

function drawSystem(data) {
  const redis = data.redis;
  const summary = el('div', 'summary');
  summary.append(
    kpiCard('Gateway', 'v' + data.version, 'up ' + humanizeSeconds(data.uptime_seconds)),
    kpiCard('Redis', redis.reachable ? 'reachable' : 'unreachable', redis.ping_ms === null ? '' : redis.ping_ms + ' ms round trip · v' + (redis.version || '?')),
    kpiCard(
      'Redis memory',
      fmtBytes(redis.used_memory_bytes),
      redis.max_memory_bytes ? 'limit ' + fmtBytes(redis.max_memory_bytes) + ' · peak ' + fmtBytes(redis.peak_memory_bytes) : 'no limit · peak ' + fmtBytes(redis.peak_memory_bytes)
    ),
    kpiCard('Stored keys', NUMBER_FORMAT.format(redis.total_keys || 0), (redis.connected_clients || 0) + ' client(s) · ' + (redis.evicted_keys || 0) + ' evicted')
  );
  const retention = data.retention;
  const rows = [
    ['Usage counters kept', retention.usage_retention_days + ' days'],
    ['Hourly counters kept', retention.hourly_retention_hours / 24 + ' days'],
    ['Recent requests kept', NUMBER_FORMAT.format(retention.usage_recent_max)],
    ['Failover events kept', NUMBER_FORMAT.format(retention.failover_event_log_size)],
  ];
  const origin = window.location.origin;
  const scrape = el('pre', 'code-block');
  scrape.textContent =
    'scrape_configs:\n  - job_name: palimpsest\n    metrics_path: /operator/v1/metrics\n    authorization:\n      credentials: <operator key>\n    static_configs:\n      - targets: [' +
    origin.replace(/^https?:\/\//, '') +
    ']';
  systemPage.body.replaceChildren(
    summary,
    panelSection('Retention', dataTable([{ label: 'Setting', cell: (r) => r[0] }, { label: 'Value', cell: (r) => r[1] }], rows, '')),
    panelSection('Prometheus', el('p', 'note', 'Metrics are served in Prometheus text format with an operator key as the bearer token.'), scrape)
  );
}

async function loadSystem() {
  await runLoad(systemPage, 'Loading system status…', async () => {
    const data = await apiFetch(SYSTEM_PATH);
    systemPage.panel.report(data.redis.reachable ? 'Healthy.' : 'Redis did not answer a ping.', data.redis.reachable ? 'ok' : 'error');
    drawSystem(data);
  });
}

function renderSystem(view) {
  systemPage.panel = createStatus();
  systemPage.body = el('div', 'stack');
  mountPage(view, {
    title: 'System',
    status: systemPage.panel,
    actions: [refreshButton(() => loadSystem())],
    content: [systemPage.body],
  });
  loadSystem();
}

// -- Per-user detail -------------------------------------------------------------

async function showUserDetail(accountId) {
  const handle = openModal({
    title: 'User details',
    wide: true,
    cancelLabel: 'Close',
    content: el('p', 'note', 'Loading…'),
  });
  try {
    const [data] = await Promise.all([apiFetch('/operator/v1/accounts/' + encodeURIComponent(accountId) + '/stats?days=30'), loadAccountNames()]);
    const t = data.totals;
    const errors = t.errors_4xx + t.errors_5xx;
    const summary = el('div', 'summary overview-summary');
    summary.append(
      kpiCard('Requests (30d)', fmtCompact(t.requests), data.display_name || shortId(data.account_id)),
      kpiCard('Tokens (30d)', fmtCompact(t.prompt_tokens + t.completion_tokens), plural(data.models.length, 'model')),
      kpiCard('Error rate', t.requests ? fmtPct(errors / t.requests) : '—', NUMBER_FORMAT.format(errors) + ' failed'),
      kpiCard('Latency p95', fmtMs(t.p95_ms || null), ''),
      kpiCard('Est. cost (30d)', data.cost.total ? fmtMoney(data.cost.total, data.cost.currency) : '—', data.cost.unpriced_tokens ? 'some models unpriced' : ''),
      kpiCard('Sessions / keys', data.session_count + ' / ' + data.key_count, 'last used ' + (data.last_used_at ? formatDate(data.last_used_at) : 'never'))
    );
    const cap = data.cap;
    const capNode = cap.daily_cap
      ? [el('span', null, 'Daily cap today: ' + cap.used_today + ' of ' + NUMBER_FORMAT.format(cap.daily_cap) + ' (' + cap.pct + '%) — default cap; a per-user override is not reflected'), meter(Math.min(100, cap.pct))]
      : [el('span', 'note', 'No default daily cap is configured. Today: ' + cap.used_today + ' request(s).')];
    const capBox = el('div', 'pool-strip');
    capBox.append(...capNode);
    handle.body.replaceChildren(
      el('p', 'note', 'Status: ' + data.status + ' · created ' + formatDate(data.created_at)),
      summary,
      capBox,
      panelSection(
        'Requests per day',
        trendChart(
          data.daily.map((day) => day.day.slice(5)),
          [
            { label: 'Requests', values: data.daily.map((day) => day.requests) },
            { label: 'Errors', values: data.daily.map((day) => day.errors_4xx + day.errors_5xx) },
          ]
        )
      ),
      panelSection(
        'Models used',
        dataTable(
          [
            { label: 'Provider', cell: (row) => providerName(row.provider) },
            { label: 'Model', cell: (row) => row.model },
            { label: 'Requests', cell: (row) => tokens(row.requests) },
            { label: 'Tokens', cell: (row) => tokens(row.total_tokens) },
          ],
          data.models,
          'No usage in the last 30 days.'
        )
      ),
      panelSection(
        'Recent requests',
        dataTable(
          [
            { label: 'Time', cell: (row) => formatDate(row.timestamp) },
            { label: 'Model', cell: (row) => row.model },
            { label: 'Outcome', cell: (row) => outcomeBadge(row.outcome) },
            { label: 'Latency', cell: (row) => fmtMs(row.latency_ms) },
          ],
          data.recent,
          'No recent requests.'
        )
      )
    );
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      handle.status.report(err.message, 'error');
    }
  }
}
