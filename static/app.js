'use strict';

// ─── Throughput history ──────────────────────────────────────────────────────
const HISTORY_LEN = 60;   // 60 × 2 s = 2-minute window
const dlHistory = new Array(HISTORY_LEN).fill(0);
const ulHistory = new Array(HISTORY_LEN).fill(0);
const labels    = new Array(HISTORY_LEN).fill('');

// ─── Chart.js setup ─────────────────────────────────────────────────────────
const ctx = document.getElementById('throughput-chart').getContext('2d');
const chart = new Chart(ctx, {
  type: 'line',
  data: {
    labels,
    datasets: [
      {
        label: 'Download',
        data: dlHistory,
        borderColor: '#22d3ee',
        backgroundColor: 'rgba(34,211,238,.08)',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.4,
        fill: true,
      },
      {
        label: 'Upload',
        data: ulHistory,
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59,130,246,.08)',
        borderWidth: 2,
        pointRadius: 0,
        tension: 0.4,
        fill: true,
      },
    ],
  },
  options: {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    plugins: {
      legend: { labels: { color: '#94a3b8', font: { size: 12 } } },
      tooltip: {
        callbacks: {
          label: c => ` ${c.dataset.label}: ${fmtBps(c.raw)}`,
        },
      },
    },
    scales: {
      x: { display: false },
      y: {
        min: 0,
        ticks: {
          color: '#64748b',
          callback: v => fmtBps(v),
          maxTicksLimit: 5,
        },
        grid: { color: 'rgba(30,45,71,.8)' },
      },
    },
  },
});

// ─── Formatters ──────────────────────────────────────────────────────────────
function fmtBps(bps) {
  if (bps >= 1e9) return (bps / 1e9).toFixed(2) + ' Gbps';
  if (bps >= 1e6) return (bps / 1e6).toFixed(2) + ' Mbps';
  if (bps >= 1e3) return (bps / 1e3).toFixed(1) + ' Kbps';
  return Math.round(bps) + ' bps';
}

function fmtBytes(b) {
  if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
  if (b >= 1e9)  return (b / 1e9 ).toFixed(1) + ' GB';
  if (b >= 1e6)  return (b / 1e6 ).toFixed(1) + ' MB';
  if (b >= 1e3)  return (b / 1e3 ).toFixed(1) + ' KB';
  return b + ' B';
}

function fmtTime(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}

// ─── DOM helpers ─────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);

function el(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls)  e.className = cls;
  if (html !== undefined) e.innerHTML = html;
  return e;
}

function bwCell(bps, cls) {
  const pct = Math.min(100, (bps / 100e6) * 100);  // 100 Mbps = full bar
  const cell = el('td', 'bw-cell');
  cell.innerHTML = `
    <div class="bw-label">${fmtBps(bps)}</div>
    <div class="bw-bar-bg">
      <div class="bw-bar-fill ${cls}" style="width:${pct.toFixed(1)}%"></div>
    </div>`;
  return cell;
}

function methodBadge(method) {
  const map = {
    nmap:         ['method-nmap',         'nmap'],
    arp:          ['method-arp',          'ARP'],
    arp_cache:    ['method-arp_cache',    'cache'],
    mikrotik_api: ['method-mikrotik_api', 'RouterOS'],
  };
  const [cls, label] = map[method] || ['method-arp_cache', method || '?'];
  return `<span class="method-badge ${cls}">${label}</span>`;
}

// ─── Device table ─────────────────────────────────────────────────────────────
function renderDevices(devices) {
  const tbody = $('device-tbody');
  tbody.innerHTML = '';

  if (!devices || devices.length === 0) {
    const row = tbody.insertRow();
    row.className = 'empty-row';
    const cell = row.insertCell();
    cell.colSpan = 7;
    cell.textContent = 'No devices found. Try scanning again.';
    return;
  }

  devices.sort((a, b) => {
    const ai = a.ip.split('.').map(Number);
    const bi = b.ip.split('.').map(Number);
    for (let i = 0; i < 4; i++) {
      if (ai[i] !== bi[i]) return ai[i] - bi[i];
    }
    return 0;
  });

  for (const dev of devices) {
    const row = tbody.insertRow();

    // Status
    row.insertCell().innerHTML = `<span class="dot dot-online" title="online"></span>`;

    // Hostname / IP
    const hostname = dev.hostname || dev.ip;
    const nameCell = row.insertCell();
    nameCell.innerHTML = `<div class="device-name">${esc(hostname)}</div>
      ${dev.hostname ? `<div class="device-ip">${esc(dev.ip)}</div>` : ''}`;

    // MAC
    row.insertCell().innerHTML = `<span class="mac">${esc(dev.mac || '—')}</span>`;

    // Vendor
    row.insertCell().textContent = dev.vendor || '—';

    // Bandwidth
    row.appendChild(bwCell(dev.download_bps || 0, 'dl'));
    row.appendChild(bwCell(dev.upload_bps   || 0, 'ul'));

    // Source
    const methodCell = row.insertCell();
    methodCell.innerHTML = methodBadge(dev.scan_method);
  }
}

