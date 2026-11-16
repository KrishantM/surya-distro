(function () {
  function fit() {
    document.querySelectorAll('[data-fit]').forEach(function (el) {
      var size = parseFloat(getComputedStyle(el).fontSize), min = size * 0.4;
      while ((el.scrollHeight > el.clientHeight + 1 || el.scrollWidth > el.clientWidth + 1) && size > min) {
        size -= 1; el.style.fontSize = size + 'px';
      }
    });
    return true;
  }
  window.__ready = document.fonts.ready.then(fit);
})();
