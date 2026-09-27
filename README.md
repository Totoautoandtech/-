# ς੮ ς८Րɿƿ੮

Tableau de bord privé de création de vidéos verticales : génération de script avec Gemini, B-roll Pexels, montage FFmpeg, connexion TikTok et sauvegarde Google Drive.

## Déploiement Render

Le dépôt contient un `render.yaml` et un `Dockerfile`. Le service doit être créé ou synchronisé comme **Blueprint Render**. La sonde de santé est disponible sur `/api/sante` et le tableau de bord est servi avec `Cache-Control: no-store` afin qu'un ancien design ne reste pas en cache.

### Variables obligatoires

- `GEMINI_API_KEYS`
- `PEXELS_API_KEY`

### Connexion TikTok

Créer une application dans TikTok for Developers, activer **Login Kit**, puis renseigner sur Render :

- `TIKTOK_CLIENT_KEY`
- `TIKTOK_CLIENT_SECRET`

URI de redirection à autoriser (remplacer le domaine) :

```text
https://VOTRE-SERVICE.onrender.com/api/oauth/tiktok/callback
```

La portée utilisée est `user.info.basic`.

### Sauvegarde Google Drive

Dans Google Cloud, activer **Google Drive API**, configurer l'écran de consentement OAuth et créer un client OAuth de type « Application Web ». Renseigner sur Render :

- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

URI de redirection à autoriser :

```text
https://VOTRE-SERVICE.onrender.com/api/oauth/google/callback
```

L'application utilise la portée limitée `drive.file` : elle ne peut gérer que les fichiers qu'elle a elle-même créés. Une vidéo terminée est automatiquement envoyée vers Drive lorsque le compte est connecté.

Si le service est derrière un autre domaine que l'URL Render, définir aussi `APP_BASE_URL=https://votre-domaine.tld`.

> Les jetons OAuth restent en mémoire côté serveur et ne sont jamais envoyés au navigateur. Après un redémarrage d'instance Render, il faut reconnecter les services.

## Développement local

```bash
python -m pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

Puis ouvrir <http://localhost:8000>. Pour tester OAuth en local, définir `APP_BASE_URL` avec une URL HTTPS publique et enregistrer les callbacks correspondants chez TikTok et Google.
