<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Générateur de vidéos</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #FAFAF9; --surface: #FFFFFF; --ink: #14141C; --ink-soft: #5C5C68;
    --border: #E4E4E9; --accent: #4338CA; --accent-ink: #FFFFFF; --accent-soft: #EEF0FF;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font-family: 'Inter', -apple-system, sans-serif; line-height: 1.5;
    padding: env(safe-area-inset-top,0px) 20px env(safe-area-inset-bottom,0px);
  }
  h1, h2 { font-family: 'Space Grotesk', sans-serif; }
  .wrap { max-width: 640px; margin: 0 auto; padding: 32px 0 80px; }
  h1 { font-size: 1.5rem; margin: 0 0 6px; }
  .sous { color: var(--ink-soft); margin: 0 0 24px; font-size: 0.95rem; }

  .onglets { display: flex; gap: 6px; margin-bottom: 20px; border-bottom: 1px solid var(--border); }
  .onglet {
    padding: 10px 4px; font-size: 0.85rem; font-weight: 600; color: var(--ink-soft);
    background: none; border: none; border-bottom: 2px solid transparent; cursor: pointer; flex: 1;
  }
  .onglet.actif { color: var(--accent); border-bottom-color: var(--accent); }

  .mode { display: none; }
  .mode.actif { display: block; }

  .etape { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 24px; margin-bottom: 20px; }
  .etape h2 { font-size: 1.05rem; margin: 0 0 6px; }
  .etape .aide { color: var(--ink-soft); font-size: 0.85rem; margin: 0 0 16px; }
  .etape.masque { display: none; }

  label { display: block; font-size: 0.85rem; font-weight: 600; margin-bottom: 6px; }
  input[type=text], textarea, select {
    width: 100%; border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px;
    font-family: inherit; font-size: 0.95rem; margin-bottom: 16px; background: var(--bg); color: var(--ink);
  }
  textarea { resize: vertical; min-height: 70px; }
  textarea.liens { min-height: 90px; font-family: monospace; font-size: 0.85rem; }

  button {
    background: var(--accent); color: var(--accent-ink); border: none; border-radius: 10px;
    padding: 12px 20px; font-weight: 600; font-size: 0.95rem; cursor: pointer; width: 100%;
  }
  button:disabled { opacity: 0.55; cursor: not-allowed; }

  .style-choix { display: flex; gap: 10px; margin-bottom: 16px; flex-wrap: wrap; }
  .style-choix label {
    display: flex; align-items: center; gap: 6px; border: 1px solid var(--border);
    border-radius: 999px; padding: 6px 14px; font-weight: 500; font-size: 0.85rem;
    cursor: pointer; margin: 0;
  }
  .style-choix input { width: auto; margin: 0; }

  .statut { font-size: 0.88rem; color: var(--ink-soft); margin-top: 12px; min-height: 1.2em; }
  .erreur { color: #B3261E; }

  video { width: 100%; border-radius: 14px; margin-top: 12px; background: #000; }
  a.telecharger {
    display: block; text-align: center; margin-top: 12px; text-decoration: none;
    color: var(--accent); font-weight: 600; font-size: 0.9rem;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>Générateur de vidéos</h1>
  <p class="sous">Toujours à partir de liens — jamais de texte à coller.</p>

  <div class="onglets">
    <button class="onglet actif" data-mode="reference">Vidéo de référence</button>
    <button class="onglet" data-mode="montage">Montage multi-vidéos</button>
    <button class="onglet" data-mode="libre">Thème libre</button>
  </div>

  <!-- ============ MODE 1 : VIDÉO DE RÉFÉRENCE ============ -->
  <div class="mode actif" id="mode-reference">
    <div class="etape">
      <h2>1. Vidéo de référence</h2>
      <p class="aide">Un lien TikTok. Le hook d'origine est repris tel quel, le reste du script est réécrit et traduit pour toi.</p>
      <label for="ref-lien">Lien TikTok</label>
      <input type="text" id="ref-lien" placeholder="https://www.tiktok.com/@...">
      <button id="btn-reference">Récupérer le script</button>
      <p class="statut" id="statut-reference"></p>
    </div>
  </div>

  <!-- ============ MODE 2 : MONTAGE MULTI-VIDÉOS ============ -->
  <div class="mode" id="mode-montage">
    <div class="etape">
      <h2>1. Script</h2>
      <p class="aide">Ton hook et ton script (tape-les, ou récupère-les d'abord via l'onglet "Vidéo de référence").</p>
      <label for="mon-hook">Accroche (hook)</label>
      <textarea id="mon-hook"></textarea>
      <label for="mon-corps">Corps du script</label>
      <textarea id="mon-corps" style="min-height:110px;"></textarea>

      <label for="mon-liens">Liens des vidéos sources (un par ligne)</label>
      <textarea class="liens" id="mon-liens" placeholder="https://www.tiktok.com/@.../video/111&#10;https://www.tiktok.com/@.../video/222"></textarea>

      <label>Style des sous-titres</label>
      <div class="style-choix" id="style-choix-montage"></div>

      <button id="btn-montage">Générer le montage</button>
      <p class="statut" id="statut-montage">L'IA regarde chaque vidéo pour y repérer les bons passages — ça peut prendre plusieurs minutes selon le nombre de vidéos.</p>
    </div>
  </div>

  <!-- ============ MODE 3 : THÈME LIBRE (PEXELS) ============ -->
  <div class="mode" id="mode-libre">
    <div class="etape" id="etape-libre-lien">
      <h2>1. Lien source</h2>
      <p class="aide">TikTok, article, page produit... l'IA en tire un script depuis zéro.</p>
      <label for="libre-lien">Lien</label>
      <input type="text" id="libre-lien" placeholder="https://...">
      <button id="btn-analyser">Analyser</button>
      <p class="statut" id="statut-analyser"></p>
    </div>

    <div class="etape masque" id="etape-libre-script">
      <h2>2. Script</h2>
      <label for="hook">Accroche (hook)</label>
      <textarea id="hook"></textarea>
      <label for="corps">Corps du script</label>
      <textarea id="corps" style="min-height: 120px;"></textarea>
      <label for="mot-cle">Thème visuel (B-roll Pexels)</label>
      <input type="text" id="mot-cle">
      <label>Style des sous-titres</label>
      <div class="style-choix" id="style-choix-libre"></div>
      <button id="btn-video">Générer la vidéo</button>
      <p class="statut" id="statut-video"></p>
    </div>
  </div>

  <div class="etape masque" id="etape-resultat">
    <h2>Résultat</h2>
    <video id="lecteur" controls playsinline></video>
    <a class="telecharger" id="lien-telechargement" download>Télécharger la vidéo</a>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);

// Appel réseau robuste : si le serveur met trop de temps ou plante, on affiche un
// message clair au lieu de faire planter la page sur une réponse vide/illisible.
async function appelJSON(url, corps) {
  const res = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(corps),
  });
  const texte = await res.text();
  let data = {};
  if (texte) {
    try { data = JSON.parse(texte); }
    catch {
      throw new Error(
        res.ok
          ? "Réponse du serveur illisible."
          : `Le serveur a mis trop de temps à répondre ou a planté (erreur ${res.status}). Réessaie avec moins de vidéos ou des vidéos plus courtes.`
      );
    }
  }
  if (!res.ok) throw new Error(data.detail || `Erreur ${res.status}`);
  return data;
}

