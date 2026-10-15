/* =====================================================================
   ς੮ ς८Րɿƿ੮ — Studio vidéo (JS natif, aucune dépendance).

   Quatre sections :
     1. Créer      : 5 onglets (Lien → vidéo, IA vidéo, RsT, SsT, Montage multi-source)
     2. Historique : dernières créations, lecteur et téléchargement Drive
     3. Paramètres : mode, transitions, fournisseurs IA, thème, serveur, entraînements
     4. Connexions : TikTok, Google Drive OAuth

   Tout le comportement métier existant (lancement jobs, RsT multiple,
   voix off /api/voixoff, fournisseurs, livraison séparée, reprise
   après actualisation, clés localStorage vesper.*) est conservé à l'identique.
   ===================================================================== */
(() => {
  'use strict';

  const $ = (sel, r = document) => r.querySelector(sel);
  const $$ = (sel, r = document) => [...r.querySelectorAll(sel)];
  const U = window.JobUtils;

  /* ------------------------------------------------------------------
     ÉTAT GLOBAL
  ------------------------------------------------------------------ */

  const SETTINGS_KEY = 'vesper.settings.v3';
  let settings = loadSettings();
  let serverConfig = null;
  let integrations = { tiktok: {}, google_drive: {} };
  let currentVideo = '';
  let diagnosticData = null;
  let activeJobId = '';
  let activeJobType = '';
  let diagnosticTimer = 0;
  let historyTimer = 0;
  let batchDrafts = U.loadBatchDrafts(localStorage);
  let rstJobs = U.loadRstJobs(localStorage);
  const rstEtats = {};
  const rstSuivis = new Set();
  let trainingProfiles = U.loadTrainingProfiles(localStorage);
  let activeTrainingProfileId = '';
  let sstNombreNoms = 3;
  const voixOff = { lien: '', rst: '', montage: '' };

  const guards = {
    analyser: U.submissionGuard(), video: U.submissionGuard(),
    reference: U.submissionGuard(), rst: U.submissionGuard(), sst: U.submissionGuard(),
    montage: U.submissionGuard(), training: U.submissionGuard(), diagnostic: U.submissionGuard(),
    batch: U.submissionGuard()
  };

  const statusLabels = {
    queued: 'En file d’attente', validating: 'Validation', downloading: 'Téléchargement',
    searching: 'Recherche TikTok', analysing: 'Analyse IA', selecting: 'Sélection des plans',
    editing: 'Montage FFmpeg', subtitling: 'Sous-titres', uploading: 'Sauvegarde Drive',
    completed: 'Terminé', failed: 'Échec', cancelled: 'Annulé'
  };

  const typeLabels = {
    analyser: 'Analyse', video: 'Lien → vidéo', reference: 'Référence',
    ia: 'IA vidéo', 'ia-video': 'IA vidéo',
    rst: 'RsT', sst: 'SsT', montage: 'Montage'
  };

  const ETATS_FINAL = ['completed', 'failed', 'cancelled'];
  const RST_MAX = 6;

  const iaUtiliseeLigne = (data) => {
    if (!data || !data.fournisseur_texte) return '';
    let ligne = ` · IA utilisée (texte) : ${data.fournisseur_texte}`;
    if (Array.isArray(data.bascules_fournisseur) && data.bascules_fournisseur.length) {
      ligne += ` · ${data.bascules_fournisseur.length} bascule(s)`;
    }
    return ligne;
  };

  let rstNomsChoix = [3, 5];
  let rstNombreNoms = 3;

  /* ------------------------------------------------------------------
     NAVIGATION : 4 sections, 5 onglets dans Créer
  ------------------------------------------------------------------ */

  const SEC = ['Créer', 'Historique', 'Paramètres', 'Connexions'];
  // Clés internes utilisées aussi par l'ancien code pour les data-tab (lien/ia/rst/sst/montage)
  const TABS = [
    { key: 'lien', label: 'Lien → vidéo' },
    { key: 'ia', label: 'IA vidéo' },
    { key: 'rst', label: 'RsT' },
    { key: 'sst', label: 'SsT' },
    { key: 'montage', label: 'Montage multi-source' }
  ];
  let sec = 0;
  let tab = 'lien';

  /* ------------------------------------------------------------------
     PETITS OUTILS
  ------------------------------------------------------------------ */

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const fmtDuree = (secondes) => {
    const valeur = Number(secondes);
    if (!Number.isFinite(valeur) || valeur <= 0) return 'durée inconnue';
    if (valeur < 60) return `${valeur.toFixed(1).replace('.', ',')} s`;
    const minutes = Math.floor(valeur / 60);
    const reste = Math.round(valeur % 60);
    return `${minutes} min ${String(reste).padStart(2, '0')}`;
  };

  const fmtHeure = (unix) => {
    try { return new Date(unix * 1000).toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' }); }
    catch (_) { return ''; }
  };

  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

  /* --- Toasts (nouveau conteneur #toasts) + rétrocompatibilité #toast --- */
  function toast(message, erreur = false) {
    // Élément moderne
    const conteneur = $('#toasts');
    if (conteneur) {
      const t = el('div', 'toast' + (erreur ? ' er' : ''));
      t.textContent = message;
      conteneur.append(t);
      setTimeout(() => { t.classList.add('out'); setTimeout(() => t.remove(), 320); }, 4000);
    }
    // Rétrocompatibilité
    const ancien = document.getElementById('toast');
    if (ancien) {
      ancien.textContent = message;
      ancien.className = 'toast-old show' + (erreur ? ' error' : '');
      clearTimeout(ancien._timer);
      ancien._timer = setTimeout(() => { ancien.className = 'toast-old'; }, 5200);
    }
  }

  function setStatus(element, message, type = '') {
    if (!element) return;
    element.textContent = message;
    element.className = 'stt' + (type ? ' ' + type : '');
  }

  async function requestJSON(url, options = {}) {
    const method = options.method || (options.body === undefined ? 'GET' : 'POST');
    const started = Date.now();
    let attempt = 0;
    for (;;) {
      let response;
      try {
        response = await fetch(url, {
          method,
          credentials: 'same-origin',
          headers: options.body === undefined ? {} : { 'Content-Type': 'application/json' },
          body: options.body === undefined ? undefined : JSON.stringify(options.body)
        });
      } catch (_) {
        if (!options.retryTransient || Date.now() - started > (options.retryForMs || 90000)) {
          throw new Error('Réseau temporairement inaccessible. Le travail est conservé ; réessaie dans un instant.');
        }
        options.onRetry?.('Render redémarre, nouvelle tentative…');
        await U.sleep(U.backoffDelay(attempt++));
        continue;
      }
      let data = {};
      try { data = await response.json(); } catch (_) { /* réponse vide */ }
      if (response.ok) return data;
      if (options.retryTransient && U.isTransientStatus(response.status)
          && Date.now() - started <= (options.retryForMs || 90000)) {
        options.onRetry?.(`Render redémarre, nouvelle tentative… (${response.status})`);
        await U.sleep(U.backoffDelay(attempt++));
        continue;
      }
      const error = new Error(data.detail || `Le serveur a répondu ${response.status} pendant cette étape.`);
      error.status = response.status;
      throw error;
    }
  }

  async function collerPressePapier(appliquer) {
    try {
      if (!navigator.clipboard || !navigator.clipboard.readText) throw new Error('indisponible');
      const texte = await navigator.clipboard.readText();
      if (!texte.trim()) { toast('Le presse-papiers est vide.', true); return; }
      appliquer(texte.trim());
    } catch (_) {
      toast('Accès au presse-papiers refusé par le navigateur : colle avec Ctrl+V / ⌘V.', true);
    }
  }

  /* ------------------------------------------------------------------
     RÉGLAGES
  ------------------------------------------------------------------ */

  function loadSettings() {
    try {
      const brut = JSON.parse(localStorage.getItem(SETTINGS_KEY) || 'null');
      return {
        mode: brut && brut.mode === 'rapide' ? 'rapide' : 'qualite',
        intensite: brut && [0, 1, 2, 3].includes(Number(brut.intensite)) ? Number(brut.intensite) : 2,
        theme: brut && ['auto', 'clair', 'noir'].includes(brut.theme) ? brut.theme : 'auto',
        fournisseur: brut && ['auto', 'gemini', 'groq', 'mistral'].includes(brut.fournisseur)
          ? brut.fournisseur : 'auto'
      };
    } catch (_) { return { mode: 'qualite', intensite: 2, theme: 'auto', fournisseur: 'auto' }; }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    syncSettingsUI();
  }

  /* ------------------------------------------------------------------
     THÈME — data-theme: "dark" / "light" / pas d'attribut = auto
     Les UI parlent encore "clair" / "noir" / "auto" à l'utilisateur.
  ------------------------------------------------------------------ */

  const mediaSombre = window.matchMedia ? window.matchMedia('(prefers-color-scheme: dark)') : null;

  function themeEffectif() {
    if (settings.theme === 'noir') return 'dark';
    if (settings.theme === 'clair') return 'light';
    return (mediaSombre && mediaSombre.matches) ? 'dark' : 'light';
  }

  function appliquerTheme() {
    const theme = themeEffectif();
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'dark' ? '#000000' : '#f5f5f7';
  }

  function syncThemeSeg() {
    // Dans la vue Paramètres, la seg "theme" reflète settings.theme.
    const seg = $('#theme-choices');
    if (!seg) return;
    const i = ['auto', 'clair', 'noir'].indexOf(settings.theme);
    seg.style.setProperty('--i', i);
    $$('button', seg).forEach((b, idx) => {
      b.setAttribute('aria-checked', idx === i ? 'true' : 'false');
    });
  }

  function bindTheme() {
    // Les boutons data-theme-choix sont créés au render de Paramètres ; le click
    // est géré par le délégateur global (handleSegClick).
    if (mediaSombre) {
      mediaSombre.addEventListener('change', () => {
        if (settings.theme === 'auto') appliquerTheme();
      });
    }
  }

  function resolutionPourMode() {
    return settings.mode === 'qualite' && serverConfig && serverConfig.allow_1080 ? '1080' : '720';
  }

  function libelleExport() {
    if (settings.mode === 'qualite') {
      return serverConfig && serverConfig.allow_1080
        ? 'Export actuel : 1080 × 1920 · 24 FPS · encodage plus fin (CRF 21).'
        : 'Export actuel : 720 × 1280 · 24 FPS · encodage plus fin (CRF 21). Le 1080 n’est pas activé sur cette instance.';
    }
    return 'Export actuel : 720 × 1280 · 24 FPS · priorité à la vitesse.';
  }

  function setSegIndex(seg, i) {
    if (!seg) return;
    seg.style.setProperty('--i', i);
    $$('button', seg).forEach((b, idx) => b.setAttribute('aria-checked', idx === i ? 'true' : 'false'));
  }

  function syncSettingsUI() {
    // Mode (Paramètres)
    setSegIndex($('#mode-cards'), settings.mode === 'rapide' ? 0 : 1);
    // Mode (Montage)
    setSegIndex($('#mode-cards-wrap-mont'), settings.mode === 'rapide' ? 0 : 1);
    // Intensité (Paramètres)
    setSegIndex($('#intensite-choices'), settings.intensite);
    // Intensité (Montage)
    setSegIndex($('#intensite-choices-mont'), settings.intensite);
    // Fournisseur
    setSegIndex($('#fournisseur-choices'), ['auto', 'gemini', 'groq', 'mistral'].indexOf(settings.fournisseur));
    // Thème
    syncThemeSeg();
    const txt = libelleExport();
    const ec = $('#export-courant'); if (ec) ec.textContent = txt;
    const ecp = $('#export-courant-parametres'); if (ecp) ecp.textContent = txt;
  }

  /* ------------------------------------------------------------------
     RENDU NAVIGATION
  ------------------------------------------------------------------ */

  function cloneTpl(id) {
    const t = document.getElementById(id);
    return t ? t.content.cloneNode(true) : null;
  }

  function renderNav() {
    const nav = $('#nav');
    nav.innerHTML = '<i class="ni"></i>' + SEC.map((s, i) =>
      `<button type="button" data-sec="${i}">${s}</button>`).join('');
    $$('button', nav).forEach((b, i) => {
      if (i === sec) b.setAttribute('aria-current', 'page');
      else b.removeAttribute('aria-current');
    });
    // Positionner le .ni
    requestAnimationFrame(() => {
      const ni = $('.ni', nav);
      const a = $('[aria-current=page]', nav);
      if (ni && a) { ni.style.top = a.offsetTop + 'px'; ni.style.height = a.offsetHeight + 'px'; }
    });
  }

  function closeDrawer() {
    document.body.classList.remove('menu');
    const bgr = $('#bgr');
    if (bgr) bgr.setAttribute('aria-expanded', 'false');
  }

  function renderTabs() {
    const tb = $('#tb');
    if (!tb) return;
    const ind = $('.ind', tb);
    $$('button', tb).forEach((b) => {
      b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
    });
    requestAnimationFrame(() => {
      const sel = $('[aria-selected=true]', tb);
      if (sel && ind) {
        ind.style.left = sel.offsetLeft + 'px';
        ind.style.width = sel.offsetWidth + 'px';
        sel.scrollIntoView({ inline: 'center', block: 'nearest' });
      }
    });
  }

  function mountTab(key) {
    const tbody = $('#tbody');
    if (!tbody) return;
    const tplId = {
      lien: 'tpl-tab-lien',
      ia: 'tpl-tab-ia',
      rst: 'tpl-tab-rst',
      sst: 'tpl-tab-sst',
      montage: 'tpl-tab-montage'
    }[key];
    tbody.replaceChildren(cloneTpl(tplId));
    tbody.className = '';
    void tbody.offsetWidth;
    tbody.className = 'anim';
    // Brancher les bindings spécifiques à l'onglet monté
    rebindTab(key);
  }

  function setTab(k) {
    tab = k;
    // Recréer les boutons de tabs si besoin (ils existent déjà si renderView a été appelé)
    renderTabs();
    mountTab(k);
    rebindAllForTab(k);
    updateGenButtons();
  }

  function renderView() {
    const h = $('#h');
    if (h) h.textContent = SEC[sec];
    renderNav();
    const view = $('#view');
    if (!view) return;
    view.className = '';
    void view.offsetWidth;
    view.className = 'anim';
    view.replaceChildren();

    if (sec === 0) {
      // CRÉER : hero + tabs + tbody
      view.append(cloneTpl('tpl-creer'));
      // Injecter les boutons d'onglets
      const tb = $('#tb');
      $$('.ind', tb).forEach((n) => n.remove()); // on n'en veut qu'un
      TABS.forEach(({ key, label }) => {
        const b = document.createElement('button');
        b.type = 'button'; b.setAttribute('role', 'tab');
        b.dataset.tab = key; b.textContent = label;
        tb.append(b);
      });
      const ind = document.createElement('i');
      ind.className = 'ind';
      tb.append(ind);
      mountTab(tab);
      renderTabs();
    } else if (sec === 1) {
      view.append(cloneTpl('tpl-historique'));
      renderHistoryShell();
    } else if (sec === 2) {
      view.append(cloneTpl('tpl-parametres'));
      // Peupler les cartes serveur/fournisseurs/training après le mount
      renderParametresShell();
    } else if (sec === 3) {
      view.append(cloneTpl('tpl-connexions'));
    }

    // Bindings communs à la vue
    rebindCommon();
    // Rebind les paramètres (fournisseurs, etc.) si on est dans Paramètres
    if (sec === 2) {
      syncSettingsUI();
      // Charger les données
      loadFournisseurs();
      renderTrainingProfiles();
      loadSante();
    }
    if (sec === 1) {
      loadHistory();
    }
    if (sec === 3) {
      loadIntegrations();
    }
    if (sec === 0) {
      loadConfig();
      requestJSON('/api/styles').then((d) => chargerStyles(d.styles)).catch(() => chargerStyles());
    }
    updateGenButtons();
  }

  function showSection(i) {
    sec = i;
    closeDrawer();
    renderView();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ------------------------------------------------------------------
     OVERLAY (dialogue modal)
  ------------------------------------------------------------------ */
  function ov(innerHTML) {
    const o = $('#ov');
    o.innerHTML = `<div class="sheet" role="dialog" aria-modal="true">${innerHTML}</div>`;
    o.hidden = false;
    const focusable = $('.sheet .btn', o);
    if (focusable) focusable.focus();
  }
  function closeOv() { $('#ov').hidden = true; }

  /* ------------------------------------------------------------------
     LOADER DE STYLE
  ------------------------------------------------------------------ */
  function chargerStyles(liste) {
    const styles = Array.isArray(liste) && liste.length ? liste : ['classique', 'jaune', 'centre'];
    ['lien', 'ia', 'montage'].forEach((type) => {
      const conteneur = $(`#styles-${type}`);
      if (!conteneur) return;
      conteneur.replaceChildren(...styles.map((style, index) => {
        const label = el('label', '', style);
        const input = document.createElement('input');
        input.type = 'radio';
        input.name = `style-${type}`;
        input.value = style;
        input.checked = index === 0;
        label.prepend(input);
        return label;
      }));
    });
  }

  /* ------------------------------------------------------------------
     RST NOMS UI (pour l'onglet RST — on utilise la seg "nombres de noms")
     NOTE : dans le nouveau design RsT, on a une seg dans la card "Génération"
     qui expose 3 noms / 5 noms (data-rst-noms). On la crée au montage.
  ------------------------------------------------------------------ */

  function ensureRstTabReady() {
    // Le DOM tpl-tab-rst n'a pas de seg "TOP N" ; on l'insère dynamiquement
    // dans la carte Génération (on la crée ici si absente).
    const tbody = $('#tbody'); if (!tbody) return;
    if ($('#rst-seg-noms', tbody)) return;
    // On ajoute une seg sous le compteur de jobs dans la première carte (source) ?
    // En réalité, la génération est actuellement lancée sans seg car on garde rstNombreNoms.
    // On ajoute la seg dans la carte de résultat ou on la met au-dessus du bouton btn-rst.
    const btnRst = $('#btn-rst');
    if (!btnRst) return;
    const wrap = el('label', 'fl');
    const sp = el('span'); sp.textContent = 'TOP N';
    const seg = el('div', 'seg');
    seg.id = 'rst-seg-noms';
    seg.setAttribute('role', 'radiogroup');
    seg.setAttribute('aria-label', 'Nombre de noms');
    seg.style.setProperty('--n', '2');
    seg.style.setProperty('--i', rstNombreNoms === 5 ? '1' : '0');
    const ind = document.createElement('i'); seg.append(ind);
    rstNomsChoix.forEach((n, idx) => {
      const b = document.createElement('button');
      b.type = 'button'; b.setAttribute('role', 'radio');
      b.dataset.rstNoms = String(n);
      b.setAttribute('aria-checked', String(n === rstNombreNoms));
      b.textContent = `${n} noms`;
      seg.append(b);
    });
    wrap.append(sp, seg);
    btnRst.parentElement.insertBefore(wrap, btnRst);
    syncRstNomsUI();
  }

  function syncRstNomsUI() {
    $$('[data-rst-noms]').forEach((b) => {
      const v = Number(b.dataset.rstNoms);
      b.setAttribute('aria-checked', String(v === rstNombreNoms));
    });
    const seg = $('#rst-seg-noms');
    if (seg) seg.style.setProperty('--i', rstNombreNoms === 5 ? '1' : '0');
    if (!rstNomsChoix.includes(rstNombreNoms)) return;
  }

  /* ------------------------------------------------------------------
     TRACKER (live)
  ------------------------------------------------------------------ */

  function setBusy(busy) {
    ['btn-analyser', 'btn-video', 'btn-rst', 'btn-sst', 'btn-reference', 'btn-montage',
      'btn-add-project', 'btn-launch-batch', 'btn-ia'].forEach((id) => {
      const bouton = $(id);
      if (!bouton) return;
      const dependDuDiagnostic = ['btn-montage', 'btn-add-project'].includes(id);
      bouton.disabled = busy || (dependDuDiagnostic && !diagnosticData) ||
        (id === 'btn-launch-batch' && batchDrafts.length === 0) ||
        (id === 'btn-ia' && !($('#ia-idee') && $('#ia-idee').value.trim().length >= 5));
    });
    const bd = $('#btn-diagnostic'); if (bd) bd.disabled = busy;
    const cc = $('#btn-cancel'); if (cc) cc.disabled = !busy;
  }

  function updateGenButtons() {
    setBusy(Boolean(activeJobId));
    // btn-ia dépend aussi du nombre de caractères
    const idee = $('#ia-idee');
    const bia = $('#btn-ia');
    if (idee && bia && !activeJobId) {
      bia.disabled = idee.value.trim().length < 5;
    }
  }

  function pillFor(s) {
    const cls = /Terminé|^Connecté/.test(s) ? 'ok'
      : s === 'Échec' ? 'er'
      : /Annulé|Non/.test(s) ? '' : 'run';
    return `<span class="pill ${cls}">${s}</span>`;
  }

  function renderSources(conteneur, data) {
    conteneur.replaceChildren();
    const sources = data.sources || [];
    const erreurs = data.source_errors || [];
    sources.forEach((source) => appendSource(conteneur, source));
    erreurs
      .filter((e) => !sources.some((s) => s.url === e.url && s.error))
      .forEach((e) => appendSource(conteneur, e, true));
  }

  function appendSource(conteneur, source, forcerErreur = false) {
    const echec = forcerErreur || ['error', 'invalid', 'unavailable', 'download_error', 'analysis_fallback', 'rejected_by_ai'].includes(source.status);
    const avertissement = source.status === 'analysis_fallback';
    const ligne = el('div', `source-item${echec && !avertissement ? ' error' : avertissement ? ' warn' : ''}`);
    ligne.append(el('i'));
    const texte = el('div');
    const titre = el('strong', '', source.url || `Source ${(Number(source.index) || 0) + 1}`);
    const dimensions = source.width
      ? `${fmtDuree(source.duration)} · ${source.width}×${source.height}${source.codec ? ` · ${source.codec}` : ''}`
      : (source.duration ? fmtDuree(source.duration) : '');
    const detail = el('span', '', source.error || source.analysis_warning || dimensions || source.status || '');
    texte.append(titre, detail);
    ligne.append(texte);
    conteneur.append(ligne);
  }

  function renderLiveTracker(data) {
    const live = $('#live');
    if (!live) return;
    live.hidden = false;
    // Première fois : remplir la structure
    if (!live.querySelector('#lp-title')) {
      live.replaceChildren(cloneTpl('tpl-live'));
      const cc = $('#btn-cancel', live);
      if (cc) cc.addEventListener('click', () => {
        if (!activeJobId || !confirm('Annuler ce travail et nettoyer ses fichiers temporaires ?')) return;
        cc.disabled = true;
        requestJSON(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, { body: {} })
          .then((d) => { const d2 = $('#lp-detail'); if (d2) d2.textContent = d.message || 'Annulation demandée…'; })
          .catch((err) => { toast(err.message, true); cc.disabled = false; });
      });
    }
    const titre = $('#lp-title');
    const pill = $('#lp-pill');
    const barre = $('#lp-bar');
    const detail = $('#lp-detail');
    const info = $('#lp-info');
    if (titre) titre.textContent = (data.type && (typeLabels[data.type] || data.type)) || 'Travail en cours';
    const pourcent = Number(data.progress || 0);
    if (barre) barre.style.width = pourcent + '%';
    const etat = statusLabels[data.status] || data.status;
    if (detail) detail.textContent = `${etat} · ${data.detail || ''}${data.status === 'queued' && data.queue_position ? ` · position ${data.queue_position}` : ''}`;
    if (info) info.textContent = iaUtiliseeLigne(data).replace(/^ ·/, '').trim();
    if (pill) pill.outerHTML = pillFor(etat + (pourcent ? ` · ${pourcent}%` : ''));
    // Affichage des sources en dessous
    const sl = live.querySelector('.source-list');
    if (sl) renderSources(sl, data);
    else if (data.sources && data.sources.length) {
      const c = el('div', 'source-list');
      renderSources(c, data);
      live.append(c);
    }
  }

  function hideLiveTracker() {
    const l = $('#live'); if (l) l.hidden = true;
  }

  /* ------------------------------------------------------------------
     waitJob / launchJob (API polling + rendu tracker)
  ------------------------------------------------------------------ */

  async function waitJob(id, type, statusElement) {
    activeJobId = id;
    activeJobType = type;
    setBusy(true);
    U.saveActiveJob(localStorage, { id, type, savedAt: Date.now() });
    for (;;) {
      let data;
      try {
        data = await requestJSON(`/api/jobs/${encodeURIComponent(id)}`, {
          retryTransient: true, retryForMs: 90000,
          onRetry: (message) => {
            setStatus(statusElement, message);
            const d = $('#lp-detail'); if (d) d.textContent = message;
          }
        });
      } catch (error) {
        if (error.status === 404) {
          U.clearActiveJob(localStorage);
          activeJobId = ''; activeJobType = '';
          setBusy(false);
          hideLiveTracker();
        }
        throw error;
      }
      renderLiveTracker(data);
      setStatus(statusElement, `${data.detail || statusLabels[data.status]} · ${data.progress || 0}%`);
      if (data.status === 'completed') {
        U.clearActiveJob(localStorage);
        activeJobId = ''; activeJobType = '';
        setBusy(false);
        hideLiveTracker();
        if (data.url) showResult(data.url, data.drive, data.title);
        loadHistory();
        return data;
      }
      if (data.status === 'failed' || data.status === 'cancelled') {
        U.clearActiveJob(localStorage);
        activeJobId = ''; activeJobType = '';
        setBusy(false);
        hideLiveTracker();
        throw new Error(data.error || (data.status === 'cancelled' ? 'Travail annulé.' : 'Le travail a échoué.'));
      }
      await U.sleep(1800);
    }
  }

  async function launchJob(type, body, statusElement) {
    const data = await requestJSON(`/api/jobs/${type}`, {
      body, retryTransient: true, retryForMs: 45000,
      onRetry: (message) => setStatus(statusElement, message)
    });
    return waitJob(data.job_id, type, statusElement);
  }

  /* ------------------------------------------------------------------
     VOIX OFF IMPORTÉE
  ------------------------------------------------------------------ */

  const VOIX_SECTIONS = {
    lien: { input: 'voix-lien', etat: 'voix-lien-etat', clear: 'voix-lien-clear' },
    rst: { input: 'voix-rst', etat: 'voix-rst-etat', clear: 'voix-rst-clear' },
    montage: { input: 'voix-montage', etat: 'voix-montage-etat', clear: 'voix-montage-clear' }
  };

  function voixOffMaxMo() {
    return Number(serverConfig && serverConfig.voix_off_max_mo) || 25;
  }
  function voixOffExtensions() {
    return (serverConfig && serverConfig.voix_off_extensions)
      || ['.aac', '.m4a', '.mp3', '.ogg', '.opus', '.wav'];
  }

  function reinitialiserVoixOff(section, message) {
    const refs = VOIX_SECTIONS[section];
    if (!refs) return;
    voixOff[section] = '';
    const champ = $(refs.input); if (champ) champ.value = '';
    const etat = $(refs.etat);
    if (!etat) return;
    etat.textContent = message
      || `Aucune voix off : la vidéo restera muette. Formats ${voixOffExtensions().join(', ')}, ${voixOffMaxMo()} Mo maximum.`;
    etat.classList.remove('success', 'error');
  }

  async function televerserVoixOff(section) {
    const refs = VOIX_SECTIONS[section];
    const champ = $(refs.input);
    const etat = $(refs.etat);
    const fichier = champ && champ.files && champ.files[0];
    if (!fichier) { reinitialiserVoixOff(section); return; }

    const extension = `.${(fichier.name.split('.').pop() || '').toLowerCase()}`;
    if (!voixOffExtensions().includes(extension)) {
      reinitialiserVoixOff(section, `Format ${extension} non accepté. Formats : ${voixOffExtensions().join(', ')}.`);
      etat.classList.add('error'); toast('Format audio non pris en charge pour la voix off.', true);
      return;
    }
    if (fichier.size > voixOffMaxMo() * 1024 * 1024) {
      reinitialiserVoixOff(section, `Fichier trop lourd (${(fichier.size / 1048576).toFixed(1)} Mo). Limite : ${voixOffMaxMo()} Mo.`);
      etat.classList.add('error'); toast('Voix off trop lourde.', true);
      return;
    }
    etat.classList.remove('success', 'error');
    etat.textContent = `Import de « ${fichier.name} » et vérification FFprobe…`;
    champ.disabled = true;
    try {
      const reponse = await fetch(`/api/voixoff?nom=${encodeURIComponent(fichier.name)}`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: fichier
      });
      let donnees = {};
      try { donnees = await reponse.json(); } catch (_) {}
      if (!reponse.ok) throw new Error(donnees.detail || `Le serveur a répondu ${reponse.status}.`);
      voixOff[section] = donnees.voix_off || '';
      etat.textContent =
        `Voix off « ${donnees.nom || fichier.name} » importée · ${fmtDuree(donnees.duree)} · ` +
        `${(Number(donnees.taille_octets || fichier.size) / 1048576).toFixed(1)} Mo · ` +
        `conservée ${donnees.expire_dans_heures || 6} h.`;
      etat.classList.add('success');
      toast('Voix off importée : elle sera montée sur la vidéo.');
    } catch (error) {
      reinitialiserVoixOff(section, `Voix off refusée : ${error.message}`);
      etat.classList.add('error'); toast(error.message, true);
    } finally { champ.disabled = false; }
  }

  function bindVoixOffForSection(section) {
    const refs = VOIX_SECTIONS[section]; if (!refs) return;
    const input = $(refs.input);
    const clear = $(refs.clear);
    if (input && !input._bound) {
      input.addEventListener('change', () => televerserVoixOff(section));
      input._bound = true;
    }
    if (clear && !clear._bound) {
      clear.addEventListener('click', () => {
        reinitialiserVoixOff(section, 'Voix off retirée : cette vidéo sera muette.');
      });
      clear._bound = true;
    }
    reinitialiserVoixOff(section);
  }

  /* ------------------------------------------------------------------
     RsT multiple
  ------------------------------------------------------------------ */

  function rstLienCourt(lien) {
    const texte = String(lien || '');
    const morceaux = texte.split('/').filter(Boolean);
    const identifiant = morceaux[morceaux.length - 1] || texte;
    const auteur = (texte.match(/@[\w.\-]+/) || [''])[0];
    return auteur ? `${auteur} · ${identifiant}` : identifiant;
  }

  function rstCompterLiens() {
    const textarea = $('#rst-liens'); if (!textarea) return;
    const parsed = U.parseRstLinks(textarea.value, RST_MAX);
    const brut = textarea.value.split(/\r?\n/).filter((l) => l.trim()).length;
    const c = $('#rst-liens-counter');
    if (c) {
      c.textContent = `${parsed.links.length}/${RST_MAX} liens${parsed.duplicates.length ? ` · ${parsed.duplicates.length} doublon(s)` : ''}`;
      c.classList.toggle('over', brut > RST_MAX || parsed.errors.length > 0);
    }
    const s = $('#statut-rst');
    if (parsed.errors.length) setStatus(s, parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
    else if (parsed.links.length) setStatus(s, `${parsed.links.length} lien(s) TikTok prêt(s) : un travail sera lancé par lien.`);
    else setStatus(s, `Colle 1 à ${RST_MAX} liens TikTok de départ, un par ligne.`);
    return parsed;
  }

  function lienTelechargement(url, libelle, nomFichier) {
    const a = el('a', '', libelle);
    a.href = url; a.download = nomFichier || ''; return a;
  }

  function foundItem(video) {
    const item = el('div', `found-item${video.selected ? ' selected' : ''}`);
    const pending = !video.selected && video.validation_status === 'awaiting_visual_ai';
    const ic = el('div', 'found-icon');
    ic.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m10 8 6 4-6 4z"/></svg>';
    item.append(ic);
    const info = el('div', 'found-info');
    info.append(el('strong', '', `@${video.author || 'tiktok'}`));
    info.append(el('span', 'found-title', video.title || '(vidéo sans légende)'));
    let meta = `${fmtDuree(video.duration)} · ${video.origin || 'recherche TikTok'}`;
    if (typeof video.score_texte === 'number') meta += ` · pertinence texte ${Math.round(video.score_texte * 100)}%`;
    info.append(el('span', 'found-meta', meta));
    if (video.nom) info.append(el('span', 'found-nom', `Nom recherché : ${video.nom}`));
    if (pending) info.append(el('span', 'found-meta', 'Validation visuelle Gemini en cours — aucune retenue annoncée'));
    if (!video.selected && video.rejet) info.append(el('span', 'found-rejet', `Écartée : ${video.rejet}`));
    item.append(info);
    const side = el('div', 'found-side');
    side.append(el('span', `badge ${video.selected ? 'ok' : pending ? '' : 'ko'}`, video.selected ? 'retenue' : pending ? 'en validation' : 'écartée'));
    const ouvrir = el('a', 'found-open', 'Ouvrir ↗');
    ouvrir.href = video.url; ouvrir.target = '_blank'; ouvrir.rel = 'noopener';
    side.append(ouvrir);
    item.append(side);
    return item;
  }

  function boutonAction(texte, handler) {
    const b = el('button', 'btn', texte);
    b.addEventListener('click', handler); return b;
  }

  function renderRstJobs() {
    const liste = $('#job-list');
    const blocJobs = $('#rst-jobs');
    if (!liste || !blocJobs) return;
    liste.replaceChildren();
    blocJobs.classList.toggle('hidden', rstJobs.length === 0);
    if (!rstJobs.length) { const c = $('#rst-jobs-counter'); if (c) c.textContent = ''; return; }
    const actifs = rstJobs.filter((e) => !ETATS_FINAL.includes((rstEtats[e.id] || {}).status)).length;
    const c = $('#rst-jobs-counter'); if (c) c.textContent = `${actifs} en cours · ${rstJobs.length} travail(aux)`;

    rstJobs.forEach((entree, index) => {
      const data = rstEtats[entree.id] || { status: 'queued', progress: 0, detail: 'En attente sur Render' };
      const fini = ETATS_FINAL.includes(data.status);
      const carte = el('article', `job-card${data.status === 'completed' ? ' done' : data.status === 'failed' ? ' ko' : ''}`);
      carte.dataset.job = entree.id;
      const haut = el('div', 'job-top');
      const titre = el('p', 'job-title');
      titre.append(el('span', 'job-index', `RsT ${index + 1}/${rstJobs.length}`));
      titre.append(document.createTextNode(entree.titre || rstLienCourt(entree.lien)));
      haut.append(titre, el('span', 'job-pct', `${Number(data.progress || 0)}%`));
      carte.append(haut);
      carte.append(el('p', 'job-detail',
        `${statusLabels[data.status] || data.status} · ${data.detail || ''}` +
        (data.status === 'queued' && data.queue_position ? ` · position ${data.queue_position}` : '') +
        iaUtiliseeLigne(data)));
      const barre = el('div', 'job-progress');
      barre.append(Object.assign(el('i'), { style: `width:${Number(data.progress || 0)}%` }));
      carte.append(barre);
      if (data.found_videos && data.found_videos.length)
        carte.append(el('p', 'job-found', `${data.found_videos.length} vidéo(s) réellement trouvée(s)`));
      if (data.error) carte.append(el('p', 'job-error', data.error));
      if (data.livraison_erreur) carte.append(el('p', 'job-note', `Livraison complémentaire incomplète : ${data.livraison_erreur} La vidéo déjà rendue reste téléchargeable.`));
      if (data.script_erreur) carte.append(el('p', 'job-note', `Script .txt non livré : ${data.script_erreur} La vidéo reste téléchargeable.`));
      if (data.voix_erreur) carte.append(el('p', 'job-note', `Voix off non générée : ${data.voix_erreur} Le script reste téléchargeable.`));

      const actions = el('div', 'job-actions');
      if (data.url) {
        actions.append(boutonAction('Voir', () => showResult(data.url, data.drive, data.title || entree.titre)));
        actions.append(lienTelechargement(data.url, 'Vidéo (muette)', ''));
      }
      if (data.script_url) actions.append(lienTelechargement(data.script_url, 'Script .txt', data.script_nom || 'script.txt'));
      if (data.voix_url) actions.append(lienTelechargement(data.voix_url, 'Voix off .mp3', data.voix_nom || 'voix-off.mp3'));
      if (!fini) {
        actions.append(boutonAction('Annuler', async (ev) => {
          const b = ev.currentTarget;
          if (!confirm(`Annuler « ${entree.titre || rstLienCourt(entree.lien)} » ?`)) return;
          b.disabled = true;
          try { await requestJSON(`/api/jobs/${encodeURIComponent(entree.id)}/cancel`, { body: {} }); }
          catch (err) { toast(err.message, true); b.disabled = false; }
        }));
      } else {
        actions.append(boutonAction('Retirer', () => {
          rstJobs = rstJobs.filter((a) => a.id !== entree.id);
          delete rstEtats[entree.id];
          U.saveRstJobs(localStorage, rstJobs);
          renderRstJobs(); renderRstFoundBlocs();
        }));
      }
      const ouvrir = el('a', '', 'Lien de départ ↗');
      ouvrir.href = entree.lien; ouvrir.target = '_blank'; ouvrir.rel = 'noopener';
      actions.append(ouvrir);
      carte.append(actions);
      liste.append(carte);
    });
  }

  function renderRstFoundBlocs() {
    const conteneur = $('#rst-found');
    const resultats = $('#rst-resultats');
    const resume = $('#rst-resume');
    const compteur = $('#rst-compteur');
    if (!conteneur || !resultats) return;
    const avec = rstJobs.filter((e) => (rstEtats[e.id] || {}).found_videos);
    resultats.classList.toggle('hidden', avec.length === 0);
    if (!avec.length) { conteneur.replaceChildren(); if (compteur) compteur.textContent = ''; return; }
    let total = 0, ret = 0;
    conteneur.replaceChildren();
    avec.forEach((entree, index) => {
      const data = rstEtats[entree.id] || {};
      const videos = data.found_videos || [];
      const rqs = data.search_queries || [];
      const retenues = videos.filter((v) => v.selected).length;
      total += videos.length; ret += retenues;
      const bloc = el('section', 'found-bloc');
      bloc.dataset.job = entree.id;
      const tete = el('div', 'list-head');
      const h = document.createElement('h4'); h.textContent = `RsT ${index + 1} · ${entree.titre || rstLienCourt(entree.lien)}`;
      tete.append(h, el('span', 'counter', `${videos.length} trouvée(s) · ${retenues} retenue(s)`));
      bloc.append(tete);
      const noms = data.noms || [];
      if (noms.length) {
        const couv = data.noms_couverts || [];
        bloc.append(el('p', 'hint', `TOP ${noms.length} noms extraits de la vidéo de départ : ${noms.join(' · ')}.` + (couv.length ? ` ${couv.length} nom(s) réellement présent(s) dans le montage.` : '')));
      }
      bloc.append(el('p', 'hint', videos.length ? `Recherches réellement effectuées : ${rqs.join(' · ')}. Aucune vidéo inventée : chaque ligne provient de TikTok.` : 'Recherche en cours…'));
      const liste = el('div', 'found-list');
      videos.forEach((v) => liste.append(foundItem(v)));
      bloc.append(liste);
      conteneur.append(bloc);
    });
    if (compteur) compteur.textContent = `${total} trouvée(s) · ${ret} retenue(s) sur ${avec.length} travail(aux)`;
    if (resume) resume.textContent = 'Chaque bloc correspond à un lien de départ et à son propre travail RsT. Aucune donnée inventée.';
  }

  async function suivreRstJob(entree) {
    if (rstSuivis.has(entree.id)) return;
    rstSuivis.add(entree.id);
    try {
      for (;;) {
        let data;
        try {
          data = await requestJSON(`/api/jobs/${encodeURIComponent(entree.id)}`, { retryTransient: true, retryForMs: 90000 });
        } catch (error) {
          if (error.status === 404) {
            rstEtats[entree.id] = { status: 'failed', progress: 0, detail: 'Travail expiré', error: 'Travail introuvable ou expiré (6 h).' };
            renderRstJobs(); return;
          }
          rstEtats[entree.id] = { ...(rstEtats[entree.id] || {}), detail: error.message };
          renderRstJobs(); await U.sleep(4000); continue;
        }
        rstEtats[entree.id] = data;
        renderRstJobs();
        if (Array.isArray(data.found_videos)) renderRstFoundBlocs();
        if (ETATS_FINAL.includes(data.status)) {
          if (data.status === 'completed') {
            if (data.url) showResult(data.url, data.drive, data.title || entree.titre);
            toast(`RsT terminé : ${entree.titre || rstLienCourt(entree.lien)}`);
          } else if (data.status === 'failed') {
            toast(`RsT échoué : ${data.error || 'travail interrompu'}`, true);
          }
          const restants = rstJobs.filter((a) => !ETATS_FINAL.includes((rstEtats[a.id] || {}).status));
          U.saveRstJobs(localStorage, restants);
          loadHistory(); majStatutRst(); return;
        }
        await U.sleep(2200);
      }
    } finally { rstSuivis.delete(entree.id); }
  }

  function majStatutRst() {
    const actifs = rstJobs.filter((e) => !ETATS_FINAL.includes((rstEtats[e.id] || {}).status));
    const termines = rstJobs.length - actifs.length;
    const s = $('#statut-rst');
    if (!s) return;
    if (actifs.length) setStatus(s, `${actifs.length} travail(aux) RsT en cours, ${termines} terminé(s). Le suivi reprend après une actualisation.`);
    else if (rstJobs.length) setStatus(s, `${rstJobs.length} travail(aux) RsT terminé(s).`, 'success');
    setBusy(Boolean(activeJobId) || actifs.length > 0);
  }

  function resumeRstJobs() {
    rstJobs = U.loadRstJobs(localStorage);
    if (!rstJobs.length) return;
    renderRstJobs();
    const s = $('#statut-rst'); if (s) setStatus(s, 'Reprise du suivi des travaux RsT après actualisation…');
    rstJobs.forEach(suivreRstJob);
  }

  /* ------------------------------------------------------------------
     IA VIDEO
  ------------------------------------------------------------------ */
  function bindIaVideoTab() {
    const button = $('#btn-ia');
    const statut = $('#statut-ia');
    const caseVoix = $('#ia-voixoff');
    const etatVoix = $('#ia-voixoff-etat');
    const idee = $('#ia-idee');
    const cn = $('#cn-ia');
    if (!button || !caseVoix || !etatVoix || !idee) return;

    const reinit = () => {
      etatVoix.textContent = caseVoix.checked
        ? 'Le script rédigé sera lu par edge-tts : la vidéo partira avec sa voix off.'
        : 'Non cochée : la vidéo reste muette (sous-titres uniquement).';
    };
    requestJSON('/api/sante').then((d) => {
      caseVoix.disabled = !d.voix_off_generee_disponible;
      if (caseVoix.disabled) caseVoix.checked = false;
      if (!d.voix_off_generee_disponible) {
        etatVoix.textContent = 'Synthèse vocale indisponible sur ce serveur (module edge-tts absent) : la vidéo restera muette.';
      }
    }).catch(() => {
      caseVoix.disabled = true; caseVoix.checked = false;
      etatVoix.textContent = 'État de la synthèse vocale inconnu (API injoignable) : la vidéo restera muette.';
    });
    caseVoix.addEventListener('change', reinit);

    const updateIdea = () => {
      if (idee.value.length > 2000) idee.value = idee.value.slice(0, 2000);
      const n = idee.value.trim().length;
      if (cn) cn.textContent = `${idee.value.length} / 2000`;
      if (button) button.disabled = Boolean(activeJobId) || n < 5;
    };
    idee.addEventListener('input', updateIdea);
    updateIdea();

    button.addEventListener('click', async () => {
      const ideeV = idee.value.trim();
      if (ideeV.length < 5) { setStatus(statut, 'Décris ton idée en quelques mots (5 caractères minimum).', 'error'); return; }
      button.disabled = true;
      setStatus(statut, 'Rédaction du script, recherche et validation des images…');
      try {
        await launchJob('ia-video', {
          titre: ideeV.slice(0, 120), idee: ideeV, voix_off: Boolean(caseVoix.checked),
          style: (document.querySelector('input[name="style-ia"]:checked') || {}).value || 'classique',
          fournisseur: settings.fournisseur, idempotency_key: uuid()
        }, statut);
        setStatus(statut, 'Vidéo IA terminée.', 'success');
      } catch (error) { setStatus(statut, error.message, 'error'); }
      finally { button.disabled = Boolean(activeJobId) || idee.value.trim().length < 5; }
    });
  }

  /* ------------------------------------------------------------------
     LIEN → VIDEO
  ------------------------------------------------------------------ */
  function bindLienTab() {
    const btnColler = $('#lien-coller');
    const btnAnalyser = $('#btn-analyser');
    const btnVideo = $('#btn-video');
    if (btnColler && !btnColler._bound) {
      btnColler.addEventListener('click', () => collerPressePapier((t) => { const i = $('#lien-input'); if (i) i.value = t.split(/\r?\n/)[0].trim(); }));
      btnColler._bound = true;
    }
    bindVoixOffForSection('lien');
    if (btnAnalyser && !btnAnalyser._bound) {
      btnAnalyser.addEventListener('click', () => guards.analyser.run(async () => {
        const statut = $('#statut-analyser');
        const lien = $('#lien-input').value.trim();
        if (!lien) { setStatus(statut, 'Ajoute un lien source (article, page ou TikTok).', 'error'); return; }
        btnAnalyser.disabled = true;
        setStatus(statut, 'Analyse du contenu…');
        try {
          const data = await launchJob('analyser', { lien, fournisseur: settings.fournisseur, idempotency_key: uuid() }, statut);
          const h = $('#hook'); const c = $('#corps'); const m = $('#mot-cle');
          if (h) h.value = data.hook || '';
          if (c) c.value = data.corps || '';
          if (m) m.value = data.mot_cle_broll || '';
          const card = $('#etape-script'); if (card) { card.classList.remove('lock'); card.removeAttribute('inert'); }
          setStatus(statut, 'Script prêt — relis-le et modifie-le avant de générer.', 'success');
        } catch (error) { setStatus(statut, error.message, 'error'); }
        finally { btnAnalyser.disabled = Boolean(activeJobId); }
      }));
      btnAnalyser._bound = true;
    }
    if (btnVideo && !btnVideo._bound) {
      btnVideo.addEventListener('click', () => guards.video.run(async () => {
        const statut = $('#statut-video');
        btnVideo.disabled = true;
        setStatus(statut, 'Préparation de la vidéo…');
        try {
          const style = document.querySelector('input[name="style-lien"]:checked')?.value || 'classique';
          await launchJob('video', {
            hook: $('#hook')?.value || '', corps: $('#corps')?.value || '',
            mot_cle_broll: $('#mot-cle')?.value || '', style,
            mode: settings.mode, voix_off: voixOff.lien, idempotency_key: uuid()
          }, statut);
          setStatus(statut, 'Vidéo terminée.', 'success');
        } catch (error) { setStatus(statut, error.message, 'error'); }
        finally { btnVideo.disabled = Boolean(activeJobId); }
      }));
      btnVideo._bound = true;
    }
  }

  /* ------------------------------------------------------------------
     RsT TAB binding
  ------------------------------------------------------------------ */
  function bindRstTab() {
    ensureRstTabReady();
    bindVoixOffForSection('rst');
    const textarea = $('#rst-liens');
    const btnColler = $('#rst-coller');
    const btnClear = $('#rst-clear');
    const btnRst = $('#btn-rst');
    if (textarea && !textarea._bound) {
      textarea.addEventListener('input', rstCompterLiens); textarea._bound = true;
    }
    if (btnColler && !btnColler._bound) {
      btnColler.addEventListener('click', () => collerPressePapier((t) => {
        const ex = $('#rst-liens').value.trim();
        $('#rst-liens').value = ex ? `${ex}\n${t}` : t;
        rstCompterLiens(); toast('Liens collés dans RsT.');
      }));
      btnColler._bound = true;
    }
    if (btnClear && !btnClear._bound) {
      btnClear.addEventListener('click', () => { $('#rst-liens').value = ''; rstCompterLiens(); setStatus($('#statut-rst'), 'Liens RsT effacés.'); });
      btnClear._bound = true;
    }
    if (btnRst && !btnRst._bound) {
      btnRst.addEventListener('click', () => guards.rst.run(async () => {
        const statut = $('#statut-rst');
        const parsed = U.parseRstLinks($('#rst-liens').value, RST_MAX);
        if (parsed.errors.length) { setStatus(statut, parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error'); return; }
        if (!parsed.links.length) { setStatus(statut, `Colle 1 à ${RST_MAX} liens TikTok de départ, un par ligne.`, 'error'); return; }
        const actifs = rstJobs.filter((e) => !ETATS_FINAL.includes((rstEtats[e.id] || {}).status));
        if (actifs.length + parsed.links.length > RST_MAX) { setStatus(statut, `${RST_MAX} travaux RsT maximum en parallèle (${actifs.length} déjà en cours).`, 'error'); return; }
        btnRst.disabled = true;
        setStatus(statut, `Lancement de ${parsed.links.length} travail(aux) RsT…`);
        let lances = 0; const echecs = [];
        for (const lien of parsed.links) {
          const titre = `RsT · ${rstLienCourt(lien)}`;
          try {
            const rep = await requestJSON('/api/jobs/rst', {
              body: { titre, lien, mode: settings.mode, intensite_transitions: settings.intensite,
                nombre_noms: rstNombreNoms, fournisseur: settings.fournisseur,
                voix_off: voixOff.rst, idempotency_key: uuid() },
              retryTransient: true, retryForMs: 45000,
              onRetry: (m) => setStatus(statut, m)
            });
            const entree = { id: rep.job_id, lien, titre };
            if (!rstJobs.some((a) => a.id === entree.id)) rstJobs.push(entree);
            U.saveRstJobs(localStorage, rstJobs);
            renderRstJobs(); suivreRstJob(entree);
            lances += 1;
          } catch (error) { echecs.push(`${rstLienCourt(lien)} : ${error.message}`); }
        }
        btnRst.disabled = false;
        if (lances) { $('#rst-liens').value = ''; rstCompterLiens(); toast(`${lances} travail(aux) RsT lancé(s).`); }
        setStatus(statut,
          echecs.length ? `${lances} travail(aux) lancé(s). Échec : ${echecs.join(' · ')}` :
          `${lances} travail(aux) RsT lancé(s). Render les traite l’un après l’autre ; le suivi survit à une actualisation.`,
          echecs.length ? 'error' : 'success');
      }));
      btnRst._bound = true;
    }
    rstCompterLiens();
    renderRstJobs();
    renderRstFoundBlocs();
  }

  /* ------------------------------------------------------------------
     SsT
  ------------------------------------------------------------------ */
  function syncSstUI() {
    $$('[data-sst-nombre]').forEach((b) => b.setAttribute('aria-checked', String(Number(b.dataset.sstNombre) === sstNombreNoms)));
    const seg = $('#sst-nombre-choices'); if (seg) seg.style.setProperty('--i', sstNombreNoms === 5 ? '1' : '0');
    $$('[data-sst-nom]').forEach((input) => {
      const vis = Number(input.dataset.sstNom) < sstNombreNoms;
      input.hidden = !vis; input.required = vis;
    });
    const c = $('#sst-noms-counter'); if (c) c.textContent = `0/${sstNombreNoms}`;
  }
  function collectSstNames() {
    const names = [...$$('[data-sst-nom]')].slice(0, sstNombreNoms).map((i) => i.value.trim());
    const c = $('#sst-noms-counter'); if (c) c.textContent = `${names.filter(Boolean).length}/${sstNombreNoms}`;
    return names;
  }

  function bindSstTab() {
    const lien = $('#sst-lien');
    const btnColler = $('#sst-coller');
    const openTrain = $('#sst-open-training');
    const selProf = $('#sst-profil');
    const btnSst = $('#btn-sst');

    if (btnColler && !btnColler._bound) {
      btnColler.addEventListener('click', () => collerPressePapier((t) => { const i = $('#sst-lien'); if (i) i.value = t.split(/\r?\n/)[0].trim(); }));
      btnColler._bound = true;
    }
    $$('[data-sst-nombre]').forEach((b) => {
      if (b._bound) return;
      b.addEventListener('click', () => { sstNombreNoms = Number(b.dataset.sstNombre); syncSstUI(); });
      b._bound = true;
    });
    $$('[data-sst-nom]').forEach((input) => {
      if (input._bound) return;
      input.addEventListener('input', collectSstNames); input._bound = true;
    });
    if (openTrain && !openTrain._bound) {
      openTrain.addEventListener('click', () => showSection(2)); openTrain._bound = true;
    }
    if (selProf && !selProf._bound) {
      selProf.addEventListener('change', () => {
        const p = trainingProfiles.find((pr) => pr.id === selProf.value);
        if (!p) { setStatus($('#statut-sst'), ''); return; }
        const nb = Number((p.donnees || {}).sources_exemples_analysees || 0);
        const nbAuto = Number(p.sources_automatiquement_ajoutees || p.source_count || 0);
        const conf = Math.round(Number((p.donnees || {}).confiance_moyenne || 0) * 100);
        if (!nb && !nbAuto && !conf) {
          setStatus($('#statut-sst'),
            'Ce profil est enregistré mais pas encore entraîné (0 source, 0 exemple analysé, confiance 0 %). Ajoute des exemples dans Paramètres puis clique sur « Analyser avec l’IA et alimenter ».',
            'error');
        } else {
          setStatus($('#statut-sst'),
            `Profil « ${p.nom_sujet || 'Sujet'} » sélectionné : les candidates seront aussi comparées à sa signature visuelle.`,
            'success');
        }
      });
      selProf._bound = true;
    }
    renderTrainingProfileSelectors();
    if (btnSst && !btnSst._bound) {
      btnSst.addEventListener('click', () => guards.sst.run(async () => {
        const lienV = $('#sst-lien').value.trim();
        const names = collectSstNames();
        const parsed = U.normalizeTikTokLink(lienV);
        if (!parsed.ok) { setStatus($('#statut-sst'), `Lien source invalide : ${parsed.error}.`, 'error'); return; }
        if (names.some((n) => !n) || new Set(names.map((n) => n.toLocaleLowerCase())).size !== names.length) {
          setStatus($('#statut-sst'), `Saisis exactement ${sstNombreNoms} noms différents.`, 'error'); return;
        }
        btnSst.disabled = true;
        setStatus($('#statut-sst'), 'SsT lance une recherche distincte pour chaque nom…');
        try {
          await launchJob('sst', {
            titre: `SsT · TOP ${sstNombreNoms}`,
            lien: parsed.url, nombre_noms: sstNombreNoms, noms: names,
            profil_id: $('#sst-profil').value, mode: settings.mode,
            intensite_transitions: settings.intensite, fournisseur: settings.fournisseur,
            voix_off: voixOff.rst,
            idempotency_key: uuid()
          }, $('#statut-sst'));
          setStatus($('#statut-sst'), `SsT terminé : ${sstNombreNoms} noms utilisés tels quels.`, 'success');
        } catch (error) { setStatus($('#statut-sst'), error.message, 'error'); }
        finally { btnSst.disabled = Boolean(activeJobId); }
      }));
      btnSst._bound = true;
    }
    syncSstUI();
  }

  /* ------------------------------------------------------------------
     MONTAGE multi-source
  ------------------------------------------------------------------ */
  function resetDiagnostic() {
    diagnosticData = null;
    const bm = $('#btn-montage'); if (bm) bm.disabled = true;
    const ba = $('#btn-add-project'); if (ba) ba.disabled = true;
    const d = $('#diagnostic'); if (d) d.classList.add('hidden');
  }
  function currentProject(risqueAccepte = false) {
    const style = document.querySelector('input[name="style-montage"]:checked')?.value || 'classique';
    return {
      titre: $('#mon-title')?.value.trim() || `Création ${batchDrafts.length + 1}`,
      hook: $('#mon-hook')?.value.trim() || '',
      corps: $('#mon-corps')?.value.trim() || '',
      liens_videos: U.parseLinks($('#mon-liens')?.value || '', 20).links,
      lien_reference_style: $('#mon-style-ref')?.value.trim() || '',
      style, resolution: resolutionPourMode(), mode: settings.mode,
      intensite_transitions: settings.intensite, voix_off: voixOff.montage,
      estimated_seconds: Number(diagnosticData?.estimated_seconds || 0),
      accepter_risque: risqueAccepte, idempotency_key: uuid()
    };
  }
  function renderBatchDrafts() {
    U.saveBatchDrafts(localStorage, batchDrafts);
    const c = $('#batch-counter'); if (c) c.textContent = `${batchDrafts.length}/6`;
    const bd = $('#batch-drafts'); if (bd) bd.classList.toggle('hidden', batchDrafts.length === 0);
    const blb = $('#btn-launch-batch'); if (blb) blb.disabled = batchDrafts.length === 0 || Boolean(activeJobId);
    const liste = $('#batch-draft-list'); if (!liste) return;
    liste.replaceChildren();
    batchDrafts.forEach((p, index) => {
      const item = el('div', 'draft-item');
      const haut = el('div', 'draft-top');
      haut.append(el('p', 'draft-title', p.titre));
      const ret = el('button', 'draft-remove', '×');
      ret.title = 'Retirer de la file';
      ret.addEventListener('click', () => { batchDrafts.splice(index, 1); renderBatchDrafts(); });
      haut.append(ret);
      item.append(haut);
      item.append(el('p', 'draft-meta',
        `${p.liens_videos.length} source(s) · ${Math.max(1, Math.ceil(p.estimated_seconds / 60))} min estimées` +
        `${p.lien_reference_style ? ' · référence de style' : ''} · ${p.mode === 'qualite' ? 'Qualité' : 'Rapide'}`));
      liste.append(item);
    });
    if (batchDrafts.length >= 6) { const b = $('#btn-add-project'); if (b) b.disabled = true; }
  }
  function resetProjectForm() {
    ['mon-title', 'mon-hook', 'mon-corps', 'mon-liens', 'mon-style-ref'].forEach((id) => { const i = $(id); if (i) i.value = ''; });
    resetDiagnostic(); validateLinksLocally(false);
  }
  function validateLinksLocally(planifierServeur = true) {
    const ta = $('#mon-liens'); if (!ta) return { links: [], errors: [], duplicates: [] };
    const parsed = U.parseLinks(ta.value, 20);
    const brut = ta.value.split(/\r?\n/).filter((l) => l.trim()).length;
    const c = $('#liens-counter');
    if (c) {
      c.textContent = `${Math.min(parsed.links.length, 20)}/20 liens${parsed.duplicates.length ? ` · ${parsed.duplicates.length} doublon(s)` : ''}`;
      c.classList.toggle('over', brut > 20 || parsed.errors.length > 0);
    }
    resetDiagnostic();
    clearTimeout(diagnosticTimer);
    const s = $('#statut-montage');
    if (parsed.errors.length) setStatus(s, parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
    else if (parsed.links.length) {
      setStatus(s, `${parsed.links.length} lien(s) de forme valide. Lance « Valider » pour vérifier l’accès réel.`);
      if (planifierServeur) diagnosticTimer = setTimeout(runDiagnostic, 1100);
    } else setStatus(s, 'Ajoute entre 1 et 20 liens TikTok.');
    return parsed;
  }
  function seuilEstimationRisque() {
    const v = Number(serverConfig?.seuil_estimation_risque);
    return Number.isFinite(v) && v > 0 ? v : U.SEUIL_ESTIMATION_RISQUE;
  }
  function estimationDansBudget(data) {
    if (!data) return true;
    if (typeof data.likely_within_budget === 'boolean') return data.likely_within_budget;
    if (typeof data.likely_under_10_minutes === 'boolean') return data.likely_under_10_minutes;
    return Number(data.estimated_seconds || 0) <= Number(data.budget_seconds || seuilEstimationRisque());
  }
  function renderDiagnostic(data) {
    diagnosticData = data;
    const d = $('#diagnostic'); if (d) d.classList.remove('hidden');
    const dt = $('#diagnostic-title'); if (dt) dt.textContent = `${data.valid_count} source(s) réellement accessible(s) · ${data.invalid_count} erreur(s)`;
    const es = $('#estimate'); if (es) {
      es.textContent = `${data.estimated_label} sur Render gratuit — estimation, pas une promesse.${data.warning ? ' ' + data.warning : ''}`;
      es.classList.toggle('warning', !estimationDansBudget(data));
    }
    const l = $('#diagnostic-sources'); if (l) {
      l.replaceChildren();
      (data.sources || []).forEach((s) => appendSource(l, s));
      (data.errors || []).filter((e) => !(data.sources || []).some((s) => s.url === e.url)).forEach((e) => appendSource(l, e, true));
      if (data.reference) appendSource(l, { ...data.reference, url: `Référence · ${data.reference.url}` }, data.reference.status !== 'valid');
    }
    const inutilisable = data.valid_count < 1 || data.reference?.status === 'unavailable' || Boolean(activeJobId);
    const bm = $('#btn-montage'); if (bm) bm.disabled = inutilisable;
    const ba = $('#btn-add-project'); if (ba) ba.disabled = inutilisable || batchDrafts.length >= 6;
    setStatus($('#statut-montage'), data.valid_count ? 'Validation terminée : état, durée et erreurs sont réels.' : 'Aucune source accessible.', data.valid_count ? 'success' : 'error');
  }
  async function runDiagnostic() {
    return guards.diagnostic.run(async () => {
      const parsed = U.parseLinks($('#mon-liens')?.value || '', 20);
      if (!parsed.links.length || parsed.errors.length) return;
      const bd = $('#btn-diagnostic'); if (bd) bd.disabled = true;
      setStatus($('#statut-montage'), `Validation réelle 0/${parsed.links.length} — FFprobe peut prendre un moment…`);
      try {
        const data = await requestJSON('/api/montage/diagnostic', {
          body: { liens_videos: parsed.links, lien_reference_style: $('#mon-style-ref')?.value.trim() || '' },
          retryTransient: true, retryForMs: 45000, onRetry: (m) => setStatus($('#statut-montage'), m)
        });
        renderDiagnostic(data);
      } catch (error) { resetDiagnostic(); setStatus($('#statut-montage'), error.message, 'error'); }
      finally { if (bd) bd.disabled = Boolean(activeJobId); }
    });
  }

  function bindMontageTab() {
    bindVoixOffForSection('montage');
    const btnColler = $('#btn-coller-sources');
    const btnDiag = $('#btn-diagnostic');
    const btnClear = $('#btn-clear-sources');
    const monLiens = $('#mon-liens');
    const monRef = $('#mon-style-ref');
    const refColler = $('#ref-coller');
    const btnRef = $('#btn-reference');
    const btnAdd = $('#btn-add-project');
    const btnBatch = $('#btn-launch-batch');
    const btnMon = $('#btn-montage');

    if (btnColler && !btnColler._bound) {
      btnColler.addEventListener('click', () => collerPressePapier((t) => {
        const ex = $('#mon-liens').value.trim();
        $('#mon-liens').value = ex ? `${ex}\n${t}` : t;
        validateLinksLocally(true); toast('Liens collés dans les sources.');
      })); btnColler._bound = true;
    }
    if (btnDiag && !btnDiag._bound) {
      btnDiag.addEventListener('click', () => runDiagnostic()); btnDiag._bound = true;
    }
    if (btnClear && !btnClear._bound) {
      btnClear.addEventListener('click', () => { $('#mon-liens').value = ''; resetDiagnostic(); validateLinksLocally(false); setStatus($('#statut-montage'), 'Sources effacées.'); });
      btnClear._bound = true;
    }
    if (monLiens && !monLiens._bound) { monLiens.addEventListener('input', () => validateLinksLocally(true)); monLiens._bound = true; }
    if (monRef && !monRef._bound) {
      monRef.addEventListener('input', () => { resetDiagnostic(); clearTimeout(diagnosticTimer); diagnosticTimer = setTimeout(runDiagnostic, 1100); });
      monRef._bound = true;
    }
    if (refColler && !refColler._bound) {
      refColler.addEventListener('click', () => collerPressePapier((t) => { const i = $('#mon-style-ref'); if (i) i.value = t.split(/\r?\n/)[0].trim(); }));
      refColler._bound = true;
    }
    if (btnRef && !btnRef._bound) {
      btnRef.addEventListener('click', () => guards.reference.run(async () => {
        const statut = $('#statut-reference');
        const lien = $('#mon-style-ref').value.trim();
        if (!lien) { setStatus(statut, 'Colle d’abord un lien TikTok de référence.', 'error'); return; }
        btnRef.disabled = true;
        setStatus(statut, 'Transcription et réécriture du script de la référence…');
        try {
          const data = await launchJob('reference', { lien, fournisseur: settings.fournisseur, idempotency_key: uuid() }, statut);
          const h = $('#mon-hook'); const c = $('#mon-corps');
          if (h) h.value = data.hook || '';
          if (c) c.value = data.corps || '';
          setStatus(statut, 'Script récupéré depuis la référence. Vérifie les sources puis valide-les.', 'success');
        } catch (error) { setStatus(statut, error.message, 'error'); }
        finally { btnRef.disabled = Boolean(activeJobId); }
      })); btnRef._bound = true;
    }
    if (btnAdd && !btnAdd._bound) {
      btnAdd.addEventListener('click', () => {
        const statut = $('#statut-montage');
        if (batchDrafts.length >= 6) { setStatus(statut, 'La file contient déjà 6 projets.', 'error'); return; }
        if (!$('#mon-hook')?.value.trim() || !$('#mon-corps')?.value.trim()) { setStatus(statut, 'Complète l’accroche et le script.', 'error'); return; }
        if (!diagnosticData?.valid_count) { setStatus(statut, 'Valide d’abord les sources de ce projet.', 'error'); return; }
        batchDrafts.push(currentProject(false)); renderBatchDrafts(); resetProjectForm();
        setStatus(statut, `Projet ajouté. Prépare le suivant ou lance les ${batchDrafts.length} création(s).`, 'success');
      }); btnAdd._bound = true;
    }
    if (btnBatch && !btnBatch._bound) {
      btnBatch.addEventListener('click', () => guards.batch.run(async () => {
        if (!batchDrafts.length) return;
        const seuil = seuilEstimationRisque();
        const risques = batchDrafts.filter((p) => p.estimated_seconds > seuil);
        if (risques.length && !confirm(`${risques.length} projet(s) risquent de dépasser ${Math.floor(seuil / 60)} minutes chacun sur Render gratuit.\n\nLancer quand même la file ?`)) return;
        btnBatch.disabled = true;
        const projets = batchDrafts.map((p) => ({ ...p, accepter_risque: p.estimated_seconds > seuil }));
        try {
          const res = await requestJSON('/api/jobs/montage/batch', { body: { projets, idempotency_key: uuid() }, retryTransient: true, retryForMs: 45000, onRetry: (m) => setStatus($('#statut-montage'), m) });
          batchDrafts = []; U.clearBatchDrafts(localStorage); renderBatchDrafts();
          setStatus($('#statut-montage'), `${res.count} création(s) placée(s) dans la file. Le suivi continue même après une actualisation.`, 'success');
          toast(`${res.count} création(s) ajoutée(s) à la file.`); loadHistory();
        } catch (error) { setStatus($('#statut-montage'), error.message, 'error'); }
        finally { btnBatch.disabled = batchDrafts.length === 0 || Boolean(activeJobId); }
      })); btnBatch._bound = true;
    }
    if (btnMon && !btnMon._bound) {
      btnMon.addEventListener('click', () => guards.montage.run(async () => {
        const statut = $('#statut-montage');
        if (!$('#mon-hook')?.value.trim() || !$('#mon-corps')?.value.trim()) { setStatus(statut, 'Complète l’accroche et le script.', 'error'); return; }
        if (!diagnosticData || !diagnosticData.valid_count) { setStatus(statut, 'Valide d’abord les sources et l’estimation.', 'error'); return; }
        let risqueAccepte = false;
        if (!estimationDansBudget(diagnosticData)) {
          risqueAccepte = confirm(`${diagnosticData.warning}\n\nLancer malgré le risque d’interruption à la limite globale ?`);
          if (!risqueAccepte) return;
        }
        btnMon.disabled = true;
        setStatus(statut, 'Création du travail idempotent…');
        try {
          await launchJob('montage', currentProject(risqueAccepte), statut);
          setStatus(statut, 'Montage terminé.', 'success');
        } catch (error) { setStatus(statut, error.message, 'error'); }
        finally { btnMon.disabled = Boolean(activeJobId) || !diagnosticData; }
      })); btnMon._bound = true;
    }

    renderBatchDrafts();
    validateLinksLocally(false);
  }

  function rebindTab(key) {
    // Commun aux onglets :
    if (key === 'lien') bindLienTab();
    else if (key === 'ia') bindIaVideoTab();
    else if (key === 'rst') bindRstTab();
    else if (key === 'sst') bindSstTab();
    else if (key === 'montage') bindMontageTab();
    // Charger les styles si pas encore chargés
    requestJSON('/api/styles').then((d) => chargerStyles(d.styles)).catch(() => chargerStyles());
  }

  function rebindAllForTab(key) {
    // Les bindings d'onglets sont posés par rebindTab ; pas d'autres.
  }

  /* ------------------------------------------------------------------
     GESTION DES SEG (délégateur global)
  ------------------------------------------------------------------ */
  function handleSegClick(b) {
    const seg = b.closest('.seg');
    if (!seg) return false;
    const k = Number(b.dataset.k !== undefined ? b.dataset.k
      : b.dataset.modeSeg !== undefined ? (b.dataset.modeSeg === 'rapide' ? 0 : 1)
      : b.dataset.intensiteMont !== undefined ? Number(b.dataset.intensiteMont)
      : b.dataset.modePar !== undefined ? (b.dataset.modePar === 'rapide' ? 0 : 1)
      : b.dataset.intensitePar !== undefined ? Number(b.dataset.intensitePar)
      : b.dataset.fournisseurPar !== undefined ? ['auto','gemini','groq','mistral'].indexOf(b.dataset.fournisseurPar)
      : b.dataset.themeChoix !== undefined ? ['auto','clair','noir'].indexOf(b.dataset.themeChoix)
      : b.dataset.rstNoms !== undefined ? (Number(b.dataset.rstNoms) === 5 ? 1 : 0)
      : b.dataset.sstNombre !== undefined ? (Number(b.dataset.sstNombre) === 5 ? 1 : 0)
      : -1);
    if (k < 0) return false;
    seg.style.setProperty('--i', k);
    $$('button', seg).forEach((x) => x.setAttribute('aria-checked', String(x === b)));

    // Appliquer les effets
    if (seg.id === 'theme-choices' || b.dataset.themeChoix !== undefined) {
      settings.theme = ['auto','clair','noir'][k]; saveSettings(); appliquerTheme();
    } else if (seg.id === 'mode-cards' || b.dataset.modePar !== undefined) {
      settings.mode = k === 0 ? 'rapide' : 'qualite'; saveSettings();
      // Synchroniser l'autre seg si présent
      const a = $('#mode-cards-wrap-mont'); if (a) setSegIndex(a, k);
      toast(settings.mode === 'qualite' ? 'Mode Qualité activé.' : 'Mode Rapide activé.');
    } else if (b.dataset.modeSeg !== undefined) {
      settings.mode = k === 0 ? 'rapide' : 'qualite'; saveSettings();
      const a = $('#mode-cards'); if (a) setSegIndex(a, k);
      toast(settings.mode === 'qualite' ? 'Mode Qualité activé.' : 'Mode Rapide activé.');
    } else if (seg.id === 'intensite-choices' || b.dataset.intensitePar !== undefined) {
      settings.intensite = k; saveSettings();
      const a = $('#intensite-choices-mont'); if (a) setSegIndex(a, k);
    } else if (b.dataset.intensiteMont !== undefined) {
      settings.intensite = k; saveSettings();
      const a = $('#intensite-choices'); if (a) setSegIndex(a, k);
    } else if (seg.id === 'fournisseur-choices' || b.dataset.fournisseurPar !== undefined) {
      settings.fournisseur = ['auto','gemini','groq','mistral'][k]; saveSettings();
      toast(settings.fournisseur === 'auto'
        ? 'Sélection automatique : l’ordre du serveur s’applique, bascule journalisée en cas d’échec.'
        : `Fournisseur « ${['Auto','Gemini','Groq','Mistral'][k]} » imposé : aucun repli silencieux.`);
    } else if (b.dataset.rstNoms !== undefined) {
      rstNombreNoms = Number(b.dataset.rstNoms); syncRstNomsUI();
    } else if (b.dataset.sstNombre !== undefined) {
      sstNombreNoms = Number(b.dataset.sstNombre); syncSstUI();
    }
    return true;
  }

  /* ------------------------------------------------------------------
     CONFIG / SANTÉ / FOURNISSEURS
  ------------------------------------------------------------------ */

  async function loadConfig() {
    try {
      serverConfig = await requestJSON('/api/config');
      const rst = serverConfig.rst || { candidats_max: 40, sources_max: 20, liens_par_lancement: 6 };
      const sl = $('#source-limit');
      if (sl) sl.textContent = `Paramètres après « ? » retirés et doublons supprimés. Durée maximale : ${serverConfig.max_source_seconds} s par source. Limite globale : ${Math.floor(serverConfig.job_timeout_seconds / 60)} min ${String(serverConfig.job_timeout_seconds % 60).padStart(2, '0')}.`;
      if (Array.isArray(rst.noms_choix) && rst.noms_choix.length) {
        rstNomsChoix = rst.noms_choix;
        if (!rstNomsChoix.includes(rstNombreNoms)) rstNombreNoms = rst.noms_defaut || rstNomsChoix[0];
        syncRstNomsUI();
      }
    } catch (error) { const sl = $('#source-limit'); if (sl) sl.textContent = error.message; }
    syncSettingsUI();
  }

  function ligneServeur(nom, actif, details) {
    const ligne = el('div', 'server-row');
    const g = el('b');
    const i = el('i', actif ? '' : 'off'); g.append(i, document.createTextNode(nom));
    const d = el('span', '', details);
    ligne.append(g, d);
    return ligne;
  }

  async function loadSante() {
    let sante = null;
    try { sante = await requestJSON('/api/sante'); } catch (_) {}
    const sr = $('#server-rows');
    if (sr) {
      sr.replaceChildren();
      if (!sante) { sr.append(el('p', 'server-row', 'API injoignable.')); }
      else {
        sr.append(
          ligneServeur('API', sante.ok, 'en ligne'),
          ligneServeur('Gemini', sante.gemini_configure, sante.gemini_configure ? 'configuré' : 'non configuré'),
          ligneServeur('FFmpeg', sante.ffmpeg_installe && sante.ffprobe_installe, sante.ffmpeg_installe ? 'prêt' : 'absent')
        );
      }
    }
    const complet = $('#parametres-server');
    if (complet && sante) {
      complet.replaceChildren(
        ligneServeur('API', sante.ok, sante.ok ? 'en ligne' : 'hors ligne'),
        ligneServeur('Gemini (scripts)', sante.gemini_configure, sante.gemini_configure ? 'configuré' : 'non configuré'),
        ligneServeur('Pexels (B-roll)', sante.pexels_configure, sante.pexels_configure ? 'configuré' : 'non configuré'),
        ligneServeur('FFmpeg / FFprobe', sante.ffmpeg_installe && sante.ffprobe_installe, sante.ffmpeg_installe ? 'installé' : 'absent'),
        ligneServeur('TikTok OAuth', sante.tiktok_configure, sante.tiktok_configure ? 'configuré' : 'non configuré'),
        ligneServeur('Google Drive OAuth', sante.google_drive_configure, sante.google_drive_configure ? 'configuré' : 'non configuré'),
        el('div', 'server-row', `Durée max / source : ${sante.max_source_seconds} s`),
        el('div', 'server-row', `Limite globale / travail : ${Math.floor(sante.job_timeout_seconds / 60)} min ${String(sante.job_timeout_seconds % 60).padStart(2, '0')}`)
      );
      if (serverConfig && serverConfig.rst) {
        complet.append(el('div', 'server-row',
          `RsT : ${serverConfig.rst.liens_par_lancement || 6} liens par lancement, TOP ${(serverConfig.rst.noms_choix || rstNomsChoix).join('/')} noms recherchés, jusqu’à ${serverConfig.rst.candidats_max} candidates, ${serverConfig.rst.sources_max} sources retenues, plans de ${serverConfig.rst.duree_max_plan || 5} s max`));
        complet.append(el('div', 'server-row',
          `Voix off importée : ${voixOffMaxMo()} Mo maximum · ${voixOffExtensions().join(' ')}`));
      }
      if (serverConfig && serverConfig.voix_off_generee) {
        const vog = serverConfig.voix_off_generee;
        complet.append(ligneServeur('Voix off générée', Boolean(sante.voix_off_generee_disponible), `${vog.moteur} · ${vog.voix} · gratuit, sans clé`));
        complet.append(el('div', 'server-row', 'Livraison RsT : vidéo muette + script .txt + voix off .mp3, téléchargés séparément'));
      }
    }
    // top-status display (Paramètres card server-card)
    const tsd = $('#top-status-display');
    if (tsd && sante) tsd.innerHTML = `<i class="dot"></i>${sante.ok ? 'Studio opérationnel' : 'Service indisponible'}`;
  }

  function fmtHeureLocale(iso) {
    try { return new Date(iso).toLocaleTimeString('fr-FR', { hour:'2-digit', minute:'2-digit', second:'2-digit' }); }
    catch (_) { return iso; }
  }

  function renderFournisseurs(data) {
    const cont = $('#fournisseur-list'); if (!cont) return;
    cont.replaceChildren();
    const liste = data.fournisseurs || [];
    const cfg = liste.filter((f) => f.configure);
    const actif = $('#fournisseur-actif');
    if (actif) {
      if (settings.fournisseur === 'auto') {
        actif.textContent = cfg.length ? `Auto → ${cfg[0].libelle} d’abord` : 'Auto → aucun fournisseur configuré';
      } else {
        const ch = liste.find((f) => f.nom === settings.fournisseur);
        actif.textContent = ch && ch.configure ? `Fixé → ${ch.libelle}` : 'Fixé → non configuré';
      }
    }
    liste.forEach((f, index) => {
      const item = el('div', 'fournisseur-item');
      const nom = el('span', 'fournisseur-nom fournisseur-tete');
      nom.append(el('i', f.configure ? '' : 'off'), document.createTextNode(`${index + 1}. ${f.libelle}`));
      item.append(nom);
      const meta = [f.configure ? 'clé présente sur le serveur' : 'non configuré (clé absente)'];
      meta.push(f.quota_communiquee
        ? `quota communiqué : ${f.quota_communiquee.restant ?? 'n/d'} / ${f.quota_communiquee.limite ?? 'n/d'} requêtes`
        : 'quota non communiqué par l’API');
      if (f.fenetre_reinitialisee) meta.push(`fenêtre réinitialisée à ${fmtHeureLocale(f.fenetre_reinitialisee)}`);
      item.append(el('span', 'fournisseur-meta', meta.join(' · ')));
      if (f.derniere_erreur) item.append(el('p', 'fournisseur-erreur', f.derniere_erreur));
      cont.append(item);
    });
  }

  async function loadFournisseurs() {
    try {
      const data = await requestJSON('/api/fournisseurs-texte');
      renderFournisseurs(data);
    } catch (_) {
      const c = $('#fournisseur-list'); if (c) c.replaceChildren(el('p', 'hint', 'État des fournisseurs indisponible (API injoignable).'));
    }
  }

  /* ------------------------------------------------------------------
     CONNEXIONS
  ------------------------------------------------------------------ */
  async function loadIntegrations() {
    try { integrations = await requestJSON('/api/integrations/status'); renderIntegrations(); }
    catch (error) { toast(error.message, true); }
  }
  function renderIntegrations() {
    const t = integrations.tiktok || {};
    const d = integrations.google_drive || {};
    const ts = $('#tiktok-state'); if (ts) ts.textContent = t.connected ? (t.display_name || 'Compte connecté') : (t.configured ? 'Non connecté' : 'Configuration serveur requise');
    const tb = $('#connect-tiktok'); if (tb) tb.textContent = t.connected ? 'Connecté · Déconnecter' : 'Connecter TikTok';
    const ds = $('#drive-state'); if (ds) ds.textContent = d.connected ? (d.email || d.name || 'Sauvegarde active') : (d.configured ? 'Non connecté' : 'Configuration serveur requise');
    const db = $('#connect-drive'); if (db) db.textContent = d.connected ? 'Sauvegarde active · Déconnecter' : 'Activer la sauvegarde';
  }
  function bindConnexions() {
    const ct = $('#connect-tiktok');
    const cd = $('#connect-drive');
    if (ct && !ct._bound) {
      ct.addEventListener('click', async () => {
        if (integrations.tiktok && integrations.tiktok.connected) { await requestJSON('/api/integrations/tiktok/disconnect', { body: {} }); await loadIntegrations(); return; }
        if (!integrations.tiktok || !integrations.tiktok.configured) { toast('TikTok OAuth n’est pas configuré : renseigne les variables serveur.', true); return; }
        location.href = '/api/oauth/tiktok/start';
      }); ct._bound = true;
    }
    if (cd && !cd._bound) {
      cd.addEventListener('click', async () => {
        if (integrations.google_drive && integrations.google_drive.connected) { await requestJSON('/api/integrations/google-drive/disconnect', { body: {} }); await loadIntegrations(); return; }
        if (!integrations.google_drive || !integrations.google_drive.configured) { toast('Google Drive OAuth n’est pas configuré.', true); return; }
        location.href = '/api/oauth/google/start';
      }); cd._bound = true;
    }
  }

  /* ------------------------------------------------------------------
     LECTEUR / RÉSULTAT / DRIVE
  ------------------------------------------------------------------ */
  function showDrive(drive) {
    const m = $('#drive-message'); if (!m) return;
    m.replaceChildren();
    const dl = $('#drive-link'); if (dl) dl.classList.add('hidden');
    if (!drive || drive.status === 'not_connected' || drive.status === 'pending') { m.textContent = 'Connecte Google Drive pour conserver ce rendu automatiquement.'; return; }
    if (drive.status === 'uploading') { m.textContent = 'Sauvegarde automatique sur Drive en cours…'; return; }
    if (drive.status === 'failed') { m.textContent = `Erreur Drive : ${drive.error || 'sauvegarde impossible'}. La sauvegarde manuelle reste disponible.`; return; }
    m.append(document.createTextNode('Sauvegarde Drive réussie · '));
    const lien = el('a', '', 'ouvrir dans Drive ↗');
    lien.href = drive.url; lien.target = '_blank'; lien.rel = 'noopener'; m.append(lien);
    if (dl) { dl.href = drive.url; dl.classList.remove('hidden'); }
  }
  function showResult(url, drive, titre) {
    if (!url) return;
    currentVideo = url;
    showSection(1);
    // Attendre le render
    requestAnimationFrame(() => {
      const pt = $('#player-title'); if (pt) pt.textContent = titre ? `Dernier rendu · ${titre}` : 'Dernier rendu';
      const v = $('#lecteur'); if (v) { v.src = url; }
      const t = $('#telecharger'); if (t) t.href = url;
      const pc = $('#player-card'); if (pc) pc.classList.remove('hidden');
      showDrive(drive);
    });
  }
  function bindPlayer() {
    const v = $('#lecteur'); if (v && !v._bound) {
      v.addEventListener('error', () => {
        if ($('#player-card')?.classList.contains('hidden')) return;
        toast('Cette vidéo n’est plus disponible côté serveur (expiration après 6 h).', true);
      }); v._bound = true;
    }
    const sd = $('#save-drive');
    if (sd && !sd._bound) {
      sd.addEventListener('click', async () => {
        if (!currentVideo) return;
        if (!integrations.google_drive || !integrations.google_drive.connected) { toast('Connecte Google Drive pour cette sauvegarde manuelle.', true); return; }
        sd.disabled = true; $('#drive-message').textContent = 'Sauvegarde manuelle sur Drive…';
        try {
          const data = await requestJSON('/api/integrations/google-drive/backup', { body: { url: currentVideo } });
          showDrive({ status: 'completed', ...data }); toast('Vidéo envoyée sur Google Drive.');
        } catch (error) { $('#drive-message').textContent = `Erreur Drive : ${error.message}`; toast(error.message, true); }
        finally { sd.disabled = false; }
      }); sd._bound = true;
    }
  }

  /* ------------------------------------------------------------------
     HISTORIQUE (shell)
  ------------------------------------------------------------------ */
  function renderHistoryShell() {
    bindPlayer();
    // Réinitialiser player card
    const pc = $('#player-card'); if (pc) pc.classList.add('hidden');
  }

  function renderHistory(data) {
    const cont = $('#history-list'); if (!cont) return;
    cont.replaceChildren();
    const jobs = data.jobs || [];
    if (!jobs.length) { cont.append(el('p', 'hint', 'Aucune création récente. Lance ta première vidéo depuis la section Créer.')); return false; }
    let actifs = false;
    jobs.forEach((job) => {
      const enCours = !ETATS_FINAL.includes(job.status); actifs ||= enCours;
      const item = el('article', 'history-item');
      const haut = el('div', 'history-top');
      const titre = el('p', 'history-title');
      titre.append(el('span', 'history-type', typeLabels[job.type] || job.type || 'Création'));
      titre.append(document.createTextNode(job.title || 'Création vidéo'));
      const pct = el('span', `history-pct${job.status === 'completed' ? ' done' : ETATS_FINAL.includes(job.status) ? ' ko' : ''}`, `${job.progress || 0}%`);
      haut.append(titre, pct);
      const meta = el('p', 'history-meta');
      const file = job.status === 'queued' && job.queue_position ? ` · position ${job.queue_position}` : '';
      const lot = job.batch_total > 1 ? ` · lot ${Number(job.batch_index) + 1}/${job.batch_total}` : '';
      const tr = job.type === 'rst' && job.found_count ? ` · ${job.found_count} vidéo(s) trouvée(s)` : '';
      meta.textContent = `${statusLabels[job.status] || job.status}${file}${lot}${tr} · ${job.detail || ''}` + iaUtiliseeLigne(job) + ` · ${fmtHeure(job.created_at_unix)}`;
      const barre = el('div', 'history-progress'); barre.append(Object.assign(el('i'), { style: `width:${Number(job.progress || 0)}%` }));
      item.append(haut, meta, barre);
      if (job.error || job.source_error_count) item.append(el('p', 'history-error', job.error || `${job.source_error_count} avertissement(s) source`));
      const actions = el('div', 'history-links');
      if (job.url) {
        actions.append(boutonAction('Voir', () => showResult(job.url, job.drive, job.title)));
        const tel = el('a', '', 'Télécharger'); tel.href = job.url; tel.download = ''; actions.append(tel);
      }
      if (job.drive && job.drive.url) {
        const a = el('a', '', 'Drive ↗'); a.href = job.drive.url; a.target = '_blank'; a.rel = 'noopener'; actions.append(a);
      }
      if (enCours) {
        actions.append(boutonAction('Annuler', async (ev) => {
          const b = ev.currentTarget;
          if (!confirm(`Annuler « ${job.title || 'cette création'} » ?`)) return;
          b.disabled = true;
          try { await requestJSON(`/api/jobs/${encodeURIComponent(job.job_id)}/cancel`, { body: {} }); await loadHistory(); }
          catch (error) { toast(error.message, true); b.disabled = false; }
        }));
      }
      if (actions.childElementCount) item.append(actions);
      cont.append(item);
    });
    return actifs;
  }

  async function loadHistory() {
    clearTimeout(historyTimer);
    try {
      const data = await requestJSON('/api/jobs', { retryTransient: true, retryForMs: 45000, onRetry: (m) => { const c = $('#history-list'); if (c) c.replaceChildren(el('p', 'hint', m)); } });
      const actifs = renderHistory(data);
      historyTimer = setTimeout(loadHistory, actifs ? 3000 : 30000);
    } catch (error) {
      const c = $('#history-list'); if (c) c.replaceChildren(el('p', 'hint', error.message));
      historyTimer = setTimeout(loadHistory, 15000);
    }
  }

  /* ------------------------------------------------------------------
     PSEUDO VUE ENTRAÎNEMENT (intégrée dans Paramètres)
  ------------------------------------------------------------------ */

  function renderTrainingProfileSelectors() {
    const sel = $('#sst-profil'); if (!sel) return;
    const cur = sel.value;
    sel.replaceChildren(new Option('Aucun profil', ''));
    trainingProfiles.forEach((p) => {
      sel.append(new Option(
        `${p.nom_sujet || p.subject || 'Sujet'}${(p.aliases || p.alias || []).length ? ` · ${(p.aliases || p.alias).join(', ')}` : ''}`,
        p.id));
    });
    if (trainingProfiles.some((p) => p.id === cur)) sel.value = cur;
  }

  function renderTrainingProfiles() {
    const list = $('#training-profile-list'); if (!list) return;
    list.replaceChildren();
    const c = $('#training-profile-count'); if (c) c.textContent = `${trainingProfiles.length} profil${trainingProfiles.length > 1 ? 's' : ''}`;
    if (!trainingProfiles.length) { list.append(el('p', 'hint', 'Aucun profil mémorisé. Crée ton premier sujet ci-dessus.')); renderTrainingProfileSelectors(); return; }
    trainingProfiles.forEach((p) => {
      const aliases = p.aliases || p.alias || [];
      const card = el('article', 'training-profile-card');
      const head = el('div', 'training-profile-head');
      head.append(el('div', '', p.nom_sujet || p.subject || 'Sujet sans nom'));
      head.append(el('span', 'badge ok', `${Number(p.sources_automatiquement_ajoutees || p.source_count || 0)} source(s)`));
      card.append(head);
      card.append(el('p', 'training-profile-meta', aliases.length ? `Alias : ${aliases.join(' · ')}` : 'Aucun alias'));
      const det = p.donnees || {};
      const ex = Number(det.sources_exemples_analysees || 0);
      const conf = Math.round(Number(det.confiance_moyenne || 0) * 100);
      card.append(el('p', 'training-profile-meta', `${ex} exemple(s) analysé(s) · confiance moyenne ${conf}%`));
      if (!ex && !Number(p.sources_automatiquement_ajoutees || p.source_count || 0) && !conf) {
        card.append(el('p', 'training-profile-note', 'Profil seulement enregistré, pas encore entraîné : ajoute des bons et mauvais exemples TikTok puis clique sur « Analyser avec l’IA et alimenter ».'));
      }
      const actions = el('div', 'training-profile-actions');
      actions.append(boutonAction('Utiliser dans SsT', () => { setTab('sst'); showSection(0); setTimeout(() => { const s = $('#sst-profil'); if (s) s.value = p.id; }, 50); }));
      actions.append(boutonAction('Modifier / analyser', () => {
        activeTrainingProfileId = p.id;
        const n = $('#profil-nom'); const a = $('#profil-alias'); const b = $('#profil-bons'); const m = $('#profil-mauvais');
        if (n) n.value = p.nom_sujet || '';
        if (a) a.value = aliases.join(', ');
        if (b) b.value = (p.bons_exemples || []).join('\n');
        if (m) m.value = (p.mauvais_exemples || []).join('\n');
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }));
      actions.append(boutonAction('Supprimer', () => supprimerProfil(p.id)));
      card.append(actions);
      list.append(card);
    });
    renderTrainingProfileSelectors();
  }

  async function persisterProfils() {
    try {
      const data = await requestJSON('/api/profils-entrainement/synchroniser', { body: { profiles: trainingProfiles }, retryTransient: true, retryForMs: 15000 });
      if (Array.isArray(data.profiles)) {
        trainingProfiles = U.mergeTrainingProfiles(trainingProfiles, data.profiles);
        U.saveTrainingProfiles(localStorage, trainingProfiles);
      }
    } catch (_) {}
    renderTrainingProfiles();
  }

  async function loadTrainingProfilesInit() {
    const local = U.loadTrainingProfiles(localStorage);
    try {
      const data = await requestJSON('/api/profils-entrainement', { retryTransient: true, retryForMs: 15000 });
      trainingProfiles = U.mergeTrainingProfiles(local, data.profiles || data.profils || []);
    } catch (_) { trainingProfiles = local; }
    U.saveTrainingProfiles(localStorage, trainingProfiles);
    renderTrainingProfiles();
    await persisterProfils();
  }

  async function supprimerProfil(id) {
    if (!confirm('Supprimer ce profil du navigateur et du serveur privé ?')) return;
    try {
      await requestJSON(`/api/profils-entrainement/${encodeURIComponent(id)}`, { method: 'DELETE' });
      trainingProfiles = trainingProfiles.filter((p) => p.id !== id);
      U.saveTrainingProfiles(localStorage, trainingProfiles);
      if (activeTrainingProfileId === id) activeTrainingProfileId = '';
      renderTrainingProfiles(); toast('Profil supprimé des deux emplacements.');
    } catch (error) { setStatus($('#statut-profil'), error.message, 'error'); }
  }

  async function analyserProfil(id) {
    const btn = $('#btn-analyser-profil'); if (btn) btn.disabled = true;
    setStatus($('#statut-profil'), 'Téléchargement des exemples, analyse Gemini et recherche de nouvelles candidates…');
    try {
      const data = await requestJSON(`/api/profils-entrainement/${encodeURIComponent(id)}/analyser`, { body: {}, retryTransient: true, retryForMs: 30000, onRetry: (m) => setStatus($('#statut-profil'), m) });
      const profile = data.profile || data.profil;
      trainingProfiles = trainingProfiles.map((i) => i.id === id ? profile : i);
      U.saveTrainingProfiles(localStorage, trainingProfiles);
      renderTrainingProfiles();
      const r = $('#training-result'); if (r) { r.classList.remove('hidden'); r.textContent = `Apprentissage terminé : ${profile.sources_automatiquement_ajoutees || 0} nouvelle(s) source(s) validée(s), signatures positive et négative conservées.`; }
      setStatus($('#statut-profil'), 'Profil analysé et alimenté uniquement avec des sources validées.', 'success');
    } catch (error) { setStatus($('#statut-profil'), error.message, 'error'); }
    finally { if (btn) btn.disabled = false; }
  }

  function bindTraining() {
    const np = $('#btn-nouveau-profil');
    const ap = $('#btn-analyser-profil');
    if (np && !np._bound) {
      np.addEventListener('click', () => {
        activeTrainingProfileId = '';
        ['profil-nom', 'profil-alias', 'profil-bons', 'profil-mauvais'].forEach((id) => { const i = $(id); if (i) i.value = ''; });
        const r = $('#training-result'); if (r) r.classList.add('hidden');
        setStatus($('#statut-profil'), 'Nouveau profil prêt.');
      }); np._bound = true;
    }
    if (ap && !ap._bound) {
      ap.addEventListener('click', () => guards.training.run(async () => {
        const n = $('#profil-nom')?.value.trim();
        if (!n) { setStatus($('#statut-profil'), 'Le nom du sujet est obligatoire.', 'error'); return; }
        const aliases = ($('#profil-alias')?.value || '').split(/[,;\n]+/).map((v) => v.trim()).filter(Boolean);
        const good = ($('#profil-bons')?.value || '').split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
        const bad = ($('#profil-mauvais')?.value || '').split(/\r?\n/).map((v) => v.trim()).filter(Boolean);
        const body = { id: activeTrainingProfileId, nom_sujet: n, aliases, good_examples: good, bad_examples: bad };
        try {
          const endpoint = activeTrainingProfileId
            ? `/api/profils-entrainement/${encodeURIComponent(activeTrainingProfileId)}` : '/api/profils-entrainement';
          const data = await requestJSON(endpoint, { method: activeTrainingProfileId ? 'PUT' : 'POST', body });
          const profile = data.profile || data.profil;
          activeTrainingProfileId = profile.id;
          trainingProfiles = [profile, ...trainingProfiles.filter((i) => i.id !== profile.id)];
          U.saveTrainingProfiles(localStorage, trainingProfiles);
          renderTrainingProfiles();
          await analyserProfil(profile.id);
        } catch (error) { setStatus($('#statut-profil'), error.message, 'error'); }
      })); ap._bound = true;
    }
  }

  function renderParametresShell() {
    bindTraining();
  }

  /* ------------------------------------------------------------------
     BINDINGS COMMUNS (délégateur global + burger)
  ------------------------------------------------------------------ */
  function rebindCommon() {
    // Burger
    const bgr = $('#bgr');
    if (bgr && !bgr._bound) {
      bgr.addEventListener('click', () => {
        const o = document.body.classList.toggle('menu');
        bgr.setAttribute('aria-expanded', o ? 'true' : 'false');
      });
      bgr._bound = true;
    }
    // Scrim
    const scrim = $('#scrim');
    if (scrim && !scrim._bound) { scrim.addEventListener('click', closeDrawer); scrim._bound = true; }
  }

  /* ------------------------------------------------------------------
     DÉLÉGATEUR GLOBAL DE CLICS
  ------------------------------------------------------------------ */
  function installGlobalHandlers() {
    document.addEventListener('click', (e) => {
      const t = e.target;
      if (t.id === 'ov') { closeOv(); return; }
      const b = t.closest('button');
      if (!b) {
        // clic sur un lien ou autre — laisser passer
        return;
      }
      const D = b.dataset;

      if (D.sec != null) { showSection(+D.sec); return; }
      if (b.id === 'bgr') { /* géré dans rebindCommon */ return; }
      if (D.tab) { setTab(D.tab); return; }
      if (D.paste) {
        const id = D.paste; const i = document.getElementById(id);
        if (!i) return;
        collerPressePapier((texte) => { i.value = texte.split(/\r?\n/)[0].trim(); i.dispatchEvent(new Event('input', { bubbles: true })); });
        return;
      }
      if (D.rm != null) {
        const f = b.closest('.fl');
        const inp = f && $('input[type=file]', f);
        if (inp) inp.value = '';
        const fn = f && $('.fn', f); if (fn) fn.textContent = '';
        return;
      }
      if ('close' in D) { closeOv(); return; }
      // Seg click
      if (b.closest('.seg')) { handleSegClick(b); return; }
      // Refresh history
      if (b.id === 'refresh-history') { loadHistory(); return; }
    });

    // Change: inputs file → voix off (sections montées)
    document.addEventListener('change', (e) => {
      if (e.target.type !== 'file') return;
      // Déterminer la section
      const secInput = e.target.id;
      for (const section of Object.keys(VOIX_SECTIONS)) {
        if (VOIX_SECTIONS[section].input === secInput) { televerserVoixOff(section); return; }
      }
      // Retour générique
      const f = e.target.files && e.target.files[0];
      const n = e.target.closest('.fl') && $('.fn', e.target.closest('.fl'));
      if (f && f.size > 26214400) { toast('Fichier trop volumineux (25 Mo max).', true); e.target.value = ''; if (n) n.textContent = ''; }
      else if (n && f) n.textContent = f.name;
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') { closeOv(); document.body.classList.remove('menu'); }
    });

    document.addEventListener('pointermove', (e) => {
      const c = e.target.closest && e.target.closest('.card');
      if (c) { const r = c.getBoundingClientRect(); c.style.setProperty('--mx', e.clientX - r.left + 'px'); c.style.setProperty('--my', e.clientY - r.top + 'px'); }
      const h = e.target.closest && e.target.closest('.hero');
      if (h) { const r = h.getBoundingClientRect(); h.style.setProperty('--px', ((e.clientX - r.left) / r.width - .5) * 2); h.style.setProperty('--py', ((e.clientY - r.top) / r.height - .5) * 2); }
    });
  }

  /* ------------------------------------------------------------------
     REPRISE APRÈS ACTUALISATION
  ------------------------------------------------------------------ */
  async function resumeJob() {
    const saved = U.loadActiveJob(localStorage);
    if (!saved) return;
    if (saved.type === 'rst') { U.clearActiveJob(localStorage); return; }
    const statut = saved.type === 'montage' ? null
      : saved.type === 'sst' ? null : saved.type === 'reference' ? null
      : saved.type === 'video' ? null : null;
    showSection(0);
    setTab(saved.type === 'montage' ? 'montage' : saved.type === 'sst' ? 'sst' : saved.type === 'ia-video' ? 'ia' : saved.type === 'video' ? 'lien' : 'lien');
    // On laisse waitJob remplir le tracker
    try {
      const data = await waitJob(saved.id, saved.type, null);
      if (saved.type === 'analyser') {
        requestAnimationFrame(() => {
          const h = $('#hook'); const c = $('#corps'); const m = $('#mot-cle');
          if (h) h.value = data.hook || '';
          if (c) c.value = data.corps || '';
          if (m) m.value = data.mot_cle_broll || '';
          const card = $('#etape-script'); if (card) { card.classList.remove('lock'); card.removeAttribute('inert'); }
        });
      } else if (saved.type === 'reference') {
        requestAnimationFrame(() => {
          setTab('montage');
          const h = $('#mon-hook'); const c = $('#mon-corps');
          if (h) h.value = data.hook || '';
          if (c) c.value = data.corps || '';
        });
      }
    } catch (error) { /* notif déjà faite */ }
  }

  /* ------------------------------------------------------------------
     DÉMARRAGE
  ------------------------------------------------------------------ */
  function init() {
    appliquerTheme();
    bindTheme();
    installGlobalHandlers();
    renderNav();
    renderView();
    bindConnexions();
    updateGenButtons();

    const params = new URLSearchParams(location.search);
    if (params.get('status')) {
      toast(params.get('status') === 'connected'
        ? `${params.get('integration') === 'drive' ? 'Google Drive' : 'TikTok'} connecté avec succès.`
        : 'La connexion a échoué. Réessaie depuis Connexions.',
        params.get('status') !== 'connected');
      history.replaceState({}, '', location.pathname);
    }

    loadConfig().then(loadSante);
    loadFournisseurs();
    loadTrainingProfilesInit();
    loadIntegrations().then(loadHistory);
    resumeJob();
    resumeRstJobs();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
