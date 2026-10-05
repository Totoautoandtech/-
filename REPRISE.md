# REPRISE — état réel du projet

Récapitulatif destiné à la **prochaine session**. Il ne décrit que ce qui a été
réellement vérifié : ce qui tourne, ce qui n'a jamais pu être testé, et pourquoi.

Dernière mise à jour : 3 octobre 2026.

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
   - moteurs publics, dans l'ordre : **DuckDuckGo lite** (premier : son passage
     par le relais de traduction Google a été vérifié en production — vraies
     URLs vidéo, dont `@parishilton/video/7655569088227380511`), puis
     **SearXNG** (instances `opnxng.com` puis `search.inetol.net` — elles
     agrègent Google CSE/Bing/DuckDuckGo depuis leur propre serveur, donc
     c'est leur IP qui absorbe les blocages ; résultats du miroir
     `sticktock.com` normalisés en `tiktok.com`), puis **Ecosia**, **Bing** ;
     chacun **en direct puis via deux relais publics sans clé** — le relais
     de lecture `r.jina.ai`, puis le relais de traduction Google
     `translate.goog` (un « 202 Accepted » est réessayé une fois, un 202 déjà
     servi est accepté) — quand l'IP du serveur est bloquée ou que la page
     reste vide ;
   - requête **sans `site:`** pour DuckDuckGo et SearXNG (`tiktok.com @auteur
     video` / `tiktok.com <nom> video`) : « site:tiktok.com/@… » rend une page
     DDG vide quand elle est servie via le proxy de traduction Google, et les
     moteurs agrégés par SearXNG perdent l'opérateur ; Ecosia et Bing gardent
     « site:tiktok.com … » ;
   - un moteur **injoignable par tous les chemins** lève `ErreurApp` et passe
     au disjoncteur (`etat.bloquees`) : il n'est plus retenté pendant le
     travail — une page honnêtement vide, elle, ne bloque rien ;
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
   Avant la répartition, `_classer_candidats_rst()` annote chaque candidate d'un
   **score texte** (0..1) calculé sur des données réelles uniquement (titre TikWM
   + hashtags, nom recherché, origine de la découverte) : priorité aux titres
   contenant le nom recherché ou un mot du sujet (téléphone, smartphone, modèle,
   marque) ; pour un sujet téléphone, une vidéo dont le titre **ou** l'origine
   évoque voyage / Dubaï / skyline / building / lifestyle **sans aucun mot tech**
   est fortement pénalisée puis écartée. Le score est affiché dans la carte
   « Vidéos trouvées par RsT », l'origine réelle reste conservée.
7. **Sélection** : jusqu'à 20 sources dans les limites de durée et de temps Render.
   Une candidate sous `SEUIL_SCORE_TEXTE_RST` est rejetée avec le motif
   « titre hors sujet », même s'il reste de la place. Si tout est rejeté, le
   travail échoue en listant les vrais motifs de rejet.
8. **Montage** en **plans de 5 s maximum** (`RST_DUREE_MAX_PLAN = 5.0`). La vidéo
   de départ est passée en **référence de STYLE** (`lien_reference=lien`,
   `reference_optionnelle=True`) : `analyser_style_reference()` en extrait la
   grammaire visuelle (rythme, durée moyenne des plans, transitions, style de
   sous-titres) pour reproduire ce style de montage — **jamais le contenu** :
   images, logo et watermark de la référence ne sont pas réutilisés (voir
   `PROMPT_STYLE`). Référence inaccessible → repli honnête sur le style
   professionnel par défaut, avec avertissement.
9. **Pertinence visuelle stricte** : `exiger_pertinence_visuelle=True` dans la
   configuration RsT. `PROMPT_ANALYSE` interdit à Gemini de noter un plan pour sa
   seule esthétique (paysage / skyline / b-roll générique sans lien avec le
   script : 0.25 maximum) ; `selectionner_plan()` filtre ensuite chaque segment
   sous des seuils durs (hook 0.36, corps 0.45) et le repli temporel aveugle est
   interdit pour un segment contenant du texte.
10. **Livraison séparée** : voir §4.

### Règle RsT n°1 : échouer plutôt que livrer hors sujet

