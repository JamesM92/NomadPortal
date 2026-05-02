var RNODE_PRESETS = {
  eu868: { freq: 867500000, bw: 125000, sf: 8, cr: 5, pwr: 7,  port: '/dev/ttyUSB0' },
  us915: { freq: 915000000, bw: 125000, sf: 8, cr: 5, pwr: 17, port: '/dev/ttyUSB0' },
  au915: { freq: 915000000, bw: 125000, sf: 8, cr: 5, pwr: 17, port: '/dev/ttyUSB0' },
};

function removeRow(btn) {
  btn.closest('tr').remove();
  renumber();
}

function renumber() {
  [
    ['tcp-client-body', 'tcp_client_enabled'],
    ['tcp-server-body', 'tcp_server_enabled'],
    ['udp-body',        'udp_enabled'],
    ['rnode-body',      'rnode_enabled'],
    ['i2p-body',        'i2p_enabled'],
    ['i2p-body',        'i2p_connectable'],
  ].forEach(function (pair) {
    var tbody = document.getElementById(pair[0]);
    if (!tbody) return;
    tbody.querySelectorAll('[name="' + pair[1] + '"]').forEach(function (el, i) {
      el.value = i;
    });
  });
}

function addRow(tbodyId, html) {
  var tbody = document.getElementById(tbodyId);
  var i     = tbody.rows.length;
  var tr    = document.createElement('tr');
  tr.innerHTML = html(i);
  tbody.appendChild(tr);
}

var modeOptions = '<option value="">full</option>' +
  '<option value="gateway">gateway</option>' +
  '<option value="access_point">access point</option>' +
  '<option value="roaming">roaming</option>' +
  '<option value="boundary">boundary</option>';

var rmBtn = '<td><button type="button" class="btn btn-sm btn-danger" data-action="remove-row">✕</button></td>';

function addTcpClient() {
  addRow('tcp-client-body', function (i) {
    return '<td><input type="checkbox" name="tcp_client_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="tcp_client_name" placeholder="Name" style="width:130px"></td>' +
      '<td><input type="text" name="tcp_client_host" placeholder="hostname or IP" style="width:100%;min-width:160px"></td>' +
      '<td><input type="number" name="tcp_client_port" value="4965" min="1" max="65535" style="width:64px"></td>' +
      '<td><select name="tcp_client_mode" style="width:104px;font-size:12px;padding:4px 6px;">' + modeOptions + '</select></td>' +
      '<td><input type="text" name="tcp_client_network_name" placeholder="optional" style="width:130px"></td>' +
      '<td><input type="text" name="tcp_client_passphrase" placeholder="optional" style="width:110px"></td>' +
      rmBtn;
  });
}

function addTcpServer() {
  addRow('tcp-server-body', function (i) {
    return '<td><input type="checkbox" name="tcp_server_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="tcp_server_name" placeholder="Name" style="width:130px"></td>' +
      '<td><input type="text" name="tcp_server_ip" value="0.0.0.0" style="width:130px"></td>' +
      '<td><input type="number" name="tcp_server_port" value="4242" min="1" max="65535" style="width:64px"></td>' +
      '<td><select name="tcp_server_mode" style="width:104px;font-size:12px;padding:4px 6px;">' + modeOptions + '</select></td>' +
      '<td><input type="text" name="tcp_server_network_name" placeholder="optional" style="width:130px"></td>' +
      '<td><input type="text" name="tcp_server_passphrase" placeholder="optional" style="width:110px"></td>' +
      rmBtn;
  });
}

function addUdp() {
  addRow('udp-body', function (i) {
    return '<td><input type="checkbox" name="udp_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="udp_name" placeholder="Name" style="width:120px"></td>' +
      '<td><input type="text" name="udp_listen_ip" value="0.0.0.0" style="width:120px"></td>' +
      '<td><input type="number" name="udp_listen_port" value="4242" min="1" max="65535" style="width:64px"></td>' +
      '<td><input type="text" name="udp_forward_ip" value="255.255.255.255" style="width:150px"></td>' +
      '<td><input type="number" name="udp_forward_port" value="4242" min="1" max="65535" style="width:64px"></td>' +
      rmBtn;
  });
}

function addRNode() {
  addRow('rnode-body', function (i) {
    return '<td><input type="checkbox" name="rnode_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="rnode_name" placeholder="RNode" style="width:120px"></td>' +
      '<td><input type="text" name="rnode_port" value="/dev/ttyUSB0" style="width:130px"></td>' +
      '<td><input type="number" name="rnode_frequency" value="867500000" style="width:100px"></td>' +
      '<td><input type="number" name="rnode_bandwidth" value="125000" style="width:90px"></td>' +
      '<td><input type="number" name="rnode_txpower" value="7" min="0" max="27" style="width:56px"></td>' +
      '<td><input type="number" name="rnode_sf" value="8" min="5" max="12" style="width:48px"></td>' +
      '<td><input type="number" name="rnode_cr" value="5" min="5" max="8" style="width:48px"></td>' +
      rmBtn;
  });
}

