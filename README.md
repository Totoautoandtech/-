# ς੮ ς८Րɿƿ┮

Tableau de bord privé de création de vidéos verticales avec FastAPI, JavaScript natif, FFmpeg/FFprobe, Gemini, TikWM, Pexels, TikTok OAuth et Google Drive OAuth.

## Interface

Le dashboard (sombre, premium, responsive) est construit en HTML/CSS/JS natif uniquement :

- `static/index.html` — structure des vues ;
- `static/styles.css` — thème sombre complet, mobile inclus ;
- `static/main.js` — logique des trois sections, suivi des jobs, historique, OAuth.

La barre latérale contient **Créer**, **Mes créations**, **Connexions** et **Paramètres**. La vue Créer propose exactement trois sections :

1. **Lien → vidéo** — un lien d'article, de page ou de TikTok devient un script éditable (accroche + corps + univers visuel), puis une vidéo verticale sous-titrée à partir de B-roll Pexels.
2. **RsT** — jusqu'à **6 liens TikTok de départ** (un par ligne, compteur `0/6`, boutons Coller et
   Tout supprimer). Chaque lien lance **son propre travail** `POST /api/jobs/rst` et dispose d'une
   carte de suivi indépendante (progression, annulation, bouton Voir) ainsi que de son propre bloc
   « Vidéos trouvées par RsT ». Le suivi de tous ces travaux reprend après actualisation de la page.
   Pour chaque lien, le pipeline **TOP N** : RsT lit la légende réelle, rédige le script, puis demande
   à l'IA les **3 ou 5 noms** réellement cités (personnes, lieux, marques, œuvres…) — choix fait dans
   l'interface. **Chaque nom donne lieu à sa propre recherche TikTok**, les candidates sont réparties
   nom par nom (tour de rôle) pour qu'aucun nom n'écrase les autres, puis RsT retient jusqu'à 20 bonnes
   sources dans les limites de durée et de temps Render et lance le montage en **plans de 5 s maximum**.
   La recherche explore les publications du créateur de départ et chaque nom du TOP N à travers une
   **chaîne publique multi-sources** tolérante aux plages IP cloud : TikWM d'abord, puis — si ses
   endpoints de recherche répondent 403 depuis Render — les moteurs publics **SearXNG** (instances
   `opnxng.com` et `search.inetol.net`, qui agrègent Google/Bing/DuckDuckGo depuis leur propre
   serveur — requête ciblée par mots-clés, leurs moteurs perdant l'opérateur `site:`),
   **DuckDuckGo, Ecosia et Bing** (chacun en direct, puis via deux **relais publics sans
   clé** : le relais de lecture `r.jina.ai` et le relais de traduction Google `translate.goog`),
   l'**archive web Wayback** pour l'auteur, et le miroir **Urlebird** en dernier recours. Une
   source injoignable est mise hors circuit pour le reste du travail, celle qui a répondu passe
   en premier, et un budget de temps protège le montage. Les liens du miroir `sticktock.com`
   (mêmes auteurs et identifiants vidéo que TikTok) sont normalisés en `tiktok.com` puis
   revalidés par TikWM comme tous les autres. **Chaque lien
   découvert est revalidé par TikWM `/api/`** (identifiant, auteur, titre, durée réels) avant toute
   sélection — l'origine réelle de chaque vidéo est conservée dans « Vidéos trouvées par RsT », qui
   affiche durée, auteur, origine, nom recherché et raison d'exclusion. Si l'IA ne trouve aucun nom,
   RsT retombe sur les mots-clés réellement présents dans la légende (hashtags, thème visuel, mots
   fréquents). Aucune vidéo, résultat ou miniature inventé : si aucune source ne donne rien, le
   travail échoue avec un message qui détaille les recherches tentées. Le diagnostic
   `GET /api/rst/sources` vérifie en direct, depuis le serveur, quelle source passe.
   **Livraison séparée** : la vidéo finale reste **muette**, tandis que le **script `.txt`** et une
   **voix off `.mp3` générée** se téléchargent à côté, depuis la carte du travail.
3. **Montage multi-source** — jusqu'à 20 liens collés manuellement, organisés en onglets Script / Sources / Référence / Réglages, avec les boutons Coller, Valider et Tout supprimer. La validation affiche l'état réel de chaque source (accessible, durée, dimensions, codec, erreur).

Les **modes Rapide et Qualité** (720 × 1280 priorité vitesse, ou CRF 21 + 1080 × 1920 si `AUTORISER_EXPORT_1080`) et l'**intensité des transitions** (aucune, légère, modérée, forte) sont réglables dans Paramètres et dans l'onglet Réglages ; ils s'appliquent aux trois sections. Le suivi de génération est réel (états, progression, état par source), l'historique des 6 dernières heures permet lecture, téléchargement, Drive et annulation, et un travail en cours est repris après actualisation de la page. Aucun faux compte, aucune fausse donnée, aucun abonnement payant.

## Voix off : importée ou générée

Deux voies, aucune ne coûte quoi que ce soit :

- **Importée** — l'utilisateur envoie son propre enregistrement (`.aac .m4a .mp3 .ogg .opus .wav`,
  25 Mo maximum, conservé 6 h et lié à sa session). Cette piste-là est **incrustée dans la vidéo**.
