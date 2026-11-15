/*
 * Small loading spinner inside buttons, for the whole site.
 *
 * A spinner appears in the button the user just pressed, and goes away when the page changes or the
 * request ends. It replaces the button's own icon (so nothing shifts) or sits in front of its label.
 * It starts by itself for:
 *   - form submissions (button, Enter key, or form.submit() called from a click)
 *   - links that look like buttons (a link with padding, rounded corners and a border or background)
 *   - fetch() calls started by a button click
 * and by hand with  CBI.btnLoading.start(button) / .stop(button) / .wrap(button, promise).
 *
 * Opt out: data-no-spinner on the form / link / button.
 * Force on a button or link that is not a form: data-cbi-loading (optional data-cbi-timeout="5000",
 * in milliseconds, for actions that do not leave the page, e.g. a file download).
 *
 * The old full-screen loader (window.showLoadingOverlay / hideLoadingOverlay) now drives the same
 * in-button spinner, so no modal is shown.
 */
(function () {
  'use strict';
  if (window.CBI && window.CBI.btnLoading) return;

  var CBI = (window.CBI = window.CBI || {});
  var ICON = '.material-symbols-outlined, i.feather, svg';
  var BUTTONISH = 'button, a[href], [role="button"], input[type="submit"], input[type="button"]';
  var NOT_BUTTONS = 'nav, aside, [role="menu"], [role="menuitem"], [role="tab"], .sidebar-theme, ' +
                    '.pagination-nav, .pagination-footer, .pagination-btn, .dropdown-menu, .cbi-no-spinner';
  var parts = new WeakMap();   // button -> {spinner, icon, timer}
  var busy = [];               // buttons currently spinning
  var last = { el: null, t: 0 };

  /* ------------------------------------------------------------------ spinner on / off */

  function start(btn, opts) {
    if (!btn || btn.nodeType !== 1 || parts.has(btn)) return false;
    var spinner = document.createElement('span');
    spinner.className = 'cbi-spinner';
    spinner.setAttribute('aria-hidden', 'true');

    var icon = btn.tagName === 'INPUT' ? null : btn.querySelector(ICON);
    if (icon) {
      var size = icon.tagName.toLowerCase() === 'svg'
        ? icon.getBoundingClientRect().width
        : parseFloat(getComputedStyle(icon).fontSize);
      spinner.style.fontSize = (size || 18) + 'px';
      icon.classList.add('cbi-icon-hidden');
      icon.parentNode.insertBefore(spinner, icon);
    } else if (btn.tagName !== 'INPUT') {
      spinner.className += ' cbi-spinner--lead';
      btn.insertBefore(spinner, btn.firstChild);
    }

    var entry = { spinner: spinner, icon: icon, timer: null };
    var timeout = opts && opts.timeout;
    if (timeout > 0) entry.timer = setTimeout(function () { stop(btn); }, timeout);
    parts.set(btn, entry);
    busy.push(btn);
    btn.classList.add('cbi-btn-loading');
    btn.setAttribute('aria-busy', 'true');
    return true;
  }

  function stop(btn) {
    var entry = btn && parts.get(btn);
    if (!entry) return;
    if (entry.timer) clearTimeout(entry.timer);
    if (entry.spinner.parentNode) entry.spinner.parentNode.removeChild(entry.spinner);
    if (entry.icon) entry.icon.classList.remove('cbi-icon-hidden');
    btn.classList.remove('cbi-btn-loading');
    btn.removeAttribute('aria-busy');
    parts.delete(btn);
    var i = busy.indexOf(btn);
    if (i > -1) busy.splice(i, 1);
  }

  function stopAll() {
    busy.slice().forEach(stop);
    Array.prototype.forEach.call(document.querySelectorAll('form[data-cbi-busy]'), function (f) {
      f.removeAttribute('data-cbi-busy');
    });
  }

  function wrap(btn, work) {
    start(btn);
    var p;
    try { p = Promise.resolve(typeof work === 'function' ? work() : work); }
    catch (err) { stop(btn); throw err; }
    return p.finally(function () { stop(btn); });
  }

  /* ------------------------------------------------------------------ what counts as a button */

  function isButtonLikeLink(a) {
    if (a.hasAttribute('data-no-spinner') || a.closest(NOT_BUTTONS) || a.querySelector('img')) return false;
    var cs = getComputedStyle(a);
    var padX = parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight);
    var padY = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    var radius = parseFloat(cs.borderTopLeftRadius) || 0;
    var border = parseFloat(cs.borderTopWidth) > 0 && cs.borderTopStyle !== 'none' &&
                 cs.borderTopColor !== 'rgba(0, 0, 0, 0)' && cs.borderTopColor !== 'transparent';
    var bg = cs.backgroundColor !== 'rgba(0, 0, 0, 0)' && cs.backgroundColor !== 'transparent';
    if (radius < 4 || !(border || bg)) return false;
    if (padX >= 12 && padY >= 4) return true;                    // labelled button
    // Icon-only button: a rounded square holding just an icon (no padding, a fixed size).
    var icon = a.querySelector(ICON);
    var box = a.getBoundingClientRect();
    return !!icon && a.textContent.trim() === icon.textContent.trim() && box.width >= 28 && box.height >= 28;
  }

  function leavesThePage(a, e) {
    var href = a.getAttribute('href') || '';
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
    if (a.target && a.target !== '_self') return false;
    if (a.hasAttribute('download')) return false;
    return !/^(#|javascript:|mailto:|tel:)/i.test(href) && href !== '';
  }

  /* The button / link pressed within the last `ms` milliseconds (null when it is not a real button). */
  function recentButton(ms, onlyButtons) {
    var el = last.el;
    if (!el || Date.now() - last.t > ms) return null;
    if (el.tagName === 'A') return onlyButtons || !isButtonLikeLink(el) ? null : el;
    return el;
  }

  /* ------------------------------------------------------------------ automatic triggers */

  // Clicks: remember what was pressed; start the spinner for link-buttons and data-cbi-loading.
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest(BUTTONISH) : null;
    last = { el: el, t: Date.now() };
    if (!el) return;
    if (parts.has(el)) { e.preventDefault(); return; }            // already working: ignore a second click

    var forced = el.hasAttribute('data-cbi-loading');
    var isLink = el.tagName === 'A';
    if (!forced && !(isLink && isButtonLikeLink(el))) return;
    if (isLink && !leavesThePage(el, e)) return;
    var timeout = parseInt(el.getAttribute('data-cbi-timeout'), 10) || 0;
    // Wait for the page's own handlers: a click they cancel (confirm(), a modal...) is not an action yet.
    setTimeout(function () { if (!e.defaultPrevented) start(el, { timeout: timeout }); }, 0);
  }, true);

  // Form submissions (button, Enter key). A second submit while the first is running is ignored.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || form.tagName !== 'FORM') return;
    if (form.hasAttribute('data-cbi-busy')) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    var target = (e.submitter && e.submitter.formTarget) || form.target;
    if (form.hasAttribute('data-no-spinner') || (target && target !== '_self') || form.method === 'dialog') return;
    var btn = e.submitter || form.querySelector('button[type="submit"], button:not([type]), input[type="submit"]');
    form.setAttribute('data-cbi-busy', '1');
    setTimeout(function () {
      if (e.defaultPrevented) { form.removeAttribute('data-cbi-busy'); return; }   // handled by the page (AJAX...)
      if (btn && !btn.hasAttribute('data-no-spinner')) start(btn);
    }, 0);
  }, true);

  // form.submit() does not fire a submit event: use the button that was just pressed.
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    var target = this.target;
    if (!this.hasAttribute('data-no-spinner') && (!target || target === '_self')) {
      var btn = recentButton(2000, true);
      if (btn && btn.tagName !== 'A') start(btn);
    }
    return nativeSubmit.apply(this, arguments);
  };

  // fetch() started by a button click: spin until the request ends.
  if (typeof window.fetch === 'function') {
    var nativeFetch = window.fetch;
    window.fetch = function () {
      var btn = recentButton(300, true);
      var request = nativeFetch.apply(this, arguments);
      if (btn && btn.tagName !== 'A' && !btn.hasAttribute('data-no-spinner') && start(btn)) {
        var done = function () { stop(btn); };
        request.then(done, done);
      }
      return request;
    };
  }

  // Back / forward from the browser cache must not show a spinner that never ends.
  window.addEventListener('pageshow', function (e) { if (e.persisted) stopAll(); });

  /* ------------------------------------------------------------------ old full-screen loader */

  window.showLoadingOverlay = function () {
    var btn = recentButton(2500, false);
    if (btn) start(btn);
  };
  window.hideLoadingOverlay = function () { stopAll(); };

  CBI.btnLoading = { start: start, stop: stop, stopAll: stopAll, wrap: wrap, isButtonLikeLink: isButtonLikeLink };
})();