function addI2P() {
  addRow('i2p-body', function (i) {
    return '<td><input type="checkbox" name="i2p_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="i2p_name" placeholder="I2P Interface" style="width:120px"></td>' +
      '<td style="text-align:center;"><input type="checkbox" name="i2p_connectable" value="' + i + '" style="accent-color:var(--accent)"></td>' +
      '<td><input type="text" name="i2p_peers" placeholder="abc123.b32.i2p, def456.b32.i2p" style="width:100%"></td>' +
      rmBtn;
  });
}

function applyRNodePreset(key) {
  var p = RNODE_PRESETS[key];
  if (!p) return;
  addRow('rnode-body', function (i) {
    return '<td><input type="checkbox" name="rnode_enabled" value="' + i + '" style="accent-color:var(--accent)" checked></td>' +
      '<td><input type="text" name="rnode_name" value="' + key.toUpperCase() + ' RNode" style="width:120px"></td>' +
      '<td><input type="text" name="rnode_port" value="' + p.port + '" style="width:130px"></td>' +
      '<td><input type="number" name="rnode_frequency" value="' + p.freq + '" style="width:100px"></td>' +
      '<td><input type="number" name="rnode_bandwidth" value="' + p.bw + '" style="width:90px"></td>' +
      '<td><input type="number" name="rnode_txpower" value="' + p.pwr + '" min="0" max="27" style="width:56px"></td>' +
      '<td><input type="number" name="rnode_sf" value="' + p.sf + '" min="5" max="12" style="width:48px"></td>' +
      '<td><input type="number" name="rnode_cr" value="' + p.cr + '" min="5" max="8" style="width:48px"></td>' +
      rmBtn;
  });
}

// Event delegation for remove buttons (handles both static and dynamically-added rows)
document.addEventListener('click', function (e) {
  if (e.target.matches('[data-action="remove-row"]')) {
    removeRow(e.target);
  }
  if (e.target.matches('[data-action="rnode-preset"]')) {
    e.preventDefault();
    applyRNodePreset(e.target.dataset.preset);
  }
});

// Wire up "Add" buttons
(function () {
  function wire(id, fn) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('click', fn);
  }
  wire('btn-add-tcp-client', addTcpClient);
  wire('btn-add-tcp-server', addTcpServer);
  wire('btn-add-udp',        addUdp);
  wire('btn-add-rnode',      addRNode);
  wire('btn-add-i2p',        addI2P);
})();

// "Apply now" — graceful gunicorn reload to pick up new interface config.
(function () {
  var btn = document.getElementById('btn-apply-now');
  if (!btn) return;
  var stat = document.getElementById('apply-status');
  var csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  btn.addEventListener('click', async function () {
    if (!confirm(btn.dataset.confirm)) return;
    btn.disabled = true;
    stat.textContent = 'Reloading…';
    stat.style.color = 'var(--text-dim)';
    try {
      var res  = await fetch(btn.dataset.reloadUrl, {
        method: 'POST',
        headers: { 'X-CSRF-Token': csrf, 'Accept': 'application/json' },
      });
      var data = await res.json();
      if (!data.ok) {
        stat.textContent = data.error || 'Reload failed.';
        stat.style.color = 'var(--error)';
        btn.disabled = false;
        return;
      }
      stat.textContent = 'Reload signalled — waiting for worker to come back…';
      var deadline = Date.now() + 60000;
      var ok = false;
      while (Date.now() < deadline) {
        await new Promise(function (r) { setTimeout(r, 1500); });
        try {
          var s = await fetch('/api/status', { cache: 'no-store' });
          if (s.ok) { ok = true; break; }
        } catch (_) {}
      }
      if (ok) {
        stat.textContent = 'Reloaded. Reticulum is reading the new config.';
        stat.style.color = 'var(--accent2)';
        // Likely logged out — give a hint
        setTimeout(function () { location.href = '/admin'; }, 1500);
      } else {
        stat.textContent = 'Worker did not come back in time — check container logs.';
        stat.style.color = 'var(--error)';
        btn.disabled = false;
      }
    } catch (e) {
      stat.textContent = 'Request failed: ' + e.message;
      stat.style.color = 'var(--error)';
      btn.disabled = false;
    }
  });
})();
