(() => {
  'use strict';
  const state = { data: null, events: [], filter: 'all', selected: null, controller: null, timer: null };
  const $ = (id) => document.getElementById(id);
  const text = (node, value) => { node.textContent = value == null || value === '' ? '–' : String(value); };
  const esc = (value) => value == null ? '–' : String(value);
  const compact = (value) => Number(value || 0).toLocaleString('pt-BR');
  const ago = (iso) => {
    if (!iso) return 'sem registro';
    const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (seconds < 5) return 'agora';
    if (seconds < 60) return `há ${seconds}s`;
    if (seconds < 3600) return `há ${Math.floor(seconds / 60)}min`;
    return `há ${Math.floor(seconds / 3600)}h`;
  };
  const time = (iso) => iso ? new Date(iso).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '–';
  const stateClass = (value) => {
    const v = String(value || '').toLowerCase();
    if (['healthy', 'connected', 'ready', 'connected', 'running'].some(x => v.includes(x))) return 'ok';
    if (['degraded', 'connecting', 'reconnecting', 'starting', 'unknown'].some(x => v.includes(x))) return 'warn';
    if (['failed', 'unhealthy', 'disconnected', 'stopped', 'disabled'].some(x => v.includes(x))) return v === 'disabled' ? '' : 'bad';
    return '';
  };
  const humanState = (value) => ({ healthy: 'saudável', degraded: 'degradado', unhealthy: 'indisponível', connected: 'conectado', disconnected: 'sem conexão', reconnecting: 'reconectando', connecting: 'conectando', ready: 'pronto', disabled: 'desabilitado', stopped: 'parado', starting: 'iniciando', failed: 'falhou', unknown: 'sem confirmação' }[String(value || '').toLowerCase()] || value || 'sem dados');
  const setPill = (id, value) => { const node = $(id); text(node, humanState(value)); node.className = `state-pill ${stateClass(value)}`; };
  const dl = (id, rows) => { const node = $(id); node.replaceChildren(); rows.forEach(([label, value]) => { const dt = document.createElement('dt'); const dd = document.createElement('dd'); text(dt, label); text(dd, value); node.append(dt, dd); }); };

  function renderStatus(data) {
    const snap = data.snapshot || {};
    const status = snap.status || 'unknown';
    const health = snap.health || {};
    const system = $('system-status');
    text(system, humanState(status));
    text($('system-detail'), status === 'healthy' ? 'Pipeline respondendo dentro do contrato.' : status === 'degraded' ? 'Há componentes que precisam de atenção.' : 'O backend não confirmou saúde suficiente.');
    $('system-orb').className = `status-orb ${stateClass(status)}`;
    const env = health.EventProcessor?.state || data.capabilities?.roblox_bridge || 'local';
    text($('mode-label'), `modo ${data.integrations?.tiktok?.configured ? 'live configurado' : 'sem TikTok ativo'}`);
    text($('capability-label'), `${Object.values(data.capabilities || {}).filter(v => v === 'ready').length} integrações prontas`);
    const throughput = snap.throughput || {};
    const received = Number(throughput.received || 0);
    text($('events-rate'), received ? compact(received) : '0');
    text($('events-rate-note'), received ? 'total recebido nesta execução' : 'nenhum evento nesta execução');
    const queue = snap.queue || {};
    const bridge = data.integrations?.roblox || {};
    const depth = Number(queue.depth_total || 0);
    const capacity = Number(bridge.buffer_capacity || 0) || 0;
    text($('queue-depth'), compact(depth));
    const utilization = capacity ? Math.min(100, (depth / capacity) * 100) : 0;
    $('queue-meter').style.width = `${utilization}%`;
    $('queue-meter').className = utilization >= 90 ? 'bad' : utilization >= 70 ? 'warn' : '';
    text($('queue-note'), capacity ? `${utilization.toFixed(0)}% do buffer Roblox` : 'capacidade não informada');
    const latency = snap.latency?.end_to_end || {};
    text($('latency-p95'), latency.p95_ms == null ? '–' : `${Number(latency.p95_ms).toFixed(1)}ms`);
    text($('latency-note'), latency.count ? `${compact(latency.count)} amostras · média ${Number(latency.avg_ms || 0).toFixed(1)}ms` : 'sem amostra suficiente');
    text($('environment-label'), health.EventProcessor ? 'LOCAL' : 'AGUARDANDO');
  }

  function renderIntegrations(data) {
    const i = data.integrations || {};
    const tt = i.tiktok || {};
    setPill('tiktok-state', tt.state || 'disabled');
    text($('tiktok-target'), tt.unique_id ? `@${tt.unique_id}` : 'alvo não configurado');
    dl('tiktok-details', [['estado', humanState(tt.state)], ['buffer', compact(tt.buffer_depth)], ['recebidos', compact(tt.metrics?.events_received)], ['última conexão', ago(tt.metrics?.last_successful_connection)]]);
    const rb = i.roblox || {};
    const rbState = rb.last_poll_at ? 'connected' : 'unknown';
    setPill('roblox-state', rbState);
    dl('roblox-details', [['último poll', ago(rb.last_poll_at)], ['pendentes', compact(rb.buffer_depth)], ['entregues', compact(rb.events_delivered_total)], ['evictados', compact(rb.events_evicted_total)]]);
    const obs = i.obs || {};
    setPill('obs-state', obs.status || 'disabled');
    text($('obs-scene'), obs.current_scene ? `cena: ${obs.current_scene}` : obs.enabled ? 'cena não confirmada' : 'integração desligada');
    dl('obs-details', [['conexão', humanState(obs.connection_state || obs.status)], ['fila', compact(obs.queue_depth)], ['falhas', compact(obs.metrics?.obs_request_failures_total)], ['latência', obs.last_request_latency_ms != null ? `${obs.last_request_latency_ms}ms` : 'sem dado']]);
    const mq = i.mqtt || {};
    setPill('mqtt-state', mq.status || 'disabled');
    text($('mqtt-broker'), mq.broker ? `${mq.broker.host}:${mq.broker.port}` : 'broker não configurado');
    dl('mqtt-details', [['conexão', humanState(mq.connection_state || mq.status)], ['fila', compact(mq.queue_depth)], ['publicadas', compact(mq.metrics?.mqtt_publish_total)], ['dispositivos vistos', compact((mq.devices || []).length)]]);
  }

  function eventTypeClass(type) { const t = String(type || '').toLowerCase(); return t.includes('gift') ? 'type-gift' : t.includes('comment') ? 'type-comment' : t.includes('follow') ? 'type-follow' : t.includes('command') ? 'type-action' : ''; }
  function renderEvents() {
    const list = $('event-list'); list.replaceChildren();
    const events = state.events.filter(e => state.filter === 'all' || e.event_type === state.filter);
    if (!events.length) { const empty = document.createElement('div'); empty.className = 'empty-state'; text(empty, 'Nenhum evento nesta janela.'); const small = document.createElement('small'); text(small, 'O backend não entregou eventos compatíveis com o filtro.'); empty.append(small); list.append(empty); return; }
    events.slice(-50).reverse().forEach(event => {
      const row = document.createElement('div'); row.className = `event-row${state.selected === event.event_id ? ' selected' : ''}`; row.tabIndex = 0;
      const cells = [time(event.timestamp), event.event_type, event.user?.display_name || event.source || 'sem origem', `P${event.priority ?? '–'}`, event.event_id];
      cells.forEach((value, index) => { const span = document.createElement('span'); text(span, value); if (index === 0) span.className = 'event-time'; if (index === 1) span.className = `event-type ${eventTypeClass(event.event_type)}`; if (index === 2) span.className = 'event-user'; if (index === 3) span.className = 'priority'; if (index === 4) span.className = 'event-id'; row.append(span); });
      const select = () => { state.selected = event.event_id; renderEvents(); renderTrace(event); };
      row.addEventListener('click', select); row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(); } }); list.append(row);
    });
  }
  function renderTrace(event) {
    if (!event) return; text($('trace-title'), event.event_type || 'Evento'); text($('trace-copy'), 'Correlação preservada no envelope; confirmação final só aparece quando a integração fornece essa evidência.'); const node = $('trace-fields'); node.replaceChildren(); [['event_id', event.event_id], ['source', event.source], ['user', event.user?.display_name || 'não informado'], ['sequence', event.sequence_number], ['payload', JSON.stringify(event.payload || {})]].forEach(([key, value]) => { const row = document.createElement('div'); row.className = 'trace-field'; const label = document.createElement('span'); const val = document.createElement('strong'); text(label, key); text(val, value); row.append(label, val); node.append(row); });
  }

  function renderQueue(data) {
    const q = data.snapshot?.queue || {}; const values = q.depth_by_priority || {}; const max = Math.max(1, ...Object.values(values).map(Number)); const node = $('priority-bars'); node.replaceChildren(); [0,1,2,3,4].forEach(p => { const line = document.createElement('div'); line.className = `priority-line p${p}`; const label = document.createElement('span'); text(label, `P${p}`); const track = document.createElement('div'); track.className = 'priority-track'; const bar = document.createElement('span'); bar.style.width = `${Math.min(100, (Number(values[p] || values[String(p)] || 0) / max) * 100)}%`; track.append(bar); const total = document.createElement('strong'); text(total, compact(values[p] || values[String(p)] || 0)); line.append(label, track, total); node.append(line); }); const summary = $('queue-summary'); summary.replaceChildren(); [['drops', q.dropped], ['overflow', q.overflow], ['total', q.depth_total]].forEach(([label, value]) => { const span = document.createElement('span'); text(span, `${label} `); const strong = document.createElement('strong'); text(strong, compact(value)); span.append(strong); summary.append(span); });
  }

  async function loadRules() { const node = $('rules-list'); try { const response = await fetch('/api/dashboard/rules', { cache: 'no-store' }); if (!response.ok) throw new Error('regras indisponíveis'); const data = await response.json(); node.replaceChildren(); if (!data.rules?.length) { text(node, 'Nenhuma regra carregada.'); return; } data.rules.slice(0, 100).forEach(rule => { const row = document.createElement('div'); row.className = 'rule-row'; const main = document.createElement('div'); main.className = 'rule-main'; const title = document.createElement('strong'); text(title, rule.id || 'regra sem id'); const prio = document.createElement('span'); text(prio, rule.priority || 'sem prioridade'); main.append(title, prio); const desc = document.createElement('div'); desc.className = 'rule-desc'; text(desc, `${rule.event_type || 'evento'} → ${(rule.actions || []).map(a => a.type).join(', ') || 'sem ação'}`); const tags = document.createElement('div'); tags.className = 'rule-tags'; [rule.enabled === false ? 'desabilitada' : 'ativa', rule.cooldown ? `cooldown ${rule.cooldown.seconds || 0}s` : 'sem cooldown', rule.stop_processing ? 'encerra avaliação' : 'continua avaliação'].forEach(value => { const tag = document.createElement('span'); text(tag, value); tags.append(tag); }); row.append(main, desc, tags); node.append(row); }); } catch (error) { node.replaceChildren(); const empty = document.createElement('div'); empty.className = 'empty-state'; text(empty, 'Não foi possível carregar as regras.'); node.append(empty); } }

  async function loadLogs() { const node = $('log-list'); const level = encodeURIComponent($('log-level').value); const q = encodeURIComponent($('log-query').value); try { const response = await fetch(`/api/dashboard/logs?limit=100&level=${level}&q=${q}`, { cache: 'no-store' }); if (!response.ok) throw new Error('logs indisponíveis'); const data = await response.json(); node.replaceChildren(); if (!data.logs?.length) { const empty = document.createElement('div'); empty.className = 'empty-state'; text(empty, 'Nenhum log compatível.'); node.append(empty); return; } data.logs.forEach(log => { const row = document.createElement('div'); row.className = `log-row ${String(log.level || '').toLowerCase()}`; [['time', log.timestamp || log.created_at || '–'], ['log-level', log.level || 'INFO'], ['log-component', log.component || log.logger || 'engine'], ['log-message', log.message || JSON.stringify(log)]].forEach(([className, value]) => { const span = document.createElement('span'); span.className = className; text(span, value); row.append(span); }); node.append(row); }); } catch { node.replaceChildren(); const empty = document.createElement('div'); empty.className = 'empty-state'; text(empty, 'Logs indisponíveis no momento.'); node.append(empty); } }

  async function refresh() { if (state.controller) state.controller.abort(); state.controller = new AbortController(); try { const response = await fetch('/api/dashboard/snapshot', { cache: 'no-store', signal: state.controller.signal }); if (!response.ok) throw new Error('backend indisponível'); state.data = await response.json(); state.events = state.data.events || []; renderStatus(state.data); renderIntegrations(state.data); renderEvents(); renderQueue(state.data); const now = new Date(); text($('last-updated'), now.toLocaleTimeString('pt-BR')); $('connection-dot').className = 'dot ok'; $('update-pulse').className = 'pulse ok'; text($('connection-label'), 'API conectada'); } catch (error) { if (error.name === 'AbortError') return; $('connection-dot').className = 'dot bad'; $('update-pulse').className = 'pulse bad'; text($('connection-label'), 'API indisponível'); text($('system-status'), 'Sem conexão'); text($('system-detail'), 'A Local API não respondeu. Verifique se o engine está rodando.'); $('system-orb').className = 'status-orb bad'; } finally { state.controller = null; } }
  function schedule() { clearTimeout(state.timer); state.timer = setTimeout(async () => { await refresh(); schedule(); }, 3000); }
  $('event-filter').addEventListener('change', e => { state.filter = e.target.value; renderEvents(); }); $('refresh-now').addEventListener('click', refresh); $('check-api').addEventListener('click', refresh); $('log-refresh').addEventListener('click', loadLogs); $('log-level').addEventListener('change', loadLogs); $('log-query').addEventListener('keydown', e => { if (e.key === 'Enter') loadLogs(); });
  refresh(); loadRules(); loadLogs(); schedule();
})();