Le 3 octobre 2026, un rendu réel est parti en production avec un plan de
**skyline / Burj Khalifa à Dubaï** sous le sous-titre « Des performances
incroyables… » alors que la vidéo de départ était un **TOP 3 téléphones** de
`@actumobile.fr` — un b-roll générique « joli mais sans aucun rapport ».

Décision définitive : **RsT doit échouer proprement plutôt que de livrer un
montage hors sujet.** Concrètement, si aucun plan ne passe les seuils de
pertinence pour un segment, le rendu s'arrête avec le message :
`Aucune scène assez pertinente pour le segment « … ». RsT arrête le rendu plutôt
que de produire une vidéo hors sujet. Relance avec une vidéo de départ plus
précise ou colle manuellement de meilleures sources.` Dans ce cas : relever le
message exact + la carte « Vidéos trouvées par RsT » (durées, rejets), **ne rien
inventer**.

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
**Diagnostic réel du 2 octobre 2026 sur Render** (`GET /api/rst/sources`, 3 sondages) :
TikWM `/api/` ok, `/user/posts` et `/feed/search` 403 ; DDG direct timeout puis
202 ; `r.jina.ai` renvoie 403 à l'IP Render (OK depuis ailleurs) ; `translate.goog`
renvoie 202 puis 400 selon l'hôte ; Ecosia 403 partout (disjoncteur) ; Bing répond
vide honnêtement ; **Wayback OK** (5 liens `@parishilton` en 2,6 s) ; Urlebird 403.
**SearXNG : les deux instances répondent depuis Render en ~1 s** (pages de 6,7 et
9,1 Ko) mais 0 lien vidéo avec la requête `site:` — d'où le format sans `site:`.
**Découverte clé du 2 octobre** : DDG servi via `translate.goog` rend une page
VRAIMENT vide pour `site:tiktok.com/@parishilton video` mais PLEINE de résultats
pour `tiktok.com @parishilton video` (vidéo 7655569088227380511 confirmée, liens
enveloppés `translate.google.com/website?…u=…uddg=<percent>` décodés par le
parseur) — et ce relais répond 200 depuis Render.

**Sondage 5 (déployé, format sans `site:`)** : `duckduckgo` **« ok » en direct
depuis Render — 3 vrais liens en 0,7 s** (`@parishilton/video/7644249407940087070`
en exemple) ; `wayback` « ok » (5 liens). **La découverte publique fonctionne
depuis Render sans relais.** Sondage 6 (requête par nom « Galaxy A56 ») : DDG
direct 202 puis traduction vide — throttling DDG sur le diagnostic en rafale
(les sondes interrogent tous les moteurs en parallèle) ; le même requêtage via
translate depuis une IP datacenter rend des URLs vidéo réelles
(`@jallll9/video/7541748076139040056`…). Un travail RsT réel espace ses
recherches (revalidation TikWM 1 req/s, génération du script…), et la chaîne
retombe sur Wayback (auteurs) et les mots-clés si une recherche donne vide.
Tous les tests automatisés reposent sur des doublures (`monkeypatch`) — aucun
réseau n'est contacté dans les tests.

---

## 6. Tests

### Correctif SsT — secours public de recherche (5 octobre 2026)

Symptôme : « SsT n'a trouvé aucune source exploitable pour les noms saisis » alors
que les noms étaient valides. Cause : SsT n'utilisait que TikWM `/feed/search`,
qui répond 403 ou vide depuis certaines IP de serveur (Render) alors que
l'endpoint unitaire `/api/` fonctionne encore. RsT avait déjà une chaîne de
secours publique ; SsT ne l'utilisait pas.

Correctif (refait après la perte du commit local `d407a4f`, jamais poussé) :

- `_produire_sst()` enchaîne, **pour chaque nom saisi séparément** : TikWM
  `/feed/search` puis, s'il répond 403/vide/erreur, la même chaîne publique que
  RsT (`_decouvrir_publique` : moteurs publics, relais publics, archive Wayback,
  miroir Urlebird) avec un `EtatSourcesDecouverte` partagé (disjoncteurs) ;
- chaque lien public est **revalidé par TikWM `/api/`** (identifiant, auteur,
  titre, durée réels) à la cadence de 1 req/s avant de devenir une candidate ;
- les doublons TikTok sont écartés par identifiant vidéo ; la vidéo source SsT
  n'est jamais candidate (référence de style uniquement) ;