// --- Navigation entre les 3 modes ---
document.querySelectorAll('.onglet').forEach(bouton => {
  bouton.addEventListener('click', () => {
    document.querySelectorAll('.onglet').forEach(b => b.classList.remove('actif'));
    document.querySelectorAll('.mode').forEach(m => m.classList.remove('actif'));
    bouton.classList.add('actif');
    $('mode-' + bouton.dataset.mode).classList.add('actif');
  });
});

// --- Styles de sous-titres (chargés une fois, dupliqués dans les 2 sélecteurs concernés) ---
async function chargerStyles() {
  const res = await fetch('/api/styles');
  const { styles } = await res.json();
  const html = styles.map((s, i) => `<label><input type="radio" name="style-montage-radio" value="${s}" ${i === 0 ? 'checked' : ''}> ${s}</label>`).join('');
  const htmlLibre = styles.map((s, i) => `<label><input type="radio" name="style-libre-radio" value="${s}" ${i === 0 ? 'checked' : ''}> ${s}</label>`).join('');
  $('style-choix-montage').innerHTML = html;
  $('style-choix-libre').innerHTML = htmlLibre;
}
chargerStyles();

function afficherResultat(url) {
  $('lecteur').src = url;
  $('lien-telechargement').href = url;
  $('etape-resultat').classList.remove('masque');
  $('etape-resultat').scrollIntoView({ behavior: 'smooth' });
}

