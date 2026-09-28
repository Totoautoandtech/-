(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const U = window.JobUtils;
  let integrations = { tiktok: {}, google_drive: {} };
  let currentVideo = '';
  let diagnosticData = null;
  let activeJobId = '';
  let diagnosticTimer = 0;
  const guards = {
    analyser: U.submissionGuard(), video: U.submissionGuard(),
    reference: U.submissionGuard(), montage: U.submissionGuard(), diagnostic: U.submissionGuard()
  };

  const setStatus = (element, message, type = '') => {
    element.textContent = message;
    element.className = `status ${type}`;
  };
  const toast = (message, error = false) => {
    const element = $('toast');
    element.textContent = message;
    element.className = `toast show${error ? ' error' : ''}`;
    clearTimeout(element._timer);
    element._timer = setTimeout(() => { element.className = 'toast'; }, 5000);
  };
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);

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
      } catch (error) {
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

  document.querySelectorAll('.tab').forEach(tab => {
    tab.onclick = () => {
      document.querySelectorAll('.tab').forEach(item => item.classList.toggle('active', item === tab));
      document.querySelectorAll('.tool').forEach(item => item.classList.toggle('active', item.dataset.tool === tab.dataset.tab));
    };
  });
  const selectTab = name => document.querySelector(`.tab[data-tab="${name}"]`)?.click();

  function styles(list) {
    const safe = Array.isArray(list) && list.length ? list : ['classique', 'jaune', 'centre'];
    ['libre', 'montage'].forEach(type => {
      $(`styles-${type}`).replaceChildren(...safe.map((style, index) => {
        const label = document.createElement('label');
        const input = document.createElement('input');
        input.type = 'radio'; input.name = `style-${type}`; input.value = style; input.checked = index === 0;
        label.append(input, document.createTextNode(style));
        return label;
      }));
    });
  }

  async function loadConfig() {
    try {
      const config = await requestJSON('/api/config');
      $('source-limit').textContent = `Paramètres après ? retirés et doublons supprimés. Durée maximale : ${config.max_source_seconds} s par source. Limite globale : ${Math.floor(config.job_timeout_seconds / 60)} min ${String(config.job_timeout_seconds % 60).padStart(2, '0')}.`;
      if (config.allow_1080) {
        const option = document.createElement('option');
        option.value = '1080'; option.textContent = '1080 × 1920 · 24 FPS · plus lent';
        $('mon-resolution').append(option);
      }
    } catch (error) { $('source-limit').textContent = error.message; }
  }

  async function loadIntegrations() {
    try {
      integrations = await requestJSON('/api/integrations/status');
      renderIntegrations();
    } catch (error) { toast(error.message, true); }
  }
  function renderIntegrations() {
    const tiktok = integrations.tiktok, drive = integrations.google_drive;
    $('tiktok-state').textContent = tiktok.connected ? (tiktok.display_name || 'Compte connecté') : (tiktok.configured ? 'Non connecté' : 'Configuration Render requise');
    $('connect-tiktok').textContent = tiktok.connected ? 'Connecté · Déconnecter' : 'Connecter TikTok';
    $('connect-tiktok').classList.toggle('connected', Boolean(tiktok.connected));
    $('drive-state').textContent = drive.connected ? (drive.email || drive.name || 'Sauvegarde active') : (drive.configured ? 'Non connecté' : 'Configuration Render requise');
    $('connect-drive').textContent = drive.connected ? 'Sauvegarde active · Déconnecter' : 'Activer la sauvegarde';
    $('connect-drive').classList.toggle('connected', Boolean(drive.connected));
  }

  $('connect-tiktok').onclick = async () => {
    if (integrations.tiktok.connected) {
      await requestJSON('/api/integrations/tiktok/disconnect', { body: {} }); await loadIntegrations(); return;
    }
    if (!integrations.tiktok.configured) { toast('Configure les identifiants TikTok uniquement dans les variables serveur Render.', true); return; }
    location.href = '/api/oauth/tiktok/start';
  };
  $('connect-drive').onclick = async () => {
    if (integrations.google_drive.connected) {
      await requestJSON('/api/integrations/google-drive/disconnect', { body: {} }); await loadIntegrations(); return;
    }
    if (!integrations.google_drive.configured) { toast('Configure les identifiants Google uniquement dans les variables serveur Render.', true); return; }
    location.href = '/api/oauth/google/start';
  };

  async function backup() {
    if (!currentVideo) return;
    if (!integrations.google_drive.connected) { toast('Connecte Google Drive pour cette sauvegarde manuelle.', true); return; }
    $('save-drive').disabled = true;
    $('drive-message').textContent = 'Sauvegarde manuelle sur Drive…';
    try {
      const data = await requestJSON('/api/integrations/google-drive/backup', { body: { url: currentVideo } });
      showDrive({ status: 'completed', ...data });
    } catch (error) {
      $('drive-message').textContent = `Erreur Drive : ${error.message}`; toast(error.message, true);
    } finally { $('save-drive').disabled = false; }
  }
  $('save-drive').onclick = backup;

  function showDrive(drive) {
    const message = $('drive-message');
    message.replaceChildren();
    if (!drive || drive.status === 'not_connected' || drive.status === 'pending') {
      message.textContent = 'Connecte Drive pour conserver ce rendu automatiquement.'; return;
    }
    if (drive.status === 'uploading') { message.textContent = 'Sauvegarde automatique en cours…'; return; }
    if (drive.status === 'failed') { message.textContent = `Erreur Drive : ${drive.error || 'sauvegarde impossible'}. La sauvegarde manuelle reste disponible.`; return; }
    message.append(document.createTextNode('Sauvegarde Drive réussie · '));
    const link = document.createElement('a');
    link.href = drive.url; link.target = '_blank'; link.rel = 'noopener'; link.style.color = 'var(--green)'; link.textContent = 'ouvrir dans Drive ↗';
    message.append(link);
  }

  function showResult(url, drive) {
    currentVideo = url;
    $('lecteur').src = url; $('telecharger').href = url;
    $('result-empty').style.display = 'none'; $('result').classList.add('visible');
    showDrive(drive);
  }

  function setBusy(busy) {
    ['btn-analyser', 'btn-video', 'btn-reference', 'btn-montage'].forEach(id => { $(id).disabled = busy || (id === 'btn-montage' && !diagnosticData); });
    $('btn-cancel').disabled = !busy;
  }

  const statusLabels = {
    queued: 'En attente sur Render', validating: 'Validation', downloading: 'Téléchargement',
    analysing: 'Analyse IA', selecting: 'Sélection des plans', editing: 'Montage FFmpeg',
    subtitling: 'Sous-titres ASS', uploading: 'Sauvegarde Drive', completed: 'Terminé',
    failed: 'Échec', cancelled: 'Annulé'
  };

  function appendSource(container, source, forceError = false) {
    const row = document.createElement('div');
    const failed = forceError || ['error', 'invalid', 'unavailable', 'download_error'].includes(source.status);
    row.className = `source-item${failed ? ' error' : ''}`;
    const dot = document.createElement('i');
    const text = document.createElement('div');
    const title = document.createElement('strong');
    title.textContent = source.url || `Source ${(source.index ?? 0) + 1}`;
    const dimensions = source.width ? `${source.duration?.toFixed?.(1) || source.duration} s · ${source.width}×${source.height}` : '';
    const detail = document.createElement('span');
    detail.textContent = source.error || source.analysis_warning || dimensions || source.status || '';
    text.append(title, detail); row.append(dot, text); container.append(row);
  }

  function renderJob(data) {
    $('job-panel').classList.add('visible');
    const percent = Number(data.progress || 0);
    $('job-percent').textContent = `${percent}%`;
    $('job-progress').style.width = `${percent}%`;
    $('job-detail').textContent = `${statusLabels[data.status] || data.status} · ${data.detail || ''}`;
    const list = $('job-sources'); list.replaceChildren();
    (data.sources || []).forEach(source => appendSource(list, source));
    (data.source_errors || []).filter(error => !((data.sources || []).some(s => s.url === error.url && s.error))).forEach(error => appendSource(list, error, true));
    showDrive(data.drive);
  }

  async function waitJob(id, type, statusElement) {
    activeJobId = id;
    setBusy(true);
    U.saveActiveJob(localStorage, { id, type, savedAt: Date.now() });
    selectTab(type === 'montage' ? 'montage' : type === 'reference' ? 'reference' : 'libre');
    for (;;) {
      let data;
      try {
        data = await requestJSON(`/api/jobs/${encodeURIComponent(id)}`, {
          retryTransient: true, retryForMs: 90000,
          onRetry: message => { setStatus(statusElement, message); $('job-detail').textContent = message; }
        });
      } catch (error) {
        if (error.status === 404) {
          U.clearActiveJob(localStorage); activeJobId = ''; setBusy(false);
        } else {
          // Le suivi peut être repris après actualisation ; ne permet pas de créer un doublon.
          setBusy(true);
        }
        throw error;
      }
      renderJob(data);
      setStatus(statusElement, `${data.detail || statusLabels[data.status]} · ${data.progress || 0}%`);
      if (data.status === 'completed') {
        U.clearActiveJob(localStorage); activeJobId = ''; setBusy(false);
        if (data.url) showResult(data.url, data.drive);
        return data;
      }
      if (data.status === 'failed' || data.status === 'cancelled') {
        U.clearActiveJob(localStorage); activeJobId = ''; setBusy(false);
        throw new Error(data.error || (data.status === 'cancelled' ? 'Travail annulé.' : 'Le travail a échoué.'));
      }
      await U.sleep(1800);
    }
  }

  async function launchJob(type, body, statusElement) {
    const data = await requestJSON(`/api/jobs/${type}`, {
      body, retryTransient: true, retryForMs: 45000,
      onRetry: message => setStatus(statusElement, message)
    });
    return waitJob(data.job_id, type, statusElement);
  }

  $('btn-analyser').onclick = () => guards.analyser.run(async () => {
    const button = $('btn-analyser'), status = $('statut-analyser'), link = $('libre-lien').value.trim();
    if (!link) { setStatus(status, 'Ajoute un lien source.', 'error'); return; }
    button.disabled = true; setStatus(status, 'Analyse du contenu…');
    try {
      const data = await launchJob('analyser', { lien: link, idempotency_key: uuid() }, status);
      $('hook').value = data.hook; $('corps').value = data.corps; $('mot-cle').value = data.mot_cle_broll;
      $('etape-script').classList.add('visible'); setStatus(status, 'Script prêt — relis-le avant de générer.', 'success');
    } catch (error) { setStatus(status, error.message, 'error'); }
    finally { button.disabled = Boolean(activeJobId); }
  });

  $('btn-video').onclick = () => guards.video.run(async () => {
    const button = $('btn-video'), status = $('statut-video');
    button.disabled = true; setStatus(status, 'Préparation de la vidéo…');
    try {
      const style = document.querySelector('input[name="style-libre"]:checked')?.value || 'classique';
      await launchJob('video', {
        hook: $('hook').value, corps: $('corps').value,
        mot_cle_broll: $('mot-cle').value, style, idempotency_key: uuid()
      }, status);
      setStatus(status, 'Vidéo terminée.', 'success');
    } catch (error) { setStatus(status, error.message, 'error'); }
    finally { button.disabled = Boolean(activeJobId); }
  });

  $('btn-reference').onclick = () => guards.reference.run(async () => {
    const button = $('btn-reference'), status = $('statut-reference'), link = $('ref-lien').value.trim();
    if (!link) { setStatus(status, 'Ajoute un lien TikTok.', 'error'); return; }
    button.disabled = true; setStatus(status, 'Transcription et réécriture…');
    try {
      const data = await launchJob('reference', { lien: link, idempotency_key: uuid() }, status);
      $('mon-hook').value = data.hook; $('mon-corps').value = data.corps;
      setStatus(status, 'Script récupéré. Ajoute maintenant tes sources.', 'success'); selectTab('montage');
    } catch (error) { setStatus(status, error.message, 'error'); }
    finally { button.disabled = Boolean(activeJobId); }
  });

  function resetDiagnostic() {
    diagnosticData = null; $('btn-montage').disabled = true;
    $('diagnostic').classList.remove('visible');
  }

  function validateLinksLocally(scheduleServer = true) {
    const parsed = U.parseLinks($('mon-liens').value, 20);
    const rawCount = $('mon-liens').value.split(/\r?\n/).filter(line => line.trim()).length;
    $('liens-counter').textContent = `${Math.min(parsed.links.length, 20)}/20 liens${parsed.duplicates.length ? ` · ${parsed.duplicates.length} doublon(s)` : ''}`;
    $('liens-counter').classList.toggle('over', rawCount > 20 || parsed.errors.length > 0);
    resetDiagnostic(); clearTimeout(diagnosticTimer);
    if (parsed.errors.length) {
      setStatus($('statut-montage'), parsed.errors.map(e => `Ligne ${e.index + 1} : ${e.error}`).join(' · '), 'error');
    } else if (parsed.links.length) {
      setStatus($('statut-montage'), `${parsed.links.length} lien(s) de forme valide. Vérification de l’accès…`);
      if (scheduleServer) diagnosticTimer = setTimeout(runDiagnostic, 900);
    } else { setStatus($('statut-montage'), 'Ajoute entre 1 et 20 liens TikTok.'); }
    return parsed;
  }

  function renderDiagnostic(data) {
    diagnosticData = data;
    $('diagnostic').classList.add('visible');
    $('diagnostic-title').textContent = `${data.valid_count} source(s) accessible(s) · ${data.invalid_count} erreur(s)`;
    $('estimate').textContent = `${data.estimated_label} sur Render gratuit — estimation, pas une promesse.${data.warning ? ` ${data.warning}` : ''}`;
    $('estimate').classList.toggle('warning', !data.likely_under_10_minutes);
    const list = $('diagnostic-sources'); list.replaceChildren();
    (data.sources || []).forEach(source => appendSource(list, source));
    (data.errors || []).filter(error => !(data.sources || []).some(s => s.url === error.url)).forEach(error => appendSource(list, error, true));
    if (data.reference) appendSource(list, { ...data.reference, url: `Référence · ${data.reference.url}` }, data.reference.status !== 'valid');
    $('btn-montage').disabled = data.valid_count < 1 || data.reference?.status === 'unavailable' || Boolean(activeJobId);
    setStatus(
      $('statut-montage'),
      data.valid_count ? 'Diagnostic terminé. Vérifie l’estimation avant le lancement.' : 'Aucune source accessible.',
      data.valid_count ? 'success' : 'error'
    );
  }

  async function runDiagnostic() {
    return guards.diagnostic.run(async () => {
      const parsed = U.parseLinks($('mon-liens').value, 20);
      if (!parsed.links.length || parsed.errors.length) return;
      const button = $('btn-diagnostic'); button.disabled = true;
      setStatus($('statut-montage'), `Validation technique 0/${parsed.links.length} — FFprobe peut prendre un moment…`);
      try {
        const data = await requestJSON('/api/montage/diagnostic', {
          body: { liens_videos: parsed.links, lien_reference_style: $('mon-style-ref').value.trim() },
          retryTransient: true, retryForMs: 45000,
          onRetry: message => setStatus($('statut-montage'), message)
        });
        renderDiagnostic(data);
      } catch (error) { resetDiagnostic(); setStatus($('statut-montage'), error.message, 'error'); }
      finally { button.disabled = false; }
    });
  }

  $('mon-liens').addEventListener('input', () => validateLinksLocally(true));
  $('mon-style-ref').addEventListener('input', () => { resetDiagnostic(); clearTimeout(diagnosticTimer); diagnosticTimer = setTimeout(runDiagnostic, 900); });
  $('btn-diagnostic').onclick = runDiagnostic;

  $('btn-montage').onclick = () => guards.montage.run(async () => {
    const button = $('btn-montage'), status = $('statut-montage');
    if (!$('mon-hook').value.trim() || !$('mon-corps').value.trim()) { setStatus(status, 'Complète l’accroche et le script.', 'error'); return; }
    if (!diagnosticData || !diagnosticData.valid_count) { setStatus(status, 'Valide d’abord les sources et l’estimation.', 'error'); return; }
    let riskAccepted = false;
    if (!diagnosticData.likely_under_10_minutes) {
      riskAccepted = confirm(`${diagnosticData.warning}\n\nLancer malgré le risque d’interruption à la limite globale ?`);
      if (!riskAccepted) return;
    }
    button.disabled = true; setStatus(status, 'Création du travail idempotent…');
    try {
      const style = document.querySelector('input[name="style-montage"]:checked')?.value || 'classique';
      await launchJob('montage', {
        hook: $('mon-hook').value, corps: $('mon-corps').value,
        // Relance aussi les sources temporairement indisponibles : elles peuvent avoir récupéré,
        // et leur erreur restera attachée au job si elles échouent encore.
        liens_videos: U.parseLinks($('mon-liens').value, 20).links,
        lien_reference_style: $('mon-style-ref').value.trim(), style,
        resolution: $('mon-resolution').value,
        estimated_seconds: diagnosticData.estimated_seconds,
        accepter_risque: riskAccepted,
        idempotency_key: uuid()
      }, status);
      setStatus(status, 'Montage terminé.', 'success');
    } catch (error) { setStatus(status, error.message, 'error'); }
    finally { button.disabled = Boolean(activeJobId) || !diagnosticData; }
  });

  $('btn-cancel').onclick = async () => {
    if (!activeJobId || !confirm('Annuler ce travail et nettoyer ses fichiers temporaires ?')) return;
    $('btn-cancel').disabled = true;
    try {
      const data = await requestJSON(`/api/jobs/${encodeURIComponent(activeJobId)}/cancel`, { body: {} });
      $('job-detail').textContent = data.message;
    } catch (error) { toast(error.message, true); $('btn-cancel').disabled = false; }
  };

  async function resumeJob() {
    const saved = U.loadActiveJob(localStorage);
    if (!saved) return;
    const status = saved.type === 'montage' ? $('statut-montage')
      : saved.type === 'reference' ? $('statut-reference')
        : saved.type === 'video' ? $('statut-video') : $('statut-analyser');
    setStatus(status, 'Reprise du suivi après actualisation…');
    try {
      const data = await waitJob(saved.id, saved.type, status);
      if (saved.type === 'analyser') {
        $('hook').value = data.hook; $('corps').value = data.corps; $('mot-cle').value = data.mot_cle_broll;
        $('etape-script').classList.add('visible');
      } else if (saved.type === 'reference') {
        $('mon-hook').value = data.hook; $('mon-corps').value = data.corps; selectTab('montage');
      }
      setStatus(status, 'Travail retrouvé et terminé.', 'success');
    } catch (error) { setStatus(status, error.message, 'error'); }
  }

  const params = new URLSearchParams(location.search);
  if (params.get('status')) {
    toast(params.get('status') === 'connected'
      ? `${params.get('integration') === 'drive' ? 'Google Drive' : 'TikTok'} connecté avec succès.`
      : 'La connexion a échoué. Réessaie.', params.get('status') !== 'connected');
    history.replaceState({}, '', location.pathname);
  }

  requestJSON('/api/styles').then(data => styles(data.styles)).catch(() => styles());
  loadConfig(); loadIntegrations(); validateLinksLocally(false); resumeJob();
})();
