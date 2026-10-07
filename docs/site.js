// Copy-to-clipboard for install command blocks.
document.querySelectorAll('.copy-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var target = document.getElementById(btn.getAttribute('data-copy'));
    if (!target || !navigator.clipboard) return;
    navigator.clipboard.writeText(target.textContent).then(function () {
      var label = btn.innerHTML;
      btn.classList.add('copied');
      btn.textContent = 'Copied';
      setTimeout(function () {
        btn.classList.remove('copied');
        btn.innerHTML = label;
      }, 1600);
    });
  });
});