// --- Mode 1 : vidéo de référence ---
$('btn-reference').addEventListener('click', async () => {
  const lien = $('ref-lien').value.trim();
  const statut = $('statut-reference');
  if (!lien) { statut.textContent = 'Colle un lien TikTok.'; statut.className = 'statut erreur'; return; }

  $('btn-reference').disabled = true;
  statut.className = 'statut';
  statut.textContent = 'Transcription + réécriture en cours...';

  try {
    const data = await appelJSON('/api/reference', { lien });

    // Bascule vers l'onglet Montage avec le script pré-rempli
    document.querySelector('.onglet[data-mode="montage"]').click();
    $('mon-hook').value = data.hook;
    $('mon-corps').value = data.corps;
    statut.textContent = '✅ Script récupéré — bascule sur "Montage multi-vidéos" pour ajouter tes vidéos sources.';
  } catch (err) {
    statut.textContent = '❌ ' + err.message;
    statut.className = 'statut erreur';
  } finally {
    $('btn-reference').disabled = false;
  }
});

// --- Mode 2 : montage multi-vidéos ---
$('btn-montage').addEventListener('click', async () => {
  const hook = $('mon-hook').value.trim();
  const corps = $('mon-corps').value.trim();
  // On ne garde que les vraies URLs (http...) : ignore les lignes vides ou mal collées.
  const liens_videos = $('mon-liens').value.split('\n').map(l => l.trim()).filter(l => l.startsWith('http'));
  const style = document.querySelector('input[name=style-montage-radio]:checked')?.value || 'classique';
  const statut = $('statut-montage');

  if (!hook || !corps) { statut.textContent = 'Renseigne le hook et le script.'; statut.className = 'statut erreur'; return; }
  if (!liens_videos.length) { statut.textContent = 'Ajoute au moins un vrai lien de vidéo (commençant par http).'; statut.className = 'statut erreur'; return; }

  $('btn-montage').disabled = true;
  statut.className = 'statut';
  statut.textContent = `Analyse de ${liens_videos.length} vidéo(s) par l'IA, puis montage... patiente.`;

  try {
    const data = await appelJSON('/api/montage', { hook, corps, liens_videos, style });
    statut.textContent = '';
    afficherResultat(data.url);
  } catch (err) {
    statut.textContent = '❌ ' + err.message;
    statut.className = 'statut erreur';
  } finally {
    $('btn-montage').disabled = false;
  }
});

// --- Mode 3 : thème libre (Pexels) ---
$('btn-analyser').addEventListener('click', async () => {
  const lien = $('libre-lien').value.trim();
  const statut = $('statut-analyser');
  if (!lien) { statut.textContent = 'Colle un lien d\'abord.'; statut.className = 'statut erreur'; return; }

  $('btn-analyser').disabled = true;
  statut.className = 'statut';
  statut.textContent = 'Analyse en cours...';

  try {
    const data = await appelJSON('/api/analyser', { lien });
    $('hook').value = data.hook;
    $('corps').value = data.corps;
    $('mot-cle').value = data.mot_cle_broll;
    $('etape-libre-script').classList.remove('masque');
    statut.textContent = '';
  } catch (err) {
    statut.textContent = '❌ ' + err.message;
    statut.className = 'statut erreur';
  } finally {
    $('btn-analyser').disabled = false;
  }
});

$('btn-video').addEventListener('click', async () => {
  const statut = $('statut-video');
  const style = document.querySelector('input[name=style-libre-radio]:checked')?.value || 'classique';

  $('btn-video').disabled = true;
  statut.className = 'statut';
  statut.textContent = 'Génération en cours (B-roll + montage)...';

  try {
    const data = await appelJSON('/api/video', {
      hook: $('hook').value, corps: $('corps').value,
      mot_cle_broll: $('mot-cle').value, style,
    });
    statut.textContent = '';
    afficherResultat(data.url);
  } catch (err) {
    statut.textContent = '❌ ' + err.message;
    statut.className = 'statut erreur';
  } finally {
    $('btn-video').disabled = false;
  }
});
</script>
</body>
</html>