- après le premier 403, `/feed/search` n'est plus réessayé pour les noms
  suivants (même comportement que RsT) ;
- l'échec « aucune source » détaille désormais : noms recherchés, recherches
  tentées, erreurs TikWM, erreurs des sources publiques, raisons de rejet ;
- `_selectionner_sources_sst()` rejette explicitement « durée inconnue ou
  invalide » et marque les non-retenues `rejected_before_ai` ; aucune candidate
  n'est `selected=true` avant la validation visuelle Gemini ;
- les candidates refusées par l'entraînement IA portent leur `raison_refus`
  directement sur l'entrée `candidates_analysees` du profil ;
- l'interface Entraînement IA signale clairement un profil « seulement
  enregistré, pas encore entraîné » (0 source, 0 exemple, confiance 0 %), et la
  sélection d'un tel profil dans SsT affiche le message d'invitation.

Règle associée : **aucun nom saisi n'est jamais remplacé, complété ou cherché
« à la place »** — le secours public utilise exactement le nom demandé.

### Correctif RsT — pertinence visuelle stricte + référence de style (3 octobre 2026)

Un rendu réel (départ `@actumobile.fr`, TOP 3 téléphones) a produit un plan de
skyline / Burj Khalifa sous « Des performances incroyables… » : la découverte
publique ramenait de vraies vidéos… mais hors sujet, et le montage acceptait un
plan « beau » sans vérifier son lien avec le script.

Correctif à conserver :

- la vidéo de départ est passée en **référence de style** à
  `construire_montage_professionnel` (`lien_reference=lien`,
  `reference_optionnelle=True`) : le montage reprend la grammaire visuelle de la
  source (rythme, durée des plans, sous-titres, transitions), jamais son contenu ;
- `ConfigurationMontage.exiger_pertinence_visuelle` (activé seulement en RsT)
  pousse `selectionner_plan()` en mode strict : filtrage sous les seuils
  hook 0.36 / corps 0.45, refus du repli temporel aveugle pour un segment avec du
  texte, et `ErreurMontage` claire si aucune scène ne passe ;
- `PROMPT_ANALYSE` interdit explicitement de récompenser l'esthétique sans lien
  avec le script (paysage/skyline/b-roll générique : 0.25 max) ;
- `_classer_candidats_rst()` + `_score_texte_candidat_rst()` classent les
  candidates par pertinence texte et `_selectionner_sources_rst()` écarte les
  titres « voyage / ville / skyline » sans mot du sujet (seuil 0.10) —
  l'origine réelle reste dans `found_videos`, rien n'est inventé ;
- la durée de la vidéo de départ est comptée dans l'estimation Render
  (`_reduire_selon_estimation(..., duree_reference=…)`) puisqu'elle est
  téléchargée et analysée comme une source de plus.

Règle associée (voir §3) : **RsT échoue plutôt que de livrer un montage hors
sujet**, et la vidéo de départ sert de **référence de style, pas de contenu**.

### Correctif RsT — plafond d'analyses sous Render Free (2 octobre 2026)

Un travail réel a échoué pendant « Analyse 5/16 » : 16 sources avaient été
retenues alors qu'une analyse Gemini peut coûter environ 60 s sur Render quand
l'API sature (réessais 2/4/8/16 s sur 429/503). L'estimateur ne comptait que
10 s par analyse et la sélection RsT utilisait un plafond fixe trop optimiste.

Correctif à conserver :

- `studio_montage.estimer_duree_traitement()` compte désormais, par source,
  **30 s d'analyse Gemini + 8 s de transfert/latence** au lieu de 10 s ;
- `_produire_rst()` calcule un `plafond_reel` à partir du budget restant du job
  (`max(90, min(540, restant - LIVRAISON_RESERVE - 45))`) puis le transmet à
  `_reduire_selon_estimation()` avant le montage ;
- l'interface reçoit un détail explicite du type
  `Budget restant : N s — X source(s) retenue(s)` ; sur Render Free, X doit
  typiquement tomber autour de 9-10 sources plutôt que 16+ ;
- le test RsT de bout en bout vérifie que la sélection reste entre 4 et 12
  sources et que les candidates écartées portent la raison
  `retirée pour rester sous la limite de temps Render`.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt -r requirements.txt
.venv/bin/python -m pytest -q                    # 112 tests
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
