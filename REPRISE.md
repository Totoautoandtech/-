# REPRISE — état réel du projet

Récapitulatif destiné à la **prochaine session**. Il ne décrit que ce qui a été
réellement vérifié : ce qui tourne, ce qui n'a jamais pu être testé, et pourquoi.

Dernière mise à jour : 30 septembre 2026.

---

## 1. Le projet en une page

Studio vidéo **ς੮ ς८Րɿƿ┮** : générateur de vidéos verticales sous-titrées.

| | |
|---|---|
| Dépôt | `Totoautoandtech/-` |
| Production | <https://s8-s8rrw8.onrender.com> — service Render **existant**, auto-deploy sur `main` |
| Backend | FastAPI + FFmpeg (`app.py`, `studio_montage.py`) |
| Frontend | HTML / CSS / JS **natifs**, aucun framework (`static/`) |
| Langue | interface **entièrement en français** |
| Thème | noir & blanc minimal, Inter + DM Mono, accent blanc `#ffffff` |

### Règles permanentes — à ne jamais enfreindre

1. **Aucune fausse donnée.** Pas de vidéo inventée, pas de miniature factice, pas
   de compte fictif. Si un service ne répond pas, on le dit dans l'interface.
2. **Aucun abonnement payant.** Speechma a été écarté pour cette raison.
3. **Pas de framework frontend.** HTML/CSS/JS natifs uniquement.
4. **Ne jamais créer de nouveau service Render.** Le déploiement se fait par
   `git push` sur `main`, qui déclenche l'auto-deploy du service existant.

---

## 2. Les trois sections de l'application

1. **Lien → vidéo** — un lien (article, page, TikTok) → script éditable → vidéo
   verticale sous-titrée.
2. **RsT** — jusqu'à 6 liens TikTok de départ, un travail indépendant par lien.
   C'est le mode le plus élaboré : voir §3.
3. **Montage multi-source** — jusqu'à 20 liens collés manuellement, chaque source
   réellement vérifiée (accès, durée, codec) avant montage.

---

## 3. RsT — pipeline TOP N (état actuel)

Pour **chaque** lien de départ :

1. **Lecture** de la vidéo TikTok via TikWM → légende réelle + auteur.
2. **Script** rédigé par Gemini à partir de la légende.
3. **TOP N** : Gemini extrait les **3 ou 5 noms** réellement cités (personnes,
   lieux, marques, œuvres…). Le nombre est choisi dans l'interface.
   → `extraire_noms_rst()` dans `app.py`.
4. **Découverte multi-sources résiliente** : les publications du créateur de
   départ (`@auteur`) et chaque nom du TOP N (puis les mots-clés de repli) sont
   cherchés à travers une chaîne publique **sans clé ni compte** :
   - TikWM `/user/posts` et `/feed/search` d'abord — **403 depuis Render**
     (blocage de plage IP, constaté en production) ;
   - moteurs **DuckDuckGo lite**, **Ecosia**, **Bing** (requête
     `site:tiktok.com … video`), chacun **en direct puis via deux relais
     publics sans clé** — le relais de lecture `r.jina.ai`, puis le relais de
     traduction Google `translate.goog` — quand l'IP du serveur est bloquée
     ou que la page reste vide ;
   - **archive web Wayback** (CDX) pour les publications d'un auteur ;
   - miroir **Urlebird** en dernier recours (Cloudflare le bloque sur Render).
   Disjoncteurs : une source en échec n'est plus tentée pendant le travail, la
   dernière source gagnante passe en tête, budget de temps global (150 s) et
   par requête (45 s). → `_decouvrir_publique()` dans `app.py`.
5. **Revalidation stricte** : chaque URL découverte est revalidée par TikWM
   `/api/` (identifiant, auteur, titre et durée **réels**), à la cadence de
   1 req/s. L'origine réelle (source + mode + recherche) est conservée dans
   `found_videos`. Rien n'est jamais inventé.
6. **Répartition** : `_repartir_par_nom()` sert les candidates nom par nom, à tour
   de rôle, pour qu'un nom prolifique ne monopolise pas le quota de sources.
7. **Sélection** : jusqu'à 20 sources dans les limites de durée et de temps Render.
8. **Montage** en **plans de 5 s maximum** (`RST_DUREE_MAX_PLAN = 5.0`).
9. **Livraison séparée** : voir §4.

### Diagnostic en production

`GET /api/rst/sources?auteur=…&requete=…` sonde en direct, **depuis l'IP du
serveur**, chaque source (TikWM, moteurs direct/relais, Wayback, Urlebird) et
retourne pour chacune : statut (`ok` / `vide` / `bloque`), mode, nombre de liens
et un exemple. C'est l'outil pour vérifier un déploiement en une requête.

### Si la recherche ne donne rien

- Le TOP N ne ramène rien → **repli** sur les mots-clés réellement présents dans
  la légende (hashtags, thème visuel, mots fréquents).
- Toujours rien → échec avec un message qui **liste les recherches tentées** et
  leur résultat.
- Aucune requête constructible (ni auteur, ni nom, ni mot-clé) → message dédié
  qui explique quoi faire.