- **Générée (RsT)** — le script est lu par [`edge-tts`](https://pypi.org/project/edge-tts/), qui
  utilise les voix Microsoft Edge : **gratuit, sans clé d'API et sans compte**. Voix par défaut
  `fr-FR-DeniseNeural`, réglable via `EDGE_TTS_VOIX` et `EDGE_TTS_DEBIT`.
  *Speechma a été écarté : payant.*

La voix générée n'est **jamais** mixée dans la vidéo : RsT livre trois fichiers indépendants —
la vidéo muette, `script.txt` et `voix-off.mp3` — pour laisser le montage final libre.

`edge-tts` contacte `speech.platform.bing.com`. Si ce domaine est bloqué par le réseau, la synthèse
échoue **proprement** : le travail se termine quand même, la vidéo et le script sont livrés, et
l'interface affiche la raison exacte de l'absence de MP3. Aucun audio factice n'est produit.

## Montage multi-source

Le mode **Montage multi-source** accepte de 1 à 20 liens TikTok (un par ligne). Avant le lancement, le diagnostic :

- retire les paramètres après `?` et les doublons ;
- résout chaque source puis lit durée, dimensions, cadence et codec avec FFprobe ;
- signale chaque lien inaccessible sans condamner les autres ;
- rappelle la durée maximale par source et estime le temps de calcul ;
- avertit lorsqu'un Render gratuit a peu de chances de finir en moins de 10 minutes.

Pendant le travail, les téléchargements sont séquentiels. Les aperçus couvrent **toute la durée autorisée** à environ 360p/6 FPS, sans audio, et sont supprimés juste après l'analyse. Une seule analyse Gemini est lancée par défaut (`2` maximum configurable). Les originaux, jamais les aperçus, alimentent un graphe FFmpeg final en 720 × 1280, 24 FPS, H.264/yuv420p. L'accroche utilise des plans de 0,6 à 1,5 s ; les scènes principales visent 4,5 à 5,5 s.

Une référence de style facultative peut guider approximativement le rythme, les coupes, les zooms et les sous-titres ASS. Ses images, son son, son logo, son watermark et son contenu créatif ne sont jamais recopiés.

Les jobs publient les états `queued`, `validating`, `downloading`, `analysing`, `selecting`, `editing`, `subtitling`, `uploading`, `completed` ou `failed`, avec progression et détail. L'identifiant est gardé dans `localStorage` : une actualisation reprend le suivi. Les erreurs réseau/502/503/504 sont retentées avec backoff et un travail peut être annulé.

Il est possible de préparer **jusqu'à six projets complets et différents** (titre, script, 1–20 sources, référence de style et réglages propres), puis de lancer le lot. Render Free les traite séquentiellement pour rester sous 512 Mo. La file et les résultats sont visibles pendant six heures dans le même navigateur ; chaque vidéo terminée est aussi envoyée vers Drive si le compte est connecté. Six travaux proches de la limite individuelle de 9 min 30 représentent environ 57 minutes de file.

## Déploiement Render

Le dépôt contient `render.yaml` et un `Dockerfile`. Le Dockerfile installe FFmpeg, FFprobe, libass via FFmpeg, fontconfig, DejaVu et Liberation. La sonde est `/api/sante`.

Aucun service payant n'est intégré à l'application. Elle est conçue pour les quotas gratuits de Render, Gemini et Pexels ; leurs limites et conditions externes peuvent toutefois évoluer. Aucune durée ne peut être garantie sur un CPU gratuit partagé.

### Variables externes

À renseigner uniquement dans les variables secrètes Render, jamais dans Git :

- `GEMINI_API_KEYS` — une ou plusieurs clés séparées par des virgules ;
- `PEXELS_API_KEY` — nécessaire au mode B-roll libre ;
- `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET` — Login Kit TikTok ;
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` — OAuth Google Drive ;
- `GMAIL_ADRESSE`, `GMAIL_MOT_DE_PASSE_APP` — envoi e-mail facultatif ;
- `DESTINATAIRE_EMAIL` — destinataire facultatif ;
- `APP_BASE_URL` — URL HTTPS publique si différente de `RENDER_EXTERNAL_URL`.

### Réglages du pipeline

| Variable | Défaut | Rôle |
|---|---:|---|
| `GEMINI_MODEL` | `gemini-2.5-flash` | modèle Gemini principal |
| `GEMINI_MODELES` | `gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash` | chaîne de modèles de secours, essayés dans l'ordre en cas de 503 / 429 |
| `DUREE_CIBLE_SECONDES` | `30` | durée du mode B-roll |
| `DUREE_MAX_SOURCE_SECONDES` | `180` | limite annoncée et appliquée par source/référence |
| `JOB_TIMEOUT_SECONDES` | `570` | limite globale, soit 9 min 30 |
| `FFMPEG_TIMEOUT_SECONDES` | `240` | timeout d'une commande FFmpeg |
| `GEMINI_TIMEOUT_SECONDES` | `120` | timeout d'un appel Gemini |
| `TIKWM_TIMEOUT_SECONDES` | `30` | timeout TikWM |
| `RELAIS_LECTURE_URL` | `https://r.jina.ai/` | relais de lecture public utilisé par la découverte RsT quand l'IP du serveur est bloquée (gratuit, sans clé) |
| `DOWNLOAD_TIMEOUT_SECONDES` | `120` | timeout par téléchargement |
| `GEMINI_ANALYSES_CONCURRENTES` | `1` | `1` conseillé avec 512 Mo, maximum `2` |
| `BUDGET_DISQUE_SOURCES_MO` | `700` | budget temporaire cumulé |
| `FFMPEG_PRESET` | `ultrafast` | `ultrafast`, `superfast` ou `veryfast` |
| `FFMPEG_THREADS` | `1` | `1` conseillé sur Render Free, maximum `2` |
| `AUTORISER_EXPORT_1080` | `false` | affiche l'option 1080 × 1920, nettement plus lente |
| `RST_CANDIDATS_MAX` | `40` | vidéos TikTok candidates recherchées par RsT |
| `RST_SOURCES_MAX` | `20` | sources retenues par RsT pour le montage final |
| `EDGE_TTS_VOIX` | `fr-FR-DeniseNeural` | voix edge-tts de la voix off générée |
| `EDGE_TTS_DEBIT` | `+0%` | débit de la voix off générée |

Le TOP N (3 ou 5 noms) et le plafond de 5 s par plan ne sont pas configurables par variable
d'environnement : ce sont des constantes du pipeline (`RST_NOMS_CHOIX`, `RST_DUREE_MAX_PLAN`),
exposées en lecture via `/api/config`.

### OAuth

Callbacks à autoriser, en remplaçant le domaine :

```text
https://VOTRE-SERVICE.onrender.com/api/oauth/tiktok/callback
https://VOTRE-SERVICE.onrender.com/api/oauth/google/callback
```

TikTok utilise `user.info.basic`. Google utilise `drive.file`, limité aux fichiers créés par l'application. Les jetons restent côté serveur. Une vidéo terminée est envoyée automatiquement vers Drive si le compte est connecté ; une erreur Drive ne supprime pas le rendu local et le bouton manuel reste disponible.

Les sessions OAuth et jobs sont en mémoire. La file continue côté serveur sans dépendre de la page ouverte et une actualisation du navigateur est prise en charge. En revanche, Render Free ne garantit pas un processus continu pendant une heure : une mise en veille, un redémarrage ou un redéploiement efface la file en mémoire. Drive reste donc la récupération la plus fiable pour les vidéos déjà terminées ; l'interface n'annonce jamais qu'un lot est garanti tant que ces limites gratuites existent.

## Résilience Gemini (erreurs 503 « high demand »)

Gemini renvoie régulièrement `503 UNAVAILABLE — This model is currently experiencing high demand`
lorsque le modèle demandé est temporairement saturé. L'application ne s'arrête plus là :

1. **Chaîne de modèles de secours** — `GEMINI_MODELES` liste les modèles essayés dans l'ordre
   (défaut `gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash`). Dès qu'un modèle répond
   `503`, `UNAVAILABLE` ou `429`, l'appel bascule sur le modèle suivant, puis sur la clé suivante
   de `GEMINI_API_KEYS`.
2. **Retry renforcé** — pour les statuts `503`, `429` et `500`, jusqu'à **5 tentatives par modèle**
   avec backoff exponentiel (2, 4, 8, 16 s), toujours dans la limite de `GEMINI_TIMEOUT_SECONDES`.
   Une erreur définitive (`400`, clé invalide…) n'est pas réessayée : on passe directement au modèle
   suivant.
3. **Message clair** — si tous les modèles et toutes les clés sont saturés, l'interface affiche
   exactement : « Gemini est momentanément saturé (503). Réessaie dans quelques minutes. »

`GET /api/config` expose la chaîne réellement active dans `gemini_modeles`.

## Voix off importée

Les vidéos ne sont plus muettes : chaque section accepte un fichier **« Voix off · facultatif »**.

- `POST /api/voixoff?nom=fichier.mp3` — le fichier audio est envoyé dans le **corps HTTP brut**,
  25 Mo maximum, extensions `.mp3`, `.wav`, `.m4a`, `.aac`, `.ogg`, `.opus`.
- Le serveur vérifie la présence d'une vraie piste audio avec **FFprobe** (`_sonder_audio`), stocke
  le fichier dans `travail/voixoff/<session>/`, le lie à la session du navigateur et le purge au
  bout de 6 h comme les rendus.
- L'identifiant renvoyé est transmis dans le champ `voix_off` de `/api/jobs/video`,
  `/api/jobs/montage` et `/api/jobs/rst`.
- Intégration FFmpeg réelle : le montage ajoute l'audio via `[N:a]apad[a]` puis
  `-map [a] -c:a aac -b:a 160k -shortest` (sans voix off, le rendu reste `-an`).

Aucune synthèse vocale payante : c'est l'enregistrement fourni par l'utilisateur qui est monté.

## Performances attendues sur Render gratuit

Ordres de grandeur pour des sources **courtes (environ 15 à 30 s)**, réseau et APIs disponibles, export 720p :

| Sources | Fourchette prudente |
|---:|---:|
| 1 | 2 à 4 min |
| 5 | 4 à 7 min |
| 10 | 7 à 10 min |
| 20 | souvent 10 à 18 min, donc risque d'interruption à 9 min 30 |

Des sources proches de 180 s, un Render froid, TikWM lent, les quotas Gemini ou un export 1080p augmentent fortement ces durées. Vingt longues vidéos ne peuvent pas être garanties sous 10 minutes sur 512 Mo et très peu de CPU. Le diagnostic affiché avant lancement est calculé à partir des durées FFprobe et reste une estimation, pas une promesse.

## Développement et tests

```bash
python -m pip install -r requirements-dev.txt
python -m py_compile app.py studio_montage.py
pytest -q
node --check static/main.js
node --check static/job-utils.js
node --test tests/js/job-utils.test.cjs
git diff --check
uvicorn app:app --host 0.0.0.0 --port 8000
```

Smoke tests locaux : `/`, `/api/sante`, `/api/config`, `/api/styles`, `/static/main.js`, `/static/styles.css` et le cycle création/lecture/annulation d'un job (y compris un job RsT simulé).

Test de l'image quand Docker est disponible :

```bash
docker build -t vesper-video-generator .
docker run --rm -p 8000:8000 vesper-video-generator
```
