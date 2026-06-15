(function () {
  var _csrf = document.querySelector('meta[name="csrf-token"]')?.content || '';

  // Live title preview
  var _previewTimer = null;
  function previewTitle() {
    var text = document.getElementById('s-app-title').value;
    fetch('/admin/api/preview/title', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': _csrf },
      body: JSON.stringify({ text: text }),
    }).then(function (r) { return r.json(); }).then(function (d) {
      var el = document.getElementById('title-preview');
      if (el) el.innerHTML = d.html || '(empty)';
    }).catch(function () {});
  }
  document.getElementById('s-app-title').addEventListener('input', function () {
    clearTimeout(_previewTimer);
    _previewTimer = setTimeout(previewTitle, 350);
  });
  previewTitle();

  // ---- Access preset ↔ per-audience table ----
  // Admin defaults are unrestricted in every preset; only a super admin can
  // change them. The admin inputs are server-rendered with `disabled`, so
  // writeTable() skips disabled inputs to avoid touching values the user
  // can't legitimately change.
  var ADMIN_DEFAULTS = {
    admins_default_lock: false,
    admins_address_bar: 'enabled',
    admins_nodes_panel: true,
    admins_messages_panel: true,
  };
  var PRESETS = {
    public: Object.assign({
      guests_default_lock: false, users_default_lock: false,
      guests_address_bar: 'enabled', users_address_bar: 'enabled',
      guests_nodes_panel: true,  users_nodes_panel: true,
      guests_messages_panel: true, users_messages_panel: true,
      users_can_message: true,
    }, ADMIN_DEFAULTS),
    gated: Object.assign({
      guests_default_lock: true,  users_default_lock: false,
      guests_address_bar: 'hidden', users_address_bar: 'enabled',
      guests_nodes_panel: false, users_nodes_panel: true,
      guests_messages_panel: false, users_messages_panel: true,
      users_can_message: true,
    }, ADMIN_DEFAULTS),
    locked: Object.assign({
      guests_default_lock: true,  users_default_lock: true,
      guests_address_bar: 'hidden', users_address_bar: 'hidden',
      guests_nodes_panel: false, users_nodes_panel: false,
      guests_messages_panel: false, users_messages_panel: false,
      users_can_message: true,
    }, ADMIN_DEFAULTS),
  };
  var FIELD_TO_INPUT_ID = {
    guests_default_lock:    's-guests-default-lock',
    users_default_lock:     's-users-default-lock',
    admins_default_lock:    's-admins-default-lock',
    guests_address_bar:     's-guests-address-bar',
    users_address_bar:      's-users-address-bar',
    admins_address_bar:     's-admins-address-bar',
    guests_nodes_panel:     's-guests-nodes-panel',
    users_nodes_panel:      's-users-nodes-panel',
    admins_nodes_panel:     's-admins-nodes-panel',
    guests_messages_panel:  's-guests-messages-panel',
    users_messages_panel:   's-users-messages-panel',
    admins_messages_panel:  's-admins-messages-panel',
    users_can_message:      's-users-can-message',
  };

  function readTable() {
    var out = {};
    Object.keys(FIELD_TO_INPUT_ID).forEach(function (k) {
      var el = document.getElementById(FIELD_TO_INPUT_ID[k]);
      out[k] = el.type === 'checkbox' ? el.checked : el.value;
    });
    return out;
  }

  function writeTable(values) {
    Object.keys(values).forEach(function (k) {
      var el = document.getElementById(FIELD_TO_INPUT_ID[k]);
      if (!el || el.disabled) return;   // skip super-admin-only fields
      if (el.type === 'checkbox') el.checked = !!values[k];
      else el.value = values[k];
    });
  }

  function matchingPreset(values) {
    for (var name in PRESETS) {
      var tpl = PRESETS[name];
      var match = Object.keys(tpl).every(function (k) { return tpl[k] === values[k]; });
      if (match) return name;
    }
    return 'custom';
  }

  function refreshPresetLabel() {
    var sel    = document.getElementById('s-access-preset');
    var preset = matchingPreset(readTable());
    var custom = sel.querySelector('option[value="custom"]');
    if (preset === 'custom') {
      custom.disabled = false;
      custom.hidden   = false;
      sel.value = 'custom';
    } else {
      sel.value = preset;
      custom.disabled = true;
      custom.hidden   = true;
    }
  }

  document.getElementById('s-access-preset').addEventListener('change', function () {
    var preset = this.value;
    if (PRESETS[preset]) {
      writeTable(PRESETS[preset]);
      refreshPresetLabel();
    }
  });

  Object.values(FIELD_TO_INPUT_ID).forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', refreshPresetLabel);
  });
  refreshPresetLabel();

  document.getElementById('btn-save-settings').addEventListener('click', async function () {
    var status = document.getElementById('settings-status');
    status.textContent = 'Saving…';
    status.style.color = 'var(--text-dim)';
    try {
      var payload = readTable();
      payload.app_title     = document.getElementById('s-app-title').value.trim() || '`F4af■ NomadPortal`f';
      payload.site_name     = document.getElementById('s-site-name').value.trim();
      payload.default_node  = document.getElementById('s-default-node').value.trim().toLowerCase();
      payload.abuse_contact = document.getElementById('s-abuse-contact').value.trim();
      // Site-hosting tri-state. Empty string from the dropdown means
      // "fall through to env var" — send as null so the server keeps
      // the legacy SITE_HOSTING / SITE_ANNOUNCE env defaults.
      function _triState(id) {
        var raw = document.getElementById(id).value;
        if (raw === 'true') return true;
        if (raw === 'false') return false;
        return null;
      }
      payload.hosting_enabled = _triState('s-hosting-enabled');
      payload.auto_announce   = _triState('s-auto-announce');
      // Announce interval: empty = "use env var" (send null), otherwise
      // parse the dropdown's value as integer seconds. The server clamps
      // out-of-range values; client just sends what was selected.
      var rawIv = document.getElementById('s-announce-interval').value;
      payload.announce_interval = (rawIv === '') ? null : parseInt(rawIv, 10);
      var res = await fetch('/admin/api/ui/settings', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
          'X-CSRF-Token': _csrf,
        },
        body: JSON.stringify(payload),
      });
      var data = await res.json();
      if (data.ok) {
        status.textContent = 'Saved.';
        status.style.color = 'var(--accent2)';
      } else {
        status.textContent = data.error || 'Error saving.';
        status.style.color = 'var(--error)';
      }
    } catch (e) {
      status.textContent = 'Request failed.';
      status.style.color = 'var(--error)';
    }
    setTimeout(function () { status.textContent = ''; }, 3000);
  });

  // ---- Blocklist ----
  function esc(s) {
    // Escape every char that has special meaning in HTML attribute or
    // text contexts: &<>"'  — only escaping &<> leaves attribute-quote
    // breakouts open (CodeQL's incomplete-html-attribute-sanitization
    // rule flags exactly that). Quote pair both forms because the
    // template literals around this function mix " and ' delimiters.
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  async function loadBlocklist() {
    var wrap = document.getElementById('blocklist-table-wrap');
    try {
      var res  = await fetch('/api/blocklist', { headers: { 'X-CSRF-Token': _csrf } });
      var data = await res.json();
      var list = data.blocked || [];
      if (!list.length) {
        wrap.innerHTML = '<p class="dim">No nodes blocked.</p>';
        return;
      }
      var rows = list.map(function (h) {
        return '<tr><td class="mono dim" style="font-size:11px;">' + esc(h) + '</td>' +
               '<td style="width:80px;text-align:right;">' +
               '<button class="btn btn-sm btn-neutral" data-unblock="' + esc(h) + '">Unblock</button>' +
               '</td></tr>';
      }).join('');
      wrap.innerHTML = '<table class="admin-table"><thead><tr><th>Hash</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';
      wrap.querySelectorAll('[data-unblock]').forEach(function (btn) {
        btn.addEventListener('click', async function () {
          var hash = btn.dataset.unblock;
          await fetch('/api/blocklist/' + encodeURIComponent(hash), {
            method: 'DELETE',
            headers: { 'X-CSRF-Token': _csrf },
          });
          loadBlocklist();
        });
      });
    } catch (e) {
      wrap.innerHTML = '<p class="dim" style="color:var(--error)">Failed to load blocklist.</p>';
    }
  }

  document.getElementById('btn-block-node').addEventListener('click', async function () {
    var input = document.getElementById('blocklist-hash-input');
    var hash  = input.value.trim().toLowerCase();
    if (!hash) return;
    try {
      await fetch('/api/blocklist', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': _csrf },
        body: JSON.stringify({ hash: hash }),
      });
      input.value = '';
      loadBlocklist();
    } catch (e) {}
  });

  loadBlocklist();
})();
