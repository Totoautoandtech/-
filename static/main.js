/* =====================================================================
   ς੮ ς८Րɿƿ┮ — Dashboard vidéo (JS natif, aucune dépendance).

   Trois sections de création :
     1. « Lien → vidéo »     : un lien → script éditable → vidéo verticale.
     2. « RsT »               : un lien TikTok → analyse, script, recherche de
                                vidéos réellement trouvées, sélection, montage.
     3. « Montage multi-source » : 1 à 20 liens collés, validés un par un.

   Tout ce qui est affiché provient de l'API : aucune donnée inventée.
   ===================================================================== */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const U = window.JobUtils;

  /* ------------------------------------------------------------------
     ÉTAT GLOBAL
  ------------------------------------------------------------------ */

  const SETTINGS_KEY = 'vesper.settings.v2';
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
  // RsT multiple : jusqu'à 6 travaux indépendants suivis en parallèle.
  let rstJobs = U.loadRstJobs(localStorage);
  const rstEtats = {};   // job_id -> dernier état réel renvoyé par l'API
  const rstSuivis = new Set();
  // Voix off importée par section : identifiant renvoyé par POST /api/voixoff.
  const voixOff = { lien: '', rst: '', montage: '' };

  const guards = {
    analyser: U.submissionGuard(), video: U.submissionGuard(),
    reference: U.submissionGuard(), rst: U.submissionGuard(),
    montage: U.submissionGuard(), diagnostic: U.submissionGuard(),
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
    rst: 'RsT', montage: 'Montage'
  };

  const ETATS_FINAL = ['completed', 'failed', 'cancelled'];
  const RST_MAX = 6;

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

  function toast(message, erreur = false) {
    const element = $('toast');
    element.textContent = message;
    element.className = `toast show${erreur ? ' error' : ''}`;
    clearTimeout(element._timer);
    element._timer = setTimeout(() => { element.className = 'toast'; }, 5200);
  }

  function setStatus(element, message, type = '') {
    if (!element) return;
    element.textContent = message;
    element.className = `status ${type}`;
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
     RÉGLAGES (mode Rapide / Qualité, intensité des transitions)
  ------------------------------------------------------------------ */

  function loadSettings() {
    try {
      const brut = JSON.parse(localStorage.getItem(SETTINGS_KEY) || 'null');
      return {
        mode: brut && brut.mode === 'qualite' ? 'qualite' : 'rapide',
        intensite: brut && [0, 1, 2, 3].includes(Number(brut.intensite)) ? Number(brut.intensite) : 2
      };
    } catch (_) { return { mode: 'rapide', intensite: 2 }; }
  }

  function saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(settings));
    syncSettingsUI();
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

  function syncSettingsUI() {
    document.querySelectorAll('#mode-cards input, #mode-cards-parametres input').forEach((radio) => {
      radio.checked = radio.value === settings.mode;
    });
    document.querySelectorAll('[data-intensite]').forEach((bouton) => {
      bouton.classList.toggle('active', Number(bouton.dataset.intensite) === settings.intensite);
    });
    const texte = libelleExport();
    $('export-courant').textContent = texte;
    $('export-courant-parametres').textContent = texte;
  }

  function bindSettings() {
    document.querySelectorAll('#mode-cards input, #mode-cards-parametres input').forEach((radio) => {
      radio.addEventListener('change', () => {
        settings.mode = radio.value;
        saveSettings();
        toast(settings.mode === 'qualite' ? 'Mode Qualité activé.' : 'Mode Rapide activé.');
      });
    });
    document.querySelectorAll('[data-intensite]').forEach((bouton) => {
      bouton.addEventListener('click', () => {
        settings.intensite = Number(bouton.dataset.intensite);
        saveSettings();
      });
    });
  }

  /* ------------------------------------------------------------------
     NAVIGATION (vues latérales, onglets de section, sous-onglets)
  ------------------------------------------------------------------ */

  const viewTitles = {
    creer: 'Créer', creations: 'Mes créations',
    connexions: 'Connexions', parametres: 'Paramètres'
  };

  function showView(name) {
    document.querySelectorAll('.nav-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach((view) => {
      view.classList.toggle('active', view.id === `view-${name}`);
    });
    $('view-title').textContent = viewTitles[name] || name;
    closeDrawer();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  function closeDrawer() {
    $('sidebar').classList.remove('open');
    $('scrim').hidden = true;
  }

  function bindNavigation() {
    document.querySelectorAll('.nav-item').forEach((item) => {
      item.addEventListener('click', () => showView(item.dataset.view));
    });
    $('burger').addEventListener('click', () => {
      const ouvert = $('sidebar').classList.toggle('open');
      $('scrim').hidden = !ouvert;
    });
    $('scrim').addEventListener('click', closeDrawer);
  }

  function selectTab(name) {
    document.querySelectorAll('#tabs-creer .seg-item').forEach((onglet) => {
      onglet.classList.toggle('active', onglet.dataset.tab === name);
    });
    document.querySelectorAll('.tool').forEach((outil) => {
      outil.classList.toggle('active', outil.dataset.tool === name);
    });
  }

  function selectMtab(name) {
    document.querySelectorAll('#montage-tabs .seg-item').forEach((onglet) => {
      onglet.classList.toggle('active', onglet.dataset.mtab === name);
    });
    document.querySelectorAll('.mtab').forEach((panneau) => {
      panneau.classList.toggle('active', panneau.dataset.mtab === name);
    });
  }

  function bindTabs() {
    document.querySelectorAll('#tabs-creer .seg-item').forEach((onglet) => {
      onglet.addEventListener('click', () => selectTab(onglet.dataset.tab));
    });
    document.querySelectorAll('#montage-tabs .seg-item').forEach((onglet) => {
      onglet.addEventListener('click', () => selectMtab(onglet.dataset.mtab));
    });
  }

  /* ------------------------------------------------------------------
     CONFIGURATION + ÉTAT SERVEUR (données réelles)
  ------------------------------------------------------------------ */

  async function loadConfig() {
    try {
      serverConfig = await requestJSON('/api/config');
      const rst = serverConfig.rst || { candidats_max: 40, sources_max: 20, liens_par_lancement: 6 };
      $('source-limit').textContent =
        `Paramètres après « ? » retirés et doublons supprimés. Durée maximale : ${serverConfig.max_source_seconds} s par source. ` +
        `Limite globale : ${Math.floor(serverConfig.job_timeout_seconds / 60)} min ${String(serverConfig.job_timeout_seconds % 60).padStart(2, '0')}.`;
      const accroche = document.querySelector('.tool[data-tool="rst"] .lead');
      if (accroche) {
        accroche.innerHTML =
          `Colle <strong>jusqu’à ${rst.liens_par_lancement || 6} liens TikTok de départ</strong>, un par ligne. Chaque lien lance <strong>son propre travail</strong>. ` +
          `Pour chacun, RsT analyse la vidéo, prépare le script, recherche jusqu’à <strong>${rst.candidats_max} vidéos TikTok candidates</strong>, ` +
          `sélectionne jusqu’à <strong>${rst.sources_max} bonnes sources</strong>, puis crée automatiquement le montage final. ` +
          'Seules les vidéos réellement trouvées sont affichées.';
      }
    } catch (error) {
      $('source-limit').textContent = error.message;
    }
    syncSettingsUI();
  }

  function ligneServeur(nom, actif, details) {
    const ligne = el('div', 'server-row');
    const gauche = el('b');
    const point = el('i', actif ? '' : 'off');
    gauche.append(point, document.createTextNode(nom));
    const droite = el('span', '', details);
    ligne.append(gauche, droite);
    return ligne;
  }

  async function loadSante() {
    let sante = null;
    try { sante = await requestJSON('/api/sante'); } catch (_) { /* serveur injoignable */ }
    const conteneur = $('server-rows');
    conteneur.replaceChildren();
    if (!sante) {
      conteneur.append(el('p', 'server-row', 'API injoignable.'));
      return;
    }
    conteneur.append(
      ligneServeur('API', sante.ok, 'en ligne'),
      ligneServeur('Gemini', sante.gemini_configure, sante.gemini_configure ? 'configuré' : 'non configuré'),
      ligneServeur('FFmpeg', sante.ffmpeg_installe && sante.ffprobe_installe, sante.ffmpeg_installe ? 'prêt' : 'absent')
    );

    const complet = $('parametres-server');
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
        `RsT : ${serverConfig.rst.liens_par_lancement || 6} liens par lancement, jusqu’à ${serverConfig.rst.candidats_max} candidates recherchées, ${serverConfig.rst.sources_max} sources retenues`));
      complet.append(el('div', 'server-row',
        `Voix off importée : ${voixOffMaxMo()} Mo maximum · ${voixOffExtensions().join(' ')}`));
    }
    const top = $('top-status');
    top.querySelector('span').textContent = sante.ok ? 'Studio opérationnel' : 'Service indisponible';
  }

  /* ------------------------------------------------------------------
     STYLES DE SOUS-TITRES
  ------------------------------------------------------------------ */

  function chargerStyles(liste) {
    const styles = Array.isArray(liste) && liste.length ? liste : ['classique', 'jaune', 'centre'];
    ['lien', 'montage'].forEach((type) => {
      const conteneur = $(`styles-${type}`);
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
     CONNEXIONS (TikTok OAuth, Google Drive OAuth)
  ------------------------------------------------------------------ */

  async function loadIntegrations() {
    try {
      integrations = await requestJSON('/api/integrations/status');
      renderIntegrations();
    } catch (error) { toast(error.message, true); }
  }

  function renderIntegrations() {
    const tiktok = integrations.tiktok || {};
    const drive = integrations.google_drive || {};
    $('tiktok-state').textContent = tiktok.connected
      ? (tiktok.display_name || 'Compte connecté')
      : (tiktok.configured ? 'Non connecté' : 'Configuration serveur requise');
    $('connect-tiktok').textContent = tiktok.connected ? 'Connecté · Déconnecter' : 'Connecter TikTok';
    $('drive-state').textContent = drive.connected
      ? (drive.email || drive.name || 'Sauvegarde active')
      : (drive.configured ? 'Non connecté' : 'Configuration serveur requise');
    $('connect-drive').textContent = drive.connected ? 'Sauvegarde active · Déconnecter' : 'Activer la sauvegarde';
  }

  function bindIntegrations() {
    $('connect-tiktok').addEventListener('click', async () => {
      if (integrations.tiktok && integrations.tiktok.connected) {
        await requestJSON('/api/integrations/tiktok/disconnect', { body: {} });
        await loadIntegrations();
        return;
      }
      if (!integrations.tiktok || !integrations.tiktok.configured) {
        toast('TikTok OAuth n’est pas configuré : renseigne les variables serveur sur Render.', true);
        return;
      }
      location.href = '/api/oauth/tiktok/start';
    });

    $('connect-drive').addEventListener('click', async () => {
      if (integrations.google_drive && integrations.google_drive.connected) {
        await requestJSON('/api/integrations/google-drive/disconnect', { body: {} });
        await loadIntegrations();
        return;
      }
      if (!integrations.google_drive || !integrations.google_drive.configured) {
        toast('Google Drive OAuth n’est pas configuré : renseigne les variables serveur sur Render.', true);
        return;
      }
      location.href = '/api/oauth/google/start';
    });
  }

  /* ------------------------------------------------------------------
     RÉSULTAT / LECTEUR / DRIVE
  ------------------------------------------------------------------ */

  function showDrive(drive) {
    const message = $('drive-message');
    message.replaceChildren();
    $('drive-link').classList.add('hidden');
    if (!drive || drive.status === 'not_connected' || drive.status === 'pending') {
      message.textContent = 'Connecte Google Drive pour conserver ce rendu automatiquement.';
      return;
    }
    if (drive.status === 'uploading') {
      message.textContent = 'Sauvegarde automatique sur Drive en cours…';
      return;
    }
    if (drive.status === 'failed') {
      message.textContent = `Erreur Drive : ${drive.error || 'sauvegarde impossible'}. La sauvegarde manuelle reste disponible.`;
      return;
    }
    message.append(document.createTextNode('Sauvegarde Drive réussie · '));
    const lien = el('a', '', 'ouvrir dans Drive ↗');
    lien.href = drive.url; lien.target = '_blank'; lien.rel = 'noopener';
    message.append(lien);
    $('drive-link').href = drive.url;
    $('drive-link').classList.remove('hidden');
  }

  function showResult(url, drive, titre) {
    if (!url) return;
    currentVideo = url;
    $('player-title').textContent = titre ? `Dernier rendu · ${titre}` : 'Dernier rendu';
    $('lecteur').src = url;
    $('telecharger').href = url;
    $('player-card').classList.remove('hidden');
    showDrive(drive);
    showView('creations');
  }

  function bindPlayer() {
    $('lecteur').addEventListener('error', () => {
      if ($('player-card').classList.contains('hidden')) return;
      toast('Cette vidéo n’est plus disponible côté serveur (expiration après 6 h).', true);
    });
    $('save-drive').addEventListener('click', async () => {
      if (!currentVideo) return;
      if (!integrations.google_drive || !integrations.google_drive.connected) {
        toast('Connecte Google Drive pour cette sauvegarde manuelle.', true);
        return;
      }
      $('save-drive').disabled = true;
      $('drive-message').textContent = 'Sauvegarde manuelle sur Drive…';
      try {
        const data = await requestJSON('/api/integrations/google-drive/backup', { body: { url: currentVideo } });
        showDrive({ status: 'completed', ...data });
        toast('Vidéo envoyée sur Google Drive.');
      } catch (error) {
        $('drive-message').textContent = `Erreur Drive : ${error.message}`;
        toast(error.message, true);
      } finally { $('save-drive').disabled = false; }
    });
  }


  /* ------------------------------------------------------------------
     VOIX OFF IMPORTÉE (fichier de l'utilisateur, aucun service payant)
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
    voixOff[section] = '';
    $(refs.input).value = '';
    const etat = $(refs.etat);
    etat.textContent = message
      || `Aucune voix off : la vidéo restera muette. Formats ${voixOffExtensions().join(', ')}, ${voixOffMaxMo()} Mo maximum.`;
    etat.classList.remove('success', 'error');
  }

  async function televerserVoixOff(section) {
    const refs = VOIX_SECTIONS[section];
    const champ = $(refs.input);
    const etat = $(refs.etat);
    const fichier = champ.files && champ.files[0];
    if (!fichier) { reinitialiserVoixOff(section); return; }

    const extension = `.${(fichier.name.split('.').pop() || '').toLowerCase()}`;
    if (!voixOffExtensions().includes(extension)) {
      reinitialiserVoixOff(section, `Format ${extension} non accepté. Formats : ${voixOffExtensions().join(', ')}.`);
      etat.classList.add('error');
      toast('Format audio non pris en charge pour la voix off.', true);
      return;
    }
    if (fichier.size > voixOffMaxMo() * 1024 * 1024) {
      reinitialiserVoixOff(section, `Fichier trop lourd (${(fichier.size / 1048576).toFixed(1)} Mo). Limite : ${voixOffMaxMo()} Mo.`);
      etat.classList.add('error');
      toast('Voix off trop lourde.', true);
      return;
    }

    etat.classList.remove('success', 'error');
    etat.textContent = `Import de « ${fichier.name} » et vérification FFprobe…`;
    champ.disabled = true;
    try {
      const reponse = await fetch(`/api/voixoff?nom=${encodeURIComponent(fichier.name)}`, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/octet-stream' },
        body: fichier
      });
      let donnees = {};
      try { donnees = await reponse.json(); } catch (_) { /* réponse vide */ }
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
      etat.classList.add('error');
      toast(error.message, true);
    } finally {
      champ.disabled = false;
    }
  }

  function bindVoixOff() {
    Object.keys(VOIX_SECTIONS).forEach((section) => {
      const refs = VOIX_SECTIONS[section];
      $(refs.input).addEventListener('change', () => televerserVoixOff(section));
      $(refs.clear).addEventListener('click', () => {
        reinitialiserVoixOff(section, 'Voix off retirée : cette vidéo sera muette.');
      });
      reinitialiserVoixOff(section);
    });
  }

  /* ------------------------------------------------------------------
     SUIVI RÉEL DE GÉNÉRATION (tracker commun aux trois sections)
  ------------------------------------------------------------------ */

  function setBusy(busy) {
    ['btn-analyser', 'btn-video', 'btn-rst', 'btn-reference', 'btn-montage',
      'btn-add-project', 'btn-launch-batch'].forEach((id) => {
      const bouton = $(id);
      if (!bouton) return;
      const dependDuDiagnostic = ['btn-montage', 'btn-add-project'].includes(id);
      bouton.disabled = busy || (dependDuDiagnostic && !diagnosticData) ||
        (id === 'btn-launch-batch' && batchDrafts.length === 0);
    });
    $('btn-diagnostic').disabled = busy;
    $('btn-cancel').disabled = !busy;
  }

  function appendSource(conteneur, source, forcerErreur = false) {
    const echec = forcerErreur || ['error', 'invalid', 'unavailable', 'download_error', 'analysis_fallback'].includes(source.status);
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

  function renderSources(conteneur, data) {
    conteneur.replaceChildren();
    const sources = data.sources || [];
    const erreurs = data.source_errors || [];
    sources.forEach((source) => appendSource(conteneur, source));
    erreurs
      .filter((erreur) => !sources.some((source) => source.url === erreur.url && source.error))
      .forEach((erreur) => appendSource(conteneur, erreur, true));
    conteneur.classList.toggle('hidden', conteneur.childElementCount === 0);
  }

  function renderJob(data) {
    $('tracker').classList.remove('hidden');
    $('tracker-title').textContent =
      data.status === 'completed' ? 'Création terminée'
        : data.status === 'failed' ? 'Création échouée'
          : data.status === 'cancelled' ? 'Travail annulé'
            : `${typeLabels[data.type] || 'Création'} en cours`;
    const pourcent = Number(data.progress || 0);
    $('tracker-pct').textContent = `${pourcent}%`;
    $('tracker-bar').style.width = `${pourcent}%`;
    $('tracker-detail').textContent =
      `${statusLabels[data.status] || data.status} · ${data.detail || ''}` +
      (data.status === 'queued' && data.queue_position ? ` · position ${data.queue_position}` : '');

    renderSources($('tracker-sources'), data);

    const panneauScript = $('tracker-script');
    if (data.type === 'rst' && data.script) {
      panneauScript.replaceChildren();
      panneauScript.append(el('strong', '', `Accroche — ${data.script.hook || ''}`));
      panneauScript.append(el('p', '', data.script.corps || ''));
      panneauScript.classList.remove('hidden');
    } else {
      panneauScript.classList.add('hidden');
    }

  }

  /* ------------------------------------------------------------------
     RsT : « Vidéos trouvées par RsT » (données réelles uniquement)
  ------------------------------------------------------------------ */

  function foundItem(video) {
    const item = el('div', `found-item${video.selected ? ' selected' : ''}`);

    const icone = el('div', 'found-icon');
    icone.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m10 8 6 4-6 4z"/></svg>';
    item.append(icone);

    const info = el('div', 'found-info');
    info.append(el('strong', '', `@${video.author || 'tiktok'}`));
    info.append(el('span', 'found-title', video.title || '(vidéo sans légende)'));
    info.append(el('span', 'found-meta', `${fmtDuree(video.duration)} · ${video.origin || 'recherche TikTok'}`));
    if (!video.selected && video.rejet) info.append(el('span', 'found-rejet', `Écartée : ${video.rejet}`));
    item.append(info);

    const cote = el('div', 'found-side');
    cote.append(el('span', `badge ${video.selected ? 'ok' : 'ko'}`, video.selected ? 'retenue' : 'écartée'));
    const ouvrir = el('a', 'found-open', 'Ouvrir ↗');
    ouvrir.href = video.url;
    ouvrir.target = '_blank';
    ouvrir.rel = 'noopener';
    cote.append(ouvrir);
    item.append(cote);

    return item;
  }

  async function waitJob(id, type, statusElement) {
    activeJobId = id;
    activeJobType = type;
    setBusy(true);
    $('nav-dot').classList.remove('hidden');
    U.saveActiveJob(localStorage, { id, type, savedAt: Date.now() });
    for (;;) {
      let data;
      try {
        data = await requestJSON(`/api/jobs/${encodeURIComponent(id)}`, {
          retryTransient: true, retryForMs: 90000,
          onRetry: (message) => {
            setStatus(statusElement, message);
            $('tracker-detail').textContent = message;
          }
        });
      } catch (error) {
        if (error.status === 404) {
          U.clearActiveJob(localStorage);
          activeJobId = ''; activeJobType = '';
          setBusy(false);
          $('nav-dot').classList.add('hidden');
        } else {
          setBusy(true); // le suivi reprend après actualisation ; pas de double lancement
        }
        throw error;
      }
      renderJob(data);
      setStatus(statusElement, `${data.detail || statusLabels[data.status]} · ${data.progress || 0}%`);
      if (data.status === 'completed') {
        U.clearActiveJob(localStorage);
        activeJobId = ''; activeJobType = '';
        setBusy(false);
        $('nav-dot').classList.add('hidden');
        if (data.url) showResult(data.url, data.drive, data.title);
        loadHistory();
        return data;
      }
      if (data.status === 'failed' || data.status === 'cancelled') {
        U.clearActiveJob(localStorage);
        activeJobId = ''; activeJobType = '';
        setBusy(false);
        $('nav-dot').classList.add('hidden');
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
     SECTION 1 : LIEN → VIDÉO
  ------------------------------------------------------------------ */

  function bindLienVideo() {
    $('lien-coller').addEventListener('click', () => {
      collerPressePapier((texte) => { $('lien-input').value = texte.split(/\r?\n/)[0].trim(); });
    });

    $('btn-analyser').addEventListener('click', () => guards.analyser.run(async () => {
      const bouton = $('btn-analyser'), statut = $('statut-analyser');
      const lien = $('lien-input').value.trim();
      if (!lien) { setStatus(statut, 'Ajoute un lien source (article, page ou TikTok).', 'error'); return; }
      bouton.disabled = true;
      setStatus(statut, 'Analyse du contenu…');
      try {
        const data = await launchJob('analyser', { lien, idempotency_key: uuid() }, statut);
        $('hook').value = data.hook || '';
        $('corps').value = data.corps || '';
        $('mot-cle').value = data.mot_cle_broll || '';
        $('etape-script').classList.remove('hidden');
        setStatus(statut, 'Script prêt — relis-le et modifie-le avant de générer.', 'success');
      } catch (error) {
        setStatus(statut, error.message, 'error');
      } finally { bouton.disabled = Boolean(activeJobId); }
    }));

    $('btn-video').addEventListener('click', () => guards.video.run(async () => {
      const bouton = $('btn-video'), statut = $('statut-video');
      bouton.disabled = true;
      setStatus(statut, 'Préparation de la vidéo…');
      try {
        const style = document.querySelector('input[name="style-lien"]:checked')?.value || 'classique';
        await launchJob('video', {
          hook: $('hook').value, corps: $('corps').value,
          mot_cle_broll: $('mot-cle').value, style,
          mode: settings.mode, voix_off: voixOff.lien, idempotency_key: uuid()
        }, statut);
        setStatus(statut, 'Vidéo terminée.', 'success');
      } catch (error) {
        setStatus(statut, error.message, 'error');
      } finally { bouton.disabled = Boolean(activeJobId); }
    }));
  }

  /* ------------------------------------------------------------------
     SECTION 2 : RsT
  ------------------------------------------------------------------ */

  function rstLienCourt(lien) {
    const texte = String(lien || '');
    const morceaux = texte.split('/').filter(Boolean);
    const identifiant = morceaux[morceaux.length - 1] || texte;
    const auteur = (texte.match(/@[\w.\-]+/) || [''])[0];
    return auteur ? `${auteur} · ${identifiant}` : identifiant;
  }

  function rstCompterLiens() {
    const parsed = U.parseRstLinks($('rst-liens').value, RST_MAX);
    const brut = $('rst-liens').value.split(/\r?\n/).filter((ligne) => ligne.trim()).length;
    $('rst-liens-counter').textContent =
      `${parsed.links.length}/${RST_MAX} liens${parsed.duplicates.length ? ` · ${parsed.duplicates.length} doublon(s)` : ''}`;
    $('rst-liens-counter').classList.toggle('over', brut > RST_MAX || parsed.errors.length > 0);
    if (parsed.errors.length) {
      setStatus($('statut-rst'), parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
    } else if (parsed.links.length) {
      setStatus($('statut-rst'), `${parsed.links.length} lien(s) TikTok prêt(s) : un travail sera lancé par lien.`);
    } else {
      setStatus($('statut-rst'), `Colle 1 à ${RST_MAX} liens TikTok de départ, un par ligne.`);
    }
    return parsed;
  }

  /* --- Une carte de suivi par travail RsT --- */

  function renderRstJobs() {
    const liste = $('job-list');
    liste.replaceChildren();
    $('rst-jobs').classList.toggle('hidden', rstJobs.length === 0);
    if (!rstJobs.length) { $('rst-jobs-counter').textContent = ''; return; }

    const actifs = rstJobs.filter((entree) => !ETATS_FINAL.includes((rstEtats[entree.id] || {}).status)).length;
    $('rst-jobs-counter').textContent = `${actifs} en cours · ${rstJobs.length} travail(aux)`;

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
        (data.status === 'queued' && data.queue_position ? ` · position ${data.queue_position}` : '')));

      const barre = el('div', 'job-progress');
      barre.append(Object.assign(el('i'), { style: `width:${Number(data.progress || 0)}%` }));
      carte.append(barre);

      if (data.found_videos && data.found_videos.length) {
        carte.append(el('p', 'job-found', `${data.found_videos.length} vidéo(s) réellement trouvée(s)`));
      }
      if (data.error) carte.append(el('p', 'job-error', data.error));

      const actions = el('div', 'job-actions');
      if (data.url) {
        actions.append(boutonAction('Voir', () => showResult(data.url, data.drive, data.title || entree.titre)));
        const telecharger = el('a', '', 'Télécharger');
        telecharger.href = data.url; telecharger.download = '';
        actions.append(telecharger);
      }
      if (!fini) {
        actions.append(boutonAction('Annuler', async (evenement) => {
          const bouton = evenement.currentTarget;
          if (!confirm(`Annuler « ${entree.titre || rstLienCourt(entree.lien)} » ?`)) return;
          bouton.disabled = true;
          try {
            await requestJSON(`/api/jobs/${encodeURIComponent(entree.id)}/cancel`, { body: {} });
          } catch (error) { toast(error.message, true); bouton.disabled = false; }
        }));
      } else {
        actions.append(boutonAction('Retirer', () => {
          rstJobs = rstJobs.filter((autre) => autre.id !== entree.id);
          delete rstEtats[entree.id];
          U.saveRstJobs(localStorage, rstJobs);
          const bloc = document.querySelector(`.found-bloc[data-job="${entree.id}"]`);
          if (bloc) bloc.remove();
          renderRstJobs();
          renderRstFoundBlocs();
        }));
      }
      const ouvrir = el('a', '', 'Lien de départ ↗');
      ouvrir.href = entree.lien; ouvrir.target = '_blank'; ouvrir.rel = 'noopener';
      actions.append(ouvrir);
      carte.append(actions);

      liste.append(carte);
    });
  }

  /* --- « Vidéos trouvées par RsT » : un bloc par travail --- */

  function renderRstFoundBlocs() {
    const conteneur = $('rst-found');
    const avecVideos = rstJobs.filter((entree) => (rstEtats[entree.id] || {}).found_videos);
    $('rst-resultats').classList.toggle('hidden', avecVideos.length === 0);
    if (!avecVideos.length) { conteneur.replaceChildren(); $('rst-compteur').textContent = ''; return; }

    let total = 0, retenuesTotal = 0;
    conteneur.replaceChildren();
    avecVideos.forEach((entree, index) => {
      const data = rstEtats[entree.id] || {};
      const videos = data.found_videos || [];
      const requetes = data.search_queries || [];
      const retenues = videos.filter((video) => video.selected).length;
      total += videos.length; retenuesTotal += retenues;

      const bloc = el('section', 'found-bloc');
      bloc.dataset.job = entree.id;
      const tete = el('div', 'list-head');
      tete.append(el('h4', '', `RsT ${index + 1} · ${entree.titre || rstLienCourt(entree.lien)}`));
      tete.append(el('span', 'counter', `${videos.length} trouvée(s) · ${retenues} retenue(s)`));
      bloc.append(tete);
      bloc.append(el('p', 'hint', videos.length
        ? `Recherches réellement effectuées : ${requetes.join(' · ')}. Aucune vidéo inventée : chaque ligne provient de TikTok.`
        : 'Recherche en cours…'));

      const liste = el('div', 'found-list');
      videos.forEach((video) => liste.append(foundItem(video)));
      bloc.append(liste);
      conteneur.append(bloc);
    });
    $('rst-compteur').textContent = `${total} trouvée(s) · ${retenuesTotal} retenue(s) sur ${avecVideos.length} travail(aux)`;
    $('rst-resume').textContent =
      'Chaque bloc correspond à un lien de départ et à son propre travail RsT. Aucune donnée inventée.';
  }

  /* --- Suivi indépendant d'un travail RsT --- */

  async function suivreRstJob(entree) {
    if (rstSuivis.has(entree.id)) return;
    rstSuivis.add(entree.id);
    try {
      for (;;) {
        let data;
        try {
          data = await requestJSON(`/api/jobs/${encodeURIComponent(entree.id)}`, {
            retryTransient: true, retryForMs: 90000
          });
        } catch (error) {
          if (error.status === 404) {
            rstEtats[entree.id] = {
              status: 'failed', progress: 0, detail: 'Travail expiré',
              error: 'Travail introuvable ou expiré (6 h).'
            };
            renderRstJobs();
            return;
          }
          rstEtats[entree.id] = {
            ...(rstEtats[entree.id] || {}), detail: error.message
          };
          renderRstJobs();
          await U.sleep(4000);
          continue;
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
          const restants = rstJobs.filter(
            (autre) => !ETATS_FINAL.includes((rstEtats[autre.id] || {}).status)
          );
          U.saveRstJobs(localStorage, restants);
          loadHistory();
          majStatutRst();
          return;
        }
        await U.sleep(2200);
      }
    } finally {
      rstSuivis.delete(entree.id);
    }
  }

  function majStatutRst() {
    const actifs = rstJobs.filter((entree) => !ETATS_FINAL.includes((rstEtats[entree.id] || {}).status));
    const termines = rstJobs.length - actifs.length;
    if (actifs.length) {
      setStatus($('statut-rst'), `${actifs.length} travail(aux) RsT en cours, ${termines} terminé(s). Le suivi reprend après une actualisation.`);
    } else if (rstJobs.length) {
      setStatus($('statut-rst'), `${rstJobs.length} travail(aux) RsT terminé(s).`, 'success');
    }
    $('nav-dot').classList.toggle('hidden', !actifs.length && !activeJobId);
    $('btn-rst').disabled = Boolean(activeJobId);
  }

  function bindRst() {
    $('rst-coller').addEventListener('click', () => {
      collerPressePapier((texte) => {
        const existant = $('rst-liens').value.trim();
        $('rst-liens').value = existant ? `${existant}\n${texte}` : texte;
        rstCompterLiens();
        toast('Liens collés dans RsT.');
      });
    });

    $('rst-clear').addEventListener('click', () => {
      $('rst-liens').value = '';
      rstCompterLiens();
      setStatus($('statut-rst'), 'Liens RsT effacés.');
    });

    $('rst-liens').addEventListener('input', rstCompterLiens);

    $('btn-rst').addEventListener('click', () => guards.rst.run(async () => {
      const bouton = $('btn-rst'), statut = $('statut-rst');
      const parsed = U.parseRstLinks($('rst-liens').value, RST_MAX);
      if (parsed.errors.length) {
        setStatus(statut, parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
        return;
      }
      if (!parsed.links.length) {
        setStatus(statut, `Colle 1 à ${RST_MAX} liens TikTok de départ, un par ligne.`, 'error');
        return;
      }
      const actifs = rstJobs.filter((entree) => !ETATS_FINAL.includes((rstEtats[entree.id] || {}).status));
      if (actifs.length + parsed.links.length > RST_MAX) {
        setStatus(statut, `${RST_MAX} travaux RsT maximum en parallèle (${actifs.length} déjà en cours).`, 'error');
        return;
      }

      bouton.disabled = true;
      setStatus(statut, `Lancement de ${parsed.links.length} travail(aux) RsT…`);
      let lances = 0;
      const echecs = [];
      for (const lien of parsed.links) {
        const titre = `RsT · ${rstLienCourt(lien)}`;
        try {
          const reponse = await requestJSON('/api/jobs/rst', {
            body: {
              titre, lien,
              mode: settings.mode,
              intensite_transitions: settings.intensite,
              voix_off: voixOff.rst,
              idempotency_key: uuid()
            },
            retryTransient: true, retryForMs: 45000,
            onRetry: (message) => setStatus(statut, message)
          });
          const entree = { id: reponse.job_id, lien, titre };
          if (!rstJobs.some((autre) => autre.id === entree.id)) rstJobs.push(entree);
          U.saveRstJobs(localStorage, rstJobs);
          renderRstJobs();
          suivreRstJob(entree);
          lances += 1;
        } catch (error) {
          echecs.push(`${rstLienCourt(lien)} : ${error.message}`);
        }
      }
      bouton.disabled = false;
      if (lances) {
        $('rst-liens').value = '';
        rstCompterLiens();
        toast(`${lances} travail(aux) RsT lancé(s).`);
      }
      setStatus(
        statut,
        echecs.length
          ? `${lances} travail(aux) lancé(s). Échec : ${echecs.join(' · ')}`
          : `${lances} travail(aux) RsT lancé(s). Render les traite l’un après l’autre ; le suivi survit à une actualisation.`,
        echecs.length ? 'error' : 'success'
      );
    }));
  }

  function resumeRstJobs() {
    rstJobs = U.loadRstJobs(localStorage);
    if (!rstJobs.length) return;
    renderRstJobs();
    setStatus($('statut-rst'), 'Reprise du suivi des travaux RsT après actualisation…');
    rstJobs.forEach((entree) => suivreRstJob(entree));
  }

  /* ------------------------------------------------------------------
     SECTION 3 : MONTAGE MULTI-SOURCE
  ------------------------------------------------------------------ */

  function resetDiagnostic() {
    diagnosticData = null;
    $('btn-montage').disabled = true;
    $('btn-add-project').disabled = true;
    $('diagnostic').classList.add('hidden');
  }

  function currentProject(risqueAccepte = false) {
    const style = document.querySelector('input[name="style-montage"]:checked')?.value || 'classique';
    return {
      titre: $('mon-title').value.trim() || `Création ${batchDrafts.length + 1}`,
      hook: $('mon-hook').value.trim(),
      corps: $('mon-corps').value.trim(),
      liens_videos: U.parseLinks($('mon-liens').value, 20).links,
      lien_reference_style: $('mon-style-ref').value.trim(),
      style,
      resolution: resolutionPourMode(),
      mode: settings.mode,
      intensite_transitions: settings.intensite,
      voix_off: voixOff.montage,
      estimated_seconds: Number(diagnosticData?.estimated_seconds || 0),
      accepter_risque: risqueAccepte,
      idempotency_key: uuid()
    };
  }

  function renderBatchDrafts() {
    U.saveBatchDrafts(localStorage, batchDrafts);
    $('batch-counter').textContent = `${batchDrafts.length}/6`;
    $('batch-drafts').classList.toggle('hidden', batchDrafts.length === 0);
    $('btn-launch-batch').disabled = batchDrafts.length === 0 || Boolean(activeJobId);
    const liste = $('batch-draft-list');
    liste.replaceChildren();
    batchDrafts.forEach((projet, index) => {
      const item = el('div', 'draft-item');
      const haut = el('div', 'draft-top');
      haut.append(el('p', 'draft-title', projet.titre));
      const retirer = el('button', 'draft-remove', '×');
      retirer.title = 'Retirer de la file';
      retirer.addEventListener('click', () => { batchDrafts.splice(index, 1); renderBatchDrafts(); });
      haut.append(retirer);
      item.append(haut);
      item.append(el('p', 'draft-meta',
        `${projet.liens_videos.length} source(s) · ${Math.max(1, Math.ceil(projet.estimated_seconds / 60))} min estimées` +
        `${projet.lien_reference_style ? ' · référence de style' : ''} · ${projet.mode === 'qualite' ? 'Qualité' : 'Rapide'}`));
      liste.append(item);
    });
    if (batchDrafts.length >= 6) $('btn-add-project').disabled = true;
  }

  function resetProjectForm() {
    ['mon-title', 'mon-hook', 'mon-corps', 'mon-liens', 'mon-style-ref'].forEach((id) => { $(id).value = ''; });
    resetDiagnostic();
    validateLinksLocally(false);
  }

  function validateLinksLocally(planifierServeur = true) {
    const parsed = U.parseLinks($('mon-liens').value, 20);
    const brut = $('mon-liens').value.split(/\r?\n/).filter((ligne) => ligne.trim()).length;
    $('liens-counter').textContent =
      `${Math.min(parsed.links.length, 20)}/20 liens${parsed.duplicates.length ? ` · ${parsed.duplicates.length} doublon(s)` : ''}`;
    $('liens-counter').classList.toggle('over', brut > 20 || parsed.errors.length > 0);
    resetDiagnostic();
    clearTimeout(diagnosticTimer);
    if (parsed.errors.length) {
      setStatus($('statut-montage'), parsed.errors.map((e) => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
    } else if (parsed.links.length) {
      setStatus($('statut-montage'), `${parsed.links.length} lien(s) de forme valide. Lance « Valider » pour vérifier l’accès réel.`);
      if (planifierServeur) diagnosticTimer = setTimeout(runDiagnostic, 1100);
    } else {
      setStatus($('statut-montage'), 'Ajoute entre 1 et 20 liens TikTok.');
    }
    return parsed;
  }

  function renderDiagnostic(data) {
    diagnosticData = data;
    $('diagnostic').classList.remove('hidden');
    $('diagnostic-title').textContent =
      `${data.valid_count} source(s) réellement accessible(s) · ${data.invalid_count} erreur(s)`;
    $('estimate').textContent =
      `${data.estimated_label} sur Render gratuit — estimation, pas une promesse.${data.warning ? ` ${data.warning}` : ''}`;
    $('estimate').classList.toggle('warning', !data.likely_under_10_minutes);
    const liste = $('diagnostic-sources');
    liste.replaceChildren();
    (data.sources || []).forEach((source) => appendSource(liste, source));
    (data.errors || [])
      .filter((erreur) => !(data.sources || []).some((source) => source.url === erreur.url))
      .forEach((erreur) => appendSource(liste, erreur, true));
    if (data.reference) {
      appendSource(liste, { ...data.reference, url: `Référence · ${data.reference.url}` }, data.reference.status !== 'valid');
    }
    const inutilisable = data.valid_count < 1 || data.reference?.status === 'unavailable' || Boolean(activeJobId);
    $('btn-montage').disabled = inutilisable;
    $('btn-add-project').disabled = inutilisable || batchDrafts.length >= 6;
    setStatus(
      $('statut-montage'),
      data.valid_count ? 'Validation terminée : état, durée et erreurs sont réels.' : 'Aucune source accessible.',
      data.valid_count ? 'success' : 'error'
    );
  }

  async function runDiagnostic() {
    return guards.diagnostic.run(async () => {
      const parsed = U.parseLinks($('mon-liens').value, 20);
      if (!parsed.links.length || parsed.errors.length) return;
      const bouton = $('btn-diagnostic');
      bouton.disabled = true;
      setStatus($('statut-montage'), `Validation réelle 0/${parsed.links.length} — FFprobe peut prendre un moment…`);
      try {
        const data = await requestJSON('/api/montage/diagnostic', {
          body: { liens_videos: parsed.links, lien_reference_style: $('mon-style-ref').value.trim() },
          retryTransient: true, retryForMs: 45000,
          onRetry: (message) => setStatus($('statut-montage'), message)
        });
        renderDiagnostic(data);
      } catch (error) {
        resetDiagnostic();
        setStatus($('statut-montage'), error.message, 'error');
      } finally {
        bouton.disabled = Boolean(activeJobId);
      }
    });
  }

  function bindMontage() {
    $('btn-coller-sources').addEventListener('click', () => {
      collerPressePapier((texte) => {
        const existant = $('mon-liens').value.trim();
        $('mon-liens').value = existant ? `${existant}\n${texte}` : texte;
        validateLinksLocally(true);
        toast('Liens collés dans les sources.');
      });
    });

    $('btn-diagnostic').addEventListener('click', () => {
      selectMtab('sources');
      runDiagnostic();
    });

    $('btn-clear-sources').addEventListener('click', () => {
      $('mon-liens').value = '';
      resetDiagnostic();
      validateLinksLocally(false);
      setStatus($('statut-montage'), 'Sources effacées.');
    });

    $('mon-liens').addEventListener('input', () => validateLinksLocally(true));
    $('mon-style-ref').addEventListener('input', () => {
      resetDiagnostic();
      clearTimeout(diagnosticTimer);
      diagnosticTimer = setTimeout(runDiagnostic, 1100);
    });

    $('ref-coller').addEventListener('click', () => {
      collerPressePapier((texte) => { $('mon-style-ref').value = texte.split(/\r?\n/)[0].trim(); });
    });

    $('btn-reference').addEventListener('click', () => guards.reference.run(async () => {
      const bouton = $('btn-reference'), statut = $('statut-reference');
      const lien = $('mon-style-ref').value.trim();
      if (!lien) { setStatus(statut, 'Colle d’abord un lien TikTok de référence.', 'error'); return; }
      bouton.disabled = true;
      setStatus(statut, 'Transcription et réécriture du script de la référence…');
      try {
        const data = await launchJob('reference', { lien, idempotency_key: uuid() }, statut);
        $('mon-hook').value = data.hook || '';
        $('mon-corps').value = data.corps || '';
        selectMtab('script');
        setStatus(statut, 'Script récupéré depuis la référence. Vérifie les sources puis valide-les.', 'success');
      } catch (error) {
        setStatus(statut, error.message, 'error');
      } finally { bouton.disabled = Boolean(activeJobId); }
    }));

    $('btn-add-project').addEventListener('click', () => {
      const statut = $('statut-montage');
      if (batchDrafts.length >= 6) { setStatus(statut, 'La file contient déjà 6 projets.', 'error'); return; }
      if (!$('mon-hook').value.trim() || !$('mon-corps').value.trim()) {
        setStatus(statut, 'Complète l’accroche et le script (onglet Script).', 'error');
        selectMtab('script');
        return;
      }
      if (!diagnosticData?.valid_count) {
        setStatus(statut, 'Valide d’abord les sources de ce projet.', 'error');
        selectMtab('sources');
        return;
      }
      batchDrafts.push(currentProject(false));
      renderBatchDrafts();
      resetProjectForm();
      setStatus(statut, `Projet ajouté. Prépare le suivant ou lance les ${batchDrafts.length} création(s).`, 'success');
    });

    $('btn-launch-batch').addEventListener('click', () => guards.batch.run(async () => {
      if (!batchDrafts.length) return;
      const risqués = batchDrafts.filter((projet) => projet.estimated_seconds > 540);
      if (risqués.length && !confirm(
        `${risqués.length} projet(s) risquent de dépasser 9 minutes chacun sur Render gratuit.\n\nLancer quand même la file ?`
      )) return;
      const bouton = $('btn-launch-batch');
      bouton.disabled = true;
      const projets = batchDrafts.map((projet) => ({ ...projet, accepter_risque: projet.estimated_seconds > 540 }));
      try {
        const resultat = await requestJSON('/api/jobs/montage/batch', {
          body: { projets, idempotency_key: uuid() },
          retryTransient: true, retryForMs: 45000,
          onRetry: (message) => setStatus($('statut-montage'), message)
        });
        batchDrafts = [];
        U.clearBatchDrafts(localStorage);
        renderBatchDrafts();
        setStatus(
          $('statut-montage'),
          `${resultat.count} création(s) placée(s) dans la file. Le suivi continue même après une actualisation.`,
          'success'
        );
        toast(`${resultat.count} création(s) ajoutée(s) à la file.`);
        loadHistory();
      } catch (error) {
        setStatus($('statut-montage'), error.message, 'error');
      } finally {
        bouton.disabled = batchDrafts.length === 0 || Boolean(activeJobId);
      }
    }));

    $('btn-montage').addEventListener('click', () => guards.montage.run(async () => {
      const bouton = $('btn-montage'), statut = $('statut-montage');
      if (!$('mon-hook').value.trim() || !$('mon-corps').value.trim()) {
        setStatus(statut, 'Complète l’accroche et le script (onglet Script).', 'error');
        selectMtab('script');
        return;
      }
      if (!diagnosticData || !diagnosticData.valid_count) {
        setStatus(statut, 'Valide d’abord les sources et l’estimation (onglet Sources).', 'error');
        selectMtab('sources');
        return;
      }
      let risqueAccepte = false;
      if (!diagnosticData.likely_under_10_minutes) {
        risqueAccepte = confirm(`${diagnosticData.warning}\n\nLancer malgré le risque d’interruption à la limite globale ?`);
        if (!risqueAccepte) return;
      }
      bouton.disabled = true;
      setStatus(statut, 'Création du travail idempotent…');
      try {
        await launchJob('montage', currentProject(risqueAccepte), statut);
        setStatus(statut, 'Montage terminé.', 'success');
      } catch (error) {
        setStatus(statut, error.message, 'error');
      } finally {
        bouton.disabled = Boolean(activeJobId) || !diagnosticData;
      }
    }));
  }

  /* ------------------------------------------------------------------
     ANNULATION D'UN TRAVAIL
  ------------------------------------------------------------------ */

  function bindAnnulation() {
    $('btn-cancel').addEventListener('click', async () => {
      if (!activeJobId || !confirm('Annuler ce travail et nettoyer ses fichiers temporaires ?')) return;
      $('btn-cancel').disabled = true;
      try {
        const data = await requestJSON(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, { body: {} });
        $('tracker-detail').textContent = data.message || 'Annulation demandée…';
      } catch (error) {
        toast(error.message, true);
        $('btn-cancel').disabled = false;
      }
    });
  }

  /* ------------------------------------------------------------------
     MES CRÉATIONS : HISTORIQUE RÉEL
  ------------------------------------------------------------------ */

  function boutonAction(texte, gestionnaire) {
    const bouton = el('button', '', texte);
    bouton.addEventListener('click', gestionnaire);
    return bouton;
  }

  function renderHistory(data) {
    const conteneur = $('history-list');
    conteneur.replaceChildren();
    const jobs = data.jobs || [];
    if (!jobs.length) {
      conteneur.append(el('p', 'hint', 'Aucune création récente. Lance ta première vidéo depuis la section Créer.'));
      return false;
    }
    let actifs = false;
    jobs.forEach((job) => {
      const enCours = !ETATS_FINAL.includes(job.status);
      actifs ||= enCours;
      const item = el('article', 'history-item');

      const haut = el('div', 'history-top');
      const titre = el('p', 'history-title');
      titre.append(el('span', 'history-type', typeLabels[job.type] || job.type || 'Création'));
      titre.append(document.createTextNode(job.title || 'Création vidéo'));
      const pourcent = el('span', `history-pct${job.status === 'completed' ? ' done' : ETATS_FINAL.includes(job.status) ? ' ko' : ''}`,
        `${job.progress || 0}%`);
      haut.append(titre, pourcent);

      const meta = el('p', 'history-meta');
      const file = job.status === 'queued' && job.queue_position ? ` · position ${job.queue_position}` : '';
      const lot = job.batch_total > 1 ? ` · lot ${Number(job.batch_index) + 1}/${job.batch_total}` : '';
      const trouve = job.type === 'rst' && job.found_count ? ` · ${job.found_count} vidéo(s) trouvée(s)` : '';
      meta.textContent =
        `${statusLabels[job.status] || job.status}${file}${lot}${trouve} · ${job.detail || ''} · ${fmtHeure(job.created_at_unix)}`;

      const barre = el('div', 'history-progress');
      barre.append(Object.assign(el('i'), { style: `width:${Number(job.progress || 0)}%` }));

      item.append(haut, meta, barre);
      if (job.error || job.source_error_count) {
        item.append(el('p', 'history-error',
          job.error || `${job.source_error_count} avertissement(s) source`));
      }

      const actions = el('div', 'history-links');
      if (job.url) {
        actions.append(boutonAction('Voir', () => showResult(job.url, job.drive, job.title)));
        const telecharger = el('a', '', 'Télécharger');
        telecharger.href = job.url;
        telecharger.download = '';
        actions.append(telecharger);
      }
      if (job.drive && job.drive.url) {
        const drive = el('a', '', 'Drive ↗');
        drive.href = job.drive.url;
        drive.target = '_blank';
        drive.rel = 'noopener';
        actions.append(drive);
      }
      if (enCours) {
        actions.append(boutonAction('Annuler', async (evenement) => {
          const bouton = evenement.currentTarget;
          if (!confirm(`Annuler « ${job.title || 'cette création'} » ?`)) return;
          bouton.disabled = true;
          try {
            await requestJSON(`/api/jobs/${encodeURIComponent(job.job_id)}/cancel`, { body: {} });
            await loadHistory();
          } catch (error) {
            toast(error.message, true);
            bouton.disabled = false;
          }
        }));
      }
      if (actions.childElementCount) item.append(actions);
      conteneur.append(item);
    });
    $('nav-dot').classList.toggle('hidden', !actifs && !activeJobId);
    return actifs;
  }

  async function loadHistory() {
    clearTimeout(historyTimer);
    try {
      const data = await requestJSON('/api/jobs', {
        retryTransient: true, retryForMs: 45000,
        onRetry: (message) => { $('history-list').replaceChildren(el('p', 'hint', message)); }
      });
      const actifs = renderHistory(data);
      historyTimer = setTimeout(loadHistory, actifs ? 3000 : 30000);
    } catch (error) {
      $('history-list').replaceChildren(el('p', 'hint', error.message));
      historyTimer = setTimeout(loadHistory, 15000);
    }
  }

  /* ------------------------------------------------------------------
     REPRISE APRÈS ACTUALISATION
  ------------------------------------------------------------------ */

  async function resumeJob() {
    const saved = U.loadActiveJob(localStorage);
    if (!saved) return;
    if (saved.type === 'rst') { U.clearActiveJob(localStorage); return; }  // RsT a son propre suivi multiple
    const statut = saved.type === 'montage' ? $('statut-montage')
      : saved.type === 'rst' ? $('statut-rst')
        : saved.type === 'reference' ? $('statut-reference')
          : saved.type === 'video' ? $('statut-video') : $('statut-analyser');
    showView('creer');
    selectTab(saved.type === 'montage' ? 'montage' : saved.type === 'rst' ? 'rst' : 'lien');
    setStatus(statut, 'Reprise du suivi après actualisation…');
    try {
      const data = await waitJob(saved.id, saved.type, statut);
      if (saved.type === 'analyser') {
        $('hook').value = data.hook || '';
        $('corps').value = data.corps || '';
        $('mot-cle').value = data.mot_cle_broll || '';
        $('etape-script').classList.remove('hidden');
      } else if (saved.type === 'reference') {
        $('mon-hook').value = data.hook || '';
        $('mon-corps').value = data.corps || '';
        selectTab('montage');
        selectMtab('script');
      }
      setStatus(statut, 'Travail retrouvé et terminé.', 'success');
    } catch (error) {
      setStatus(statut, error.message, 'error');
    }
  }

  /* ------------------------------------------------------------------
     DÉMARRAGE
  ------------------------------------------------------------------ */

  function init() {
    bindNavigation();
    bindTabs();
    bindSettings();
    bindLienVideo();
    bindRst();
    bindMontage();
    bindAnnulation();
    bindIntegrations();
    bindPlayer();
    bindVoixOff();
    renderBatchDrafts();
    validateLinksLocally(false);
    rstCompterLiens();

    const params = new URLSearchParams(location.search);
    if (params.get('status')) {
      toast(params.get('status') === 'connected'
        ? `${params.get('integration') === 'drive' ? 'Google Drive' : 'TikTok'} connecté avec succès.`
        : 'La connexion a échoué. Réessaie depuis Connexions.',
        params.get('status') !== 'connected');
      history.replaceState({}, '', location.pathname);
    }

    requestJSON('/api/styles').then((data) => chargerStyles(data.styles)).catch(() => chargerStyles());
    loadConfig().then(loadSante);
    loadIntegrations().then(loadHistory);
    resumeJob();
    resumeRstJobs();
  }

  init();
})();
