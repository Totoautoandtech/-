# ς੮ ς८Րɿƿ੮

Tableau de bord privé de création de vidéos verticales avec FastAPI, JavaScript natif, FFmpeg/FFprobe, Gemini, TikWM, Pexels, TikTok OAuth et Google Drive OAuth.

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
| `GEMINI_MODEL` | `gemini-2.5-flash` | modèle Gemini |
| `DUREE_CIBLE_SECONDES` | `30` | durée du mode B-roll |
| `DUREE_MAX_SOURCE_SECONDES` | `180` | limite annoncée et appliquée par source/référence |
| `JOB_TIMEOUT_SECONDES` | `570` | limite globale, soit 9 min 30 |
| `FFMPEG_TIMEOUT_SECONDES` | `240` | timeout d'une commande FFmpeg |
| `GEMINI_TIMEOUT_SECONDES` | `120` | timeout d'un appel Gemini |
| `TIKWM_TIMEOUT_SECONDES` | `30` | timeout TikWM |
| `DOWNLOAD_TIMEOUT_SECONDES` | `120` | timeout par téléchargement |
| `GEMINI_ANALYSES_CONCURRENTES` | `1` | `1` conseillé avec 512 Mo, maximum `2` |
| `BUDGET_DISQUE_SOURCES_MO` | `700` | budget temporaire cumulé |
| `FFMPEG_PRESET` | `ultrafast` | `ultrafast`, `superfast` ou `veryfast` |
| `FFMPEG_THREADS` | `1` | `1` conseillé sur Render Free, maximum `2` |
| `AUTORISER_EXPORT_1080` | `false` | affiche l'option 1080 × 1920, nettement plus lente |

### OAuth

Callbacks à autoriser, en remplaçant le domaine :

```text
https://VOTRE-SERVICE.onrender.com/api/oauth/tiktok/callback
https://VOTRE-SERVICE.onrender.com/api/oauth/google/callback
```

TikTok utilise `user.info.basic`. Google utilise `drive.file`, limité aux fichiers créés par l'application. Les jetons restent côté serveur. Une vidéo terminée est envoyée automatiquement vers Drive si le compte est connecté ; une erreur Drive ne supprime pas le rendu local et le bouton manuel reste disponible.

Les sessions OAuth et jobs sont en mémoire. Une actualisation du navigateur est prise en charge, mais un redémarrage complet de l'instance efface l'état serveur ; le navigateur explique alors que le job a expiré au lieu d'afficher une simple erreur 502/503.

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
node --check static/app.js
node --check static/job-utils.js
node --test tests/js/job-utils.test.cjs
git diff --check
uvicorn app:app --host 0.0.0.0 --port 8000
```

Smoke tests locaux : `/`, `/api/sante`, `/api/config`, `/api/styles`, `/static/app.js` et le cycle création/lecture/annulation d'un job.

Test de l'image quand Docker est disponible :

```bash
docker build -t vesper-video-generator .
docker run --rm -p 8000:8000 vesper-video-generator
```
