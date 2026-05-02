// Hash navigation
(function () {
  var form = document.getElementById('hash-form');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var h = document.getElementById('hashInput').value.trim();
      if (h) window.location = '/?url=hash://' + h + '/index.mu';
    });
  }

  var revokeForm = document.getElementById('sessions-revoke-form');
  if (revokeForm) {
    revokeForm.addEventListener('submit', function (e) {
      if (!confirm('Revoke all active sessions? Everyone (including you) will be logged out immediately.')) {
        e.preventDefault();
      }
    });
  }
})();

// Favorite toggle
document.querySelectorAll('.fav-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var hash   = this.dataset.hash;
    var nowFav = this.dataset.fav !== 'true';
    var self   = this;
    fetch('/admin/nodes/' + hash + '/favorite', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ favorited: nowFav }),
    }).then(function (r) { return r.json(); }).then(function (data) {
      if (data.ok) {
        self.dataset.fav   = nowFav ? 'true' : 'false';
        self.textContent   = nowFav ? '★' : '☆';
        self.style.color   = nowFav ? 'var(--warn)' : 'var(--border)';
        self.title         = nowFav ? 'Unfavorite' : 'Favorite';
        var td = self.closest('tr').querySelector('td:nth-child(2)');
        if (td) td.style.fontWeight = nowFav ? '600' : 'normal';
      }
    });
  });
});

// Ping
document.querySelectorAll('.ping-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var hash   = this.dataset.hash;
    var result = this.previousElementSibling;
    this.disabled    = true;
    this.textContent = '…';
    result.textContent = '';
    var self = this;
    fetch('/admin/nodes/' + hash + '/ping', { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data.ms !== undefined) {
          result.textContent = data.ms + ' ms';
          result.style.color = data.ms < 500 ? 'var(--accent2)' : 'var(--warn)';
        } else {
          result.textContent = data.error || 'failed';
          result.style.color = 'var(--error)';
        }
      })
      .catch(function () {
        result.textContent = 'error';
        result.style.color = 'var(--error)';
      })
      .finally(function () {
        self.disabled    = false;
        self.textContent = 'ping';
      });
  });
});