---

## 4. Voix off — deux voies, toutes deux gratuites

| | Importée | Générée (RsT) |
|---|---|---|
| Source | fichier de l'utilisateur | `edge-tts` (voix Microsoft Edge) |
| Coût | — | **gratuit, sans clé ni compte** |
| Limites | 25 Mo, 6 h, liée à la session | voix `EDGE_TTS_VOIX`, débit `EDGE_TTS_DEBIT` |
| Résultat | **incrustée** dans la vidéo | **fichier MP3 à part** |

**La livraison RsT est volontairement séparée en trois fichiers :**

- la **vidéo finale, muette** ;
- `script.txt` — accroche, corps, noms recherchés, vidéo de départ ;
- `voix-off.mp3` — le script lu par edge-tts.

Les trois se téléchargent depuis la carte du travail et expirent ensemble (6 h).

> **Speechma a été écarté : payant.** Ne pas y revenir.

Si `speech.platform.bing.com` est injoignable, la synthèse échoue **proprement** :
aucun MP3 factice, le travail se termine avec la vidéo et le script, et l'interface
affiche la raison exacte. La livraison réserve aussi 15 s pour Drive et la
finalisation : sous 5 s restantes, le MP3 est explicitement ignoré plutôt que de
faire expirer un rendu déjà terminé. Ces chemins d'échec sont testés.

---

## 5. Limites du sandbox — NE PAS REFAIRE CES ESSAIS

Ces points ont été vérifiés et re-vérifiés. Ils sont dus à l'environnement
de développement, **pas** à un bug du code :

| Élément | État dans le sandbox | Conséquence |
|---|---|---|
| **Réseau sortant** (`curl`, aiohttp) | **totalement bloqué** | aucun appel réel possible, même vers Google |
| **`speech.platform.bing.com`** (edge-tts) | injoignable | aucun MP3 réel généré |
| **`ffmpeg` / `ffprobe`** | **absents** | aucun rendu vidéo réel |
| **`*.onrender.com`** | bloqué pour `curl` | utiliser l'outil de récupération HTTP côté agent |

En revanche, l'**outil de récupération de page HTTP du côté agent** sort sur une
IP datacenter et a permis de vérifier en direct (2 octobre 2026) : TikWM `/api/`,
`/user/posts` et `/feed/search` répondent depuis une IP datacenter générique,
DuckDuckGo **lite** renvoie de vraies URLs vidéo TikTok pour
`site:tiktok.com … video` en direct, via `r.jina.ai` **et** via
`translate.goog` (vérifié les deux le 2 octobre 2026) ; Ecosia marche depuis
certaines IP mais bloque relais et datacenters ; Bing ignore `site:` ; le CDX
Wayback liste les vidéos archivées d'un auteur (mais `r.jina.ai` est lui-même
bloqué par archive.org) ; Urlebird passe avec un navigateur seulement.
**Diagnostic réel du 2 octobre 2026 sur Render** (`GET /api/rst/sources`) :
TikWM `/api/` ok, `/user/posts` et `/feed/search` 403, Ecosia/Bing vides,
Wayback timeout, Urlebird 403 — les relais DDG portent donc la découverte.
Tous les tests automatisés reposent sur des doublures (`monkeypatch`) — aucun
réseau n'est contacté dans les tests.

---

## 6. Tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -r requirements.txt
.venv/bin/python -m pytest -q                    # 97 tests
node --test tests/js/job-utils.test.cjs          # 7 tests
```

Les deux doivent être verts avant toute publication.

- `tests/test_api.py` — API, RsT TOP N, chaîne de découverte publique (parseurs
  moteurs/relais/archive, disjoncteurs, replis 403, diagnostic `/api/rst/sources`),
  recherche vide, livraison, voix off, Gemini 503, thème.
- `tests/test_montage.py` — liens, plans de 5 s, transitions, audio.
- `tests/js/job-utils.test.cjs` — persistance des travaux RsT côté navigateur.

Rappel utile : `pytest` a besoin d'un venv, le Python système est en PEP 668
(`externally-managed-environment`).

---

## 7. Déploiement

`render.yaml` + `Dockerfile` (qui installe `ffmpeg` et les polices). **Auto-deploy
activé sur `main`** : une fusion dans `main` suffit, il n'y a **rien** à créer ni à
configurer côté Render.

Healthcheck : `/api/sante`. Le panneau « Serveur » de l'interface lit les limites
réelles de l'instance — c'est le moyen le plus rapide de vérifier un déploiement.

Variables d'environnement : voir le tableau du `README.md`.

---

## 8. Leçon de méthode — commits non poussés

Une session précédente a terminé trois commits **sans jamais les pousser**. Son
sandbox a été détruit à la fermeture, et les commits avec : ils n'étaient ni sur
GitHub, ni récupérables par `git fsck` ou `reflog` dans un nouveau clone. Le
travail a dû être **entièrement refait**.

**Pousser la branche dès le premier commit.** Un commit local n'est pas une
sauvegarde ; seul `git push` en est une.