// ─── Hardware port panels ────────────────────────────────────────────────────

/** Shorten RouterOS interface names for display. */
function shortName(name) {
  return name
    .replace('sfp-sfpplus', 'SFP+')
    .replace('ether', 'e')
    .replace('bridge', 'br')
    .replace('vlan', 'vl');
}

function renderPorts(containerId, ports) {
  const container = $(containerId);
  container.innerHTML = '';

  for (const port of ports) {
    const up = port.running;
    const card = el('div', `port-card ${up ? 'port-up' : 'port-down'}`);

    const dlTxt = up ? fmtBps(port.rx_bps) : '—';
    const ulTxt = up ? fmtBps(port.tx_bps) : '—';
    const tip   = [
      port.name,
      port.comment ? `(${port.comment})` : '',
      up ? `↓ ${dlTxt}  ↑ ${ulTxt}` : 'link down',
      `rx: ${fmtBytes(port.rx_bytes)}  tx: ${fmtBytes(port.tx_bytes)}`,
    ].filter(Boolean).join('\n');

    card.title = tip;
    card.innerHTML = `
      <div class="port-led"></div>
      <div class="port-name">${esc(shortName(port.name))}</div>
      ${port.comment ? `<div class="port-comment">${esc(port.comment)}</div>` : ''}
      ${up ? `<div class="port-bw">
        <div class="port-bw-dl">↓ ${dlTxt}</div>
        <div class="port-bw-ul">↑ ${ulTxt}</div>
      </div>` : ''}`;

    container.appendChild(card);
  }
}

function renderPortPanels(ports) {
  if (!ports) return;

  const routerPorts = ports.router || [];
  const switchPorts = ports.switch || [];

  const hasRouterPorts = routerPorts.length > 0;
  const hasSwitchPorts = switchPorts.length > 0;

  $('ports-section').style.display  = (hasRouterPorts || hasSwitchPorts) ? '' : 'none';
  $('router-panel').style.display   = hasRouterPorts ? '' : 'none';
  $('switch-panel').style.display   = hasSwitchPorts ? '' : 'none';

  if (hasRouterPorts) renderPorts('router-ports', routerPorts);
  if (hasSwitchPorts) renderPorts('switch-ports', switchPorts);
}

// ─── Summary stats ───────────────────────────────────────────────────────────
function renderSummary(data) {
  $('stat-devices').textContent = data.devices ? data.devices.length : '—';
  $('stat-dl').textContent = fmtBps(data.total_download_bps || 0);
  $('stat-ul').textContent = fmtBps(data.total_upload_bps   || 0);

  const src = data.source;
  if (src === 'mikrotik') {
    $('stat-source').textContent = 'RouterOS';
    $('stat-source').style.color = 'var(--accent2)';
  } else {
    $('stat-source').textContent = 'Local scan';
    $('stat-source').style.color = 'var(--muted)';
  }
}

// ─── Main data handler ───────────────────────────────────────────────────────
function handleData(data) {
  renderSummary(data);

  dlHistory.push(data.total_download_bps || 0);
  dlHistory.shift();
  ulHistory.push(data.total_upload_bps   || 0);
  ulHistory.shift();
  chart.update('none');

  renderDevices(data.devices || []);
  renderPortPanels(data.ports);
}

// ─── XSS helper ──────────────────────────────────────────────────────────────
function esc(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ─── WebSocket ───────────────────────────────────────────────────────────────
let ws;
let reconnectDelay = 1000;

function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    $('ws-status').textContent = 'Live';
    $('ws-status').className = 'badge badge-online';
    reconnectDelay = 1000;
  };

  ws.onmessage = evt => {
    try { handleData(JSON.parse(evt.data)); }
    catch (e) { console.error('WS parse error:', e); }
  };

  ws.onclose = ws.onerror = () => {
    $('ws-status').textContent = 'Reconnecting…';
    $('ws-status').className = 'badge badge-offline';
    setTimeout(() => {
      reconnectDelay = Math.min(reconnectDelay * 2, 30_000);
      connect();
    }, reconnectDelay);
  };
}

// ─── Manual scan ─────────────────────────────────────────────────────────────
async function triggerScan() {
  const btn = $('btn-scan');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  try {
    const res = await fetch('/api/scan', { method: 'POST' });
    if (!res.ok) throw new Error(res.statusText);
  } catch (e) {
    console.error('Scan failed:', e);
  } finally {
    btn.disabled = false;
    btn.innerHTML = `
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/>
        <path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15"/>
      </svg>
      Scan Now`;
  }
}

// ─── Boot ─────────────────────────────────────────────────────────────────────
connect();
