/* Live Flow: requests travelling client -> gateway -> provider and back.
 *
 * Loaded before app.js (and before livehistory.js); uses the shared helpers
 * declared there and in insights.js. The page polls /operator/v1/live about once
 * a second and replays each batch with its real timing on a wiring diagram: one
 * dot per request that travels to the gateway, on to a provider and model, waits
 * there for the answer, comes back and returns to the client. Wires glow while a
 * request is on them. Under it: a plain-words "now" list, the requests in
 * flight, and the history tabs of livehistory.js.
 * Metadata only. Everything rendered goes through textContent, never markup.
 */

const LIVE_PATH = '/operator/v1/live';
const LIVE_POLL_MS = 1000;
const LIVE_MAX_CLIENTS = 10;
const LIVE_NOW_ROWS = 6;
const LIVE_STALE_MS = 5 * 60 * 1000;
/* Calls kept in memory: the oldest finished ones go first once there are more. */
const LIVE_CALLS_KEEP = 2000;
const LIVE_CALLS_SLACK = 200;
/* No poll for this long (a hidden tab, a sleeping laptop): steps were missed, so the
   call history is loaded again. */
const LIVE_GAP_MS = 15000;
/* A batch is replayed with its real gaps, but never over longer than this. */
const LIVE_REPLAY_MAX_MS = 1800;
const LIVE_LEG_MS = 450;
const LIVE_SLOW = 2.5;
/* A move that waited longer than this for its turn is not worth replaying. */
const LIVE_SNAP_MS = 1500;
/* If the browser stops drawing frames (covered or minimised window), a timer
   keeps the dots moving. */
const LIVE_FALLBACK_MS = 60;
let liveFrameId = 0;

/* The diagram's grid, in viewBox units. */
const LF = {
  width: 1000,
  top: 74,
  row: 82,
  node: 62,
  clientX: 10,
  clientW: 230,
  gatewayX: 380,
  gatewayW: 240,
  providerX: 760,
  providerW: 230,
};

const livePage = {
  panel: null,
  root: null,
  svg: null,
  pollTimer: null,
  tickTimer: null,
  frame: null,
  cursor: null,
  paused: false,
  slow: false,
  firstBatch: true,
  pauseButton: null,
  slowButton: null,
  stats: null,
  nowBox: null,
  flightsBox: null,
  clients: [],
  providers: [],
  gateway: null,
  signalLayer: null,
  tokens: new Map(),
  flights: new Map(),
  /* Every call seen, by request id: loaded from the history and kept up to date by events. */
  calls: new Map(),
  /* The steps seen while this page is open, newest first. */
  feed: [],
  keyRecords: [],
  keys: {},
  keysAt: 0,
  lastPollAt: 0,
  seen: 0,
  dirty: true,
};

