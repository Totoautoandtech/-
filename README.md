# Bot Discord — Pipeline TikTok → B-roll (100 % TikTok)

Trouve une vidéo TikTok virale, la transcrit, réécrit un script accrocheur, et cherche
+ télécharge des vidéos **TikTok** (pas de banque d'images) sur le thème détecté, toutes
sans watermark.

## Installation locale

```bash
pip install -r requirements.txt
cp .env.example .env   # puis remplir les clés
```

**ffmpeg** est utilisé pour extraire l'audio : préinstallé sur Render, mais en local :
- Debian/Ubuntu : `sudo apt install ffmpeg`
- macOS : `brew install ffmpeg`
- Windows : https://ffmpeg.org/download.html

## Créer le bot Discord

Voir **NOTICE_DISCORD.md** pour la marche à suivre détaillée, étape par étape.

## Utilisation

```bash
python main.py                    # démarre le bot Discord (mode par défaut)
python main.py --niche "fitness"  # exécute le pipeline une seule fois, en CLI, sans bot
```

Commandes Discord :
- **`/broll niche:<mot-clé>`** — pipeline complet : vidéo virale → script réécrit →
  recherche TikTok sur le thème détecté → téléchargement de ~15 vidéos B-roll, toutes
  sans watermark, dans `./downloads/broll_YYYYMMDD_HHMMSS/`.
- **`/tiktok urls:<lien(s)>`** — télécharge une ou plusieurs vidéos TikTok précises et
  les renvoie directement dans le salon (10 liens max par message, limite Discord).

## Déploiement sur Render

**Service : Web Service** (le port `$PORT` est fourni automatiquement, géré par le
serveur keep-alive intégré au script).

1. Poussez ce dossier sur un repo GitHub (**sans** le fichier `.env`).
2. Render → **New → Web Service** → connectez le repo.
3. Environment : **Python 3** — Build Command : `pip install -r requirements.txt` —
   Start Command : `python main.py`.
4. Onglet **Environment** → ajoutez toutes les variables de `.env.example` (sauf `PORT`).
5. Déployez, puis vérifiez dans les logs la ligne `Bot connecté en tant que ...`.

## Points d'attention

- **RapidAPI** : il existe des dizaines de "TikTok Scraper" différents sur la marketplace,
  chacun avec son propre schéma JSON. `_extraire_liste_videos()` cible un format courant
  (type "tiktok-scraper7") — ajustez les noms de champs à l'API que vous souscrivez.
- **Cobalt** : l'instance publique `api.cobalt.tools` impose une protection anti-bot et
  n'est plus utilisable librement en intégration. Pour le repli Cobalt, auto-hébergez
  votre propre instance et renseignez `COBALT_API_URL`.
- **B-roll 100 % TikTok** : ces clips sont de l'UGC réel, donc parfois avec incrustations
  de texte ou musique/voix déjà présentes dans l'audio — contrairement à des banques
  d'images, ce ne sont pas des plans "neutres". Pensez à couper le son et à recadrer si
  besoin dans votre montage.
- **Gemini** : `GEMINI_API_KEYS` accepte plusieurs clés séparées par des virgules ;
  rotation automatique en cas d'échec (quota, erreur).
- Vérifiez les CGU de TikTok et les droits des créateurs sur le contenu récupéré avant
  toute réutilisation.
