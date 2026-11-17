/*
 * Loading feedback for the whole site, in two layers:
 *
 *  1. PAGE SHIELD. While a page change, a form submission or a request started by a click is running,
 *     the page is blocked: nothing behind can be clicked or tabbed to, so nobody can fire a second
 *     action or leave the first one half-done. The shield is invisible for the first 150 ms (fast
 *     actions never flash anything), then a small "Chargement…" card appears. After 5 minutes the shield
 *     gives up on its own and a dialog explains the likely causes, so a download or a cancelled
 *     navigation can never leave the page locked. Escape also releases it.
 *       CBI.block({title, subtitle})  ->  returns release()      CBI.unblockAll()
 *     Opt out: data-no-block on a link / button / form (downloads: data-cbi-timeout does it too).
 *
 *  2. A small spinner inside the button that was pressed (the part below).
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
 * window.showLoadingOverlay(title, subtitle) / hideLoadingOverlay() (called by several pages) use the
 * shield above, with their own wording.
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
  // A form's fields are also its properties: an input named "target" or "method" hides form.target / form.method.
  // Forms are therefore read through the element methods only, never through those properties.
  var elHas = Element.prototype.hasAttribute, elGet = Element.prototype.getAttribute;
  var elSet = Element.prototype.setAttribute, elDel = Element.prototype.removeAttribute;
  var elFind = Element.prototype.querySelector;
  var busy = [];               // buttons currently spinning
  var last = { el: null, t: 0 };

  /* ------------------------------------------------------------------ page shield */

  var SHOW_AFTER = 150;          // ms before the card is drawn; the page is blocked from the first instant
  var GIVE_UP_AFTER = 300000;    // ms (5 min): release the page, then explain why (see giveUp)
  var shield = null, holds = 0, showTimer = null, giveUpTimer = null;
  var inerted = [];              // page children made inert while blocked
  var messages = [];             // stack of {title, subtitle}; the latest is displayed

  function buildShield() {
    var el = document.createElement('div');
    el.id = 'cbi-shield';
    el.setAttribute('role', 'alert');
    el.setAttribute('aria-live', 'assertive');
    el.innerHTML =
      '<div class="cbi-shield-card">' +
        '<span class="cbi-spinner cbi-spinner--xl" aria-hidden="true"></span>' +
        '<div class="cbi-shield-text"><strong class="cbi-shield-title"></strong><span class="cbi-shield-sub"></span></div>' +
      '</div>';
    // Keyboard: nothing but the shield's own button may act while blocked.
    el.addEventListener('keydown', function (e) { if (e.key === 'Escape') unblockAll(); });
    return el;
  }

  function paintMessage() {
    if (!shield) return;
    var m = messages[messages.length - 1] || {};
    shield.querySelector('.cbi-shield-title').textContent = m.title || 'Chargement…';
    var sub = shield.querySelector('.cbi-shield-sub');
    sub.textContent = m.subtitle || '';
    sub.hidden = !m.subtitle;
  }

  function lockPage() {
    inerted = [];
    Array.prototype.forEach.call(document.body.children, function (child) {
      if (child === shield || child.hasAttribute('inert')) return;
      if (/^(SCRIPT|STYLE|LINK|NOSCRIPT)$/.test(child.tagName)) return;
      child.setAttribute('inert', '');
      inerted.push(child);
    });
  }

  function unlockPage() {
    inerted.forEach(function (child) { child.removeAttribute('inert'); });
    inerted = [];
  }

  function block(opts) {
    opts = opts || {};
    if (!document.body) return function () {};
    var message = { title: opts.title, subtitle: opts.subtitle };
    messages.push(message);
    holds++;
    if (holds === 1) {
      shield = shield || buildShield();
      document.body.appendChild(shield);
      shield.classList.remove('is-visible');
      lockPage();
      document.body.setAttribute('aria-busy', 'true');
      showTimer = setTimeout(function () { if (shield) shield.classList.add('is-visible'); }, SHOW_AFTER);
      giveUpTimer = setTimeout(giveUp, GIVE_UP_AFTER);
    }
    paintMessage();
    var released = false;
    return function release() {
      if (released) return;
      released = true;
      var i = messages.indexOf(message);
      if (i > -1) messages.splice(i, 1);
      if (holds > 0) holds--;
      if (holds === 0) teardown(); else paintMessage();
    };
  }

  function teardown() {
    clearTimeout(showTimer); clearTimeout(giveUpTimer);
    holds = 0; messages = [];
    unlockPage();
    document.body && document.body.removeAttribute('aria-busy');
    if (shield && shield.parentNode) shield.parentNode.removeChild(shield);
  }

  function unblockAll() {
    teardown();
    stopAll();
  }

  // Still blocked after GIVE_UP_AFTER: release the page, and say why instead of failing silently.
  function giveUp() {
    var m = messages[messages.length - 1];
    var what = (m && m.title && m.title !== 'Chargement…') ? m.title : "L'opération en cours";
    unblockAll();
    explain(what);
  }

  function explain(what) {
    if (!document.body) return;
    var old = document.getElementById('cbi-timeout');
    if (old) old.parentNode.removeChild(old);
    var box = document.createElement('div');
    box.id = 'cbi-timeout';
    box.innerHTML =
      '<div class="cbi-timeout-card" role="alertdialog" aria-modal="true" aria-labelledby="cbi-timeout-title" aria-describedby="cbi-timeout-body">' +
        '<h2 id="cbi-timeout-title" class="cbi-timeout-title">L\'opération prend trop de temps</h2>' +
        '<p id="cbi-timeout-body" class="cbi-timeout-body"></p>' +
        '<p class="cbi-timeout-why">Causes possibles :</p>' +
        '<ul class="cbi-timeout-list">' +
          '<li>le serveur PBIRS, Odoo ou l\'annuaire (AD) est lent ou ne répond pas ;</li>' +
          '<li>la connexion a été interrompue entre votre poste et le site ;</li>' +
          '<li>le traitement porte sur un très grand nombre de rapports ou de personnes.</li>' +
        '</ul>' +
        '<p class="cbi-timeout-note">L\'action a peut-être été effectuée en partie ou en totalité : vérifiez la page avant de la refaire. Si cela se reproduit, prévenez l\'administrateur.</p>' +
        '<div class="cbi-timeout-actions"><button type="button" class="cbi-timeout-ok">J\'ai compris</button></div>' +
      '</div>';
    box.querySelector('.cbi-timeout-body').textContent =
      '« ' + what + ' » n\'a pas répondu depuis 5 minutes, l\'écran a donc été débloqué.';
    var onKey = function (e) { if (e.key === 'Escape') close(); };
    var close = function () {
      document.removeEventListener('keydown', onKey, true);
      if (box.parentNode) box.parentNode.removeChild(box);
    };
    box.querySelector('.cbi-timeout-ok').addEventListener('click', close);
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(box);
    box.querySelector('.cbi-timeout-ok').focus();
  }

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
      elDel.call(f, 'data-cbi-busy');
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

  // True when a click happened in the last 300 ms on something that is not opted out.
  function clickStartedWork() {
    var el = last.el;
    if (!el || Date.now() - last.t > 300) return false;
    return !(el.hasAttribute('data-no-block') || el.closest('[data-no-block]'));
  }

  /* ------------------------------------------------------------------ automatic triggers */

  // Clicks: remember what was pressed; start the spinner for link-buttons and data-cbi-loading.
  document.addEventListener('click', function (e) {
    var el = e.target && e.target.closest ? e.target.closest(BUTTONISH) : null;
    last = { el: el, t: Date.now() };
    if (!el) return;
    if (parts.has(el)) { e.preventDefault(); return; }            // already working: ignore a second click

    // Any link that really changes the page blocks it until the next page replaces this one.
    if (el.tagName === 'A' && leavesThePage(el, e) && !el.hasAttribute('data-no-block') &&
        !el.hasAttribute('data-cbi-timeout') && !el.closest('[data-bs-toggle], [data-no-block]') &&
        el.origin === window.location.origin) {
      setTimeout(function () { if (!e.defaultPrevented) block(); }, 0);
    }

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
    if (elHas.call(form, 'data-cbi-busy')) { e.preventDefault(); e.stopImmediatePropagation(); return; }
    var target = (e.submitter && elGet.call(e.submitter, 'formtarget')) || elGet.call(form, 'target');
    var method = (elGet.call(form, 'method') || '').toLowerCase();
    if (elHas.call(form, 'data-no-spinner') || (target && target !== '_self') || method === 'dialog') return;
    var btn = e.submitter || elFind.call(form, 'button[type="submit"], button:not([type]), input[type="submit"]');
    elSet.call(form, 'data-cbi-busy', '1');
    setTimeout(function () {
      if (e.defaultPrevented) { elDel.call(form, 'data-cbi-busy'); return; }   // handled by the page (AJAX...)
      if (btn && !btn.hasAttribute('data-no-spinner')) start(btn);
      if (!elHas.call(form, 'data-no-block')) block();
    }, 0);
  }, true);

  // form.submit() does not fire a submit event: use the button that was just pressed.
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    var target = elGet.call(this, 'target');
    if (!elHas.call(this, 'data-no-spinner') && (!target || target === '_self')) {
      var btn = recentButton(2000, true);
      if (btn && btn.tagName !== 'A') start(btn);
      if (!elHas.call(this, 'data-no-block')) block();
    }
    return nativeSubmit.apply(this, arguments);
  };

  // fetch() started by a button click: spin until the request ends.
  if (typeof window.fetch === 'function') {
    var nativeFetch = window.fetch;
    window.fetch = function () {
      var btn = recentButton(300, true);
      var release = clickStartedWork() ? block() : null;
      var request = nativeFetch.apply(this, arguments);
      if (btn && btn.tagName !== 'A' && !btn.hasAttribute('data-no-spinner') && start(btn)) {
        var done = function () { stop(btn); };
        request.then(done, done);
      }
      if (release) request.then(release, release);
      return request;
    };
  }

  // XMLHttpRequest (jQuery $.ajax...) started by a click behaves the same way.
  if (window.XMLHttpRequest) {
    var nativeSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.send = function () {
      if (clickStartedWork()) {
        var release = block();
        this.addEventListener('loadend', release);
      }
      return nativeSend.apply(this, arguments);
    };
  }

  // Back / forward from the browser cache must not show a spinner that never ends.
  window.addEventListener('pageshow', function (e) { if (e.persisted) unblockAll(); });

  /* ------------------------------------------------------------------ old full-screen loader */

  var overlayRelease = null;
  window.showLoadingOverlay = function (title, subtitle) {
    var btn = recentButton(2500, false);
    if (btn) start(btn);
    if (overlayRelease) overlayRelease();
    overlayRelease = block({ title: title, subtitle: subtitle });
  };
  window.hideLoadingOverlay = function () {
    if (overlayRelease) { overlayRelease(); overlayRelease = null; }
    stopAll();
  };

  CBI.btnLoading = { start: start, stop: stop, stopAll: stopAll, wrap: wrap };
  CBI.block = block;
  CBI.unblockAll = unblockAll;
})();
