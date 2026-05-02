(function () {
  var wrap       = document.getElementById('log-wrap');
  var dot        = document.getElementById('status-dot');
  var label      = document.getElementById('status-label');
  var filterSel  = document.getElementById('levelFilter');
  var autoScroll = document.getElementById('autoScroll');
  var clearBtn   = document.getElementById('btn-clear-log');
  var currentFilter = '';

  var streamUrl = wrap.dataset.streamUrl;

  function appendLine(data) {
    var el = document.createElement('span');
    el.className = 'log-line ll-' + data.level;
    el.dataset.level = data.level;
    el.textContent = data.msg;
    if (currentFilter && data.level !== currentFilter) el.classList.add('hidden');
    wrap.appendChild(el);
    wrap.appendChild(document.createTextNode('\n'));
    if (autoScroll.checked) wrap.scrollTop = wrap.scrollHeight;
  }

  function applyFilter() {
    currentFilter = filterSel.value;
    wrap.querySelectorAll('.log-line').forEach(function (el) {
      if (!currentFilter || el.dataset.level === currentFilter)
        el.classList.remove('hidden');
      else
        el.classList.add('hidden');
    });
  }

  function setStatus(connected) {
    dot.style.background = connected ? 'var(--accent2)' : 'var(--error)';
    label.textContent    = connected ? 'live' : 'disconnected — reload to reconnect';
    label.style.color    = connected ? 'var(--accent2)' : 'var(--error)';
  }

  function connect() {
    var es = new EventSource(streamUrl);
    es.onopen    = function () { setStatus(true); };
    es.onerror   = function () { setStatus(false); es.close(); setTimeout(connect, 5000); };
    es.onmessage = function (e) {
      try { appendLine(JSON.parse(e.data)); } catch (_) {}
    };
  }

  if (filterSel) filterSel.addEventListener('change', applyFilter);
  if (clearBtn)  clearBtn.addEventListener('click', function () { wrap.innerHTML = ''; });

  connect();
})();
