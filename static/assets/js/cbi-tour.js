/*
 * Visites guidées du portail : moteur.
 * Les textes sont dans cbi-tours.js ; driver.js (vendored) dessine la bulle et le surlignage.
 *
 *  - La visite générale se lance seule la première fois (par version), sauf sur les pages de rapport.
 *  - Les visites de page sont proposées par une petite pastille, jamais imposées.
 *  - Une visite est « vue » dès qu'elle est terminée ou fermée : le serveur s'en souvient par utilisateur.
 *  - Le bouton « Visite guidée » du menu relance une visite à la demande.
 */
(function () {
  'use strict';

  var TOURS = window.CBI_TOURS;
  var driverFactory = window.driver && window.driver.js && window.driver.js.driver;
  var configEl = document.getElementById('cbi-tour-config');
  if (!TOURS || !driverFactory || !configEl) return;

  var page = configEl.getAttribute('data-page') || '';
  var urls = { seen: configEl.getAttribute('data-seen-url'), reset: configEl.getAttribute('data-reset-url') };
  var csrf = configEl.getAttribute('data-csrf') || '';
  var seen = {};
  try { seen = JSON.parse(document.getElementById('cbi-tour-seen').textContent) || {}; } catch (e) { seen = {}; }

  var reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var active = null;       // la visite en cours : { key, drv, steps, openedDrawer, openedDetails }
  var chip = null;

  /* ---------------------------------------------------------------- utilitaires */

  function isMobile() { return window.innerWidth < 1024; }
  function sidebar() { return document.getElementById('sidebar'); }
  function drawerOpen() { var s = sidebar(); return !!s && !s.classList.contains('-translate-x-full'); }
  function toggleDrawer() { if (typeof window.toggleSidebar === 'function') window.toggleSidebar(); }

  function isShown(el) {
    if (!el) return false;
    if (!el.getClientRects().length) return false;
    var style = window.getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  }

  function tourForPage() {
    var keys = Object.keys(TOURS);
    for (var i = 0; i < keys.length; i++) {
      if (TOURS[keys[i]].pages && TOURS[keys[i]].pages.indexOf(page) !== -1) return keys[i];
    }
    return null;
  }

  function isSeen(key) { return (seen[key] || 0) >= TOURS[key].version; }

  function post(url, payload) {
    if (!url) return;
    try {
      fetch(url, {
        method: 'POST',
        credentials: 'same-origin',
        keepalive: true,
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrf },
        body: JSON.stringify(payload)
      }).catch(function () { /* hors ligne : la visite sera reproposée */ });
    } catch (e) { /* rien */ }
  }

  function markSeen(key) {
    seen[key] = TOURS[key].version;
    post(urls.seen, { tour: key, version: TOURS[key].version });
  }

  /* Les étapes dont l'élément est absent ou caché sont sautées : la visite s'adapte au profil. */
  function resolveSteps(key) {
    var steps = [];
    TOURS[key].steps.forEach(function (step) {
      if (!step.el) { steps.push({ spec: step, node: null }); return; }
      var node = document.querySelector(step.el);
      var visible = isShown(node);
      // Un lien dans un groupe replié : visible si le groupe l'est (il sera déplié pour l'étape).
      var group = node && node.tagName !== 'DETAILS' ? node.closest('details') : null;
      if (!visible && group && !group.open) visible = isShown(group.querySelector('summary'));
      if (!visible && step.open && node && node.tagName === 'DETAILS') visible = isShown(node.querySelector('summary'));
      if (visible) steps.push({ spec: step, node: node });
    });
    return steps;
  }

  /* ---------------------------------------------------------------- menu latéral (mobile) et <details> */

  /* Ouvre ou ferme ce que l'étape demande, puis continue (le tiroir se déplace en 300 ms). */
  function prepare(tour, entry, done) {
    var spec = entry.spec;
    var node = entry.node;
    var details = !node ? null : node.tagName === 'DETAILS' ? (spec.open ? node : null) : node.closest('details');
    if (details && !details.open) {
      tour.openedDetails.push(details);
      details.open = true;
    }
    if (isMobile()) {
      var wantsDrawer = !!spec.drawer;
      if (wantsDrawer !== drawerOpen()) {
        if (wantsDrawer) tour.openedDrawer = true;
        toggleDrawer();
        window.setTimeout(done, 340);
        return;
      }
    }
    done();
  }

  /* ---------------------------------------------------------------- lancement */

  function start(key) {
    if (!TOURS[key]) return false;
    if (active) active.drv.destroy();
    hideChip();

    // Menu replié par le bouton « plein écran » : on le rouvre le temps de la visite.
    var uncollapsed = document.body.classList.contains('legacy-collapsed');
    if (uncollapsed) document.body.classList.remove('legacy-collapsed');

    var steps = resolveSteps(key);
    if (!steps.length) {
      if (uncollapsed) document.body.classList.add('legacy-collapsed');
      return false;
    }

    var tour = { key: key, steps: steps, drv: null, openedDrawer: false, openedDetails: [] };
    var total = steps.length;

    function move(delta) {
      var next = tour.drv.getActiveIndex() + delta;
      if (next < 0) return;
      if (next >= total) { tour.drv.destroy(); return; }
      prepare(tour, steps[next], function () { tour.drv.moveTo(next); });
    }

    tour.drv = driverFactory({
      steps: steps.map(function (entry) {
        var step = { popover: { title: entry.spec.title, description: entry.spec.text } };
        if (entry.node) {
          step.element = entry.node;
          step.popover.side = entry.spec.side || 'bottom';
          step.popover.align = 'center';
        }
        return step;
      }),
      popoverClass: 'cbi-tour-popover',
      showProgress: total > 1,
      progressText: '{{current}} / {{total}}',
      nextBtnText: 'Suivant',
      prevBtnText: 'Précédent',
      doneBtnText: 'Terminer',
      allowClose: true,
      allowKeyboardControl: true,
      overlayColor: '#0b0d12',
      overlayOpacity: 0.62,
      stagePadding: 6,
      stageRadius: 10,
      popoverOffset: 12,
      smoothScroll: !reducedMotion,
      animate: !reducedMotion,
      onNextClick: function () { move(1); },
      onPrevClick: function () { move(-1); },
      onDestroyed: function () {
        // Fermée ou terminée : dans les deux cas on ne la repropose pas.
        markSeen(key);
        tour.openedDetails.forEach(function (d) { d.open = false; });
        if (uncollapsed) document.body.classList.add('legacy-collapsed');
        if (tour.openedDrawer && isMobile() && drawerOpen()) toggleDrawer();
        active = null;
        if (key === 'main') offerPageTour();
      }
    });

    active = tour;
    prepare(tour, steps[0], function () { tour.drv.drive(0); });
    return true;
  }

  /* ---------------------------------------------------------------- pastille « visite de cette page » */

  var CHIP_VIEWS_KEY = 'cbiTourChipViews:';
  var CHIP_MAX_VIEWS = 3;

  function chipViews(key) {
    try { return parseInt(window.localStorage.getItem(CHIP_VIEWS_KEY + key), 10) || 0; } catch (e) { return 0; }
  }
  function bumpChipViews(key) {
    try { window.localStorage.setItem(CHIP_VIEWS_KEY + key, String(chipViews(key) + 1)); } catch (e) { /* rien */ }
  }

  function hideChip() {
    if (!chip) return;
    var node = chip;
    chip = null;
    node.classList.remove('is-visible');
    window.setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 250);
  }

  function offerPageTour() {
    var key = tourForPage();
    if (!key || key === 'main' || isSeen(key) || active || chip) return;
    // Trois affichages au plus sans réponse : ensuite la visite reste accessible depuis le menu.
    if (chipViews(key) >= CHIP_MAX_VIEWS) return;
    if (!resolveSteps(key).length) return;

    bumpChipViews(key);
    chip = document.createElement('div');
    chip.className = 'cbi-tour-chip';
    chip.setAttribute('role', 'dialog');
    chip.setAttribute('aria-label', 'Visite guidée de la page');
    chip.innerHTML =
      '<span class="material-symbols-outlined cbi-tour-chip-icon" aria-hidden="true">tour</span>' +
      '<div class="cbi-tour-chip-copy"><strong>Nouveau sur cette page ?</strong><span>Une visite de 30 secondes.</span></div>' +
      '<button type="button" class="cbi-tour-chip-go">Démarrer</button>' +
      '<button type="button" class="cbi-tour-chip-close" aria-label="Ne plus proposer">' +
      '<span class="material-symbols-outlined" aria-hidden="true">close</span></button>';
    document.body.appendChild(chip);
    window.requestAnimationFrame(function () { if (chip) chip.classList.add('is-visible'); });

    chip.querySelector('.cbi-tour-chip-go').addEventListener('click', function () { start(key); });
    chip.querySelector('.cbi-tour-chip-close').addEventListener('click', function () {
      markSeen(key);
      hideChip();
    });
    window.setTimeout(hideChip, 25000);
  }

  /* ---------------------------------------------------------------- bouton « Visite guidée » du menu */

  function buildLauncher() {
    var button = document.getElementById('tour-launcher');
    if (!button) return;

    var menu = document.createElement('div');
    menu.className = 'cbi-tour-menu';
    menu.hidden = true;
    menu.setAttribute('role', 'menu');
    button.parentNode.appendChild(menu);

    function item(label, icon, handler) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'cbi-tour-menu-item';
      b.setAttribute('role', 'menuitem');
      b.innerHTML = '<span class="material-symbols-outlined" aria-hidden="true">' + icon + '</span><span></span>';
      b.lastChild.textContent = label;
      b.addEventListener('click', function () { close(); handler(b); });
      menu.appendChild(b);
    }

    function render() {
      menu.textContent = '';
      var pageKey = tourForPage();
      if (pageKey && pageKey !== 'main' && resolveSteps(pageKey).length) {
        item(TOURS[pageKey].label, 'pin_drop', function () { start(pageKey); });
      }
      item(TOURS.main.label, 'explore', function () { start('main'); });
      item('Tout revoir depuis le début', 'restart_alt', function () {
        // Les visites de page seront reproposées, et la visite générale repart tout de suite.
        seen = {};
        try {
          Object.keys(TOURS).forEach(function (k) { window.localStorage.removeItem(CHIP_VIEWS_KEY + k); });
        } catch (e) { /* rien */ }
        post(urls.reset, { tour: 'all' });
        start('main');
      });
    }

    function open() {
      render();
      menu.hidden = false;
      button.setAttribute('aria-expanded', 'true');
    }
    function close() {
      menu.hidden = true;
      button.setAttribute('aria-expanded', 'false');
    }

    button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false');
    button.addEventListener('click', function (event) {
      event.stopPropagation();
      if (menu.hidden) open(); else close();
    });
    document.addEventListener('click', function (event) {
      if (!menu.hidden && !menu.contains(event.target)) close();
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && !menu.hidden) { close(); button.focus(); }
    });
  }

  /* ---------------------------------------------------------------- démarrage */

  buildLauncher();

  window.CBITour = { start: start };

  function boot() {
    // Laisse la mise en page et les polices se poser, et n'interrompt pas un dialogue déjà ouvert.
    window.setTimeout(function () {
      if (document.querySelector('.modal.show, #shareModal.flex')) return;
      var noAuto = TOURS.main.noAuto || [];
      if (!isSeen('main') && noAuto.indexOf(page) === -1) {
        start('main');
      } else {
        offerPageTour();
      }
    }, 900);
  }

  if (document.readyState === 'complete') boot();
  else window.addEventListener('load', boot);
})();