function liveActive() {
  return window.location.hash.replace(/^#\/?/, '') === 'live' && livePage.root && livePage.root.isConnected !== false;
}

function liveStop() {
  clearInterval(livePage.pollTimer);
  clearInterval(livePage.tickTimer);
  livePage.pollTimer = null;
  livePage.tickTimer = null;
  livePage.frame = null; // a step already scheduled sees this and does nothing
}

function liveReducedMotion() {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/* -- wording ---------------------------------------------------------------- */

function liveClock(ms) {
  const d = new Date(ms);
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()) + '.' + pad(d.getMilliseconds(), 3);
}

/* Today's calls show the time; older ones the date too. */
function liveWhen(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  const time = pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
  return d.toDateString() === new Date().toDateString() ? time : pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + time;
}

function liveKey(id) {
  return livePage.keys[id] || shortId(id);
}

function liveClient(id) {
  return id ? accountLabel(id) : 'client';
}

function liveShort(text, max) {
  const value = String(text || '');
  return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

function liveTokens(ev) {
  if (ev.prompt_tokens === null || ev.prompt_tokens === undefined) {
    return '';
  }
  return ' · ' + NUMBER_FORMAT.format(ev.prompt_tokens) + ' tokens in, ' + NUMBER_FORMAT.format(ev.completion_tokens || 0) + ' out';
}

function liveOk(status) {
  return status !== null && status !== undefined && status < 400;
}

/* [step label, badge tone, sentence] for one event. */
function liveDescribe(ev, accountId) {
  const client = liveClient(accountId);
  if (ev.kind === 'request') {
    const route = ev.routed_by === 'supermode' ? '. Supermode picked ' + ev.model : ev.routed_by === 'master' ? '. Master model: ' + ev.model : '';
    return ['1 request in', 'suspended', client + ' sends a request to the gateway, asking for ' + ev.requested + route + (ev.stream ? ' (streamed)' : '') + (ev.tools ? ' (with tools)' : '')];
  }
  if (ev.kind === 'attempt') {
    return ['2 sent out', 'cooldown', 'The gateway sends it to ' + providerName(ev.provider) + ', model ' + ev.model + ', with key ' + liveKey(ev.key_id) + (ev.n > 1 ? ' (attempt ' + ev.n + ')' : '')];
  }
  if (ev.kind === 'result') {
    if (ev.ok) {
      return ['3 answer', 'active', providerName(ev.provider) + ' (' + liveKey(ev.key_id) + ') answers the gateway after ' + fmtMs(ev.ms)];
    }
    return ['3 refused', 'exhausted', providerName(ev.provider) + ' (' + liveKey(ev.key_id) + ') refuses (' + (ev.status_code || 'no answer') + (ev.cause ? ', ' + ev.cause : '') + ') after ' + fmtMs(ev.ms)];
  }
  if (ev.kind === 'hop') {
    return ['moves on', 'cooldown', 'The gateway gives up on ' + ev.from + ' and tries ' + ev.to + ' (' + ev.cause + ')'];
  }
  const good = liveOk(ev.status_code);
  return [
    good ? '4 response' : '4 failed',
    good ? 'active' : 'exhausted',
    good
      ? 'The gateway sends the response to ' + client + ': ' + ev.status_code + ' in ' + fmtMs(ev.latency_ms) + liveTokens(ev) + (ev.ttft_ms ? ', first chunk after ' + fmtMs(ev.ttft_ms) : '')
      : 'The gateway answers ' + client + ' with an error: ' + ev.status_code + ' ' + ev.outcome + ' after ' + fmtMs(ev.latency_ms),
  ];
}

/* -- the diagram ------------------------------------------------------------ */

function liveRows() {
  return Math.max(3, livePage.clients.length, livePage.providers.length);
}

function liveRowY(index) {
  return LF.top + index * LF.row + LF.node / 2;
}

function liveNode(group, x, y, width, title) {
  const rect = svgEl('rect', { x, y: y - LF.node / 2, width, height: LF.node, rx: 10, class: 'lf-node' });
  const label = svgEl('text', { x: x + width / 2, y: y - 9, class: 'lf-label', 'text-anchor': 'middle' }, title);
  const note = svgEl('text', { x: x + width / 2, y: y + 8, class: 'lf-sub', 'text-anchor': 'middle' }, '');
  const note2 = svgEl('text', { x: x + width / 2, y: y + 23, class: 'lf-sub', 'text-anchor': 'middle' }, '');
  group.append(rect, label, note, note2);
  return { rect, label, note, note2 };
}

function liveCaption(text, x, y, anchor = 'middle', extra = '') {
  return svgEl('text', { x, y, class: 'lf-caption' + extra, 'text-anchor': anchor }, text);
}

function liveLayout() {
  const rows = liveRows();
  const height = LF.top + rows * LF.row;
  livePage.svg.setAttribute('viewBox', '0 0 ' + LF.width + ' ' + height);
  const wires = svgEl('g', { class: 'lf-wires' });
  const nodes = svgEl('g', { class: 'lf-nodes' });
  const labels = svgEl('g', { class: 'lf-captions' });

  labels.append(
    liveCaption('CLIENTS', LF.clientX + LF.clientW / 2, 22),
    liveCaption('GATEWAY', LF.gatewayX + LF.gatewayW / 2, 22),
    liveCaption('PROVIDERS AND MODELS', LF.providerX + LF.providerW / 2, 22),
    liveCaption('who calls', LF.clientX + LF.clientW / 2, 38, 'middle', ' lf-caption--soft'),
    liveCaption('routes, retries, moves on', LF.gatewayX + LF.gatewayW / 2, 38, 'middle', ' lf-caption--soft'),
    liveCaption('who answers', LF.providerX + LF.providerW / 2, 38, 'middle', ' lf-caption--soft'),
    liveCaption('request →   ← response', (LF.clientX + LF.clientW + LF.gatewayX) / 2, 56, 'middle', ' lf-caption--soft'),
    liveCaption('sent →   ← answer', (LF.gatewayX + LF.gatewayW + LF.providerX) / 2, 56, 'middle', ' lf-caption--soft')
  );

  const gatewayRect = svgEl('rect', { x: LF.gatewayX, y: 62, width: LF.gatewayW, height: height - 74, rx: 14, class: 'lf-node lf-node--gateway' });
  const gwSub = svgEl('text', { x: LF.gatewayX + LF.gatewayW / 2, y: 96, class: 'lf-label', 'text-anchor': 'middle' }, '');
  const gwRouteHead = svgEl('text', { x: LF.gatewayX + LF.gatewayW / 2, y: 124, class: 'lf-sub', 'text-anchor': 'middle' }, 'last routing');
  const gwRoute = svgEl('text', { x: LF.gatewayX + LF.gatewayW / 2, y: 142, class: 'lf-route', 'text-anchor': 'middle' }, '');
  livePage.gateway = { rect: gatewayRect, sub: gwSub, route: gwRoute };

  livePage.clients.forEach((client, index) => {
    client.y = liveRowY(index);
    client.wire = svgEl('line', { x1: LF.clientX + LF.clientW, y1: client.y, x2: LF.gatewayX, y2: client.y, class: 'lf-wire' });
    wires.append(client.wire);
    client.el = liveNode(nodes, LF.clientX, client.y, LF.clientW, liveClient(client.id));
  });
  livePage.providers.forEach((provider, index) => {
    provider.y = liveRowY(index);
    provider.wire = svgEl('line', { x1: LF.gatewayX + LF.gatewayW, y1: provider.y, x2: LF.providerX, y2: provider.y, class: 'lf-wire' });
    wires.append(provider.wire);
    provider.el = liveNode(nodes, LF.providerX, provider.y, LF.providerW, providerName(provider.id));
  });

  const layer = svgEl('g', { class: 'lf-signals' });
  livePage.signalLayer = layer;
  const waiting = livePage.clients.length
    ? null
    : svgEl('text', { x: LF.clientX + LF.clientW / 2, y: liveRowY(0) + 4, class: 'lf-sub', 'text-anchor': 'middle' }, 'waiting for a request…');
  livePage.svg.replaceChildren(wires, gatewayRect, gwSub, gwRouteHead, gwRoute, labels, nodes, ...(waiting ? [waiting] : []), layer);
  for (const token of livePage.tokens.values()) {
    layer.append(token.dot);
  }
  liveNodeTexts();
}

function liveSetWire(wire, busy, tone) {
  if (!wire) {
    return;
  }
  wire.classList.toggle('is-busy', busy);
  wire.classList.toggle('is-busy--' + tone, busy);
}

function liveNodeTexts() {
  for (const client of livePage.clients) {
    if (client.el) {
      client.el.label.textContent = liveShort(liveClient(client.id), 26);
      client.el.note.textContent = client.active ? client.active + ' request(s) in flight' : 'idle';
      client.el.note2.textContent = NUMBER_FORMAT.format(client.total) + ' request(s) seen';
    }
    liveSetWire(client.wire, client.active > 0, 'req');
  }
  for (const provider of livePage.providers) {
    if (provider.el) {
      provider.el.note.textContent = provider.active ? provider.active + ' request(s) in flight' : 'idle';
      provider.el.note2.textContent = provider.last ? liveShort(provider.last, 32) : 'no traffic yet';
    }
    liveSetWire(provider.wire, provider.active > 0, 'try');
  }
  if (livePage.gateway) {
    livePage.gateway.sub.textContent = livePage.flights.size + ' in flight';
  }
}

function liveClientFor(accountId) {
  let client = livePage.clients.find((entry) => entry.id === accountId);
  if (client) {
    return client;
  }
  client = { id: accountId, y: 0, active: 0, total: 0, last: 0, el: null, wire: null };
  livePage.clients.push(client);
  if (livePage.clients.length > LIVE_MAX_CLIENTS) {
    const idle = livePage.clients.filter((entry) => !entry.active).sort((a, b) => a.last - b.last)[0];
    livePage.clients.splice(livePage.clients.indexOf(idle || livePage.clients[0]), 1);
  }
  liveLayout();
  return client;
}

function liveProviderFor(id) {
  let provider = livePage.providers.find((entry) => entry.id === id);
  if (provider) {
    return provider;
  }
  provider = { id, y: 0, active: 0, last: '', el: null, wire: null };
  livePage.providers.push(provider);
  liveLayout();
  return provider;
}

function liveFlash(target, tone) {
  if (!target || !target.rect) {
    return;
  }
  target.rect.classList.add('is-' + tone);
  setTimeout(() => target.rect.classList.remove('is-' + tone), 900);
}

/* -- the request dots ------------------------------------------------------- */

/* One dot per request. It moves along waypoints, and when it stops where it
   waits for something (the provider's answer, the end of a stream) it pulses. */
function liveToken(rid, at, tone) {
  let token = livePage.tokens.get(rid);
  if (!token) {
    const dot = svgEl('circle', { r: 9, cx: at.x, cy: at.y, class: 'lf-token lf-token--' + tone });
    if (livePage.signalLayer) {
      livePage.signalLayer.append(dot);
    }
    token = { dot, x: at.x, y: at.y, legs: [], leg: null, tone, wait: false };
    livePage.tokens.set(rid, token);
  }
  return token;
}

function liveTone(token, tone) {
  token.dot.setAttribute('class', 'lf-token lf-token--' + tone + (token.wait ? ' is-waiting' : ''));
  token.tone = tone;
}

/* Queue a move through ``points``; ``wait`` makes the dot pulse once it arrives,
   ``remove`` takes it off the diagram there. */
function liveMove(rid, from, points, tone, { wait = false, remove = false } = {}) {
  const token = liveToken(rid, from, tone);
  const factor = livePage.slow ? LIVE_SLOW : 1;
  /* Behind on the real events? Move faster until caught up. */
  const hurry = token.legs.length >= 3 && !livePage.slow ? 0.4 : 1;
  const queued = performance.now();
  points.forEach((point, index) => {
    const last = index === points.length - 1;
    token.legs.push({ to: point, ms: LIVE_LEG_MS * factor * hurry, queued, last, tone, wait: last && wait, remove: last && remove });
  });
  if (liveReducedMotion()) {
    liveFinishNow(rid, token);
    return;
  }
  liveSchedule();
}

/* The next animation step: the browser's frame if it comes, else the timer. */
function liveSchedule() {
  if (livePage.frame !== null) {
    return;
  }
  const id = ++liveFrameId;
  livePage.frame = id;
  const step = () => {
    if (livePage.frame !== id) {
      return;
    }
    livePage.frame = null;
    liveFrame(performance.now());
  };
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(step);
  }
  setTimeout(step, LIVE_FALLBACK_MS);
}

function liveFinishNow(rid, token) {
  const last = token.legs[token.legs.length - 1];
  if (last) {
    token.x = last.to.x;
    token.y = last.to.y;
    token.dot.setAttribute('cx', String(token.x));
    token.dot.setAttribute('cy', String(token.y));
    token.wait = last.wait;
    liveTone(token, last.tone);
    if (last.remove) {
      liveDropToken(rid);
    }
  }
  token.legs = [];
  token.leg = null;
}

function liveDropToken(rid) {
  const token = livePage.tokens.get(rid);
  if (token) {
    token.dot.remove();
    livePage.tokens.delete(rid);
  }
}

function liveFrame(now) {
  let moving = false;
  for (const [rid, token] of [...livePage.tokens]) {
    if (!token.leg && token.legs.length) {
      const next = token.legs.shift();
      token.leg = { ...next, ms: now - next.queued > LIVE_SNAP_MS ? 0 : next.ms, from: { x: token.x, y: token.y }, start: now };
      /* The colour of a move is its own, from the moment it starts. */
      token.wait = false;
      liveTone(token, token.leg.tone);
    }
    if (!token.leg) {
      continue;
    }
    const leg = token.leg;
    const progress = leg.ms > 0 ? Math.min(1, (now - leg.start) / leg.ms) : 1;
    const eased = progress < 0.5 ? 2 * progress * progress : 1 - Math.pow(-2 * progress + 2, 2) / 2;
    token.x = leg.from.x + (leg.to.x - leg.from.x) * eased;
    token.y = leg.from.y + (leg.to.y - leg.from.y) * eased;
    token.dot.setAttribute('cx', String(token.x));
    token.dot.setAttribute('cy', String(token.y));
    if (progress >= 1) {
      token.leg = null;
      if (leg.last || !token.legs.length) {
        token.wait = leg.wait;
        liveTone(token, leg.tone);
        if (leg.remove) {
          liveDropToken(rid);
          continue;
        }
      }
    }
    moving = true;
  }
  if (moving || [...livePage.tokens.values()].some((token) => token.legs.length)) {
    liveSchedule();
  }
}

/* -- calls ------------------------------------------------------------------ */

function liveNewCall(rid) {
  return {
    rid,
    t0: 0,
    t1: null,
    account_id: null,
    requested: '',
    model: '',
    routed_by: null,
    stream: false,
    tools: false,
    status_code: null,
    outcome: null,
    latency_ms: null,
    ttft_ms: null,
    prompt_tokens: null,
    completion_tokens: null,
    attempts: [],
    hops: [],
    done: false,
  };
}

/* Keep the calls in memory bounded: drop the oldest finished ones past the cap. */
function liveTrimCalls() {
  if (livePage.calls.size <= LIVE_CALLS_KEEP + LIVE_CALLS_SLACK) {
    return;
  }
  const finished = [...livePage.calls.values()].filter((call) => call.done).sort((a, b) => a.t0 - b.t0);
  for (const call of finished.slice(0, livePage.calls.size - LIVE_CALLS_KEEP)) {
    livePage.calls.delete(call.rid);
  }
}

/* A record of the call history becomes a finished call. */
function liveCallFromRecord(record) {
  return { ...liveNewCall(record.rid), ...record, done: true };
}

/* Keep what an event says about a call. A call that came complete from the
   history is not touched again by the events of the same request. */
function liveCallUpdate(ev) {
  let call = livePage.calls.get(ev.rid);
  if (call && call.done) {
    return call;
  }
  if (!call) {
    call = liveNewCall(ev.rid);
    call.t0 = ev.t;
    livePage.calls.set(ev.rid, call);
    liveTrimCalls();
  }
  if (ev.kind === 'request') {
    Object.assign(call, { t0: ev.t, account_id: ev.account_id, requested: ev.requested, model: ev.model, routed_by: ev.routed_by, stream: ev.stream, tools: ev.tools });
  } else if (ev.kind === 'attempt') {
    call.model = ev.model;
    call.attempts.push({ key_id: ev.key_id, provider: ev.provider, model: ev.model, ok: null, status_code: null, classification: null, ms: null });
  } else if (ev.kind === 'result') {
    const open = [...call.attempts].reverse().find((attempt) => attempt.ok === null && attempt.key_id === ev.key_id && attempt.model === ev.model);
    if (open) {
      Object.assign(open, { ok: ev.ok, status_code: ev.status_code, classification: ev.classification, ms: ev.ms });
    }
  } else if (ev.kind === 'hop') {
    call.hops.push({ from: ev.from, to: ev.to, cause: ev.cause });
  } else if (ev.kind === 'done') {
    Object.assign(call, {
      done: true,
      t1: ev.t,
      model: ev.model,
      status_code: ev.status_code,
      outcome: ev.outcome,
      latency_ms: ev.latency_ms,
      ttft_ms: ev.ttft_ms,
      prompt_tokens: ev.prompt_tokens,
      completion_tokens: ev.completion_tokens,
    });
    call.stream = ev.stream;
    if (!call.t0) {
      call.t0 = ev.t - (ev.latency_ms || 0);
    }
  }
  return call;
}

/* -- applying events -------------------------------------------------------- */

function liveGatewayEdge(client) {
  return { x: LF.gatewayX, y: client ? client.y : liveRowY(0) };
}

function liveApply(ev, animate) {
  livePage.seen += 1;
  const call = liveCallUpdate(ev);
  let flight = livePage.flights.get(ev.rid);
  if (!livePage.gateway) {
    liveLayout();
  }

  if (ev.kind === 'request') {
    const client = liveClientFor(ev.account_id);
    client.active += 1;
    client.total += 1;
    client.last = ev.t;
    flight = { rid: ev.rid, account_id: ev.account_id, requested: ev.requested, model: ev.model, started: ev.t, stage: 'gateway', provider: null, attempts: 0, last: ev.t };
    livePage.flights.set(ev.rid, flight);
    /* Adding a client redraws the diagram, so read the gateway node after it. */
    livePage.gateway.route.textContent = liveShort(ev.routed_by === 'supermode' ? 'supermode → ' + ev.model : ev.requested, 30);
    if (animate) {
      liveMove(ev.rid, { x: LF.clientX + LF.clientW, y: client.y }, [liveGatewayEdge(client)], 'req');
      liveFlash(client.el, 'hot');
      liveFlash(livePage.gateway, 'hot');
    }
  } else if (flight) {
    flight.last = ev.t;
    const client = livePage.clients.find((entry) => entry.id === flight.account_id);
    if (ev.kind === 'attempt') {
      const provider = liveProviderFor(ev.provider);
      if (flight.provider && flight.provider !== ev.provider) {
        const previous = livePage.providers.find((entry) => entry.id === flight.provider);
        if (previous && previous.active) {
          previous.active -= 1;
        }
      }
      if (flight.provider !== ev.provider) {
        provider.active += 1;
      }
      flight.provider = ev.provider;
      flight.model = ev.model;
      flight.attempts = ev.n;
      flight.stage = 'provider';
      provider.last = liveKey(ev.key_id) + ' · ' + ev.model;
      if (animate) {
        liveMove(ev.rid, liveGatewayEdge(client), [{ x: LF.gatewayX + LF.gatewayW, y: provider.y }, { x: LF.providerX, y: provider.y }], 'try', { wait: true });
        liveFlash(provider.el, 'hot');
      }
    } else if (ev.kind === 'result') {
      const provider = liveProviderFor(ev.provider);
      flight.stage = ev.ok ? 'returning' : 'retrying';
      if (animate) {
        liveMove(ev.rid, { x: LF.providerX, y: provider.y }, [{ x: LF.gatewayX + LF.gatewayW, y: provider.y }, liveGatewayEdge(client)], ev.ok ? 'ok' : 'err', { wait: ev.ok });
        liveFlash(provider.el, ev.ok ? 'good' : 'bad');
      }
    } else if (ev.kind === 'hop' && animate) {
      liveFlash(livePage.gateway, 'warn');
    } else if (ev.kind === 'done') {
      const provider = livePage.providers.find((entry) => entry.id === flight.provider);
      if (client && client.active) {
        client.active -= 1;
      }
      if (provider && provider.active) {
        provider.active -= 1;
      }
      livePage.flights.delete(ev.rid);
      if (animate && client) {
        liveMove(ev.rid, liveGatewayEdge(client), [{ x: LF.clientX + LF.clientW, y: client.y }], liveOk(ev.status_code) ? 'ok' : 'err', { remove: true });
        liveFlash(client.el, liveOk(ev.status_code) ? 'good' : 'bad');
      } else {
        liveDropToken(ev.rid);
      }
    }
  } else if (ev.kind === 'done') {
    liveDropToken(ev.rid);
  }

  const [step, tone, text] = liveDescribe(ev, call.account_id);
  livePage.feed.unshift({ ev, step, tone, text });
  livePage.feed.length = Math.min(livePage.feed.length, LIVE_FEED_KEEP);
  livePage.dirty = true;
}

function liveIngest(events, animate) {
  if (!events.length) {
    return;
  }
  events.sort((a, b) => a.t - b.t || (a.id < b.id ? -1 : 1));
  const base = events[0].t;
  for (const ev of events) {
    const delay = animate ? Math.min(ev.t - base, LIVE_REPLAY_MAX_MS) : 0;
    if (delay > 0) {
      setTimeout(() => liveApplyAndDraw(ev, true), delay);
    } else {
      liveApplyAndDraw(ev, animate);
    }
  }
}

function liveApplyAndDraw(ev, animate) {
  if (!liveActive()) {
    return;
  }
  /* Paused: the state keeps up, the screen stays as it is until Resume. */
  liveApply(ev, animate && !livePage.paused);
  if (!livePage.paused) {
    liveNodeTexts();
    liveDrawNow();
  }
}

/* -- text panels ------------------------------------------------------------ */

function liveStage(flight) {
  if (flight.stage === 'provider') {
    return 'waiting for ' + providerName(flight.provider) + ' (' + flight.model + ')';
  }
  if (flight.stage === 'retrying') {
    return 'refused, trying another';
  }
  if (flight.stage === 'returning') {
    return 'answer back, going to the client';
  }
  return 'at the gateway';
}

/* The last few steps in big, plain sentences, right under the diagram. */
function liveDrawNow() {
  const rows = livePage.feed.slice(0, LIVE_NOW_ROWS);
  if (!rows.length) {
    livePage.nowBox.replaceChildren(el('p', 'note', 'Nothing yet. When a client calls the gateway, each step is written here as it happens.'));
    return;
  }
  const list = el('ol', 'lf-now');
  rows.forEach((row, index) => {
    const item = el('li', 'lf-now-row' + (index === 0 ? ' is-newest' : ''));
    item.append(el('span', 'lf-now-time', liveClock(row.ev.t)), el('span', 'badge badge--' + row.tone, row.step), el('span', 'lf-now-text', row.text));
    list.append(item);
  });
  livePage.nowBox.replaceChildren(list);
}

function liveDrawFlights() {
  const now = Date.now();
  let dropped = false;
  for (const [rid, flight] of livePage.flights) {
    if (now - flight.last > LIVE_STALE_MS) {
      /* No news for minutes: give back what it held, or a node would say "in flight" for good. */
      const client = livePage.clients.find((entry) => entry.id === flight.account_id);
      const provider = livePage.providers.find((entry) => entry.id === flight.provider);
      if (client && client.active) {
        client.active -= 1;
      }
      if (provider && provider.active) {
        provider.active -= 1;
      }
      livePage.flights.delete(rid);
      liveDropToken(rid);
      dropped = true;
    }
  }
  if (dropped) {
    liveNodeTexts();
  }
  const rows = [...livePage.flights.values()].sort((a, b) => a.started - b.started);
  livePage.flightsBox.replaceChildren(
    dataTable(
      [
        { label: 'Client', cell: (row) => liveClient(row.account_id) },
        { label: 'Right now', cell: (row) => liveStage(row) },
        { label: 'Open for', cell: (row) => fmtMs(now - row.started) },
      ],
      rows,
      'Nothing in flight. Send a request through the gateway and watch it travel.'
    )
  );
}

function liveDrawStats() {
  const now = Date.now();
  const calls = [...livePage.calls.values()];
  const finished = calls.filter((call) => call.done).sort((a, b) => (b.t1 || 0) - (a.t1 || 0)).slice(0, 50);
  const good = finished.filter((call) => liveOk(call.status_code));
  const latencies = good.map((call) => call.latency_ms);
  const average = latencies.length ? latencies.reduce((a, b) => a + b, 0) / latencies.length : null;
  const perMinute = calls.filter((call) => now - call.t0 < 60000).length;
  const cards = el('div', 'summary lf-summary');
  cards.append(
    kpiCard('In flight', String(livePage.flights.size), 'requests being served now'),
    kpiCard('Last minute', String(perMinute), 'requests started'),
    kpiCard('Answered', finished.length ? fmtPct(good.length / finished.length, 0) : '—', 'of the last ' + finished.length + ' finished'),
    kpiCard('Average time', fmtMs(average), 'answered, last ' + good.length)
  );
  livePage.stats.replaceChildren(cards);
  livePage.panel.report(
    livePage.paused
      ? 'Paused. Resume to catch up.'
      : 'Live. ' + NUMBER_FORMAT.format(livePage.calls.size) + ' call(s) in the history, ' + NUMBER_FORMAT.format(livePage.seen) + ' step(s) seen since you opened this page.',
    'ok'
  );
}

/* -- loading and polling ---------------------------------------------------- */

async function liveLoadKeys() {
  livePage.keyRecords = await apiFetch(POOL_KEYS_PATH);
  livePage.keys = {};
  for (const key of livePage.keyRecords) {
    livePage.keys[key.id] = keyName(key);
  }
  livePage.keysAt = Date.now();
}

async function liveLoadHistory() {
  const body = await apiFetch(CALLS_PATH + '?limit=' + LIVE_HISTORY_LOAD);
  for (const record of body.data) {
    const known = livePage.calls.get(record.rid);
    if (!known || !known.done) {
      livePage.calls.set(record.rid, liveCallFromRecord(record));
    }
  }
  liveTrimCalls();
  livePage.dirty = true;
}

async function livePoll() {
  if (!liveActive()) {
    liveStop();
    return;
  }
  /* Even paused, keep polling: it keeps the gateway recording and the cursor current. */
  if (document.hidden) {
    return;
  }
  try {
    const gap = livePage.lastPollAt && Date.now() - livePage.lastPollAt > LIVE_GAP_MS;
    if (gap) {
      await liveLoadHistory();
    }
    const query = livePage.cursor ? '?after=' + encodeURIComponent(livePage.cursor) : '?limit=100';
    const data = await apiFetch(LIVE_PATH + query);
    if (!liveActive()) {
      return;
    }
    livePage.lastPollAt = Date.now();
    const history = livePage.firstBatch || gap;
    livePage.firstBatch = false;
    if (data.cursor) {
      livePage.cursor = data.cursor;
    }
    liveIngest(data.events, !history);
  } catch (err) {
    if (err instanceof SignedOut) {
      liveStop();
    } else {
      livePage.panel.report(err.message, 'error');
    }
  }
}

function liveTick() {
  if (!liveActive()) {
    liveStop();
    return;
  }
  if (livePage.paused) {
    livePage.panel.report('Paused. The page is frozen; steps keep being recorded and show on Resume.', 'ok');
    return;
  }
  liveDrawFlights();
  liveDrawStats();
  /* Running calls show how long they have taken: refresh the table now and then. */
  if (livePage.flights.size && Date.now() - liveHist.drawnAt > 2000) {
    livePage.dirty = true;
  }
  if (livePage.dirty) {
    livePage.dirty = false;
    liveDrawHistory();
  }
  if (Date.now() - livePage.keysAt > LIVE_KEYS_REFRESH_MS) {
    livePage.keysAt = Date.now();
    liveLoadKeys().then(
      () => {
        livePage.dirty = true;
      },
      () => {}
    );
  }
}

function liveReset() {
  for (const rid of [...livePage.tokens.keys()]) {
    liveDropToken(rid);
  }
  livePage.flights.clear();
  livePage.feed = [];
  livePage.seen = 0;
  for (const client of livePage.clients) {
    client.active = 0;
  }
  for (const provider of livePage.providers) {
    provider.active = 0;
  }
  liveNodeTexts();
  liveDrawNow();
  livePage.dirty = true;
  liveTick();
}

async function liveStart() {
  try {
    await Promise.all([liveLoadKeys(), loadAccountNames().catch(() => null)]);
    await liveLoadHistory();
  } catch (err) {
    if (err instanceof SignedOut) {
      return;
    }
    livePage.panel.report(err.message, 'error');
  }
  const seen = new Set(livePage.keyRecords.filter((key) => key.state !== 'suspended').map((key) => key.provider));
  livePage.providers = [...seen].sort().map((id) => ({ id, y: 0, active: 0, last: '', el: null, wire: null }));
  liveLayout();
  liveReset();
  await livePoll();
  livePage.pollTimer = setInterval(livePoll, LIVE_POLL_MS);
  livePage.tickTimer = setInterval(liveTick, 500);
}

async function liveReloadHistory() {
  livePage.panel.report('Reloading the history…', 'pending');
  try {
    await Promise.all([liveLoadKeys(), liveLoadHistory()]);
  } catch (err) {
    if (!(err instanceof SignedOut)) {
      livePage.panel.report(err.message, 'error');
    }
    return;
  }
  liveTick();
}

function liveLegend() {
  const legend = el('div', 'chart-legend lf-legend');
  for (const [tone, label] of [
    ['req', 'on its way to the gateway'],
    ['try', 'sent to a provider, waiting (pulses)'],
    ['ok', 'answer back (pulses while a stream runs)'],
    ['err', 'refused or failed'],
  ]) {
    const item = el('span', 'legend-item');
    item.append(el('span', 'lf-key lf-key--' + tone), document.createTextNode(label));
    legend.append(item);
  }
  return legend;
}

function renderLive(view) {
  liveStop();
  for (const rid of [...livePage.tokens.keys()]) {
    liveDropToken(rid);
  }
  Object.assign(livePage, {
    cursor: null,
    paused: false,
    firstBatch: true,
    clients: [],
    providers: [],
    tokens: new Map(),
    flights: new Map(),
    calls: new Map(),
    feed: [],
    seen: 0,
    frame: null,
    dirty: true,
    lastPollAt: 0,
  });
  livePage.panel = createStatus();
  livePage.root = el('div', 'stack');
  livePage.svg = svgEl('svg', { viewBox: '0 0 ' + LF.width + ' 300', class: 'lf-diagram', role: 'img', 'aria-label': 'Requests flowing from clients through the gateway to providers and back' });
  livePage.stats = el('div');
  livePage.nowBox = el('div');
  livePage.flightsBox = el('div');
  livePage.pauseButton = button('Pause', 'ghost', () => {
    livePage.paused = !livePage.paused;
    livePage.pauseButton.textContent = livePage.paused ? 'Resume' : 'Pause';
    if (!livePage.paused) {
      /* What piled up while paused is shown at once, not replayed. */
      for (const rid of [...livePage.tokens.keys()]) {
        liveDropToken(rid);
      }
      liveNodeTexts();
      liveDrawNow();
      livePage.dirty = true;
    }
    liveTick();
  });
  livePage.slowButton = button('Slow motion: off', 'ghost', () => {
    livePage.slow = !livePage.slow;
    livePage.slowButton.textContent = 'Slow motion: ' + (livePage.slow ? 'on' : 'off');
  });

  const split = el('div', 'lf-split');
  split.append(panelSection('What is happening now', livePage.nowBox), panelSection('In flight', livePage.flightsBox));
  livePage.root.append(
    livePage.stats,
    panelSection(
      'Live flow',
      el('p', 'note', 'Follow one dot: it leaves a client, enters the gateway, goes to a provider and model, waits for the answer, comes back and returns to the client. A wire glows while a request is on it.'),
      livePage.svg,
      liveLegend()
    ),
    split,
    liveHistoryPanel()
  );
  mountPage(view, {
    title: 'Live Flow',
    description: 'Watch requests travel from a client through the gateway to a provider and model, and the answer come back, as it happens.',
    status: livePage.panel,
    actions: [livePage.slowButton, livePage.pauseButton, refreshButton(() => liveReloadHistory(), 'Reload history')],
    content: [livePage.root],
  });
  liveDrawNow();
  liveStart();
}
