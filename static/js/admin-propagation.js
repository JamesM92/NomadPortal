(function () {
  var csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';
  var $ = function (id) { return document.getElementById(id); };
  var dirty = false;

  function size(b) {
    if (b == null) return '—';
    if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
    if (b >= 1e6) return (b / 1e6).toFixed(1) + ' MB';
    if (b >= 1e3) return (b / 1e3).toFixed(1) + ' KB';
    return b + ' B';
  }
  function dur(s) {
    if (s >= 86400) return Math.floor(s / 86400) + ' d ' + Math.floor(s % 86400 / 3600) + ' h';
    if (s >= 3600) return Math.floor(s / 3600) + ' h ' + Math.floor(s % 3600 / 60) + ' m';
    return Math.floor(s / 60) + ' m';
  }
  function box(label, value) {
    var d = document.createElement('div'); d.className = 'stat-box';
    var l = document.createElement('div'); l.className = 'stat-label'; l.textContent = label;
    var v = document.createElement('div'); v.className = 'stat-value'; v.textContent = value;
    d.appendChild(l); d.appendChild(v); return d;
  }
  function fill(s) {
    var c = s.settings;
    $('pn-enabled').checked = c.enabled; $('pn-name').value = c.name;
    $('pn-storage').value = c.storage_mb; $('pn-limit').value = c.transfer_limit_kb;
    $('pn-stamp').value = c.stamp_cost; $('pn-autopeer').checked = c.autopeer;
    $('pn-maxpeers').value = c.max_peers; $('pn-depth').value = c.autopeer_maxdepth;
    $('pn-static').value = c.static_peers.join('\n'); $('pn-onlystatic').checked = c.only_static;
  }
  function show(s) {
    var err = $('pn-error');
    err.hidden = !s.error; err.textContent = s.error ? 'The node could not start: ' + s.error : '';
    $('pn-restart').hidden = !s.restart_required;
    var st = $('pn-stats'); st.textContent = '';
    st.appendChild(box('State', { running: 'Running', off: 'Off', error: 'Error' }[s.state] || s.state));
    if (s.state === 'running') {
      st.appendChild(box('Address', s.address || '—'));
      st.appendChild(box('Up for', dur(s.uptime_s)));
      st.appendChild(box('Messages held', s.messages));
      st.appendChild(box('Storage', size(s.store_bytes) + ' of ' + size(s.store_limit_bytes)));
      st.appendChild(box('Peers', s.peers.length));
    }
    var tb = $('pn-peers').querySelector('tbody'); tb.textContent = '';
    s.peers.forEach(function (p) {
      var tr = document.createElement('tr');
      [p.name || p.id.slice(0, 12), p.type, p.alive ? 'reachable' : 'not reachable',
       p.incoming + ' in', p.outgoing + ' out'].forEach(function (t) {
        var td = document.createElement('td'); td.textContent = t; tr.appendChild(td);
      });
      tb.appendChild(tr);
    });
    $('pn-nopeers').hidden = s.peers.length > 0;
    if (!dirty) fill(s);
  }
  async function call(url, method, body) {
    var res = await fetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
      body: body ? JSON.stringify(body) : undefined,
    });
    return { ok: res.ok, status: res.status, data: await res.json().catch(function () { return {}; }) };
  }
  async function refresh() {
    var r = await call('/admin/api/propagation', 'GET');
    if (r.ok) show(r.data);
  }
  document.getElementById('pn-form').addEventListener('input', function () { dirty = true; });
  document.getElementById('pn-form').addEventListener('submit', async function (e) {
    e.preventDefault();
    var body = {
      enabled: $('pn-enabled').checked, name: $('pn-name').value,
      storage_mb: $('pn-storage').value, transfer_limit_kb: $('pn-limit').value,
      stamp_cost: $('pn-stamp').value, autopeer: $('pn-autopeer').checked,
      max_peers: $('pn-maxpeers').value, autopeer_maxdepth: $('pn-depth').value,
      static_peers: $('pn-static').value.split(/\s+/).filter(Boolean),
      only_static: $('pn-onlystatic').checked,
    };
    var r = await call('/admin/api/propagation', 'POST', body);
    if (r.data.errors && r.data.errors.length) {
      $('pn-msg').textContent = r.data.errors.join(' ');
    } else {
      $('pn-msg').textContent = 'Saved.'; dirty = false;
    }
    if (r.data.status) show(r.data.status);
  });
  $('pn-announce').addEventListener('click', async function () {
    var r = await call('/admin/api/propagation/announce', 'POST');
    $('pn-msg').textContent = r.ok ? 'Announced.' : (r.data.error || 'Could not announce.');
  });
  refresh();
  setInterval(refresh, 15000);
})();
