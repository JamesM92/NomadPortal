// Global confirm-dialog handler for data-confirm elements
document.addEventListener('click', function (e) {
  var el = e.target.closest('[data-confirm]');
  if (!el) return;
  var msg = el.dataset.confirm;
  if (msg && !confirm(msg)) {
    e.preventDefault();
    e.stopPropagation();
  }
});

document.addEventListener('submit', function (e) {
  var el = e.target.closest('[data-confirm]');
  if (!el) return;
  var msg = el.dataset.confirm;
  if (msg && !confirm(msg)) {
    e.preventDefault();
  }
});

// Auto-submit for checkboxes with data-autosubmit
document.addEventListener('change', function (e) {
  if (e.target.matches('[data-autosubmit]')) {
    e.target.form && e.target.form.submit();
  }
});
