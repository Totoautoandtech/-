#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Générateur de vidéos TikTok à partir d'un lien (script + hook éditables + sous-titres incrustés)

Usage prévu (outil perso, sans paiement) :
  1. Coller un lien (vidéo TikTok, ou n'importe quelle page/article) -> POST /api/analyser
     -> l'IA propose une accroche (hook), un corps de script, et un thème visuel.
  2. Relire/modifier le hook et le corps dans l'interface.
  3. POST /api/video -> le serveur cherche du B-roll (Pexels, vertical, sans texte),
     l'assemble, incruste les sous-titres avec le style choisi, et renvoie le fichier final.

Démarrage local :
    pip install -r requirements.txt
    uvicorn app:app --host 0.0.0.0 --port 8000

Déploiement Render : utiliser le `render.yaml` fourni avec le `Dockerfile` (il installe
ffmpeg), ou installer ffmpeg dans l'image/runtime avant de lancer `uvicorn app:app
--host 0.0.0.0 --port $PORT`.

Variables d'environnement :
    GEMINI_API_KEYS   (obligatoire) une ou plusieurs clés, séparées par des virgules
    GEMINI_MODEL      (défaut: gemini-2.5-flash)
    PEXELS_API_KEY    (obligatoire) clé gratuite sur pexels.com/api
    DUREE_CIBLE_SECONDES (défaut: 30)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import logging
import os
import random
import re
import secrets
import shutil
import time
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote, quote_plus, unquote, urlencode, urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from studio_montage import (
    DUREE_MAX_PLAN_DEFAUT,
    ConfigurationMontage,
    ErreurMontage,
    PLAFOND_ESTIMATION_MONTAGE,
    Rapporteur,
    SECONDES_CONTROLE_QUALITE_PLAN,
    SEUIL_ESTIMATION_RISQUE,
    TOURS_MAX_CONTROLE_QUALITE,
    TravailAnnule,
    analyser_video,
    construire_montage_professionnel,
    creer_apercu,
    diagnostiquer_montage,
    estimer_duree_traitement,
    executer_commande,
    normaliser_lien_tiktok,
    normaliser_liens_tiktok,
    sonder_video,
)

# ======================================================================================
# LOGGING & CONFIG
# ======================================================================================

logging.basicConfig(format="%(asctime)s | %(levelname)-8s | %(message)s", level=logging.INFO)
logger = logging.getLogger("videoapp")


def _env(nom: str, defaut: str = "") -> str:
    return os.getenv(nom, defaut).strip()


# Chaîne de modèles Gemini essayée dans l'ordre. Le premier modèle est le plus capable ;
# les suivants servent de secours quand Google renvoie 503 « high demand » sur le premier.
MODELES_GEMINI_DEFAUT = "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash"

# Voix off importée par l'utilisateur (aucun service payant, aucune synthèse) :
# fichier audio brut envoyé par le navigateur, conservé 6 h, lié à la session.
VOIX_OFF_MAX_MO = 25
VOIX_OFF_MAX_OCTETS = VOIX_OFF_MAX_MO * 1024 * 1024
VOIX_OFF_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus"}
DUREE_VIE_VOIX_OFF = 6 * 60 * 60

# TikWM filtre les clients qui ne ressemblent pas à un navigateur : sans ces en-têtes,
# /feed/search et /user/posts répondent 403 depuis un hébergeur, alors que /api/ passe.
# Aucune clé, aucun compte, aucun abonnement : seulement une identification honnête.
ENTETES_TIKWM = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.tikwm.com/",
}

# Urlebird : miroir public utilisé en repli RsT. En production (Render), Cloudflare
# bloque souvent son IP de datacenter ; Urlebird n'est donc que la dernière source
# de la chaîne de découverte publique multi-sources définie plus bas.
ENTETES_URLEBIRD = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://urlebird.com/",
}

# Mode RsT : nombre maximum de liens TikTok traités par lancement (un job indépendant chacun).
RST_LIENS_PAR_LANCEMENT = 6

# Mode RsT « TOP N » : l'IA extrait de la vidéo de départ 3 ou 5 noms (personnes, lieux,
# objets, marques…). Chaque nom devient une recherche TikTok distincte, et le montage
# final enchaîne les meilleures sources trouvées pour chacun d'eux.
RST_NOMS_CHOIX = (3, 5)
RST_NOMS_DEFAUT = 3
# Aucun plan du montage RsT ne dépasse 5 secondes.
RST_DUREE_MAX_PLAN = 5.0
# Règle globale, appliquée aux trois modes et vérifiée après l'export FFprobe.
DUREE_MIN_VIDEO_SECONDES = 61.0

# Livraison séparée : la vidéo finale reste MUETTE. Le script est livré en .txt et la
# voix off est synthétisée en .mp3 par edge-tts — gratuit, sans clé d'API et sans compte.
# Speechma a été écarté : payant. Les deux fichiers se téléchargent à côté de la vidéo.
EDGE_TTS_VOIX = _env("EDGE_TTS_VOIX", "fr-FR-DeniseNeural")
EDGE_TTS_DEBIT = _env("EDGE_TTS_DEBIT", "+0%")
# edge-tts appelle speech.platform.bing.com : sur un réseau qui le bloque, la synthèse
# échoue proprement et le travail se termine quand même avec la vidéo et le script.
EDGE_TTS_DELAI = 45.0
# Sous ce budget restant, tenter edge-tts ne ferait que faire expirer le travail déjà rendu.
EDGE_TTS_MINIMUM = 5.0
# Une marge est conservée pour Drive et la finalisation du job après la livraison RsT.
LIVRAISON_RESERVE = 15.0
# Marge de sécurité : au-delà, la synthèse dépasserait le temps imparti sur Render Free.
VOIX_OFF_TEXTE_MAX = 4000


@dataclass
class Config:
    gemini_api_keys: list[str]
    gemini_model: str
    gemini_modeles: list[str]
    pexels_api_key: str
    duree_cible: int
    gmail_adresse: str
    gmail_mot_de_passe: str
    destinataire_email: str
    tiktok_client_key: str
    tiktok_client_secret: str
    google_client_id: str
    google_client_secret: str
    url_publique: str
    duree_max_source: int
    delai_job: int
    delai_ffmpeg: int
    delai_gemini: int
    delai_tikwm: int
    delai_telechargement: int
    analyses_concurrentes: int
    budget_disque_sources: int
    preset_export: str
    threads_ffmpeg: int
    autoriser_export_1080: bool
    rst_candidats_max: int
    rst_sources_max: int

    @classmethod
    def charger(cls) -> "Config":
        """Charge la configuration sans empêcher le serveur de démarrer.

        Les clés externes sont vérifiées au moment où l'endpoint concerné est appelé.
        Cela permet notamment à Render, aux tests et à la page d'accueil de démarrer
        avec une réponse d'erreur explicite plutôt qu'un crash à l'import du module.
        """
        cles = [c.strip() for c in _env("GEMINI_API_KEYS").split(",") if c.strip()]
        pexels = _env("PEXELS_API_KEY")

        # Chaîne de modèles de secours : si le premier renvoie 503 (surcharge temporaire),
        # l'appel bascule automatiquement sur le suivant avant de changer de clé.
        modeles = [m.strip() for m in _env("GEMINI_MODELES", MODELES_GEMINI_DEFAUT).split(",") if m.strip()]
        if not modeles:
            modeles = [m.strip() for m in MODELES_GEMINI_DEFAUT.split(",") if m.strip()]
        # GEMINI_MODEL explicitement défini garde la priorité en tête de chaîne.
        modele_principal = _env("GEMINI_MODEL")
        if modele_principal and modele_principal not in modeles:
            modeles.insert(0, modele_principal)

        try:
            duree_cible = int(_env("DUREE_CIBLE_SECONDES", "30"))
            if duree_cible <= 0:
                raise ValueError
        except ValueError:
            logger.warning("DUREE_CIBLE_SECONDES invalide : 30 secondes utilisées.")
            duree_cible = 30

        def entier(nom: str, defaut: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(_env(nom, str(defaut)))))
            except ValueError:
                logger.warning("%s invalide : %s utilisé.", nom, defaut)
                return defaut

        if not cles:
            logger.warning("GEMINI_API_KEYS absent : les endpoints IA seront indisponibles.")
        if not pexels:
            logger.warning("PEXELS_API_KEY absent : la génération B-roll sera indisponible.")

        preset = _env("FFMPEG_PRESET", "ultrafast").lower()
        if preset not in {"ultrafast", "superfast", "veryfast"}:
            logger.warning("FFMPEG_PRESET invalide : ultrafast utilisé.")
            preset = "ultrafast"

        return cls(
            gemini_api_keys=cles,
            gemini_model=modele_principal or modeles[0],
            gemini_modeles=modeles,
            pexels_api_key=pexels,
            duree_cible=duree_cible,
            gmail_adresse=_env("GMAIL_ADRESSE"),
            gmail_mot_de_passe=_env("GMAIL_MOT_DE_PASSE_APP"),
            destinataire_email=_env("DESTINATAIRE_EMAIL", "tomheude8@gmail.com"),
            tiktok_client_key=_env("TIKTOK_CLIENT_KEY"),
            tiktok_client_secret=_env("TIKTOK_CLIENT_SECRET"),
            google_client_id=_env("GOOGLE_CLIENT_ID"),
            google_client_secret=_env("GOOGLE_CLIENT_SECRET"),
            url_publique=_env("APP_BASE_URL") or _env("RENDER_EXTERNAL_URL"),
            duree_max_source=entier("DUREE_MAX_SOURCE_SECONDES", 180, 15, 600),
            delai_job=entier("JOB_TIMEOUT_SECONDES", 1800, 60, 3600),
            delai_ffmpeg=entier("FFMPEG_TIMEOUT_SECONDES", 240, 30, 540),
            delai_gemini=entier("GEMINI_TIMEOUT_SECONDES", 120, 15, 240),
            delai_tikwm=entier("TIKWM_TIMEOUT_SECONDES", 30, 5, 90),
            delai_telechargement=entier("DOWNLOAD_TIMEOUT_SECONDES", 120, 15, 300),
            analyses_concurrentes=entier("GEMINI_ANALYSES_CONCURRENTES", 1, 1, 2),
            budget_disque_sources=entier("BUDGET_DISQUE_SOURCES_MO", 700, 100, 1500) * 1024 * 1024,
            preset_export=preset,
            threads_ffmpeg=entier("FFMPEG_THREADS", 1, 1, 2),
            autoriser_export_1080=_env("AUTORISER_EXPORT_1080", "false").lower() in {"1", "true", "oui"},
            rst_candidats_max=entier("RST_CANDIDATS_MAX", 40, 10, 60),
            rst_sources_max=entier("RST_SOURCES_MAX", 20, 5, 20),
        )


CONFIG = Config.charger()

# Tous les chemins sont ancrés sur le dossier du dépôt, pas sur le répertoire depuis
# lequel uvicorn a été lancé. C'est important pour les déploiements et les tests.
RACINE = Path(__file__).resolve().parent
DOSSIER_VIDEOS = RACINE / "videos"
DOSSIER_VIDEOS.mkdir(exist_ok=True)
DOSSIER_TRAVAIL = RACINE / "travail"
DOSSIER_TRAVAIL.mkdir(exist_ok=True)
DOSSIER_VOIX_OFF = DOSSIER_TRAVAIL / "voixoff"
DOSSIER_VOIX_OFF.mkdir(parents=True, exist_ok=True)
# Profils d'entraînement privés : un seul JSON 0600 par cookie de session.
# Le nom du sujet n'est jamais utilisé pour choisir un fichier ou un chemin.
DOSSIER_PROFILS_ENTRAINEMENT = DOSSIER_TRAVAIL / "profils-entrainement"
DOSSIER_PROFILS_ENTRAINEMENT.mkdir(parents=True, exist_ok=True)


def _dossier_voix_off(session_id: str) -> Path:
    """Un sous-dossier par session : une voix off n'est jamais visible d'une autre session."""
    empreinte = hashlib.sha256(str(session_id or "anonyme").encode("utf-8")).hexdigest()[:32]
    return DOSSIER_VOIX_OFF / empreinte


def _chemin_voix_off(session_id: str, identifiant: str) -> Optional[Path]:
    """Retourne le fichier de voix off de cette session, ou None s'il n'existe plus."""
    valeur = str(identifiant or "").strip()
    if not valeur or not re.fullmatch(r"[a-f0-9]{32}(\.[a-z0-9]{1,5})?", valeur):
        return None
    dossier = _dossier_voix_off(session_id)
    if not dossier.is_dir():
        return None
    base = valeur.split(".", 1)[0]
    for fichier in dossier.glob(f"{base}.*"):
        if fichier.is_file() and fichier.suffix.lower() in VOIX_OFF_EXTENSIONS:
            return fichier
    return None


def _purger_voix_off() -> None:
    """Supprime les voix off de plus de 6 h, comme les rendus et les travaux."""
    seuil = time.time() - DUREE_VIE_VOIX_OFF
    if not DOSSIER_VOIX_OFF.is_dir():
        return
    for dossier in DOSSIER_VOIX_OFF.iterdir():
        try:
            if not dossier.is_dir():
                continue
            for fichier in dossier.iterdir():
                if fichier.is_file() and fichier.stat().st_mtime < seuil:
                    fichier.unlink(missing_ok=True)
            if not any(dossier.iterdir()):
                dossier.rmdir()
        except OSError:
            logger.warning("Nettoyage voix off impossible pour %s", dossier)

TAILLE_MAX_TELECHARGEMENT = 100 * 1024 * 1024  # évite de remplir le disque avec un lien distant
TAILLE_MAX_PAGE_SOURCE = 2 * 1024 * 1024
DUREE_MAX_APERCU_IA = CONFIG.duree_max_source  # compatibilité des anciens helpers


def _configuration_montage(
    mode: str = "rapide",
    duree_max_plan: float = DUREE_MAX_PLAN_DEFAUT,
    exiger_pertinence_visuelle: bool = False,
) -> ConfigurationMontage:
    """Configuration du pipeline ; le mode « qualite » privilégie un encodage plus fin.

    `exiger_pertinence_visuelle` active le mode strict RsT : aucun plan n'est monté
    s'il n'est pas réellement pertinent pour le segment de script qu'il illustre.
    """
    return ConfigurationMontage(
        dossier_travail=DOSSIER_TRAVAIL,
        dossier_videos=DOSSIER_VIDEOS,
        duree_max_source=float(CONFIG.duree_max_source),
        delai_ffmpeg=float(CONFIG.delai_ffmpeg),
        delai_gemini=float(CONFIG.delai_gemini),
        delai_job=float(CONFIG.delai_job),
        budget_disque_sources=CONFIG.budget_disque_sources,
        analyses_concurrentes=CONFIG.analyses_concurrentes,
        preset=CONFIG.preset_export,
        # Le mode Qualité respecte CRF 21 ; le mode Rapide garde CRF 23 pour
        # terminer plus vite sur Render. Les exports de la configuration par défaut
        # restent donc en qualité, sans promettre du 1080 si l'instance ne l'autorise.
        crf=21 if mode == "qualite" else 23,
        threads_ffmpeg=CONFIG.threads_ffmpeg,
        autoriser_1080=CONFIG.autoriser_export_1080,
        duree_max_plan=float(duree_max_plan),
        exiger_validation_ia=True,
        # Tous les modes exigent désormais une correspondance directe entre le texte
        # et l'objet exact visible (ex. un modèle exact, jamais un produit générique).
        exiger_pertinence_visuelle=True,
        duree_min_video=DUREE_MIN_VIDEO_SECONDES,
    )


FONT_CANDIDATS = [
    RACINE / "static/fonts/Sous-titres.ttf",  # ajoutez votre propre police ici pour un rendu garanti
    Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
]


def _police() -> str:
    for chemin in FONT_CANDIDATS:
        if chemin.exists():
            return str(chemin)
    raise RuntimeError(
        "Aucune police trouvée. Ajoutez un fichier .ttf dans static/fonts/Sous-titres.ttf "
        "(ex. une police Google Fonts téléchargée), ou installez fonts-dejavu sur le serveur."
    )


def _verifier_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise ErreurApp(
            "ffmpeg est absent du serveur. Installe-le localement ou déploie l'application "
            "avec le Dockerfile fourni."
        )


async def _sonder_audio(chemin: Path) -> float:
    """Vérifie avec FFprobe que le fichier importé contient bien une piste audio lisible."""
    commande = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type,codec_name,duration:format=duration",
        "-of", "json", str(chemin),
    ]
    try:
        stdout, _ = await executer_commande(
            commande, etape="analyse de la voix off", timeout=30.0
        )
    except ErreurMontage as exc:
        raise ErreurApp(f"Fichier audio illisible : {exc}") from exc
    try:
        donnees = json.loads(stdout.decode("utf-8", errors="ignore") or "{}")
    except json.JSONDecodeError as exc:
        raise ErreurApp("Fichier audio illisible : FFprobe n'a renvoyé aucune information.") from exc
    flux = donnees.get("streams") or []
    if not flux or str(flux[0].get("codec_type")) != "audio":
        raise ErreurApp("Ce fichier ne contient aucune piste audio exploitable.")
    duree = flux[0].get("duration") or (donnees.get("format") or {}).get("duration") or 0
    try:
        return max(0.0, float(duree))
    except (TypeError, ValueError):
        return 0.0


# ======================================================================================
# ENVOI PAR E-MAIL (script + vidéo, vers un compte Gmail)
# ======================================================================================


def _envoyer_email_sync(sujet: str, corps: str, piece_jointe: Optional[Path] = None) -> None:
    """Bloquant (smtplib) : exécuté dans un thread via asyncio.to_thread."""
    import smtplib
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = sujet
    msg["From"] = CONFIG.gmail_adresse
    msg["To"] = CONFIG.destinataire_email
    msg.set_content(corps)

    LIMITE_PIECE_JOINTE = 18 * 1024 * 1024  # marge sous la limite Gmail de 25 Mo (encodage inclus)
    if piece_jointe and piece_jointe.exists() and piece_jointe.stat().st_size <= LIMITE_PIECE_JOINTE:
        msg.add_attachment(
            piece_jointe.read_bytes(), maintype="video", subtype="mp4", filename=piece_jointe.name
        )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as serveur:
        serveur.login(CONFIG.gmail_adresse, CONFIG.gmail_mot_de_passe)
        serveur.send_message(msg)


async def envoyer_script_et_video(hook: str, corps_script: str, video_path: Path) -> None:
    """
    Envoie le script et la vidéo par e-mail au compte configuré (DESTINATAIRE_EMAIL).
    Ne fait rien (juste un log) si GMAIL_ADRESSE / GMAIL_MOT_DE_PASSE_APP ne sont pas
    configurés — l'envoi e-mail est une option, pas une obligation pour que l'appli tourne.
    """
    if not (CONFIG.gmail_adresse and CONFIG.gmail_mot_de_passe):
        logger.info("Envoi e-mail désactivé (GMAIL_ADRESSE / GMAIL_MOT_DE_PASSE_APP non configurés).")
        return

    base_url = _env("RENDER_EXTERNAL_URL")
    lien_video = f"{base_url}/videos/{video_path.name}" if base_url else f"/videos/{video_path.name}"
    corps_email = (
        f"Hook :\n{hook}\n\n"
        f"Script :\n{corps_script}\n\n"
        f"Vidéo : {lien_video}\n"
        f"(pièce jointe incluse si le fichier fait moins de 18 Mo)"
    )
    try:
        await asyncio.to_thread(
            _envoyer_email_sync, f"Nouvelle vidéo — {hook[:60]}", corps_email, video_path
        )
        logger.info("E-mail envoyé à %s.", CONFIG.destinataire_email)
    except Exception as exc:  # noqa: BLE001
        logger.error("Envoi e-mail échoué : %s", exc)


STYLES_SOUS_TITRES = {
    "classique": {"couleur": "white", "contour": "black", "position": "bas"},
    "jaune": {"couleur": "yellow", "contour": "black", "position": "bas"},
    "centre": {"couleur": "white", "contour": "black", "position": "centre"},
}


class ErreurApp(Exception):
    """Erreur métier, affichable telle quelle à l'utilisateur."""


class ErreurTikwm403(ErreurApp):
    """Erreur 403 renvoyée par TikWM signalant le blocage de la plage IP du serveur."""


async def _avec_retry(fabrique, *, tentatives: int = 3, etape: str = ""):
    derniere: Optional[Exception] = None
    for essai in range(1, tentatives + 1):
        try:
            return await fabrique()
        except ErreurTikwm403:
            # 403 = IP bloquée, réessayer depuis la même IP est inutile
            raise
        except Exception as exc:  # noqa: BLE001
            derniere = exc
            logger.warning("[%s] tentative %s/%s : %s", etape, essai, tentatives, exc)
            if essai < tentatives:
                await asyncio.sleep(1.2 * essai)
    raise ErreurApp(f"Échec de l'étape « {etape} » : {derniere}") from derniere


# ======================================================================================
# EXTRACTION DU CONTENU SOURCE (lien TikTok ou lien générique)
# ======================================================================================

_RE_PAGE_TIKTOK = re.compile(r"tiktok\.com/(@[\w.\-]+/video/\d+|v/\d+|t/\w+)", re.IGNORECASE)


def _valider_url(url: str, nom: str = "Lien") -> str:
    """Valide les URL reçues avant de les transmettre à aiohttp."""
    valeur = url.strip()
    parsee = urlparse(valeur)
    if parsee.scheme not in {"http", "https"} or not parsee.netloc:
        raise ErreurApp(f"{nom} invalide : utilise une URL commençant par http:// ou https://.")
    return valeur


def _est_url_tiktok(url: str) -> bool:
    hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    return hostname == "tiktok.com" or hostname.endswith(".tiktok.com")


async def _telecharger_fichier(session: aiohttp.ClientSession, url: str, destination: Path) -> None:
    url = _valider_url(url, "URL de téléchargement")

    async def _appel():
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    raise ErreurApp(f"Téléchargement échoué ({resp.status})")

                taille_annoncee = resp.headers.get("Content-Length")
                if taille_annoncee:
                    try:
                        if int(taille_annoncee) > TAILLE_MAX_TELECHARGEMENT:
                            raise ErreurApp("Le fichier distant est trop volumineux (100 Mo maximum).")
                    except ValueError:
                        logger.warning("Content-Length invalide reçu pour %s.", url)

                total = 0
                async with aiofiles.open(destination, "wb") as f:
                    async for bloc in resp.content.iter_chunked(256 * 1024):
                        total += len(bloc)
                        if total > TAILLE_MAX_TELECHARGEMENT:
                            raise ErreurApp("Le fichier distant dépasse 100 Mo.")
                        await f.write(bloc)
        except Exception:
            # Ne jamais laisser un fichier partiel être réutilisé après un retry.
            destination.unlink(missing_ok=True)
            raise

    try:
        await asyncio.wait_for(
            _avec_retry(_appel, etape=f"téléchargement {destination.name}"),
            timeout=CONFIG.delai_telechargement,
        )
    except asyncio.TimeoutError as exc:
        destination.unlink(missing_ok=True)
        raise ErreurApp(
            f"Téléchargement de {destination.name} interrompu après "
            f"{CONFIG.delai_telechargement} secondes."
        ) from exc


async def _resoudre_video_tiktok(session: aiohttp.ClientSession, url: str) -> str:
    """Résout un lien TikTok nettoyé vers son fichier direct, avec timeout borné."""
    try:
        url = normaliser_lien_tiktok(url)
    except ErreurMontage as exc:
        raise ErreurApp(str(exc)) from exc

    async def _appel():
        try:
            async with asyncio.timeout(CONFIG.delai_tikwm):
                async with session.get("https://www.tikwm.com/api/", params={"url": url, "hd": "1"}, headers=ENTETES_TIKWM) as resp:
                    if resp.status != 200:
                        raise ErreurApp(f"TikWM inaccessible ({resp.status})")
                    try:
                        donnees = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                        raise ErreurApp("TikWM a renvoyé une réponse invalide.") from exc
        except asyncio.TimeoutError as exc:
            raise ErreurApp(f"TikWM n’a pas répondu sous {CONFIG.delai_tikwm} secondes.") from exc
        if not isinstance(donnees, dict) or donnees.get("code") != 0 or "data" not in donnees:
            message = donnees.get("msg", "réponse invalide") if isinstance(donnees, dict) else "réponse invalide"
            raise ErreurApp(f"TikWM : {message}")
        return donnees["data"]

    donnees = await _avec_retry(_appel, tentatives=2, etape="résolution TikTok")
    lien = donnees.get("play") or donnees.get("wmplay")
    if not lien:
        raise ErreurApp("Impossible de résoudre cette vidéo TikTok.")
    return lien


async def _telecharger_video_tiktok(session: aiohttp.ClientSession, url: str, destination: Path) -> Path:
    lien_direct = await _resoudre_video_tiktok(session, url)
    await _telecharger_fichier(session, lien_direct, destination)
    return destination


async def _extraire_texte_tiktok(session: aiohttp.ClientSession, url: str) -> str:
    """
    Récupère la légende écrite par le créateur (aucune IA ici : pas de téléchargement,
    pas de transcription — la légende suffit comme matière première pour le script).
    """
    url = _valider_url(url, "Lien TikTok")

    async def _appel():
        async with session.get("https://www.tikwm.com/api/", params={"url": url, "hd": "1"}, headers=ENTETES_TIKWM) as resp:
            if resp.status != 200:
                raise ErreurApp(f"TikWM inaccessible ({resp.status})")
            try:
                donnees = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                raise ErreurApp("TikWM a renvoyé une réponse invalide.") from exc
        if not isinstance(donnees, dict) or donnees.get("code") != 0 or "data" not in donnees:
            message = donnees.get("msg", "réponse invalide") if isinstance(donnees, dict) else "réponse invalide"
            raise ErreurApp(f"TikWM : {message}")
        return donnees["data"]

    donnees = await _avec_retry(_appel, tentatives=2, etape="récupération TikTok")
    titre = (donnees.get("title") or "").strip()
    if not titre:
        raise ErreurApp("Cette vidéo TikTok n'a pas de légende exploitable comme source.")
    return titre


async def _extraire_texte_page(session: aiohttp.ClientSession, url: str) -> str:
    """Récupère le titre + le texte principal d'une page web quelconque."""
    url = _valider_url(url)

    async def _appel():
        async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
            if resp.status != 200:
                raise ErreurApp(f"Page inaccessible ({resp.status})")
            morceaux: list[bytes] = []
            total = 0
            async for bloc in resp.content.iter_chunked(64 * 1024):
                restant = TAILLE_MAX_PAGE_SOURCE - total
                if restant <= 0:
                    break
                morceaux.append(bloc[:restant])
                total += min(len(bloc), restant)
            return b"".join(morceaux).decode(resp.charset or "utf-8", errors="replace")

    html = await _avec_retry(_appel, tentatives=2, etape="récupération de la page")
    soup = await asyncio.to_thread(BeautifulSoup, html, "html.parser")
    for balise in soup(["script", "style", "nav", "footer", "header"]):
        balise.decompose()
    titre = soup.title.string.strip() if soup.title and soup.title.string else ""
    corps = " ".join(soup.get_text(separator=" ").split())
    texte = f"{titre}. {corps}" if titre else corps
    if not texte.strip():
        raise ErreurApp("Aucun texte exploitable trouvé sur cette page.")
    return texte[:6000]  # on ne garde pas une page entière, l'IA n'a pas besoin de plus


async def extraire_source(url: str) -> str:
    url = _valider_url(url)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        if _RE_PAGE_TIKTOK.search(url) or _est_url_tiktok(url):
            return await _extraire_texte_tiktok(session, url)
        return await _extraire_texte_page(session, url)


# ======================================================================================
# GEMINI (rotation multi-clés)
# ======================================================================================

# Même si plusieurs jobs sont en file, jamais plus de deux requêtes Gemini lourdes.
GEMINI_SEMAPHORE = asyncio.Semaphore(2)

PROMPT_SCRIPT = (
    "Tu es un scénariste spécialisé dans les vidéos courtes virales (TikTok/Reels/Shorts). "
    "On te donne un contenu source (transcription ou article). Réponds UNIQUEMENT avec un JSON "
    "valide, sans texte avant/après, sans balises markdown, avec exactement trois clés :\n"
    '- "hook" : une phrase d\'accroche courte et percutante (moins de 12 mots), la toute première '
    "chose dite dans la vidéo, pour capter l'attention en 3 secondes.\n"
    '- "corps" : la suite du script, 130 à 160 mots et au moins 13 phrases courtes, afin de '
    "tenir naturellement au moins 1 min 1 s, dans la même langue que le contenu source.\n"
    '- "mot_cle_broll" : un thème visuel court (2 à 5 mots), l\'objet ou le concept principal à '
    "illustrer en vidéo, SANS aucune mention de texte à l'écran.\n"
    "Réponds strictement avec ce JSON."
)


# Statuts HTTP qui méritent une nouvelle tentative : surcharge, quota court terme, incident.
GEMINI_STATUTS_REESSAYABLES = {429, 500, 502, 503, 504}
# Statuts qui signalent explicitement une saturation temporaire du modèle.
GEMINI_STATUTS_SATURATION = {429, 503}
GEMINI_TENTATIVES_PAR_MODELE = 5
GEMINI_BACKOFF = (2, 4, 8, 16)
GEMINI_MESSAGE_SATURE = "Gemini est momentanément saturé (503). Réessaie dans quelques minutes."


class ErreurGemini(ErreurApp):
    """Erreur d'appel Gemini enrichie du statut HTTP et du caractère « saturation »."""

    def __init__(self, message: str, statut: int = 0, saturation: bool = False) -> None:
        super().__init__(message)
        self.statut = statut
        self.saturation = saturation


def _est_saturation_gemini(statut: int, texte: str) -> bool:
    """503/429 explicites, ou message UNAVAILABLE / OVERLOADED / RESOURCE_EXHAUSTED."""
    if statut in GEMINI_STATUTS_SATURATION:
        return True
    haut = (texte or "").upper()
    return any(
        marqueur in haut
        for marqueur in ("UNAVAILABLE", "OVERLOADED", "RESOURCE_EXHAUSTED", "HIGH DEMAND")
    )


def _modeles_gemini() -> list[str]:
    return list(CONFIG.gemini_modeles) or [CONFIG.gemini_model]


async def _appel_gemini_brut(
    parts: list[dict], *, temperature: float, system: Optional[str] = None, json_mode: bool = False
) -> str:
    """Appelle Gemini avec repli automatique de modèle puis de clé.

    Ordre d'essai : pour chaque clé (tirée au hasard), chaque modèle de la chaîne
    `GEMINI_MODELES`, avec jusqu'à 5 tentatives par modèle et un backoff exponentiel
    (2, 4, 8, 16 s) sur 503 / 429 / 500, toujours dans la limite du timeout Gemini.
    """
    cles = CONFIG.gemini_api_keys
    if not cles:
        raise ErreurApp("GEMINI_API_KEYS n'est pas configuré sur le serveur.")

    modeles = _modeles_gemini()
    payload: dict[str, Any] = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": temperature},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    debut = time.monotonic()

    def restant() -> float:
        return CONFIG.delai_gemini - (time.monotonic() - debut)

    derniere: Optional[Exception] = None
    saturation_vue = False

    async with GEMINI_SEMAPHORE:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=CONFIG.delai_gemini, connect=15)
        ) as session:
            for cle in random.sample(cles, len(cles)):
                headers = {"x-goog-api-key": cle, "Content-Type": "application/json"}
                for modele in modeles:
                    url = (
                        "https://generativelanguage.googleapis.com/v1beta/models/"
                        f"{modele}:generateContent"
                    )
                    for tentative in range(1, GEMINI_TENTATIVES_PAR_MODELE + 1):
                        if restant() <= 1.0:
                            break
                        try:
                            async with session.post(url, headers=headers, json=payload) as resp:
                                texte = await resp.text()
                                if resp.status == 200:
                                    data = json.loads(texte)
                                    return data["candidates"][0]["content"]["parts"][0]["text"]
                                raise ErreurGemini(
                                    f"Gemini a répondu {resp.status} : {texte[:200]}",
                                    statut=resp.status,
                                    saturation=_est_saturation_gemini(resp.status, texte),
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            derniere = exc
                            statut = int(getattr(exc, "statut", 0) or 0)
                            sature = bool(getattr(exc, "saturation", False))
                            saturation_vue = saturation_vue or sature
                            logger.warning(
                                "[Gemini] %s (clé %s…) tentative %s/%s : %s",
                                modele, cle[:6], tentative, GEMINI_TENTATIVES_PAR_MODELE, exc,
                            )
                            reessayable = statut == 0 or statut in GEMINI_STATUTS_REESSAYABLES
                            if not reessayable or tentative >= GEMINI_TENTATIVES_PAR_MODELE:
                                break
                            pause = GEMINI_BACKOFF[min(tentative - 1, len(GEMINI_BACKOFF) - 1)]
                            if restant() <= pause + 1.0:
                                break
                            await asyncio.sleep(pause)
                    logger.warning("[Gemini] modèle %s indisponible, modèle suivant.", modele)
                logger.warning("[Gemini] clé %s… épuisée, clé suivante.", cle[:6])

    if saturation_vue:
        raise ErreurApp(GEMINI_MESSAGE_SATURE)
    raise ErreurApp(f"Toutes les clés Gemini ont échoué : {derniere}")


def _parser_json(brut: str) -> Any:
    nettoye = brut.strip()
    if nettoye.startswith("```"):
        nettoye = nettoye.strip("`")
        if "\n" in nettoye:
            nettoye = nettoye.split("\n", 1)[-1]
        if nettoye.lower().startswith("json"):
            nettoye = nettoye[4:]
    try:
        return json.loads(nettoye)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ErreurApp(f"Réponse IA non exploitable : {brut[:200]}") from exc


async def generer_script(source_texte: str) -> dict[str, str]:
    brut = await _appel_gemini_brut(
        [{"text": source_texte}], temperature=0.8, system=PROMPT_SCRIPT, json_mode=True
    )
    resultat = _parser_json(brut)
    if not isinstance(resultat, dict):
        raise ErreurApp("Réponse IA non exploitable : un objet JSON était attendu.")
    for cle in ("hook", "corps", "mot_cle_broll"):
        valeur = resultat.get(cle)
        if not isinstance(valeur, str) or not valeur.strip():
            raise ErreurApp(f"Réponse IA incomplète : « {cle} » manquant.")
        resultat[cle] = valeur.strip()
    return resultat


PROMPT_NOMS_RST = (
    "Tu analyses la légende d'une vidéo TikTok. Ton rôle est d'en extraire les NOMS "
    "réellement évoqués : personnes, personnages, lieux, marques, équipes, œuvres, objets "
    "ou concepts précis. Réponds UNIQUEMENT avec un JSON valide, sans texte avant/après, "
    "sans balises markdown, avec exactement une clé :\n"
    '- "noms" : un tableau de {nombre} chaînes maximum, classées de la plus importante à la '
    "moins importante. Chaque nom fait 2 à 40 caractères et doit pouvoir servir tel quel de "
    "requête de recherche sur TikTok.\n"
    "N'invente jamais un nom qui ne serait pas soutenu par la légende : s'il y en a moins de "
    "{nombre}, renvoie seulement ceux qui existent vraiment. Réponds strictement avec ce JSON."
)


def _nettoyer_nom_rst(valeur: Any) -> str:
    """Normalise un nom proposé par l'IA en requête de recherche réellement utilisable."""
    texte = str(valeur or "").strip().strip("\"'`").lstrip("#@").strip()
    texte = re.sub(r"\s+", " ", texte)
    if len(texte) < 2 or len(texte) > 40:
        return ""
    # Un « nom » entièrement composé de ponctuation ou de chiffres n'est pas cherchable.
    if not re.search(r"[\wÀ-ÿ]", texte):
        return ""
    return texte


def _deduplique_noms(noms: Iterable[str], limite: int) -> list[str]:
    """Garde l'ordre d'importance, sans doublon insensible à la casse, jusqu'à `limite`."""
    retenus: list[str] = []
    vus: set[str] = set()
    for brut in noms:
        propre = _nettoyer_nom_rst(brut)
        if not propre:
            continue
        cle = propre.casefold()
        if cle in vus:
            continue
        vus.add(cle)
        retenus.append(propre)
        if len(retenus) >= limite:
            break
    return retenus


def borner_nombre_noms_rst(valeur: Any) -> int:
    """Le pipeline TOP N n'accepte que 3 ou 5 noms ; tout le reste retombe sur 3."""
    try:
        demande = int(valeur)
    except (TypeError, ValueError):
        return RST_NOMS_DEFAUT
    return demande if demande in RST_NOMS_CHOIX else RST_NOMS_DEFAUT


async def extraire_noms_rst(
    legende: str, nombre: int = RST_NOMS_DEFAUT, mot_cle_broll: str = ""
) -> list[str]:
    """Extrait de la légende les `nombre` noms les plus importants (TOP N).

    L'IA est interrogée en premier car elle reconnaît les noms propres ; si elle échoue
    ou renvoie une liste vide, on retombe sur les mots-clés réellement présents dans la
    légende. Aucun nom n'est inventé : la liste peut être plus courte que demandée.
    """
    limite = borner_nombre_noms_rst(nombre)
    noms: list[str] = []
    try:
        brut = await _appel_gemini_brut(
            [{"text": legende}],
            temperature=0.2,
            system=PROMPT_NOMS_RST.format(nombre=limite),
            json_mode=True,
        )
        resultat = _parser_json(brut)
        if isinstance(resultat, dict):
            noms = _deduplique_noms(resultat.get("noms") or [], limite)
    except (ErreurApp, ErreurMontage) as exc:
        logger.warning("[RsT] extraction des noms par l'IA indisponible : %s", exc)

    if len(noms) < limite:
        # Repli honnête : hashtags, thème visuel puis mots fréquents de la vraie légende.
        complement = _extraire_mots_cles_rst(legende, mot_cle_broll, limite=limite * 2)
        noms = _deduplique_noms([*noms, *complement], limite)
    return noms


# ======================================================================================
# MODE "VIDÉO DE RÉFÉRENCE" : reprendre le script d'une vidéo existante, hook préservé
# ======================================================================================

PROMPT_REFERENCE = (
    "Voici la transcription d'une vidéo virale. Réponds UNIQUEMENT avec un JSON valide, sans "
    "texte avant/après, sans balises markdown, avec exactement trois clés :\n"
    '- "hook" : recopie MOT POUR MOT la toute première phrase de la transcription (l\'accroche '
    "d'origine). Ne la traduis pas, ne la modifie pas, ne la reformule pas.\n"
    '- "corps" : réécris et traduis en français le reste de la transcription en 130 à 160 mots '
    "et au moins 13 phrases courtes, pour tenir au moins 1 min 1 s, sans changer le sens ni inventer d’informations.\n"
    '- "mot_cle_broll" : un thème visuel court (2 à 5 mots), sans mention de texte à l\'écran.\n'
    "Réponds strictement avec ce JSON."
)


async def _transcrire_video(video_path: Path) -> str:
    """Extrait un audio compact (ffmpeg) et le transcrit via Gemini."""
    audio_path = video_path.with_suffix(".mp3")
    # Un audio mono 16 kHz suffit pour la transcription et évite de charger plusieurs
    # dizaines de Mo en mémoire avant l'encodage base64.
    commande = [
        "ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
        "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path),
    ]
    await executer_commande(
        commande, etape="extraction audio", timeout=CONFIG.delai_ffmpeg,
        sortie_attendue=audio_path,
    )

    if not audio_path.exists() or audio_path.stat().st_size > 12 * 1024 * 1024:
        raise ErreurApp("Audio trop volumineux pour la transcription.")
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    return await _appel_gemini_brut(
        [
            {"inline_data": {"mime_type": "audio/mpeg", "data": audio_b64}},
            {"text": "Transcris cet audio mot pour mot, dans sa langue d'origine. "
                      "Réponds uniquement avec le texte transcrit, sans aucun commentaire."},
        ],
        temperature=0.0,
    )


async def generer_script_depuis_reference(lien: str) -> dict[str, str]:
    _verifier_ffmpeg()
    dossier = DOSSIER_TRAVAIL / uuid.uuid4().hex
    dossier.mkdir(parents=True, exist_ok=True)
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            video_path = await _telecharger_video_tiktok(session, lien, dossier / "source.mp4")

        transcript = await _transcrire_video(video_path)
        if not transcript.strip():
            raise ErreurApp("Transcription vide : impossible de lire ce qui est dit dans la vidéo.")

        brut = await _appel_gemini_brut(
            [{"text": transcript}], temperature=0.7, system=PROMPT_REFERENCE, json_mode=True
        )
        resultat = _parser_json(brut)
        if not isinstance(resultat, dict):
            raise ErreurApp("Réponse IA non exploitable : un objet JSON était attendu.")
        for cle in ("hook", "corps", "mot_cle_broll"):
            valeur = resultat.get(cle)
            if not isinstance(valeur, str) or not valeur.strip():
                raise ErreurApp(f"Réponse IA incomplète : « {cle} » manquant.")
            resultat[cle] = valeur.strip()
        return resultat
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


# ======================================================================================
# B-ROLL (PEXELS)
# ======================================================================================


async def chercher_broll(mot_cle: str, duree_visee: float) -> list[str]:
    mot_cle = mot_cle.strip()
    if not mot_cle:
        raise ErreurApp("Le thème visuel est requis pour chercher le B-roll.")
    if not CONFIG.pexels_api_key:
        raise ErreurApp("PEXELS_API_KEY n'est pas configuré sur le serveur.")

    url = "https://api.pexels.com/videos/search"
    headers = {"Authorization": CONFIG.pexels_api_key}
    params = {"query": mot_cle, "orientation": "portrait", "size": "large", "per_page": "40"}

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        async def _appel():
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise ErreurApp(f"Pexels a répondu {resp.status} : {texte[:200]}")
                return await resp.json()

        donnees = await _avec_retry(_appel, etape=f"recherche Pexels « {mot_cle} »")

    candidats = []
    for video in donnees.get("videos", []):
        if video.get("width", 0) >= video.get("height", 1):
            continue
        fichiers = sorted(
            (f for f in video.get("video_files", []) if f.get("link")),
            key=lambda f: f.get("width", 0) or 0, reverse=True,
        )
        if fichiers:
            # 1080/1920 px suffisent pour la sortie verticale et évitent de télécharger
            # les versions 4K inutilement lourdes sur une instance Render 512 Mo.
            raisonnables = [fichier for fichier in fichiers if (fichier.get("width", 0) or 0) <= 1920]
            candidats.append((raisonnables or fichiers)[0]["link"])

    if not candidats:
        raise ErreurApp(f"Aucune vidéo Pexels verticale trouvée pour « {mot_cle} ».")

    # On télécharge jusqu'à 12 candidates : l'IA peut en écarter (texte, logo,
    # flou). Le montage final réutilise seulement les passages qu'elle certifie.
    nb_clips = max(8, min(12, -(-int(max(duree_visee, DUREE_MIN_VIDEO_SECONDES)) // 5)))
    random.shuffle(candidats)
    return candidats[:nb_clips]


# ======================================================================================
# MODE "MONTAGE MULTI-VIDÉOS" : l'IA repère, dans les vidéos fournies, les bons passages
# ======================================================================================

PROMPT_ANALYSE_VIDEO = (
    "Voici une vidéo. Voici une liste de passages d'un script, chacun avec un identifiant :\n"
    "{segments}\n\n"
    "Pour chaque passage qui correspond à un moment RÉELLEMENT visible dans cette vidéo, indique :\n"
    "- id : l'identifiant du passage\n"
    "- debut, fin : le moment en secondes (nombres) où ce passage correspond dans la vidéo\n"
    "- pertinence : un score entre 0 et 1\n"
    "- texte_visible : true si du texte est incrusté à l'écran à ce moment précis, false sinon\n"
    "N'inclus PAS un passage si rien dans la vidéo ne lui correspond vraiment — mieux vaut un "
    "tableau court et fiable qu'un tableau complet mais inventé.\n"
    "Réponds UNIQUEMENT avec un tableau JSON de ces objets, rien d'autre, sans texte avant/après."
)


def _decouper_en_segments(hook: str, corps: str) -> list[dict]:
    """Découpage en phrases, pour le repérage vidéo (plus grossier que les cues de sous-titres)."""
    texte = f"{hook.strip()} {corps.strip()}"
    phrases = [p.strip() for p in re.split(r"(?<=[.!?])\s+", texte) if p.strip()]
    return [{"id": i, "texte": p} for i, p in enumerate(phrases)]


async def _creer_apercu_video(video_path: Path) -> Path:
    """Réduit une source avant l'envoi inline à Gemini.

    Les vidéos TikTok peuvent être lourdes. Une preview 640 px, 12 fps et 90 secondes
    conserve assez d'information visuelle pour le repérage sans exploser la RAM du Render
    gratuit quand l'encodage base64 est effectué.
    """
    apercu = video_path.with_name(f"{video_path.stem}_analyse.mp4")
    commande = [
        "ffmpeg", "-y", "-i", str(video_path), "-t", str(DUREE_MAX_APERCU_IA),
        "-vf", "scale=640:-2:force_original_aspect_ratio=decrease,fps=12",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
        "-maxrate", "500k", "-bufsize", "1000k", "-pix_fmt", "yuv420p", str(apercu),
    ]
    await executer_commande(
        commande, etape="préparation de l’aperçu historique",
        timeout=CONFIG.delai_ffmpeg, sortie_attendue=apercu,
    )
    return apercu


async def _analyser_video(video_path: Path, segments: list[dict]) -> list[dict]:
    """Demande à Gemini de repérer, dans cette vidéo, les passages qui illustrent le script."""
    apercu: Optional[Path] = None
    try:
        apercu = await _creer_apercu_video(video_path)
        taille_mo = apercu.stat().st_size / 1_048_576
        if taille_mo > 12:
            logger.warning("Preview de %s trop lourde (%.1f Mo), analyse ignorée.", video_path.name, taille_mo)
            return []

        video_b64 = base64.b64encode(apercu.read_bytes()).decode("ascii")
        segments_json = json.dumps([{"id": s["id"], "texte": s["texte"]} for s in segments], ensure_ascii=False)
        prompt = PROMPT_ANALYSE_VIDEO.format(segments=segments_json)
        brut = await _appel_gemini_brut(
            [
                {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                {"text": prompt},
            ],
            temperature=0.2,
            json_mode=True,
        )
        resultat = _parser_json(brut)
        return resultat if isinstance(resultat, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Analyse vidéo échouée pour %s : %s", video_path.name, exc)
        return []
    finally:
        if apercu:
            apercu.unlink(missing_ok=True)


def _assigner_fragments(segments: list[dict], candidats_par_video: dict[str, list[dict]]) -> dict[int, dict]:
    """
    Attribution gloutonne : pour chaque segment de script, le meilleur candidat disponible
    (sans texte incrusté préféré, puis la meilleure pertinence) — chaque segment n'est assigné
    qu'une fois.
    """
    tous = []
    for nom_video, candidats in candidats_par_video.items():
        for c in candidats:
            if not isinstance(c, dict):
                continue
            tous.append({**c, "video": nom_video})

    tous.sort(key=lambda c: (bool(c.get("texte_visible", False)), -float(c.get("pertinence", 0) or 0)))

    assignation: dict[int, dict] = {}
    for c in tous:
        seg_id = c.get("id")
        if seg_id is None or seg_id in assignation:
            continue
        try:
            debut, fin = float(c["debut"]), float(c["fin"])
        except (KeyError, TypeError, ValueError):
            continue
        if fin <= debut:
            continue
        assignation[seg_id] = {"video": c["video"], "debut": debut, "fin": min(fin, debut + 8)}
    return assignation


def _fragments_repli(segments_sans_match: list[dict], noms_videos: list[str], duree: float = 3.0) -> dict[int, dict]:
    """Pour les segments sans correspondance trouvée par l'IA : un fragment par défaut, pour éviter les trous."""
    repli = {}
    for i, seg in enumerate(segments_sans_match):
        repli[seg["id"]] = {"video": noms_videos[i % len(noms_videos)], "debut": 0.0, "fin": duree}
    return repli


# ======================================================================================
# CONSTRUCTION DE LA VIDÉO (ffmpeg)
# ======================================================================================


def _decouper_en_cues(hook: str, corps: str, mots_par_seconde: float = 2.3, mots_par_cue: int = 5) -> list[dict]:
    """Découpe hook + corps en courtes séquences de sous-titres, avec une durée par mot."""
    texte_complet = f"{hook.strip()} {corps.strip()}".strip()
    mots = texte_complet.split()
    cues, t = [], 0.0
    for i in range(0, len(mots), mots_par_cue):
        groupe = mots[i:i + mots_par_cue]
        duree = len(groupe) / mots_par_seconde
        cues.append({"texte": " ".join(groupe), "debut": t, "fin": t + duree})
        t += duree
    return cues


async def _normaliser_clip(
    source: Path, destination: Path, duree: float, debut: float = 0.0,
    largeur: int = 720, hauteur: int = 1280,
) -> None:
    commande = [
        "ffmpeg", "-y", "-ss", str(debut), "-i", str(source), "-t", str(duree),
        "-vf", f"scale={largeur}:{hauteur}:force_original_aspect_ratio=increase,"
               f"crop={largeur}:{hauteur},fps=24",
        "-an", "-c:v", "libx264", "-preset", CONFIG.preset_export,
        "-pix_fmt", "yuv420p", "-threads", str(CONFIG.threads_ffmpeg), str(destination),
    ]
    await executer_commande(
        commande, etape="normalisation vidéo", timeout=CONFIG.delai_ffmpeg,
        sortie_attendue=destination,
    )


async def _assembler_clips(normalises: list[Path], dossier: Path) -> Path:
    liste = dossier / "liste.txt"
    liste.write_text("".join(f"file '{c.resolve()}'\n" for c in normalises), encoding="utf-8")
    assemble = dossier / "assemble.mp4"
    await executer_commande(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(liste),
         "-c", "copy", str(assemble)],
        etape="assemblage vidéo", timeout=CONFIG.delai_ffmpeg,
        sortie_attendue=assemble,
    )
    return assemble


async def _incruster_sous_titres(
    assemble: Path, cues: list[dict], style: str, dossier: Path,
    largeur: int = 720, hauteur: int = 1280, crf: int = 23,
    voix_off: Optional[Path] = None,
) -> Path:
    """Fichiers texte par cue (textfile= évite tout souci d'échappement des apostrophes/accents)."""
    if style not in STYLES_SOUS_TITRES:
        style = "classique"
    reglage = STYLES_SOUS_TITRES[style]

    police = _police()
    taille_police = max(28, int(64 * hauteur / 1280))
    filtres = []
    positions = {"bas": f"h-{int(320 * hauteur / 1280)}", "centre": "(h-text_h)/2", "haut": f"{int(160 * hauteur / 1280)}"}
    y = positions.get(reglage["position"], positions["bas"])
    for i, cue in enumerate(cues):
        fichier_texte = dossier / f"cue_{i}.txt"
        fichier_texte.write_text(cue["texte"], encoding="utf-8")
        filtres.append(
            f"drawtext=fontfile={police}:textfile={fichier_texte}:fontsize={taille_police}:"
            f"fontcolor={reglage['couleur']}:borderw=4:bordercolor={reglage['contour']}:"
            f"x=(w-text_w)/2:y={y}:enable='between(t,{cue['debut']:.2f},{cue['fin']:.2f})'"
        )

    sortie = DOSSIER_VIDEOS / f"{uuid.uuid4().hex}.mp4"
    commande = ["ffmpeg", "-y", "-i", str(assemble)]
    if voix_off:
        # Deuxième entrée : la voix off importée. `apad` la prolonge si elle est plus
        # courte que l'image, `-shortest` coupe le rendu à la fin du plus court des deux.
        commande += ["-i", str(voix_off)]
    commande += ["-vf", ",".join(filtres)]
    if voix_off:
        commande += ["-af", "apad", "-map", "0:v", "-map", "1:a", "-c:a", "aac", "-b:a", "160k"]
    commande += [
        "-c:v", "libx264", "-preset", CONFIG.preset_export, "-crf", str(max(14, min(30, int(crf)))),
        "-pix_fmt", "yuv420p",
        "-r", "24", "-threads", str(CONFIG.threads_ffmpeg), "-movflags", "+faststart",
    ]
    commande += (["-shortest"] if voix_off else ["-an"]) + [str(sortie)]
    await executer_commande(
        commande,
        etape="incrustation des sous-titres", timeout=CONFIG.delai_ffmpeg,
        sortie_attendue=sortie,
    )
    return sortie


async def construire_video(
    liens_broll: list[str], cues: list[dict], duree_totale: float, style: str,
    resolution: str = "720", crf: int = 23, voix_off: Optional[Path] = None,
) -> Path:
    """Mode « thème libre » : B-roll cherché sur Pexels."""
    _verifier_ffmpeg()
    dossier = DOSSIER_TRAVAIL / uuid.uuid4().hex
    dossier.mkdir(parents=True, exist_ok=True)

    try:
        if resolution == "1080" and not CONFIG.autoriser_export_1080:
            resolution = "720"
        largeur, hauteur = (1080, 1920) if resolution == "1080" else (720, 1280)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            bruts = []
            for i, lien in enumerate(liens_broll):
                chemin = dossier / f"brut_{i}.mp4"
                await _telecharger_fichier(session, lien, chemin)
                bruts.append(chemin)

        # Chaque candidate est réellement regardée par Gemini. Une analyse en échec,
        # du texte, un watermark ou une qualité insuffisante exclut la source.
        config_ia = _configuration_montage("qualite")
        rapporteur_ia = Rapporteur(lambda **_valeurs: None, lambda: False, lambda: 540.0)
        texte_ia = " ".join(str(cue.get("texte", "")) for cue in cues).strip()
        segments_ia = [{
            "id": 0, "texte": texte_ia or "illustration exacte du sujet demandé",
            "hook": False, "duree_cible": 5.0,
        }]
        passages_valides: list[tuple[Path, float]] = []
        for brut in bruts:
            try:
                infos = await sonder_video(brut, timeout=min(25, CONFIG.delai_ffmpeg))
                scenes, avertissement = await analyser_video(
                    brut, float(infos["duration"]), segments_ia,
                    config_ia, rapporteur_ia, _appel_gemini_brut,
                )
            except (ErreurMontage, ErreurApp) as exc:
                logger.warning("B-roll %s refusé par la validation IA : %s", brut.name, exc)
                continue
            if avertissement:
                continue
            for scene in scenes:
                disponible = float(scene.get("fin", 0)) - float(scene.get("debut", 0))
                qualite = str(scene.get("qualite", "")).lower()
                texte_interdit = bool(scene.get("texte_visible")) and not bool(
                    scene.get("texte_sous_titres")
                )
                scores = {
                    int(item.get("id", -1)): float(item.get("score", 0) or 0)
                    for item in scene.get("pertinence_script", [])
                }
                pertinence = scores.get(0, float(scene.get("score_pertinence", 0) or 0))
                if (
                    disponible >= 5.0
                    and pertinence >= 0.45
                    and not texte_interdit
                    and not scene.get("personne_visible")
                    and not scene.get("watermark")
                    and not scene.get("logo_visible")
                    and not scene.get("autre_element_superpose")
                    and float(scene.get("nettete", 0) or 0) >= 0.65
                    and qualite in {"bonne", "excellent", "excellente", "professionnelle"}
                ):
                    debut_scene = max(0.0, float(scene["debut"]))
                    fin_scene = float(scene["fin"])
                    position = debut_scene
                    while position + 5.0 <= fin_scene + 1e-6:
                        passages_valides.append((brut, position))
                        position += 5.0

        if not passages_valides:
            raise ErreurApp(
                "L'IA n'a trouvé aucune source Pexels professionnelle sans texte ni watermark. "
                "Essaie un thème visuel plus précis."
            )

        # Plans de 5 s maximum, répétés en alternance si nécessaire, jusqu'à 61 s.
        normalises: list[Path] = []
        restant = max(DUREE_MIN_VIDEO_SECONDES, float(duree_totale))
        index = 0
        while restant > 0.001:
            brut, debut = passages_valides[index % len(passages_valides)]
            duree = min(5.0, restant)
            cible = dossier / f"norm_{index}.mp4"
            await _normaliser_clip(
                brut, cible, duree, debut=debut, largeur=largeur, hauteur=hauteur
            )
            normalises.append(cible)
            restant -= duree
            index += 1

        assemble = await _assembler_clips(normalises, dossier)
        sortie = await _incruster_sous_titres(
            assemble, cues, style, dossier, largeur=largeur, hauteur=hauteur, crf=21,
            voix_off=voix_off,
        )
        infos_sortie = await sonder_video(sortie, timeout=min(25, CONFIG.delai_ffmpeg))
        if float(infos_sortie["duration"]) + 0.05 < DUREE_MIN_VIDEO_SECONDES:
            sortie.unlink(missing_ok=True)
            raise ErreurApp("La vidéo exportée fait moins de 1 min 1 s ; export refusé.")
        return sortie
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


def _cues_depuis_segments(segments_ordonnes: list[dict], mots_par_cue: int = 5) -> list[dict]:
    """
    segments_ordonnes : [{"texte":.., "duree": float}, ...] dans l'ordre final du montage.
    Répartit chaque phrase en courtes cues de sous-titres, DANS la fenêtre de temps réellement
    occupée par son fragment vidéo — les sous-titres restent synchronisés avec le bon passage.
    """
    cues, t = [], 0.0
    for seg in segments_ordonnes:
        mots = seg["texte"].split() or [seg["texte"]]
        duree_segment = seg["duree"]
        nb_groupes = max(1, -(-len(mots) // mots_par_cue))
        duree_par_groupe = duree_segment / nb_groupes
        for i in range(0, len(mots), mots_par_cue):
            groupe = mots[i:i + mots_par_cue]
            cues.append({"texte": " ".join(groupe), "debut": t, "fin": t + duree_par_groupe})
            t += duree_par_groupe
    return cues


async def construire_montage(liens_videos: list[str], hook: str, corps: str, style: str) -> Path:
    """Mode « montage multi-vidéos » : l'IA repère les bons passages dans les vidéos fournies."""
    _verifier_ffmpeg()
    segments = _decouper_en_segments(hook, corps)
    if not segments:
        raise ErreurApp("Script vide : rien à monter.")

    dossier = DOSSIER_TRAVAIL / uuid.uuid4().hex
    dossier.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Téléchargement des vidéos sources fournies
        chemins_videos: dict[str, Path] = {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
            async def _telecharger_une(i: int, lien: str) -> None:
                nom = f"source_{i}"
                chemin = dossier / f"{nom}.mp4"
                try:
                    await _telecharger_video_tiktok(session, lien, chemin)
                    chemins_videos[nom] = chemin
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Téléchargement échoué pour %s : %s", lien, exc)

            # Téléchargements séquentiels : évite de cumuler plusieurs buffers réseau
            # et plusieurs fichiers temporaires en même temps sur l'instance 512 Mo.
            for i, lien in enumerate(liens_videos):
                await _telecharger_une(i, lien)

        if not chemins_videos:
            raise ErreurApp("Aucune des vidéos fournies n'a pu être téléchargée.")

        # 2. Analyse séquentielle : une seule preview et un seul payload base64 en RAM
        # à la fois. C'est volontaire sur Render Free (limite mémoire de 512 Mo).
        noms = list(chemins_videos.keys())
        candidats_par_video: dict[str, list[dict]] = {}
        for nom in noms:
            candidats_par_video[nom] = await _analyser_video(chemins_videos[nom], segments)

        # 3. Attribution du meilleur fragment par segment, repli si l'IA n'a rien trouvé
        assignation = _assigner_fragments(segments, candidats_par_video)
        sans_match = [s for s in segments if s["id"] not in assignation]
        if sans_match:
            logger.warning("%s passage(s) sans correspondance trouvée par l'IA, repli appliqué.", len(sans_match))
            assignation.update(_fragments_repli(sans_match, list(chemins_videos.keys())))

        # 4. Découpe + normalisation de chaque fragment, dans l'ordre du script
        normalises, segments_ordonnes = [], []
        for seg in segments:
            frag = assignation.get(seg["id"])
            if not frag:
                continue
            duree = max(0.8, frag["fin"] - frag["debut"])
            cible = dossier / f"norm_{seg['id']}.mp4"
            await _normaliser_clip(chemins_videos[frag["video"]], cible, duree, debut=frag["debut"])
            normalises.append(cible)
            segments_ordonnes.append({"texte": seg["texte"], "duree": duree})

        if not normalises:
            raise ErreurApp("Aucun fragment n'a pu être découpé.")

        # 5. Assemblage dans l'ordre du script, puis sous-titres synchronisés aux vrais fragments
        assemble = await _assembler_clips(normalises, dossier)
        cues = _cues_depuis_segments(segments_ordonnes)
        return await _incruster_sous_titres(assemble, cues, style, dossier)
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


# ======================================================================================
# LIVRAISON SÉPARÉE : script .txt + voix off .mp3 générée (edge-tts), vidéo muette
# ======================================================================================
#
# La vidéo finale ne contient AUCUNE piste audio générée : elle reste muette. Le script
# et sa lecture à voix haute se téléchargent à côté, en deux fichiers indépendants.
# L'utilisateur reste libre de les monter lui-même, ou d'importer sa propre voix off.


def composer_script_txt(
    script: dict[str, Any],
    seed: Optional[dict[str, Any]] = None,
    noms: Optional[list[str]] = None,
) -> str:
    """Met en forme le script livré en .txt — uniquement des données réelles du travail."""
    hook = str(script.get("hook") or "").strip()
    corps = str(script.get("corps") or "").strip()
    lignes = ["SCRIPT", "=" * 6, "", "ACCROCHE", hook or "(vide)", "", "CORPS", corps or "(vide)"]

    if noms:
        lignes += ["", "NOMS RECHERCHÉS", ", ".join(noms)]
    if seed:
        titre = str(seed.get("title") or "").strip()
        auteur = str(seed.get("author") or "").strip()
        lignes += ["", "VIDÉO DE DÉPART"]
        if auteur:
            lignes.append(f"Auteur : @{auteur}")
        if titre:
            lignes.append(f"Légende : {titre}")
        if seed.get("url"):
            lignes.append(f"Lien : {seed['url']}")

    mot_cle = str(script.get("mot_cle_broll") or "").strip()
    if mot_cle:
        lignes += ["", "UNIVERS VISUEL", mot_cle]
    lignes += [
        "", "-" * 60,
        "La vidéo livrée est muette : ce script et le MP3 de voix off se téléchargent à part.",
    ]
    return "\n".join(lignes) + "\n"


def texte_a_dire(script: dict[str, Any]) -> str:
    """Texte réellement lu par la voix off : l'accroche puis le corps, rien d'autre."""
    hook = str(script.get("hook") or "").strip()
    corps = str(script.get("corps") or "").strip()
    parle = " ".join(part for part in (hook, corps) if part).strip()
    return parle[:VOIX_OFF_TEXTE_MAX]


async def synthetiser_voix_off(
    texte: str, destination: Path, budget: float = EDGE_TTS_DELAI
) -> int:
    """Génère un MP3 avec edge-tts (gratuit, sans clé). Retourne la taille écrite.

    Lève ErreurApp si le service est injoignable ou renvoie un fichier vide : aucun
    fichier factice n'est laissé derrière, et l'appelant reste libre de continuer.
    Le délai ne dépasse jamais le budget restant du travail.
    """
    propre = str(texte or "").strip()
    if not propre:
        raise ErreurApp("Script vide : il n'y a rien à lire pour la voix off.")
    try:
        import edge_tts  # import tardif : le module n'est requis qu'à la synthèse
    except ImportError as exc:  # pragma: no cover - dépendance déclarée dans requirements
        raise ErreurApp("edge-tts n'est pas installé sur le serveur.") from exc

    delai = max(1.0, min(EDGE_TTS_DELAI, float(budget)))
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        communicate = edge_tts.Communicate(propre, EDGE_TTS_VOIX, rate=EDGE_TTS_DEBIT)
        async with asyncio.timeout(delai):
            await communicate.save(str(destination))
    except asyncio.TimeoutError as exc:
        destination.unlink(missing_ok=True)
        raise ErreurApp(
            f"La synthèse vocale n'a pas répondu sous {delai:.0f} s."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - edge-tts remonte des erreurs réseau variées
        destination.unlink(missing_ok=True)
        raise ErreurApp(f"Synthèse vocale indisponible : {exc}") from exc

    taille = destination.stat().st_size if destination.is_file() else 0
    if taille <= 0:
        destination.unlink(missing_ok=True)
        raise ErreurApp("La synthèse vocale a renvoyé un fichier audio vide.")
    return taille


async def livrer_script_et_voix(
    script: dict[str, Any],
    seed: Optional[dict[str, Any]] = None,
    noms: Optional[list[str]] = None,
    budget: float = EDGE_TTS_DELAI,
) -> dict[str, Any]:
    """Livre au mieux le script et le MP3 sans jamais compromettre le rendu RsT.

    Cette étape arrive après le montage. Une écriture disque ou edge-tts défaillant
    est donc signalé dans le résultat, plutôt que de faire expirer un travail dont la
    vidéo est déjà prête.
    """
    base = uuid.uuid4().hex
    chemin_txt = DOSSIER_VIDEOS / f"{base}.txt"
    chemin_mp3 = DOSSIER_VIDEOS / f"{base}.mp3"
    livraison: dict[str, Any] = {
        "script_url": "",
        "script_nom": "",
        "script_erreur": "",
        "voix_url": "",
        "voix_nom": "",
        "voix_erreur": "",
        "voix_moteur": f"edge-tts · {EDGE_TTS_VOIX}",
        "livraison_erreur": "",
    }

    try:
        chemin_txt.write_text(composer_script_txt(script, seed, noms), encoding="utf-8")
    except OSError as exc:
        logger.warning("[livraison] écriture du script impossible : %s", exc)
        livraison["script_erreur"] = f"Écriture du script impossible : {exc}"
    except Exception as exc:  # noqa: BLE001 - la livraison ne doit jamais casser le rendu
        logger.exception("[livraison] préparation du script impossible")
        livraison["script_erreur"] = f"Préparation du script impossible : {exc}"
    else:
        livraison["script_url"] = f"/videos/{chemin_txt.name}"
        livraison["script_nom"] = "script.txt"

    try:
        budget_restant = float(budget)
    except (TypeError, ValueError) as exc:
        livraison["voix_erreur"] = f"Budget de synthèse invalide : {exc}"
        return livraison
    if budget_restant < EDGE_TTS_MINIMUM:
        livraison["voix_erreur"] = (
            "Synthèse vocale ignorée : moins de "
            f"{EDGE_TTS_MINIMUM:.0f} s restaient pour finaliser le travail."
        )
        return livraison

    try:
        taille = await synthetiser_voix_off(
            texte_a_dire(script), chemin_mp3, budget=budget_restant
        )
    except Exception as exc:  # noqa: BLE001 - edge-tts remonte des erreurs réseau variées
        # Honnêteté : on dit pourquoi le MP3 manque, on ne livre pas d'audio factice.
        logger.warning("[voix off] synthèse impossible : %s", exc)
        livraison["voix_erreur"] = str(exc)
    else:
        livraison["voix_url"] = f"/videos/{chemin_mp3.name}"
        livraison["voix_nom"] = "voix-off.mp3"
        livraison["voix_octets"] = taille
    return livraison


# ======================================================================================
# MODE « RsT » : un seul lien TikTok de départ → script + vraies vidéos trouvées → montage
# ======================================================================================

DUREE_MIN_SOURCE_RST = 5.0

# Pertinence TEXTE des candidates RsT : un plan « joli mais hors sujet » (skyline de
# Dubaï sur un script de téléphone, par exemple) doit être repoussé voire écarté avant
# même le montage. Le score ne s'appuie que sur des données réelles : titre TikWM,
# nom recherché, origine de la découverte. Aucune métadonnée n'est inventée.

# Mots qui signalent un contenu tech / téléphone / produit (frontières de mots).
_MOTS_TECH_RST = (
    "telephone", "téléphone", "telephones", "téléphones", "smartphone", "smartphones",
    "iphone", "ipad", "android", "ios", "samsung", "galaxy", "pixel", "xiaomi", "redmi",
    "realme", "oppo", "vivo", "oneplus", "honor", "huawei", "motorola", "nokia",
    "mobile", "écran", "ecran", "screen", "phone", "unboxing", "batterie", "chargeur",
    "5g", "comparatif", "test", "review", "avis", "modele", "modèle",
)
# Mots qui signalent un b-roll générique sans rapport avec un sujet produit/tech.
_MOTS_GENERIQUES_RST = (
    "voyage", "travel", "dubai", "dubaï", "skyline", "gratte-ciel", "gratte ciel",
    "building", "burj", "khalifa", "tour eiffel", "city", "ville", "lifestyle", "vlog",
    "paysage", "coucher de soleil", "sunset", "hotel", "hôtel", "resort", "vacances",
    "holiday", "aesthetic", "nature",
)
SCORE_TEXTE_BASE_RST = 0.30        # neutre : titre sans signal particulier
SCORE_TEXTE_NOM_RST = 0.40         # le nom recherché apparaît dans le titre
SCORE_TEXTE_TECH_RST = 0.25        # mot tech/produit dans le titre ou l'origine
SCORE_TEXTE_HORS_SUJET_RST = -0.55  # b-roll générique sans aucun mot du sujet
# En dessous de ce score texte, une candidate est carrément hors sujet : jamais retenue.
SEUIL_SCORE_TEXTE_RST = 0.10


def _mots_presents(texte: str, mots: tuple[str, ...]) -> bool:
    """Vrai si l'un des mots apparaît en entier (frontières de mots, casse ignorée)."""
    minuscule = texte.casefold()
    return any(re.search(rf"(?<!\w){re.escape(mot)}(?!\w)", minuscule) for mot in mots)


def _sujet_telephone_rst(*textes: str) -> bool:
    """Détecte un sujet tech/téléphone/produit dans les vrais textes de départ."""
    combine = " ".join(t for t in textes if t)
    return _mots_presents(combine, _MOTS_TECH_RST)


def _score_texte_candidat_rst(candidate: dict, sujet_telephone: bool) -> float:
    """Score texte simple (0..1) sur des données réelles : titre (hashtags inclus),
    nom recherché et origine de la découverte.

    Priorité aux titres contenant le nom recherché ou un mot du sujet (téléphone,
    smartphone, modèle, marque). Pour un sujet téléphone, une vidéo dont le titre ou
    l'origine évoque voyage / ville / skyline sans AUCUN mot tech dans le titre est
    fortement pénalisée : jolie ne veut pas dire pertinente.
    """
    titre = str(candidate.get("title") or "").casefold()
    origine = str(candidate.get("origin") or "").casefold()
    nom = str(candidate.get("nom") or "").strip().casefold().lstrip("#")
    nom_dans_titre = bool(nom and nom in titre)
    mot_tech_dans_titre = _mots_presents(titre, _MOTS_TECH_RST)
    score = SCORE_TEXTE_BASE_RST
    if nom_dans_titre:
        score += SCORE_TEXTE_NOM_RST
    if sujet_telephone and mot_tech_dans_titre:
        score += SCORE_TEXTE_TECH_RST
    if (
        sujet_telephone
        and (_mots_presents(titre, _MOTS_GENERIQUES_RST) or _mots_presents(origine, _MOTS_GENERIQUES_RST))
        and not mot_tech_dans_titre
        and not nom_dans_titre
    ):
        score += SCORE_TEXTE_HORS_SUJET_RST
    return round(min(1.0, max(0.0, score)), 3)


def _classer_candidats_rst(candidats: list[dict], sujet_telephone: bool) -> list[dict]:
    """Annote chaque candidate avec son score texte puis les trie par pertinence.

    Le tri se fait avant la répartition par nom : dans chaque file de nom, les titres
    les plus proches du sujet passent devant, les b-rolls génériques ferment la marche.
    """
    for candidate in candidats:
        candidate["score_texte"] = _score_texte_candidat_rst(candidate, sujet_telephone)
    return sorted(candidats, key=lambda c: float(c.get("score_texte") or 0), reverse=True)


_MOTS_VIDES_RST = {
    "avec", "bien", "cette", "dans", "depuis", "des", "elle", "elles", "est", "les", "leur",
    "mais", "moins", "mes", "meme", "nous", "parce", "pour", "puis", "que", "qui", "sans",
    "ses", "son", "sont", "sous", "sur", "tant", "tout", "tous", "tres", "vous", "was",
    "this", "that", "with", "from", "have", "just", "like", "when", "what", "your",
    "tiktok", "video", "vidéo", "videos", "fyp", "pourtoi", "pourtoii", "foryou",
    "foryoupage", "viral", "funny", "comedy", "follow",
}


async def _donnees_tikwm(session: aiohttp.ClientSession, chemin: str, params: dict) -> Any:
    """Appelle l'API publique TikWM et renvoie son bloc « data » — rien n'est inventé ici."""
    url = f"https://www.tikwm.com/api{chemin}"

    async def _appel():
        async with session.get(url, params=params, headers=ENTETES_TIKWM) as resp:
            if resp.status == 403:
                raise ErreurTikwm403(f"TikWM inaccessible (403)")
            if resp.status != 200:
                raise ErreurApp(f"TikWM inaccessible ({resp.status})")
            try:
                return await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError) as exc:
                raise ErreurApp("TikWM a renvoyé une réponse invalide.") from exc

    donnees = await _avec_retry(_appel, tentatives=2, etape=f"TikWM {chemin}")
    if not isinstance(donnees, dict) or donnees.get("code") != 0 or "data" not in donnees:
        message = donnees.get("msg", "réponse invalide") if isinstance(donnees, dict) else "réponse invalide"
        raise ErreurApp(f"TikWM : {message}")
    return donnees["data"]


def _extraire_liens_urlebird(html: str, auteur_defaut: str = "") -> list[str]:
    """Extrait de vrais liens TikTok à partir du HTML public d'une page Urlebird."""
    if not html:
        return []
    liens: list[str] = []
    vus: set[str] = set()

    soup = BeautifulSoup(html, "html.parser")
    for balise_a in soup.find_all("a", href=True):
        href = balise_a["href"].strip()
        match_tt = re.search(r"https?://(?:www\.)?tiktok\.com/@([^/]+)/video/(\d+)", href)
        if match_tt:
            pseudo = match_tt.group(1).strip().lstrip("@")
            vid_id = match_tt.group(2)
            url = f"https://www.tiktok.com/@{pseudo}/video/{vid_id}"
            if vid_id not in vus:
                vus.add(vid_id)
                liens.append(url)
            continue
        match_vid = re.search(r"/video/(?:[\w\-]+-)?(\d{15,22})/?", href)
        if match_vid:
            vid_id = match_vid.group(1)
            if vid_id in vus:
                continue
            parent = balise_a.find_parent(["div", "article", "li", "section"])
            pseudo = auteur_defaut
            if parent:
                lien_user = parent.find("a", href=re.compile(r"/user/([^/]+)/?"))
                if lien_user:
                    match_u = re.search(r"/user/([^/]+)/?", lien_user["href"])
                    if match_u:
                        pseudo = match_u.group(1).strip().lstrip("@")
            pseudo = pseudo.strip().lstrip("@") if pseudo else "tiktok"
            url = f"https://www.tiktok.com/@{pseudo}/video/{vid_id}"
            vus.add(vid_id)
            liens.append(url)

    for match_vid in re.finditer(r"/video/(?:[\w\-]+-)?(\d{15,22})/?", html):
        vid_id = match_vid.group(1)
        if vid_id not in vus:
            vus.add(vid_id)
            pseudo = auteur_defaut.strip().lstrip("@") if auteur_defaut else "tiktok"
            liens.append(f"https://www.tiktok.com/@{pseudo}/video/{vid_id}")

    return liens


# ======================================================================================
# DÉCOUVERTE PUBLIQUE MULTI-SOURCES — tolérante aux plages IP cloud de Render
# ======================================================================================
# Constaté en production : depuis l'IP de Render, TikWM /user/posts et /feed/search
# répondent 403 (seul l'endpoint unitaire /api/ passe) et Urlebird est bloqué par
# Cloudflare. La découverte enchaîne donc plusieurs sources publiques, toutes sans
# clé, sans compte et sans abonnement :
#   1. moteurs de recherche — DuckDuckGo lite, Ecosia, Bing — interrogés en direct
#      puis, si l'IP du serveur est bloquée, via un relais de lecture public
#      (r.jina.ai) dont l'infrastructure demande la page à sa place ;
#   2. archive web publique (Wayback Machine) pour les publications d'un auteur ;
#   3. miroir public Urlebird (dernier recours).
# Chaque lien découvert est ensuite revalidé par TikWM /api/ : identifiant, auteur,
# titre et durée restent réels, aucune donnée n'est jamais inventée.

# Relais de lecture public : gratuit, sans clé ni compte (limite publique ~20 req/min).
RELAIS_LECTURE = _env("RELAIS_LECTURE_URL", "https://r.jina.ai/").rstrip("/") + "/"
DECOUVERTE_DELAI_MOTEUR = 10.0    # moteur de recherche interrogé en direct
DECOUVERTE_DELAI_RELAIS = 15.0    # même moteur interrogé via un relais public
DECOUVERTE_DELAI_MIROIR = 14.0    # Urlebird et archive web
DECOUVERTE_BUDGET = 150.0         # budget global de découverte pour un travail RsT
DECOUVERTE_BUDGET_REQUETE = 45.0  # budget maximal de découverte pour une seule requête

ENTETES_MOTEURS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Identifiants vidéo TikTok réels : 18 à 20 chiffres. Les identifiants plus courts
# ou les « versions » type 2.0.0.21 rencontrées dans les archives sont du bruit.
# sticktock.com est un miroir public de TikTok : mêmes auteurs, mêmes identifiants
# vidéo — les liens y sont normalisés en tiktok.com puis revalidés par TikWM /api/.
_RE_LIEN_VIDEO_TIKTOK = re.compile(
    r"(?:tiktok|sticktock)\.com/(@[\w.\-]+)/video/(\d{18,20})", re.IGNORECASE
)

# Pages « Just a moment… », captcha ou défi anti-bot : la page a répondu mais n'a
# rien donné — le relais de lecture mérite alors un essai.
_MARQUEURS_BLOCAGE = (
    "just a moment", "challenge", "captcha", "anomaly",
    "unusual traffic", "are you a robot", "security verification",
    "enable javascript and cookies",
)


def _page_bloquee(texte: str) -> bool:
    """Vrai si la page ressemble à un défi anti-bot plutôt qu'à un vrai résultat."""
    bas = (texte or "").lower()[:6000]
    return any(marqueur in bas for marqueur in _MARQUEURS_BLOCAGE)


def _extraire_liens_tiktok_texte(texte: str, auteur_attendu: str = "") -> list[str]:
    """Extrait de vrais liens vidéo TikTok d'une page de moteur ou de miroir.

    Les moteurs enveloppent souvent la destination : DuckDuckGo l'encode en
    percent-encoding dans `uddg=`, Bing la chiffre en base64 dans `u=a1…`. On
    décode tout, puis on ne retient que les identifiants vidéo plausibles, sans
    jamais fabriquer un lien qui n'est pas dans la page.
    """
    if not texte:
        return []
    attendu = auteur_attendu.strip().lstrip("@").lower()
    vus: set[str] = set()
    liens: list[str] = []

    morceaux: list[str] = [texte]
    # Bing : destinations emballées en base64 (u=a1<base64url>)
    for jeton in re.findall(r"u=a1[A-Za-z0-9\-_+/=%]{16,}", texte):
        brut = unquote(jeton[4:])
        try:
            bourre = "=" * (-len(brut) % 4)
            morceaux.append(base64.urlsafe_b64decode(brut + bourre).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001 — un jeton illisible est simplement ignoré
            continue
    # DuckDuckGo et autres : destination percent-encodée (uddg=…)
    morceaux.append(unquote(texte))

    for morceau in morceaux:
        for match in _RE_LIEN_VIDEO_TIKTOK.finditer(morceau):
            pseudo, vid_id = match.group(1).lstrip("@"), match.group(2)
            if attendu and pseudo.lower() != attendu:
                continue
            if vid_id in vus:
                continue
            vus.add(vid_id)
            liens.append(f"https://www.tiktok.com/@{pseudo}/video/{vid_id}")
    return liens


def _requete_moteur(auteur: str, requete: str, moteur: str = "") -> str:
    """Recherche qui cible les pages vidéo TikTok, par auteur ou par nom.

    Les moteurs qui perdent l'opérateur « site: » (SearXNG l'agrège, Bing
    l'ignore) ou qui le servent mal aux requêteurs relais (DuckDuckGo via le
    proxy de traduction Google rend une page vide avec « site:tiktok.com/@… »,
    et riche sans) ciblent TikTok par mots-clés seuls. Le filtre final reste
    l'extraction de vrais liens vidéo, puis la revalidation TikWM /api/.
    """
    avec_site = moteur in {"ecosia", "bing"}
    if auteur:
        pseudo = auteur.strip().lstrip("@")
        if avec_site:
            return f"site:tiktok.com/@{pseudo} video"
        return f"tiktok.com @{pseudo} video"
    terme = " ".join(requete.strip().lstrip("#").split())
    if not terme:
        return ""
    return f"site:tiktok.com {terme} video" if avec_site else f"tiktok.com {terme} video"


def _apercu_resultats(texte: str, limite: int = 3) -> str:
    """Hôtes des premières destinations d'une page, pour comprendre une page
    sans lien vidéo : le diagnostic de production affiche ce qu'elle contient
    vraiment au lieu d'un simple « vide »."""
    hottes: list[str] = []
    for url in re.findall(r"""href=["']?(https?://[^"'\s<>]+)""", texte or ""):
        hote = urlparse(url).netloc.split("@")[-1]
        if hote and hote not in hottes:
            hottes.append(hote)
        if len(hottes) >= limite:
            break
    return ", ".join(hottes)


# Chaque moteur peut avoir plusieurs instances publiques, essayées en direct
# l'une après l'autre. SearXNG agrège Google/Bing/DuckDuckGo depuis son propre
# serveur : c'est lui qui subit les blocages d'IP, pas notre service.
_URLS_MOTEURS: dict[str, list] = {
    "searxng": [
        lambda q: f"https://opnxng.com/search?q={quote_plus(q)}",
        lambda q: f"https://search.inetol.net/search?q={quote_plus(q)}",
    ],
    "duckduckgo": [
        lambda q: f"https://lite.duckduckgo.com/lite/?q={quote_plus(q)}",
    ],
    "ecosia": [
        lambda q: f"https://www.ecosia.org/search?q={quote_plus(q)}",
    ],
    "bing": [
        lambda q: f"https://www.bing.com/search?q={quote_plus(q)}&count=20",
    ],
}


async def _lire_page(session: aiohttp.ClientSession, url: str, *, entetes: dict, delai: float) -> str:
    """GET direct ; lève ErreurApp si la source répond mal — aucune donnée n'est inventée."""
    async with session.get(
        url, headers=entetes, timeout=aiohttp.ClientTimeout(total=delai), allow_redirects=True
    ) as resp:
        if resp.status != 200:
            raise ErreurApp(f"source inaccessible ({resp.status})")
        return await resp.text(errors="replace")


async def _lire_page_relais(
    session: aiohttp.ClientSession, url: str, *, delai: float = DECOUVERTE_DELAI_RELAIS
) -> str:
    """Demande la page via le relais de lecture public : c'est le relais qui contacte
    la source depuis sa propre infrastructure, ce qui contourne les blocages de
    plage IP appliqués aux datacenters (Cloudflare, 403 sélectifs…)."""
    if not RELAIS_LECTURE.startswith("http"):
        raise ErreurApp("relais de lecture non configuré")
    async with session.get(
        RELAIS_LECTURE + url, headers=ENTETES_MOTEURS,
        timeout=aiohttp.ClientTimeout(total=delai), allow_redirects=True,
    ) as resp:
        if resp.status != 200:
            raise ErreurApp(f"relais inaccessible ({resp.status})")
        return await resp.text(errors="replace")


def _url_translate(url: str) -> str:
    """Construit l'URL « traduite » servie par l'infrastructure Google : la page est
    demandée par Google, pas par notre IP — un second relais public sans clé."""
    parsee = urlparse(url)
    hote = parsee.netloc.replace(":", "-").replace(".", "-")
    chemin = parsee.path or "/"
    base = f"https://{hote}.translate.goog{chemin}"
    parametres = "_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
    if parsee.query:
        return f"{base}?{parsee.query}&{parametres}"
    return f"{base}?{parametres}"


async def _lire_page_translate(
    session: aiohttp.ClientSession, url: str, *, delai: float = DECOUVERTE_DELAI_RELAIS
) -> str:
    """Demande la page via le proxy de traduction public de Google : c'est Google
    qui contacte la source, ce qui contourne les blocages appliqués à la plage IP
    du serveur (soft-block 200 vide, 403 Cloudflare…).

    Google répond parfois « 202 Accepted » pendant qu'il prépare la page : on
    réessaie une fois, et on accepte un 202 dont le corps contient déjà la page.
    """
    cible = _url_translate(url)
    dernier_etat = "aucune réponse"
    for tentative in range(2):
        try:
            async with session.get(
                cible, headers=ENTETES_MOTEURS,
                timeout=aiohttp.ClientTimeout(total=delai), allow_redirects=True,
            ) as resp:
                texte = await resp.text(errors="replace")
                dernier_etat = f"statut {resp.status}, {len(texte)} octets"
                if resp.status == 200 and len(texte) > 100:
                    return texte
                if resp.status == 202 and len(texte) > 1500:
                    return texte  # la page est servie malgré le statut « accepté »
        except asyncio.TimeoutError:
            raise ErreurApp("relais de traduction sans réponse (délai dépassé)")
        except Exception as exc:  # noqa: BLE001 — pas de réessai utile sur erreur réseau
            raise ErreurApp(f"relais de traduction inaccessible ({exc})")
        if tentative == 0:
            await asyncio.sleep(1.5)  # page en préparation : un seul réessai
    raise ErreurApp(f"relais de traduction inaccessible ({dernier_etat})")


def _serp_vraiment_vide(texte: str) -> bool:
    """Vrai si le moteur a clairement répondu « aucun résultat » (marqueur explicite).

    Sans ce marqueur, une page vide est suspecte : certains blocages doux servent
    une page 200 sans aucun résultat, et il faut alors passer par un relais.
    """
    if not texte:
        return False
    bas = texte.lower()
    return any(
        marqueur in bas
        for marqueur in ("no results", "aucun résultat", "didn't match any documents", "keine ergebnisse")
    )


async def _decouvrir_moteur(
    session: aiohttp.ClientSession, moteur: str, *,
    auteur: str = "", requete: str = "", limite: int = 20,
) -> tuple[list[str], str, str]:
    """Interroge un moteur en direct puis, si l'IP du serveur est bloquée ou que la
    page reste vide, via deux relais publics : le relais de lecture (r.jina.ai) et
    le relais de traduction Google (translate.goog).

    Renvoie (liens réels, mode, détail) : mode vaut « direct », « relais jina »,
    « relais traduction » ou vide ; le détail décrit chaque tentative, pour le
    diagnostic de production comme pour les journaux.
    """
    q = _requete_moteur(auteur, requete, moteur)
    if not q:
        return [], "", "aucune requête constructible"
    modeles = _URLS_MOTEURS.get(moteur) or []
    if not modeles:
        return [], "", "moteur inconnu"
    etapes: list[str] = []
    page_recue = False

    # --- 1) Direct, depuis l'IP du serveur, sur chaque instance du moteur ------
    urls = [modele(q) for modele in modeles]
    for indice, url in enumerate(urls):
        try:
            debut = time.monotonic()
            texte = await _lire_page(session, url, entetes=ENTETES_MOTEURS, delai=DECOUVERTE_DELAI_MOTEUR)
            duree = time.monotonic() - debut
            page_recue = True
            liens = _extraire_liens_tiktok_texte(texte, auteur_attendu=auteur)
            if liens:
                return liens[:limite], "direct", f"direct : {len(liens)} lien(s) en {duree:.1f} s"
            etapes.append(
                f"direct vide ({len(texte)} octets, {duree:.1f} s, "
                f"destinations : {_apercu_resultats(texte) or 'aucune'})"
            )
            # Page qui dit honnêtement « aucun résultat » et répond vite : inutile de
            # consommer les relais pour la même recherche sur le même moteur. Une
            # autre instance du moteur peut pourtant agréger d'autres moteurs :
            # on la tente avant de conclure.
            if _serp_vraiment_vide(texte) and duree < 6.0:
                if indice == len(urls) - 1:
                    return [], "", "direct : aucun résultat affiché par le moteur"
                etapes.append("instance suivante : cette instance n'a aucun résultat")
                continue
            if _page_bloquee(texte):
                etapes.append("page directe ressemble à un défi anti-bot")
        except Exception as exc:  # noqa: BLE001 — IP probablement bloquée : on relaye
            etapes.append(f"direct injoignable ({exc})")

    # --- 2) Relais de lecture public (r.jina.ai) — première instance -----------
    url = modeles[0](q)
    try:
        texte = await _lire_page_relais(session, url)
        page_recue = True
        liens = _extraire_liens_tiktok_texte(texte, auteur_attendu=auteur)
        if liens:
            return liens[:limite], "relais jina", f"relais de lecture : {len(liens)} lien(s) ; " + " ; ".join(etapes)
        if _serp_vraiment_vide(texte):
            # Le moteur a répondu via le relais : la recherche est réellement vide.
            return [], "", "relais de lecture : aucun résultat affiché par le moteur ; " + " ; ".join(etapes)
        etapes.append(f"relais de lecture vide (destinations : {_apercu_resultats(texte) or 'aucune'})")
    except Exception as exc:  # noqa: BLE001
        etapes.append(f"relais de lecture injoignable ({exc})")

    # --- 3) Relais de traduction Google (translate.goog) — première instance ---
    try:
        texte = await _lire_page_translate(session, url)
        page_recue = True
        liens = _extraire_liens_tiktok_texte(texte, auteur_attendu=auteur)
        if liens:
            return liens[:limite], "relais traduction", (
                f"relais de traduction : {len(liens)} lien(s) ; " + " ; ".join(etapes)
            )
        etapes.append(f"relais de traduction vide (destinations : {_apercu_resultats(texte) or 'aucune'})")
    except Exception as exc:  # noqa: BLE001
        etapes.append(f"relais de traduction injoignable ({exc})")

    # Aucune page n'a jamais été obtenue, même par les relais : le moteur est
    # injoignable depuis ce serveur. On le déclare bloqué (ErreurApp) pour que la
    # chaîne cesse de le tenter pendant le reste du travail — contrairement à une
    # vraie page vide, qui ne bloque rien.
    if not page_recue:
        raise ErreurApp("moteur injoignable par tous les chemins : " + " ; ".join(etapes))
    return [], "", " ; ".join(etapes)


async def _decouvrir_wayback(
    session: aiohttp.ClientSession, *, auteur: str, limite: int = 20
) -> tuple[list[str], str, str]:
    """Archive web publique (Wayback Machine) : vraies pages vidéo TikTok de cet
    auteur déjà archivées, les snapshots les plus récents d'abord."""
    pseudo = auteur.strip().lstrip("@")
    if not pseudo:
        return [], "", "aucun auteur"
    annee_min = max(2018, time.gmtime().tm_year - 3)
    parametres = {
        "url": f"tiktok.com/@{pseudo}/video/*",
        "output": "json",
        "filter": "statuscode:200",
        "collapse": "urlkey",
        "fl": "original,timestamp",
        "from": f"{annee_min}0101",
        "limit": str(max(60, limite * 3)),
    }
    url = "https://web.archive.org/cdx/search/cdx?" + urlencode(parametres)
    texte = await _lire_page(session, url, entetes=ENTETES_MOTEURS, delai=DECOUVERTE_DELAI_MIROIR)
    try:
        lignes = json.loads(texte)
    except json.JSONDecodeError as exc:
        raise ErreurApp("l'archive web a renvoyé une réponse invalide") from exc
    archives: dict[str, str] = {}
    for ligne in lignes[1:] if isinstance(lignes, list) else []:
        if not isinstance(ligne, list) or len(ligne) < 2:
            continue
        match = re.search(r"/video/(\d{18,20})", str(ligne[0]))
        if not match:
            continue  # « video/0 », « video/2.0.0.21 »… : pas de vraies vidéos
        vid, horodatage = match.group(1), str(ligne[1])
        if vid not in archives or horodatage > archives[vid]:
            archives[vid] = horodatage  # on garde le snapshot le plus récent
    ordonnees = sorted(archives.items(), key=lambda c: c[1], reverse=True)[:limite]
    liens = [f"https://www.tiktok.com/@{pseudo}/video/{vid}" for vid, _ in ordonnees]
    return liens, "archive", f"{len(archives)} vidéo(s) archivée(s) depuis {annee_min}"


async def _source_urlebird(
    session: aiohttp.ClientSession, *, auteur: str = "", requete: str = "", limite: int = 20
) -> tuple[list[str], str, str]:
    """Miroir public Urlebird ; lève ErreurApp si Cloudflare bloque l'IP du serveur."""
    if auteur:
        pseudo = auteur.strip().lstrip("@")
        url = f"https://urlebird.com/user/{quote(pseudo)}/"
    elif requete:
        terme = requete.strip().lstrip("#")
        url = f"https://urlebird.com/search/?q={quote_plus(terme)}"
    else:
        return [], "", "aucune requête constructible"

    async def _appel():
        async with session.get(
            url, headers=ENTETES_URLEBIRD,
            timeout=aiohttp.ClientTimeout(total=DECOUVERTE_DELAI_MIROIR),
        ) as resp:
            if resp.status != 200:
                raise ErreurApp(f"Urlebird inaccessible ({resp.status})")
            return await resp.text()

    html = await _avec_retry(_appel, tentatives=2, etape=f"Urlebird {url}")
    liens = _extraire_liens_urlebird(html, auteur_defaut=auteur)[:limite]
    return liens, "direct", f"page Urlebird de {len(html)} octets"


async def _decouvrir_urlebird(
    session: aiohttp.ClientSession,
    *,
    auteur: str = "",
    requete: str = "",
    limite: int = 20,
) -> list[str]:
    """Découvre de vrais liens TikTok via les pages publiques Urlebird (aucun compte ni clé)."""
    try:
        liens, _, _ = await _source_urlebird(session, auteur=auteur, requete=requete, limite=limite)
        return liens
    except Exception as exc:  # noqa: BLE001
        logger.warning("[RsT] découverte Urlebird impossible pour %s : %s", auteur or requete, exc)
        return []


@dataclass
class EtatSourcesDecouverte:
    """Mémoire des sources de découverte pour un travail RsT.

    Une source en échec (403, timeout…) n'est plus tentée pendant le reste du
    travail ; celle qui a fourni les derniers liens est interrogée en premier
    pour les recherches suivantes. Le budget borne le temps total de découverte.
    """
    bloquees: set[str] = field(default_factory=set)
    gagnante: str = ""
    budget: float = DECOUVERTE_BUDGET


def _libelle_origine(source: str, mode: str, auteur: str, requete: str) -> str:
    """Origine honnête affichée dans « Vidéos trouvées par RsT »."""
    noms = {
        "searxng": "SearXNG",
        "duckduckgo": "DuckDuckGo",
        "ecosia": "Ecosia",
        "bing": "Bing",
        "wayback": "Archive web",
        "urlebird": "Urlebird",
    }
    libelle = noms.get(source, source or "source publique")
    if mode == "relais jina":
        libelle += " via relais"
    elif mode == "relais traduction":
        libelle += " via relais de traduction"
    if auteur:
        return f"{libelle} : publications de @{auteur.strip().lstrip('@')}"
    return f"{libelle} : recherche « {requete.strip()} »"


async def _decouvrir_publique(
    session: aiohttp.ClientSession,
    *,
    auteur: str = "",
    requete: str = "",
    limite: int = 20,
    etat: Optional[EtatSourcesDecouverte] = None,
) -> list[dict]:
    """Chaîne de découverte publique multi-sources, sans clé ni compte.

    Essaie dans l'ordre plusieurs sources publiques jusqu'à obtenir de vrais liens
    vidéo TikTok : moteurs de recherche (direct puis relais), archive Wayback pour
    un auteur, miroir Urlebird en dernier recours. Renvoie [{'url': …, 'origin': …}] ;
    chaque URL sera ensuite revalidée par TikWM /api/ avant toute sélection.
    """
    auteur = auteur.strip().lstrip("@")
    requete = requete.strip()
    if not auteur and not requete:
        return []
    etat = etat or EtatSourcesDecouverte()

    sources = ["duckduckgo", "searxng", "ecosia", "bing"]
    if auteur:
        sources.append("wayback")
    sources.append("urlebird")
    if etat.gagnante in sources:
        sources.remove(etat.gagnante)
        sources.insert(0, etat.gagnante)

    decouverts: list[dict] = []
    vus: set[str] = set()
    debut_requete = time.monotonic()
    for source in sources:
        if source in etat.bloquees:
            continue
        if len(decouverts) >= limite:
            break
        # Deux garde-fous de temps : un budget par requête et un budget global,
        # pour laisser à la sélection, au montage et à la livraison ce qu'il faut.
        if time.monotonic() - debut_requete > DECOUVERTE_BUDGET_REQUETE:
            logger.info("[Découverte] budget de requête épuisé : %s non tentée", source)
            break
        if etat.budget < 15.0:
            logger.info("[Découverte] budget global épuisé : %s non tentée", source)
            break
        debut = time.monotonic()
        try:
            if source == "wayback":
                liens, mode, detail = await _decouvrir_wayback(session, auteur=auteur, limite=limite)
            elif source == "urlebird":
                liens, mode, detail = await _source_urlebird(
                    session, auteur=auteur, requete=requete, limite=limite
                )
            else:
                liens, mode, detail = await _decouvrir_moteur(
                    session, source, auteur=auteur, requete=requete, limite=limite
                )
        except Exception as exc:  # noqa: BLE001 — source bloquée : on passe à la suivante
            etat.bloquees.add(source)
            etat.budget -= time.monotonic() - debut
            logger.warning("[Découverte] source %s bloquée : %s", source, exc)
            continue
        etat.budget -= time.monotonic() - debut
        if not liens:
            if detail:
                logger.info("[Découverte] %s : %s", source, detail)
            continue
        etat.gagnante = source
        origine = _libelle_origine(source, mode, auteur, requete)
        for lien in liens:
            vid = lien.rsplit("/", 1)[-1]
            if vid in vus:
                continue
            vus.add(vid)
            decouverts.append({"url": lien, "origin": origine})
            if len(decouverts) >= limite:
                break
        logger.info("[Découverte] %s → %d lien(s) (%s)", source, len(liens), mode or "aucun résultat")
    return decouverts


def _normaliser_candidat_rst(video: Any, origine: str, nom: str = "") -> Optional[dict]:
    """Convertit une vidéo réellement renvoyée par TikWM en candidate RsT, ou None."""
    if not isinstance(video, dict):
        return None
    auteur = video.get("author") if isinstance(video.get("author"), dict) else {}
    identifiant = str(video.get("video_id") or video.get("id") or "").strip()
    pseudo = str(auteur.get("unique_id") or "").strip().lstrip("@")
    if not identifiant or not pseudo:
        return None
    try:
        duree = round(float(video.get("duration") or 0), 1)
    except (TypeError, ValueError):
        duree = 0.0
    return {
        "url": f"https://www.tiktok.com/@{pseudo}/video/{identifiant}",
        "video_id": identifiant,
        "author": pseudo,
        "author_name": str(auteur.get("nickname") or pseudo).strip() or pseudo,
        "title": str(video.get("title") or "").strip(),
        "duration": duree,
        "origin": origine,
        # Nom (TOP N) qui a permis de trouver cette vidéo — vide pour le fil de l'auteur.
        "nom": str(nom or "").strip(),
    }


def _extraire_mots_cles_rst(texte: str, mot_cle_broll: str = "", limite: int = 3) -> list[str]:
    """Mots-clés de recherche réels : hashtags de la légende, thème visuel, mots fréquents."""
    retenus: list[str] = []

    def ajouter(valeur: str) -> None:
        propre = valeur.strip().lstrip("#").strip()
        if 3 <= len(propre) <= 40 and propre.lower() not in {m.lower() for m in retenus}:
            retenus.append(propre)

    for etiquette in re.findall(r"#([\wÀ-ÿ]+)", texte):
        ajouter(etiquette)
    if mot_cle_broll.strip():
        ajouter(mot_cle_broll.strip())
    occurrences: dict[str, int] = {}
    for mot in re.findall(r"[\wÀ-ÿ']+", texte):
        propre = mot.strip("'-").lower()
        if len(propre) < 4 or propre in _MOTS_VIDES_RST or propre.isdigit():
            continue
        occurrences[propre] = occurrences.get(propre, 0) + 1
    for mot in sorted(occurrences, key=occurrences.get, reverse=True):
        if len(retenus) >= limite:
            break
        ajouter(mot)
    return retenus[:limite]


def _repartir_par_nom(candidats: list[dict], noms: list[str]) -> list[dict]:
    """Alterne les candidates nom par nom pour que chaque nom du TOP N soit représenté.

    Sans cela, le premier nom — souvent le plus prolifique — remplirait tout le quota de
    sources et les autres noms n'apparaîtraient jamais dans le montage final.
    """
    if not noms:
        return list(candidats)
    files: dict[str, list[dict]] = {nom.casefold(): [] for nom in noms}
    sans_nom: list[dict] = []
    for candidate in candidats:
        cle = str(candidate.get("nom") or "").casefold()
        files[cle].append(candidate) if cle in files else sans_nom.append(candidate)

    ordonnes: list[dict] = []
    rang = 0
    while True:
        ajoute = False
        for nom in noms:
            file = files[nom.casefold()]
            if rang < len(file):
                ordonnes.append(file[rang])
                ajoute = True
        if not ajoute:
            break
        rang += 1
    # Les vidéos du fil de l'auteur ferment la marche : ce sont des sources de secours.
    return ordonnes + sans_nom


def _selectionner_sources_rst(
    candidats: list[dict], limite: int, duree_max: float
) -> tuple[list[dict], list[dict]]:
    """Retient jusqu'à `limite` candidates dont la durée réelle est exploitable.

    Une candidate dont le score texte est sous `SEUIL_SCORE_TEXTE_RST` (b-roll
    générique sans rapport avec le sujet : voyage, skyline, lifestyle…) est écartée
    quelle que soit la place restante : mieux vaut moins de sources que du hors sujet.
    """
    for candidate in candidats:
        candidate.setdefault("selected", False)
        candidate.setdefault("rejet", "")
    retenues: list[dict] = []
    for candidate in candidats:
        score_texte = candidate.get("score_texte")
        if score_texte is not None and float(score_texte) < SEUIL_SCORE_TEXTE_RST:
            candidate["rejet"] = "titre hors sujet (voyage / ville / skyline sans mot-clé du sujet)"
            continue
        if len(retenues) >= limite:
            if not candidate.get("rejet"):
                candidate["rejet"] = "quota de sources atteint"
            continue
        duree = float(candidate.get("duration") or 0)
        if duree <= 0:
            candidate["rejet"] = "durée inconnue"
            continue
        if duree < DUREE_MIN_SOURCE_RST:
            candidate["rejet"] = f"durée {duree:.0f} s trop courte"
            continue
        if duree > duree_max:
            candidate["rejet"] = f"durée {duree:.0f} s au-delà de la limite de {duree_max:.0f} s"
            continue
        candidate["selected"] = False
        candidate["validation_status"] = "awaiting_visual_ai"
        retenues.append(candidate)
    return retenues, candidats


def _reduire_selon_estimation(
    sources: list[dict], config: ConfigurationMontage,
    plafond: int = PLAFOND_ESTIMATION_MONTAGE, duree_reference: float = 0.0,
) -> list[dict]:
    """Retire les dernières sources tant que l'estimation dépasse le plafond prudent.

    La vidéo de départ — désormais référence de style — est téléchargée et analysée
    comme une source de plus : son coût est compté dans l'estimation.
    """
    while len(sources) > 3:
        durees = [float(s.get("duration") or 0) for s in sources]
        if duree_reference > 0:
            durees = [min(duree_reference, config.duree_max_source)] + durees
        estimation = estimer_duree_traitement(durees, 0.0, config)
        if estimation["estimated_seconds"] <= plafond:
            break
        retiree = sources.pop()
        retiree["selected"] = False
        retiree["rejet"] = "retirée pour rester sous la limite de temps Render"
    return sources


def _rapporteur_decale(contexte: "ContexteJob", base: float, amplitude: float) -> Rapporteur:
    """Traduit la progression d'un sous-pipeline dans la fenêtre [base, base+amplitude]."""

    def mise_a_jour(statut: str, progress: int, detail: str, **extras: Any) -> None:
        brut = max(0, min(100, int(progress)))
        contexte.update(
            statut=statut, progress=int(round(base + amplitude * brut)), detail=detail, **extras
        )

    return Rapporteur(mise_a_jour, contexte.annule, contexte.restant)


async def _produire_rst(
    requete: "RequeteRst", contexte: "ContexteJob", session_id: str = ""
) -> dict[str, Any]:
    """Mode RsT : analyse un lien TikTok, trouve de vraies vidéos, monte automatiquement.

    Pipeline TOP N : l'IA extrait 3 ou 5 noms de la légende de départ, chaque nom donne
    lieu à sa propre recherche TikTok, les sources trouvées sont réparties nom par nom,
    puis montées en plans de 5 secondes maximum.
    """
    try:
        lien = normaliser_lien_tiktok(requete.lien.strip())
    except ErreurMontage as exc:
        raise ErreurApp(f"Lien TikTok de départ invalide : {exc}") from exc

    nombre_noms = borner_nombre_noms_rst(requete.nombre_noms)
    # Trace des recherches qui n'ont rien donné : sert à expliquer un échec sans rien inventer.
    echecs_recherche: list[str] = []

    contexte.update(statut="analysing", progress=4, detail="Lecture de la vidéo TikTok de départ")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75, connect=15)) as session:
        donnees = await _donnees_tikwm(session, "/", {"url": lien, "hd": "1"})
        legende = str(donnees.get("title") or "").strip()
        if not legende:
            raise ErreurApp("Cette vidéo TikTok n'a pas de légende exploitable comme point de départ.")
        auteur = str((donnees.get("author") or {}).get("unique_id") or "").strip().lstrip("@")
        identifiant_depart = str(donnees.get("id") or donnees.get("video_id") or "").strip()

        seed_infos = {
            "url": lien, "title": legende[:200], "author": auteur,
            "duration": donnees.get("duration"),
        }

        contexte.update(statut="analysing", progress=12, detail="Rédaction du script à partir de la légende")
        script = await generer_script(legende)
        contexte.update(
            statut="analysing", progress=18, detail="Script prêt — extraction des noms à chercher",
            script=script, seed=seed_infos,
        )

        # TOP N : 3 ou 5 noms tirés de la vraie légende, chacun cherché séparément.
        noms = await extraire_noms_rst(legende, nombre_noms, script.get("mot_cle_broll", ""))
        contexte.update(
            statut="analysing", progress=20,
            detail=(
                f"{len(noms)} nom(s) à chercher : {', '.join(noms)}" if noms
                else "Aucun nom exploitable — recherche élargie à l'auteur"
            ),
            noms=noms, nombre_noms=nombre_noms,
        )

        requetes_reelles: list[str] = []
        candidats: dict[str, dict] = {}
        tikwm_recherche_bloquee = False
        # Mémoire des sources publiques : une source bloquée n'est plus tentée, la
        # dernière qui a fourni des liens est interrogée en premier ensuite.
        etat_sources = EtatSourcesDecouverte()

        async def accumuler(videos: Any, origine: str, nom: str = "") -> int:
            """Ajoute les vidéos réellement renvoyées et retourne le nombre de nouveautés."""
            nouvelles = 0
            for video in videos or []:
                candidate = _normaliser_candidat_rst(video, origine, nom)
                if not candidate or candidate["video_id"] == identifiant_depart:
                    continue
                if candidate["video_id"] not in candidats:
                    candidats[candidate["video_id"]] = candidate
                    nouvelles += 1
            return nouvelles

        async def valider_et_accumuler(
            liens_publiques: list[dict], nom: str = "", limite_nouvelles: int = 20
        ) -> int:
            """Revalide chaque lien découvert via TikWM /api/ (identifiant, auteur,
            titre et durée réels) — jamais de métadonnée inventée."""
            nouvelles = 0
            for entree in liens_publiques:
                if len(candidats) >= CONFIG.rst_candidats_max or nouvelles >= limite_nouvelles:
                    break
                try:
                    await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                    donnees_v = await _donnees_tikwm(session, "/", {"url": entree["url"], "hd": "1"})
                    cand = _normaliser_candidat_rst(donnees_v, entree["origin"], nom)
                    if not cand or cand["video_id"] == identifiant_depart:
                        continue
                    if cand["video_id"] not in candidats:
                        candidats[cand["video_id"]] = cand
                        nouvelles += 1
                except Exception as exc:  # noqa: BLE001
                    logger.debug("[RsT] revalidation TikWM échouée pour %s : %s", entree["url"], exc)
            return nouvelles

        def publier(detail: str, progression: int) -> None:
            contexte.update(
                statut="searching", progress=progression, detail=detail,
                found_videos=list(candidats.values()), search_queries=list(requetes_reelles),
                noms=noms,
            )

        # 1) Les autres publications réelles du créateur de la vidéo de départ.
        if auteur:
            requetes_reelles.append(f"@{auteur}")
            contexte.update(statut="searching", progress=24, detail=f"Publications de @{auteur}")
            nouvelles = 0
            if not tikwm_recherche_bloquee:
                try:
                    await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                    publications = await _donnees_tikwm(
                        session, "/user/posts",
                        {"unique_id": auteur, "count": str(min(CONFIG.rst_candidats_max, 40))},
                    )
                    nouvelles = await accumuler(
                        publications.get("videos") or [], f"publications de @{auteur}"
                    )
                except ErreurTikwm403:
                    logger.warning("[RsT] TikWM /user/posts bloqué (403) : découverte publique pour @%s", auteur)
                    tikwm_recherche_bloquee = True
                except ErreurApp as exc:
                    logger.warning("[RsT] publications de @%s indisponibles : %s", auteur, exc)
                    echecs_recherche.append(f"@{auteur} : {exc}")

            # TikWM bloqué (403) ou fil de l'auteur vide : la chaîne publique prend
            # le relais (moteurs de recherche, archive web, miroir Urlebird).
            if tikwm_recherche_bloquee or nouvelles == 0:
                contexte.update(
                    statut="searching", progress=26,
                    detail=f"Recherche publique : publications de @{auteur}",
                )
                liens_publiques = await _decouvrir_publique(
                    session, auteur=auteur, limite=min(CONFIG.rst_candidats_max, 40),
                    etat=etat_sources,
                )
                nouvelles += await valider_et_accumuler(
                    liens_publiques, limite_nouvelles=min(CONFIG.rst_candidats_max, 40)
                )

            if nouvelles == 0 and not any(e.startswith(f"@{auteur}") for e in echecs_recherche):
                echecs_recherche.append(f"@{auteur} : aucune autre publication")
            publier(f"{len(candidats)} vidéo(s) réellement trouvée(s)", 30)

        # 2) Une recherche TikTok par nom du TOP N, dans l'ordre d'importance.
        # Chaque nom reçoit sa part du quota pour qu'aucun n'écrase les autres.
        part_par_nom = max(5, CONFIG.rst_candidats_max // max(1, len(noms))) if noms else 0
        for rang, nom in enumerate(noms, start=1):
            if len(candidats) >= CONFIG.rst_candidats_max:
                echecs_recherche.append(f"« {nom} » : quota de candidates déjà atteint")
                continue
            requetes_reelles.append(nom)
            progression = 30 + int(8 * rang / max(1, len(noms)))
            contexte.update(
                statut="searching", progress=progression,
                detail=f"Recherche TikTok {rang}/{len(noms)} : « {nom} »",
                noms=noms,
            )
            nouvelles = 0
            if not tikwm_recherche_bloquee:
                try:
                    await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                    resultats = await _donnees_tikwm(
                        session, "/feed/search", {"keywords": nom, "count": str(part_par_nom)}
                    )
                    nouvelles = await accumuler(
                        resultats.get("videos") or [], f"recherche « {nom} »", nom
                    )
                except ErreurTikwm403:
                    logger.warning("[RsT] TikWM /feed/search bloqué (403) : découverte publique pour « %s »", nom)
                    tikwm_recherche_bloquee = True
                except ErreurApp as exc:
                    logger.warning("[RsT] recherche « %s » indisponible : %s", nom, exc)
                    echecs_recherche.append(f"« {nom} » : {exc}")

            # TikWM bloqué (403) ou aucun résultat : la chaîne publique prend le relais.
            if tikwm_recherche_bloquee or nouvelles == 0:
                contexte.update(
                    statut="searching", progress=progression,
                    detail=f"Recherche publique {rang}/{len(noms)} : « {nom} »",
                    noms=noms,
                )
                liens_publiques = await _decouvrir_publique(
                    session, requete=nom, limite=part_par_nom, etat=etat_sources
                )
                nouvelles += await valider_et_accumuler(
                    liens_publiques, nom, limite_nouvelles=part_par_nom
                )

            if nouvelles == 0 and not any(e.startswith(f"« {nom} »") for e in echecs_recherche):
                echecs_recherche.append(f"« {nom} » : aucun résultat")
            publier(f"{len(candidats)} vidéo(s) réellement trouvée(s)", progression)

        # 3) Filet de sécurité : si le TOP N n'a rien ramené, on réessaie avec les mots-clés
        # bruts de la légende. Sans cela, une légende sans nom propre condamnait le travail.
        if not candidats:
            deja_tentees = {requete.casefold() for requete in requetes_reelles}
            replis = [
                mot for mot in _extraire_mots_cles_rst(
                    legende, script.get("mot_cle_broll", ""), limite=nombre_noms
                )
                if mot.casefold() not in deja_tentees
            ]
            for mot_cle in replis:
                requetes_reelles.append(mot_cle)
                contexte.update(
                    statut="searching", progress=39,
                    detail=f"Recherche élargie « {mot_cle} »", noms=noms,
                )
                nouvelles = 0
                if not tikwm_recherche_bloquee:
                    try:
                        await asyncio.sleep(1.0)
                        resultats = await _donnees_tikwm(
                            session, "/feed/search", {"keywords": mot_cle, "count": "20"}
                        )
                        nouvelles = await accumuler(
                            resultats.get("videos") or [], f"recherche élargie « {mot_cle} »"
                        )
                    except ErreurTikwm403:
                        logger.warning("[RsT] TikWM /feed/search bloqué (403) : découverte publique pour « %s »", mot_cle)
                        tikwm_recherche_bloquee = True
                    except ErreurApp as exc:
                        logger.warning("[RsT] recherche élargie « %s » indisponible : %s", mot_cle, exc)
                        echecs_recherche.append(f"« {mot_cle} » : {exc}")

                # TikWM bloqué (403) ou aucun résultat : la chaîne publique prend le relais.
                if tikwm_recherche_bloquee or nouvelles == 0:
                    liens_publiques = await _decouvrir_publique(
                        session, requete=mot_cle, limite=20, etat=etat_sources
                    )
                    nouvelles += await valider_et_accumuler(
                        liens_publiques, limite_nouvelles=20
                    )

                if nouvelles == 0 and not any(e.startswith(f"« {mot_cle} »") for e in echecs_recherche):
                    echecs_recherche.append(f"« {mot_cle} » : aucun résultat")
                publier(f"{len(candidats)} vidéo(s) réellement trouvée(s)", 40)
                if candidats:
                    break

    # Pertinence TEXTE des candidates : pour un sujet téléphone/produit, une vidéo
    # « voyage / skyline / Dubaï » sans aucun mot tech est jolie mais hors sujet —
    # elle passe derrière les titres proches du sujet, ou est écartée à la sélection.
    sujet_telephone = _sujet_telephone_rst(
        legende, script.get("mot_cle_broll", ""), " ".join(noms),
        script.get("hook", ""), script.get("corps", ""),
    )
    # Chaque nom du TOP N est représenté à tour de rôle avant de plafonner les
    # candidates ; dans chaque file de nom, les titres les plus pertinents ouvrent.
    trouves = _repartir_par_nom(
        _classer_candidats_rst(list(candidats.values()), sujet_telephone), noms
    )[: CONFIG.rst_candidats_max]
    contexte.update(
        statut="searching", progress=42,
        detail=f"{len(trouves)} vidéo(s) trouvée(s) — sélection des meilleures sources",
        found_videos=trouves, search_queries=requetes_reelles, noms=noms,
    )
    if not trouves:
        # Recherche vide : on dit exactement ce qui a été tenté et ce que ça a donné.
        if not requetes_reelles:
            raise ErreurApp(
                "RsT n'a pu construire aucune recherche : la légende de cette vidéo ne "
                "contient ni nom ni mot-clé exploitable. Choisis une vidéo de départ dont "
                "la description mentionne un sujet précis."
            )
        detail = " ; ".join(echecs_recherche[:6]) if echecs_recherche else "aucun résultat"
        raise ErreurApp(
            f"RsT n'a trouvé aucune autre vidéo TikTok exploitable. "
            f"{len(requetes_reelles)} recherche(s) tentée(s) — {detail}."
        )

    # Mode strict RsT : la vidéo de départ sert de référence de STYLE et aucun plan
    # hors sujet ne doit passer. Mieux vaut échouer clairement qu'un montage nul.
    configuration = _configuration_montage(
        requete.mode, RST_DUREE_MAX_PLAN, exiger_pertinence_visuelle=True
    )
    selectionnees, trouves = _selectionner_sources_rst(
        trouves, CONFIG.rst_sources_max, float(CONFIG.duree_max_source)
    )
    plafond_reel = max(
        90, min(PLAFOND_ESTIMATION_MONTAGE, int(contexte.restant()) - int(LIVRAISON_RESERVE) - 45)
    )
    try:
        duree_reference = float(seed_infos.get("duration") or 0)
    except (TypeError, ValueError):
        duree_reference = 0.0
    selectionnees = _reduire_selon_estimation(
        selectionnees, configuration, plafond_reel, duree_reference=duree_reference
    )
    # Une candidate n'est pas affichée comme « retenue » pendant que Gemini et
    # FFprobe valident encore ses plans. Le statut est publié provisoirement ;
    # `selected=True` n'est rétabli qu'après le montage réussi ci-dessous.
    for candidate in trouves:
        if candidate in selectionnees:
            candidate["selected"] = False
            candidate["validation_status"] = "awaiting_visual_ai"
    if not selectionnees:
        rejets = [str(c.get("rejet") or "raison inconnue") for c in trouves if not c.get("selected")]
        detail = " ; ".join(rejets[:6]) if rejets else "durées hors limites"
        raise ErreurApp(
            "Aucune vidéo trouvée n'est exploitable pour le montage. "
            f"Principaux rejets : {detail}."
        )
    noms_couverts = sorted({s["nom"] for s in selectionnees if s.get("nom")})
    budget_restant = max(0, int(contexte.restant()))
    contexte.update(
        statut="selecting", progress=44,
        detail=(
            f"Budget restant : {budget_restant} s — "
            f"{len(selectionnees)} candidate(s) en validation sur {len(trouves)} trouvée(s)"
            + (f" — {len(noms_couverts)}/{len(noms)} nom(s) couvert(s)" if noms else "")
        ),
        found_videos=trouves, search_queries=requetes_reelles, noms=noms,
        plafond_reel=plafond_reel,
    )

    resolution = "1080" if (requete.mode == "qualite" and CONFIG.autoriser_export_1080) else "720"
    resultat = await construire_montage_professionnel(
        liens=[candidate["url"] for candidate in selectionnees],
        # La vidéo de départ sert de RÉFÉRENCE DE STYLE (rythme, durée des plans,
        # sous-titres, transitions) — jamais de contenu : ses images, son logo et
        # son watermark ne sont pas réutilisés. Inaccessible ? Style par défaut.
        lien_reference=lien,
        reference_optionnelle=True,
        hook=script["hook"].strip(),
        corps=script["corps"].strip(),
        resolution=resolution,
        style_sous_titres="classique",
        config=configuration,
        rapporteur=_rapporteur_decale(contexte, 45.0, 0.45),
        resolveur=_resoudre_video_tiktok,
        telechargeur=_telecharger_fichier,
        appel_gemini=_appel_gemini_brut,
        intensite_transitions=requete.intensite_transitions,
        voix_off=_resoudre_voix_off(session_id, requete.voix_off),
    )
    # La construction a terminé toutes les validations visuelles nécessaires.
    for candidate in selectionnees:
        candidate["selected"] = True
        candidate["validation_status"] = "validated"
    await envoyer_script_et_video(script["hook"], script["corps"], resultat["path"])

    # Livraison séparée : la vidéo reste muette, le script et la voix off partent à côté.
    contexte.update(
        statut="delivering", progress=92,
        detail="Écriture du script et génération de la voix off",
    )
    # Le rendu est déjà terminé : on garde du temps pour Drive et la finalisation au
    # lieu de laisser edge-tts consommer toute la limite globale du job.
    budget_livraison = contexte.restant() - LIVRAISON_RESERVE
    try:
        livraison = await livrer_script_et_voix(
            script, seed_infos, noms, budget=budget_livraison
        )
    except Exception as exc:  # noqa: BLE001 - protection finale d'un rendu déjà prêt
        logger.exception("[RsT] livraison complémentaire impossible")
        livraison = {
            "livraison_erreur": f"Livraison du script et de la voix off impossible : {exc}",
            "script_url": "", "script_nom": "", "script_erreur": "",
            "voix_url": "", "voix_nom": "", "voix_erreur": "",
        }

    resultat.update(
        script=script, found_videos=trouves, search_queries=requetes_reelles,
        noms=noms, nombre_noms=nombre_noms, noms_couverts=noms_couverts,
        duree_max_plan=RST_DUREE_MAX_PLAN, **livraison,
    )
    return resultat



# ======================================================================================
# PROFILS D'ENTRAÎNEMENT VISUEL
# ======================================================================================

PROMPT_PROFIL_VISUEL = """Tu es un contrôleur visuel strict. Regarde toute la vidéo jointe et retourne
UNIQUEMENT un objet JSON valide. Sujet demandé : {sujet}. Alias autorisés : {alias}.
Le but est d'apprendre une signature visuelle, pas de deviner à partir du titre.
Schéma exact :
{{"sujet_visible":"", "sujet_correspond":true, "confiance":0.0,
"passages_propres":[{{"debut":0.0,"fin":5.0,"raison":""}}],
"personnes":[], "watermarks":[], "logos_ajoutes":[], "textes":[],
"sous_titres_tiktok":[], "qualite":"bonne", "nettete":0.0,
"signature_positive":"", "signature_negative":"", "valide":false,
"raison_refus":""}}
Règles non négociables : une personne, même partielle, un pseudo, sticker, watermark,
logo ajouté ou texte autre qu'un vrai sous-titre TikTok rend le passage invalide.
Règle du doute ABSOLUE : au moindre doute, un élément d'interdiction vaut « présent » et le
passage est refusé — le doute exclut, il ne sauve jamais. Remplis toujours tous les champs :
un champ absent sera traité comme une interdiction présente. Un texte n'est un vrai
sous-titre TikTok que s'il transcrit les paroles dites ; un titre, une légende, une phrase
écrite ou une typographie décorative doivent être refusés.
L'emblème physique normal du produit ou véhicule filmé n'est pas un logo ajouté.
Identifie visuellement le sujet exact et le modèle, ne te fie ni à la popularité ni à la légende.
Les signatures déjà apprises, si elles existent, sont des contraintes supplémentaires : positive = {signature_positive}, négative = {signature_negative}.
Les timestamps doivent rester dans la durée de la vidéo et ne retenir que des passages nets,
sans personne et sans overlay interdit. La confiance est un nombre entre 0 et 1."""


def _fichier_profils_session(session_id: str) -> Path:
    empreinte = hashlib.sha256(str(session_id or "anonyme").encode("utf-8")).hexdigest()
    return DOSSIER_PROFILS_ENTRAINEMENT / f"{empreinte}.json"


def _profils_session(session_id: str) -> list[dict[str, Any]]:
    chemin = _fichier_profils_session(session_id)
    try:
        donnees = json.loads(chemin.read_text(encoding="utf-8")) if chemin.exists() else []
        return donnees if isinstance(donnees, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Profils d'entraînement illisibles pour la session : %s", exc)
        return []


def _ecrire_profils_session(session_id: str, profils: list[dict[str, Any]]) -> None:
    chemin = _fichier_profils_session(session_id)
    temporaire = chemin.with_suffix(".tmp")
    temporaire.write_text(json.dumps(profils, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporaire, 0o600)
    temporaire.replace(chemin)
    os.chmod(chemin, 0o600)


def _liste_propre(valeur: Any, limite: int = 30) -> list[str]:
    if isinstance(valeur, str):
        valeurs = re.split(r"[\n,;]+", valeur)
    elif isinstance(valeur, (list, tuple)):
        valeurs = valeur
    else:
        valeurs = []
    resultat: list[str] = []
    vus: set[str] = set()
    for entree in valeurs:
        texte = re.sub(r"\s+", " ", str(entree or "").strip())
        if not texte or len(texte) > 200 or texte.casefold() in vus:
            continue
        vus.add(texte.casefold())
        resultat.append(texte)
        if len(resultat) >= limite:
            break
    return resultat


def _liens_propres_profil(valeur: Any, limite: int = 30) -> list[str]:
    resultat: list[str] = []
    vus: set[str] = set()
    valeurs = valeur if isinstance(valeur, list) else re.split(r"[\n,;]+", str(valeur or ""))
    for brut in valeurs:
        if not str(brut).strip():
            continue
        try:
            lien = normaliser_lien_tiktok(str(brut))
        except ErreurMontage:
            continue
        if lien not in vus:
            vus.add(lien)
            resultat.append(lien)
        if len(resultat) >= limite:
            break
    return resultat


def _profil_depuis_payload(payload: dict[str, Any], existant: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Normalise les noms français et anglais afin que le format persistant reste stable."""
    base = dict(existant or {})
    source = {**base, **(payload or {})}
    nom = str(source.get("nom_sujet") or source.get("subject") or source.get("subject_name") or source.get("name") or "").strip()
    if not nom:
        raise ErreurApp("Le nom du sujet est obligatoire pour un profil d'entraînement.")
    aliases = source.get("alias", source.get("aliases", source.get("alias_sujet", [])))
    bons = source.get("bons_exemples", source.get("good_examples", source.get("good_examples_tiktok", source.get("good", []))))
    mauvais = source.get("mauvais_exemples", source.get("bad_examples", source.get("bad_examples_tiktok", source.get("bad", []))))
    identifiant = str(source.get("id") or uuid.uuid4().hex).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identifiant):
        identifiant = uuid.uuid4().hex
    maintenant = time.time()
    return {
        "id": identifiant,
        "nom_sujet": nom,
        "alias": _liste_propre(aliases),
        "aliases": _liste_propre(aliases),
        "bons_exemples": _liens_propres_profil(bons),
        "mauvais_exemples": _liens_propres_profil(mauvais),
        "donnees": dict(source.get("donnees") or {}) if isinstance(source.get("donnees"), dict) else {},
        # Ces collections sont conservées lors d'une édition ou d'une nouvelle analyse.
        "sources_validees": list(source.get("sources_validees") or []) if isinstance(source.get("sources_validees"), list) else [],
        "sources_auto_ajoutees": list(source.get("sources_auto_ajoutees") or []) if isinstance(source.get("sources_auto_ajoutees"), list) else [],
        "updated_at": float(source.get("updated_at") or maintenant),
    }


def _profil_public(profil: dict[str, Any]) -> dict[str, Any]:
    resultat = dict(profil)
    resultat["aliases"] = list(profil.get("aliases") or profil.get("alias") or [])
    resultat["source_count"] = len(profil.get("sources_validees") or [])
    resultat["sources_automatiquement_ajoutees"] = len(profil.get("sources_auto_ajoutees") or [])
    return resultat


def _champ_bool(analysis: dict[str, Any], *cles: str) -> bool:
    return any(bool(analysis.get(cle)) for cle in cles)


def _score_confiance_profil(analysis: dict[str, Any]) -> float:
    try:
        return max(0.0, min(1.0, float(analysis.get("confiance", 0))))
    except (TypeError, ValueError):
        return 0.0


def _analyse_profil_valide(analysis: dict[str, Any], profil: dict[str, Any]) -> tuple[bool, str]:
    texte_sujet = str(analysis.get("sujet_visible") or "").casefold()
    termes = [profil.get("nom_sujet", ""), *(profil.get("alias") or [])]
    sujet_confirme = bool(analysis.get("sujet_correspond")) and any(
        terme and (terme.casefold() in texte_sujet or texte_sujet in terme.casefold()) for terme in termes
    )
    personnes = analysis.get("personnes") or analysis.get("personne_visible")
    watermarks = analysis.get("watermarks") or analysis.get("watermark")
    logos = analysis.get("logos_ajoutes") or analysis.get("logo_visible")
    textes = analysis.get("textes") or analysis.get("texte_interdit")
    try:
        nettete = float(analysis.get("nettete", 0))
    except (TypeError, ValueError):
        nettete = 0.0
    qualite = str(analysis.get("qualite", "")).casefold()
    propre = qualite in {"bonne", "excellent", "excellente", "professionnelle"} and nettete >= 0.65
    if not sujet_confirme:
        return False, "sujet différent ou sujet exact non confirmé"
    if personnes:
        return False, "personne visible"
    if watermarks:
        return False, "watermark détecté"
    if logos:
        return False, "logo ajouté détecté"
    if textes:
        return False, "texte ou sticker interdit détecté"
    if not propre:
        return False, "qualité ou netteté insuffisante"
    if _score_confiance_profil(analysis) < 0.65:
        return False, "confiance IA insuffisante"
    passages = analysis.get("passages_propres") or []
    if not passages:
        return False, "aucun passage propre horodaté"
    return True, "source visuellement validée"


async def analyser_exemple_entrainement(
    session: aiohttp.ClientSession,
    url: str,
    profil: dict[str, Any],
    *,
    est_bon: bool = True,
) -> dict[str, Any]:
    """Télécharge un exemple, le fait regarder par Gemini et conserve tous ses signaux."""
    url = normaliser_lien_tiktok(url)
    direct = await _resoudre_video_tiktok(session, url)
    with tempfile.TemporaryDirectory(prefix="profil-", dir=str(DOSSIER_TRAVAIL)) as dossier_str:
        dossier = Path(dossier_str)
        source = dossier / "source.mp4"
        await _telecharger_fichier(session, direct, source)
        infos = await sonder_video(source, timeout=min(30, CONFIG.delai_ffmpeg))
        config = _configuration_montage("qualite", RST_DUREE_MAX_PLAN, True)
        rapporteur = Rapporteur(lambda *_args, **_kwargs: None, lambda: False, lambda: 1e9)
        apercu = dossier / "apercu.mp4"
        await creer_apercu(source, apercu, float(infos["duration"]), config, rapporteur)
        contenu = await asyncio.to_thread(apercu.read_bytes)
        parts = [{"inline_data": {"mime_type": "video/mp4", "data": base64.b64encode(contenu).decode("ascii")}}, {
            "text": PROMPT_PROFIL_VISUEL.format(
                sujet=profil["nom_sujet"], alias=json.dumps(profil.get("alias") or [], ensure_ascii=False),
                signature_positive=json.dumps((profil.get("donnees") or {}).get("signature_positive", []), ensure_ascii=False),
                signature_negative=json.dumps((profil.get("donnees") or {}).get("signature_negative", []), ensure_ascii=False),
            )
        }]
        brut = await asyncio.wait_for(
            _appel_gemini_brut(parts, temperature=0.1, json_mode=True),
            timeout=CONFIG.delai_gemini,
        )
    objet = _parser_json(brut)
    if not isinstance(objet, dict):
        raise ErreurApp("Analyse du profil invalide : Gemini n'a pas renvoyé un objet JSON.")
    objet.setdefault("sujet_visible", "")
    objet.setdefault("sujet_correspond", False)
    objet.setdefault("confiance", 0.0)
    objet.setdefault("passages_propres", [])
    objet.setdefault("personnes", [])
    objet.setdefault("watermarks", [])
    objet.setdefault("logos_ajoutes", [])
    objet.setdefault("textes", [])
    objet.setdefault("sous_titres_tiktok", [])
    objet.setdefault("signature_positive", "")
    objet.setdefault("signature_negative", "")
    objet["url"] = url
    objet["duration"] = infos["duration"]
    objet["est_bon_exemple"] = est_bon
    valide, raison = _analyse_profil_valide(objet, profil)
    objet["valide"] = valide
    objet["raison_refus"] = "" if valide else raison
    if valide and not str(objet.get("signature_positive") or "").strip():
        objet["signature_positive"] = (
            f"Sujet exact « {objet.get('sujet_visible', profil['nom_sujet'])} », "
            "passages propres, sans personne ni overlay interdit, qualité confirmée."
        )
    if not valide and not str(objet.get("signature_negative") or "").strip():
        objet["signature_negative"] = raison
    # Les timestamps sont bornés et triés avant d'être persistés.
    passages = []
    for passage in objet.get("passages_propres") or []:
        try:
            debut, fin = max(0.0, float(passage.get("debut", 0))), min(float(infos["duration"]), float(passage.get("fin", 0)))
        except (AttributeError, TypeError, ValueError):
            continue
        if fin > debut and fin - debut <= RST_DUREE_MAX_PLAN:
            passages.append({**passage, "debut": round(debut, 3), "fin": round(fin, 3)})
    objet["passages_propres"] = passages
    return objet


async def _candidates_profil(session: aiohttp.ClientSession, profil: dict[str, Any], limite: int = 10) -> list[dict[str, Any]]:
    candidats: dict[str, dict[str, Any]] = {}
    requetes = _liste_propre([profil["nom_sujet"], *(profil.get("alias") or [])], 10)
    for requete in requetes:
        try:
            donnees = await _donnees_tikwm(session, "/feed/search", {"keywords": requete, "count": str(max(5, limite // max(1, len(requetes))) )})
        except Exception as exc:  # noqa: BLE001
            logger.info("Recherche de profil indisponible pour %s : %s", requete, exc)
            continue
        for video in donnees.get("videos") or []:
            candidat = _normaliser_candidat_rst(video, f"entraînement « {requete} »", requete)
            if candidat and candidat["video_id"] not in candidats:
                candidats[candidat["video_id"]] = candidat
            if len(candidats) >= limite:
                break
        if len(candidats) >= limite:
            break
    return list(candidats.values())


async def analyser_profil_entrainement(profil: dict[str, Any]) -> dict[str, Any]:
    """Analyse les exemples positifs/négatifs puis enrichit le profil avec des sources validées."""
    profil = _profil_depuis_payload(profil, profil)
    exemples: list[dict[str, Any]] = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90, connect=15)) as session:
        for url in profil["bons_exemples"]:
            try:
                exemples.append(await analyser_exemple_entrainement(session, url, profil, est_bon=True))
            except Exception as exc:  # garder le profil complet même si un exemple échoue
                exemples.append({"url": url, "est_bon_exemple": True, "valide": False, "raison_refus": str(exc)})
        for url in profil["mauvais_exemples"]:
            try:
                exemples.append(await analyser_exemple_entrainement(session, url, profil, est_bon=False))
            except Exception as exc:
                exemples.append({"url": url, "est_bon_exemple": False, "valide": False, "raison_refus": str(exc)})

        profil.setdefault("donnees", {})["exemples_analyses"] = exemples
        profil["donnees"]["signature_positive"] = [e.get("signature_positive", "") for e in exemples if e.get("est_bon_exemple") and e.get("valide")]
        profil["donnees"]["signature_negative"] = [
            e.get("signature_negative") or e.get("raison_refus", "")
            for e in exemples if not e.get("est_bon_exemple") or not e.get("valide")
        ]
        profil["donnees"]["confiance_moyenne"] = round(
            sum(_score_confiance_profil(e) for e in exemples) / max(1, len(exemples)), 3
        )
        profil["donnees"]["sources_exemples_analysees"] = len(exemples)

        existantes = {str(source.get("video_id") or source.get("url", "")).strip() for source in profil.get("sources_validees", [])}
        automatiques: list[dict[str, Any]] = []
        for candidat in await _candidates_profil(session, profil, limite=10):
            if candidat["video_id"] in existantes:
                continue
            try:
                analyse = await analyser_exemple_entrainement(session, candidat["url"], profil, est_bon=True)
            except Exception as exc:
                candidat.update(valide=False, raison_refus=str(exc), validation_status="rejected")
                continue
            if analyse.get("valide"):
                candidat.update(
                    validation_status="validated", validation=analyse,
                    passages_propres=analyse.get("passages_propres", []),
                    video_id=candidat["video_id"],
                )
                automatiques.append(candidat)
                existantes.add(candidat["video_id"])
            else:
                # Le refus reste visible sur la candidate elle-même : la raison exacte
                # est conservée avec l'analyse complète dans l'historique du profil.
                candidat.update(
                    valide=False, validation_status="rejected",
                    raison_refus=str(analyse.get("raison_refus") or "validation visuelle non concluante"),
                )
            # Les refus ne sont pas ajoutés aux sources utilisables, mais leur analyse
            # reste dans l'historique afin de préserver les données du profil.
            profil["donnees"].setdefault("candidates_analysees", []).append({
                **candidat, "validation": analyse,
            })
        profil.setdefault("sources_validees", []).extend(automatiques)
        profil["sources_auto_ajoutees"] = automatiques
        profil["donnees"]["nombre_sources_auto_ajoutees"] = len(automatiques)
        profil["donnees"]["termine_le"] = time.time()
    profil["updated_at"] = time.time()
    return profil



# ======================================================================================
# SsT : noms saisis manuellement, recherche indépendante et référence de montage
# ======================================================================================

PROMPT_ADAPTATION_SST = """Réécris le script de cette vidéo TikTok pour une vidéo TOP {nombre}.
Les seuls sujets autorisés sont exactement ceux de cette liste : {noms}.
Tu ne dois supprimer, remplacer, corriger, traduire, compléter ou inventer aucun nom.
N'ajoute aucun autre nom propre. Garde le rythme et l'intention de la source.
Retourne uniquement un JSON avec les clés hook, corps et mot_cle_broll.
Le hook doit être court ; le corps doit contenir chaque nom autorisé au moins une fois.
Source : {source}"""


def _noms_sst_valides(noms: Any, nombre: Any) -> list[str]:
    try:
        attendu = int(nombre)
    except (TypeError, ValueError):
        raise ErreurApp("SsT accepte uniquement TOP 3 ou TOP 5.")
    if attendu not in RST_NOMS_CHOIX:
        raise ErreurApp("SsT accepte uniquement TOP 3 ou TOP 5.")
    resultat = [re.sub(r"\s+", " ", str(nom or "").strip()) for nom in (noms or [])]
    if len(resultat) != attendu or any(not nom or len(nom) > 80 for nom in resultat):
        raise ErreurApp(f"SsT exige exactement {attendu} noms différents saisis manuellement.")
    if len({nom.casefold() for nom in resultat}) != attendu:
        raise ErreurApp("SsT exige des noms tous différents ; l'IA ne les remplacera pas.")
    return resultat


async def adapter_script_sst(source: str, noms: list[str]) -> dict[str, str]:
    """Adapte un texte sans jamais déléguer le choix des noms à l'IA."""
    prompt = PROMPT_ADAPTATION_SST.format(
        nombre=len(noms), noms=json.dumps(noms, ensure_ascii=False), source=source[:6000]
    )
    try:
        brut = await _appel_gemini_brut([{"text": prompt}], temperature=0.2, json_mode=True)
        resultat = _parser_json(brut)
        if not isinstance(resultat, dict):
            raise ErreurApp("Réponse SsT non exploitable.")
        hook = str(resultat.get("hook") or "").strip()
        corps = str(resultat.get("corps") or "").strip()
        mot_cle = str(resultat.get("mot_cle_broll") or "").strip() or "sujet principal"
        if not hook or not corps:
            raise ErreurApp("Réponse SsT incomplète.")
    except Exception as exc:  # repli déterministe, sans création de nom
        logger.warning("Adaptation SsT IA indisponible, repli contrôlé : %s", exc)
        hook = f"TOP {len(noms)} : {noms[0]} et les autres"
        corps = "Voici les sujets demandés : " + ", ".join(noms) + ". " + source[:500]
        mot_cle = noms[0]
    # Une réponse IA qui oublierait un nom est complétée avec les noms fournis, jamais
    # avec des noms extraits ou déduits. Cela rend le contrat SsT vérifiable côté serveur.
    manquants = [nom for nom in noms if nom.casefold() not in f"{hook} {corps}".casefold()]
    if manquants:
        corps = f"{corps.rstrip('.')} . " + " . ".join(manquants)
    return {"hook": hook, "corps": corps, "mot_cle_broll": mot_cle}


def _selectionner_sources_sst(candidats: list[dict[str, Any]], noms: list[str], limite: int, duree_max: float) -> list[dict[str, Any]]:
    """Sélectionne en tours équilibrés sans annoncer une source avant la validation IA."""
    ordonnes = _repartir_par_nom(candidats, noms)
    retenues: list[dict[str, Any]] = []
    vus_noms: set[str] = set()
    for candidat in ordonnes:
        candidat["selected"] = False
        candidat["validation_status"] = "awaiting_visual_ai"
        try:
            duree = float(candidat.get("duration") or 0)
        except (TypeError, ValueError):  # durée absente ou invalide : jamais retenue
            duree = 0.0
        if duree <= 0:
            candidat["rejet"] = "durée inconnue ou invalide"
            candidat["validation_status"] = "rejected_before_ai"
            continue
        if duree < DUREE_MIN_SOURCE_RST:
            candidat["rejet"] = f"durée {duree:.0f} s trop courte"
            candidat["validation_status"] = "rejected_before_ai"
            continue
        if duree > duree_max:
            candidat["rejet"] = f"durée {duree:.0f} s au-delà de la limite"
            candidat["validation_status"] = "rejected_before_ai"
            continue
        # Un tour complet par nom est prioritaire. Les tours suivants complètent le quota.
        cle = str(candidat.get("nom") or "").casefold()
        if len(retenues) < limite and (cle not in vus_noms or len(vus_noms) >= len(noms)):
            retenues.append(candidat)
            vus_noms.add(cle)
        else:
            candidat["rejet"] = "quota équilibré atteint"
            candidat["validation_status"] = "rejected_before_ai"
    # Si une recherche n'a aucun résultat, remplir avec les noms qui ont encore des vidéos.
    for candidat in ordonnes:
        if len(retenues) >= limite:
            break
        if candidat in retenues or candidat.get("rejet"):
            continue
        retenues.append(candidat)
    return retenues


async def _produire_sst(requete: "RequeteSst", contexte: "ContexteJob", session_id: str = "") -> dict[str, Any]:
    lien = normaliser_lien_tiktok(requete.lien.strip())
    noms = _noms_sst_valides(requete.noms, requete.nombre_noms)
    profil = None
    if requete.profil_id:
        profil = next((item for item in _profils_session(session_id) if item.get("id") == requete.profil_id), None)
        if profil is None:
            raise ErreurApp("Le profil d'entraînement sélectionné n'existe plus dans cette session.")
    contexte.update(statut="analysing", progress=4, detail="Lecture de la vidéo source SsT", noms_saisis=noms, profil_id=requete.profil_id)
    # Trace honnête de la recherche : chaque échec TikWM et public est conservé pour
    # expliquer un résultat vide sans rien inventer — même principe que RsT.
    echecs_tikwm: list[str] = []
    echecs_publiques: list[str] = []

    def deja_trace(echecs: list[str], nom: str) -> bool:
        return any(echec.startswith(f"« {nom} »") for echec in echecs)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90, connect=15)) as session:
        donnees = await _donnees_tikwm(session, "/", {"url": lien, "hd": "1"})
        source = str(donnees.get("title") or "").strip()
        if not source:
            raise ErreurApp("La vidéo source SsT ne possède pas de texte exploitable.")
        contexte.update(statut="analysing", progress=14, detail="Adaptation du script aux noms saisis", noms_saisis=noms)
        script = await adapter_script_sst(source, noms)
        contexte.update(statut="searching", progress=20, detail="Recherche indépendante du nom 1", script=script, noms_saisis=noms)
        candidats: dict[str, dict[str, Any]] = {}
        recherches: list[str] = []
        quota_par_nom = max(5, CONFIG.rst_candidats_max // len(noms))
        identifiant_source = str(donnees.get("id") or donnees.get("video_id") or "")
        # Constaté en production (Render) : TikWM /feed/search peut répondre 403 ou
        # vide depuis certaines IP de serveur alors que l'endpoint unitaire /api/
        # fonctionne encore. SsT utilise donc la même chaîne de secours publique que
        # RsT : moteurs publics, relais publics, archive web, miroir — sans clé.
        etat_sources = EtatSourcesDecouverte()
        tikwm_recherche_bloquee = False

        def accumuler(video: Any, origine: str, nom: str) -> bool:
            """Ajoute une vidéo réellement renvoyée par TikWM, sauf doublon d'identifiant."""
            candidat = _normaliser_candidat_rst(video, origine, nom)
            if not candidat or candidat["video_id"] == identifiant_source:
                return False
            # Doublon TikTok : même identifiant vidéo déjà enregistré, jamais deux fois.
            if candidat["video_id"] in candidats:
                return False
            # Rien n'est annoncé « retenu » avant la validation visuelle Gemini.
            candidat["selected"] = False
            candidat["validation_status"] = "awaiting_visual_ai"
            candidats[candidat["video_id"]] = candidat
            return True

        async def revalider_lien_public(entree: dict, nom: str) -> bool:
            """Revalide un lien découvert via TikWM /api/ : identifiant, auteur, titre
            et durée restent réels — aucune métadonnée n'est jamais inventée."""
            try:
                await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                donnees_v = await _donnees_tikwm(session, "/", {"url": entree["url"], "hd": "1"})
            except Exception as exc:  # noqa: BLE001 — ce lien public n'est pas exploitable
                echecs_tikwm.append(f"« {nom} » : revalidation TikWM /api/ de {entree.get('url')} — {exc}")
                return False
            return accumuler(donnees_v, str(entree.get("origin") or f"découverte publique « {nom} »"), nom)

        # Chaque nom saisi déclenche sa propre recherche : aucun nom n'est remplacé,
        # complété ou cherché « à la place » par un sujet général.
        for rang, nom in enumerate(noms, 1):
            recherches.append(nom)
            progression = 20 + int(22 * rang / len(noms))
            contexte.update(
                statut="searching", progress=progression,
                detail=f"Recherche indépendante {rang}/{len(noms)} : « {nom} »",
                search_queries=recherches, recherches=recherches, noms_saisis=noms,
            )
            nouvelles = 0
            if tikwm_recherche_bloquee:
                echecs_tikwm.append(f"« {nom} » : TikWM /feed/search déjà bloqué (403) pour cette IP")
            else:
                try:
                    await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                    resultats = await _donnees_tikwm(session, "/feed/search", {"keywords": nom, "count": str(quota_par_nom)})
                    for video in resultats.get("videos") or []:
                        if accumuler(video, f"recherche SsT « {nom} »", nom):
                            nouvelles += 1
                except ErreurTikwm403:
                    logger.warning("[SsT] TikWM /feed/search bloqué (403) : découverte publique pour « %s »", nom)
                    tikwm_recherche_bloquee = True
                    echecs_tikwm.append(f"« {nom} » : TikWM /feed/search bloqué (403)")
                except Exception as exc:  # une recherche isolée ne remplace jamais un nom
                    logger.info("Recherche SsT indisponible pour %s : %s", nom, exc)
                    echecs_tikwm.append(f"« {nom} » : TikWM /feed/search — {exc}")

            # /feed/search bloqué (403) ou sans résultat : la chaîne publique prend le
            # relais POUR CE NOM, séparément — jamais un sujet général à sa place.
            if nouvelles == 0:
                contexte.update(
                    statut="searching", progress=progression,
                    detail=f"Recherche publique {rang}/{len(noms)} : « {nom} »",
                    search_queries=recherches, recherches=recherches, noms_saisis=noms,
                )
                try:
                    liens_publiques = await _decouvrir_publique(
                        session, requete=nom, limite=quota_par_nom, etat=etat_sources
                    )
                except Exception as exc:  # noqa: BLE001
                    echecs_publiques.append(f"« {nom} » : sources publiques — {exc}")
                    liens_publiques = []
                if not liens_publiques and not deja_trace(echecs_publiques, nom):
                    detail_bloquees = (
                        f" (sources bloquées : {', '.join(sorted(etat_sources.bloquees))})"
                        if etat_sources.bloquees else ""
                    )
                    echecs_publiques.append(
                        f"« {nom} » : aucune source publique n'a donné de lien{detail_bloquees}"
                    )
                for entree in liens_publiques[:quota_par_nom]:
                    if await revalider_lien_public(entree, nom):
                        nouvelles += 1
            if nouvelles == 0 and not deja_trace(echecs_tikwm + echecs_publiques, nom):
                echecs_publiques.append(f"« {nom} » : aucun résultat après TikWM et sources publiques")
            contexte.update(
                statut="searching", progress=progression,
                detail=f"{len(candidats)} vidéo(s) réellement trouvée(s)",
                found_videos=list(candidats.values()),
                search_queries=recherches, recherches=recherches, noms_saisis=noms,
            )
        trouves = _repartir_par_nom(list(candidats.values()), noms)[: CONFIG.rst_candidats_max]
        contexte.update(
            statut="selecting", progress=45,
            detail=f"{len(trouves)} candidate(s) trouvée(s) — validation visuelle IA obligatoire",
            found_videos=trouves, search_queries=recherches, recherches=recherches, noms_saisis=noms,
        )
        selectionnees = _selectionner_sources_sst(trouves, noms, CONFIG.rst_sources_max, float(CONFIG.duree_max_source))
        if not selectionnees:
            # Échec expliqué : noms recherchés, recherches tentées, erreurs TikWM,
            # erreurs des sources publiques et raisons de rejet — rien n'est inventé.
            rejets = [
                f"{candidate.get('author') or candidate.get('url') or 'candidate'} : "
                f"{candidate.get('rejet') or 'raison inconnue'}"
                for candidate in trouves if candidate.get("rejet")
            ]
            raise ErreurApp(
                "SsT n'a trouvé aucune source exploitable pour les noms saisis. "
                f"Noms recherchés : {', '.join(noms)}. "
                f"Recherches tentées : {', '.join(recherches) if recherches else 'aucune'}. "
                f"Erreurs TikWM : {' ; '.join(echecs_tikwm[:8]) if echecs_tikwm else 'aucune'}. "
                f"Erreurs des sources publiques : {' ; '.join(echecs_publiques[:8]) if echecs_publiques else 'aucune'}. "
                f"Raisons de rejet : {' ; '.join(rejets[:8]) if rejets else 'aucune candidate trouvée'}."
            )
        configuration = _configuration_montage(requete.mode, RST_DUREE_MAX_PLAN, True)
        # SsT applique la même réduction que RsT : la vidéo source sert de référence,
        # son coût est donc compté dans l’estimation. La répartition équitable par nom
        # établie plus haut est conservée telle quelle : on retire les dernières
        # candidates, on ne rééquilibre jamais en défavorisant un nom saisi.
        try:
            duree_reference = float(donnees.get("duration") or 0)
        except (TypeError, ValueError):
            duree_reference = 0.0
        plafond_reel = max(
            90,
            min(PLAFOND_ESTIMATION_MONTAGE, int(contexte.restant()) - int(LIVRAISON_RESERVE) - 45),
        )
        selectionnees = _reduire_selon_estimation(
            selectionnees, configuration, plafond_reel, duree_reference=duree_reference
        )
        if not selectionnees:
            raise ErreurApp(
                "SsT n'a pas pu retenir une source dans la limite de temps du rendu. "
                f"Noms recherchés : {', '.join(noms)}. Relance avec une source plus courte."
            )
        contexte.update(
            statut="selecting", progress=47,
            detail=(
                f"Budget restant : {max(0, int(contexte.restant()))} s — "
                f"{len(selectionnees)} source(s) après réduction sous la limite de temps"
            ),
            found_videos=trouves, search_queries=recherches, recherches=recherches,
            noms_saisis=noms, plafond_reel=plafond_reel,
        )
        resultat = await construire_montage_professionnel(
            liens=[candidate["url"] for candidate in selectionnees],
            lien_reference=lien,
            reference_optionnelle=False,
            hook=script["hook"], corps=script["corps"], resolution="720",
            style_sous_titres="classique", config=configuration,
            rapporteur=_rapporteur_decale(contexte, 48.0, 0.45),
            resolveur=_resoudre_video_tiktok, telechargeur=_telecharger_fichier,
            appel_gemini=_appel_gemini_brut,
            intensite_transitions=requete.intensite_transitions,
            voix_off=_resoudre_voix_off(session_id, requete.voix_off),
        )
    # Le statut « retenue » n'apparaît qu'après le téléchargement, l'analyse IA,
    # le montage et le FFprobe final. Une candidate rejetée n'est jamais affichée comme retenue.
    urls_validees = {str(item.get("url")) for item in resultat.get("sources") or [] if item.get("status") in {"analysed", "downloaded"}}
    for candidate in trouves:
        if candidate in selectionnees and (not urls_validees or candidate["url"] in urls_validees):
            candidate.update(selected=True, validation_status="validated")
        elif candidate.get("validation_status") == "awaiting_visual_ai":
            candidate.update(
                selected=False, validation_status="rejected_by_visual_ai",
                # Une raison déjà tracée reste la vraie : durée hors limites, limite de
                # temps Render… Le statut IA ne doit jamais écraser un motif honnête.
                rejet=candidate.get("rejet") or "validation visuelle IA non concluante",
            )
    resultat.update(
        script=script, found_videos=trouves, noms_saisis=noms, noms=noms,
        nombre_noms=len(noms), search_queries=recherches, profile_id=requete.profil_id,
        source_video_reference=lien, source_video_used_only_as_reference=True,
    )
    return resultat


# ======================================================================================
# CONNEXIONS TIKTOK & GOOGLE DRIVE (OAuth 2.0)
# ======================================================================================

# Les jetons restent côté serveur et ne sont jamais exposés au navigateur. Sur une instance
# Render unique, ce stockage mémoire est volontairement simple ; un redémarrage demandera une
# reconnexion, sans conserver de secret dans le dépôt ou sur le disque éphémère.
SESSIONS_INTEGRATIONS: dict[str, dict[str, Any]] = {}
ETATS_OAUTH: dict[str, dict[str, Any]] = {}
DUREE_VIE_ETAT_OAUTH = 10 * 60


def _url_publique(request: Request) -> str:
    return (CONFIG.url_publique or str(request.base_url)).rstrip("/")


def _session_id(request: Request) -> tuple[str, bool]:
    existant = request.cookies.get("creator_session", "")
    if re.fullmatch(r"[a-f0-9]{48}", existant):
        SESSIONS_INTEGRATIONS.setdefault(existant, {})
        return existant, False
    nouveau = secrets.token_hex(24)
    SESSIONS_INTEGRATIONS[nouveau] = {}
    return nouveau, True


def _poser_cookie(response, session_id: str, request: Request) -> None:
    response.set_cookie(
        "creator_session", session_id, max_age=30 * 24 * 3600, httponly=True,
        secure=_url_publique(request).startswith("https://"), samesite="lax", path="/",
    )


def _nouvel_etat_oauth(session_id: str, service: str) -> str:
    maintenant = time.time()
    for cle, valeur in list(ETATS_OAUTH.items()):
        if maintenant - valeur["created_at"] > DUREE_VIE_ETAT_OAUTH:
            ETATS_OAUTH.pop(cle, None)
    etat = secrets.token_urlsafe(32)
    ETATS_OAUTH[etat] = {"session_id": session_id, "service": service, "created_at": maintenant}
    return etat


def _consommer_etat_oauth(etat: str, service: str) -> str:
    donnees = ETATS_OAUTH.pop(etat, None)
    if not donnees or donnees["service"] != service or time.time() - donnees["created_at"] > DUREE_VIE_ETAT_OAUTH:
        raise HTTPException(400, "Connexion expirée ou invalide. Recommence depuis le tableau de bord.")
    return donnees["session_id"]


async def _requete_json(method: str, url: str, **kwargs) -> dict[str, Any]:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        async with session.request(method, url, **kwargs) as response:
            try:
                donnees = await response.json(content_type=None)
            except (json.JSONDecodeError, aiohttp.ContentTypeError) as exc:
                raise ErreurApp(f"Le service externe a renvoyé une réponse illisible ({response.status}).") from exc
            if response.status >= 400:
                erreur = donnees.get("error")
                message_erreur = erreur.get("message") if isinstance(erreur, dict) else erreur
                detail = donnees.get("error_description") or message_erreur or donnees.get("message")
                raise ErreurApp(str(detail or f"Service externe indisponible ({response.status})"))
            return donnees


async def _jeton_google_valide(integration: dict[str, Any]) -> str:
    if integration.get("access_token") and integration.get("expires_at", 0) > time.time() + 60:
        return integration["access_token"]
    refresh = integration.get("refresh_token")
    if not refresh:
        raise ErreurApp("La connexion Google Drive a expiré. Reconnecte Google Drive.")
    donnees = await _requete_json(
        "POST", "https://oauth2.googleapis.com/token",
        data={"client_id": CONFIG.google_client_id, "client_secret": CONFIG.google_client_secret,
              "refresh_token": refresh, "grant_type": "refresh_token"},
    )
    integration["access_token"] = donnees["access_token"]
    integration["expires_at"] = time.time() + int(donnees.get("expires_in", 3600))
    return integration["access_token"]


async def _sauvegarder_sur_drive(session_id: str, chemin: Path) -> dict[str, str]:
    """Envoie le fichier depuis le disque : aucun ``read_bytes`` de la vidéo en RAM."""
    integration = SESSIONS_INTEGRATIONS.get(session_id, {}).get("google_drive")
    if not integration:
        raise ErreurApp("Connecte Google Drive avant de sauvegarder la vidéo.")
    chemin = chemin.resolve()
    if chemin.parent != DOSSIER_VIDEOS.resolve() or not chemin.is_file():
        raise ErreurApp("Fichier vidéo Drive invalide ou expiré.")
    jeton = await _jeton_google_valide(integration)
    metadata = {"name": chemin.name, "description": "Créée avec ς੮ ς८Րɿƿ੮"}
    with chemin.open("rb") as flux:
        formulaire = aiohttp.FormData()
        formulaire.add_field("metadata", json.dumps(metadata), content_type="application/json; charset=UTF-8")
        formulaire.add_field("file", flux, filename=chemin.name, content_type="video/mp4")
        donnees = await _requete_json(
            "POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,name,webViewLink",
            headers={"Authorization": f"Bearer {jeton}"}, data=formulaire,
            timeout=aiohttp.ClientTimeout(total=120, connect=15, sock_read=100),
        )
    return {"id": donnees["id"], "name": donnees.get("name", chemin.name),
            "url": donnees.get("webViewLink", f"https://drive.google.com/open?id={donnees['id']}")}


# ======================================================================================
# API
# ======================================================================================

app = FastAPI(title="ς੮ ς८Րɿƿ੮ — Studio vidéo")
app.mount("/static", StaticFiles(directory=str(RACINE / "static")), name="static")
app.mount("/videos", StaticFiles(directory=str(DOSSIER_VIDEOS)), name="videos")


class RequeteAnalyser(BaseModel):
    lien: str = Field(min_length=1, max_length=2048)
    idempotency_key: str = Field(default="", max_length=80)


class RequeteReference(BaseModel):
    lien: str = Field(min_length=1, max_length=2048)
    idempotency_key: str = Field(default="", max_length=80)


class RequeteVideo(BaseModel):
    hook: str = Field(min_length=1, max_length=4000)
    corps: str = Field(min_length=1, max_length=12000)
    mot_cle_broll: str = Field(min_length=1, max_length=200)
    style: str = Field(default="classique", max_length=32)
    mode: str = Field(default="qualite", pattern="^(rapide|qualite)$")
    voix_off: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=80)


class RequeteMontage(BaseModel):
    titre: str = Field(default="", max_length=120)
    hook: str = Field(min_length=1, max_length=4000)
    corps: str = Field(min_length=1, max_length=12000)
    liens_videos: list[str] = Field(min_length=1, max_length=20)
    lien_reference_style: str = Field(default="", max_length=2048)
    style: str = Field(default="classique", max_length=32)
    resolution: str = Field(default="720", pattern="^(720|1080)$")
    mode: str = Field(default="qualite", pattern="^(rapide|qualite)$")
    intensite_transitions: int = Field(default=2, ge=0, le=3)
    voix_off: str = Field(default="", max_length=64)
    estimated_seconds: int = Field(default=0, ge=0, le=7200)
    accepter_risque: bool = False
    idempotency_key: str = Field(default="", max_length=80)


class RequeteRst(BaseModel):
    """Mode RsT : un seul lien TikTok de départ suffit."""
    titre: str = Field(default="", max_length=120)
    lien: str = Field(min_length=1, max_length=2048)
    mode: str = Field(default="qualite", pattern="^(rapide|qualite)$")
    intensite_transitions: int = Field(default=2, ge=0, le=3)
    # Pipeline TOP N : 3 ou 5 noms extraits de la vidéo de départ, rien d'autre.
    nombre_noms: int = Field(default=RST_NOMS_DEFAUT)
    voix_off: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=80)

    @field_validator("nombre_noms")
    @classmethod
    def _valider_nombre_noms(cls, valeur: int) -> int:
        if valeur not in RST_NOMS_CHOIX:
            attendus = " ou ".join(str(choix) for choix in RST_NOMS_CHOIX)
            raise ValueError(f"nombre_noms doit valoir {attendus}.")
        return valeur


class RequeteSst(BaseModel):
    """SsT : une vidéo de départ, puis 3 ou 5 noms saisis par l'utilisateur.

    Contrairement à RsT, aucun nom n'est extrait ou ajouté par l'IA. La validation
    ci-dessous est volontairement stricte afin qu'une recherche ne puisse jamais
    remplacer silencieusement un nom demandé par un sujet populaire voisin.
    """
    titre: str = Field(default="", max_length=120)
    lien: str = Field(min_length=1, max_length=2048)
    nombre_noms: int = Field(default=3)
    noms: list[str] = Field(default_factory=list, min_length=0, max_length=5)
    # Alias accepté pour les clients qui reprennent le nom affiché dans le tracker.
    noms_saisis: list[str] = Field(default_factory=list, min_length=0, max_length=5)
    profil_id: str = Field(default="", max_length=80)
    mode: str = Field(default="qualite", pattern="^(rapide|qualite)$")
    intensite_transitions: int = Field(default=2, ge=0, le=3)
    voix_off: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def _valider_noms_saisis(self):
        if self.nombre_noms not in RST_NOMS_CHOIX:
            raise ValueError("nombre_noms doit valoir 3 ou 5.")
        noms_bruts = self.noms or self.noms_saisis
        noms = [re.sub(r"\s+", " ", str(nom).strip()) for nom in noms_bruts]
        if len(noms) != self.nombre_noms or any(not nom or len(nom) > 80 for nom in noms):
            raise ValueError(f"SsT exige exactement {self.nombre_noms} noms non vides.")
        if len({nom.casefold() for nom in noms}) != len(noms):
            raise ValueError("SsT exige des noms tous différents.")
        self.noms = noms
        self.noms_saisis = noms
        return self


class RequeteProfilEntrainement(BaseModel):
    """Données persistées d'un profil visuel, sans nom de sujet codé en dur."""
    id: str = Field(default="", max_length=80)
    nom_sujet: str = Field(min_length=1, max_length=160)
    alias: list[str] = Field(default_factory=list, max_length=30)
    bons_exemples: list[str] = Field(default_factory=list, max_length=30)
    mauvais_exemples: list[str] = Field(default_factory=list, max_length=30)
    donnees: dict[str, Any] = Field(default_factory=dict)
    updated_at: float = Field(default=0.0, ge=0)


class RequeteSynchronisationProfils(BaseModel):
    profils: list[RequeteProfilEntrainement] = Field(default_factory=list, max_length=50)


class RequeteDiagnosticMontage(BaseModel):
    liens_videos: list[str] = Field(min_length=1, max_length=20)
    lien_reference_style: str = Field(default="", max_length=2048)


class RequeteLotMontages(BaseModel):
    projets: list[RequeteMontage] = Field(min_length=1, max_length=6)
    idempotency_key: str = Field(default="", max_length=80)


class RequeteSauvegardeDrive(BaseModel):
    url: str = Field(min_length=1, max_length=500)


# Une seule génération lourde à la fois sur Render Free. Les analyses internes sont
# elles-mêmes limitées à deux par le pipeline, une par défaut avec 512 Mo de RAM.
JOBS: dict[str, dict[str, Any]] = {}
JOB_TASKS: dict[str, asyncio.Task] = {}
JOB_INDEX: dict[str, str] = {}
JOB_SEMAPHORE = asyncio.Semaphore(1)
DUREE_VIE_JOB = 6 * 60 * 60


class ContexteJob:
    def __init__(self, job_id: str, job: dict[str, Any], delai: Optional[float] = None) -> None:
        self.job_id = job_id
        self.job = job
        self.debut = time.monotonic()
        self.delai = float(delai or CONFIG.delai_job)
        self.derniere_etape = "initialisation"

    def update(self, **valeurs: Any) -> None:
        detail = str(valeurs.get("detail", self.job.get("detail", "")))
        statut = str(valeurs.get("statut", valeurs.get("status", self.job.get("status", "running"))))
        valeurs.pop("statut", None)
        valeurs["status"] = statut
        valeurs["updated_at"] = time.monotonic()
        valeurs["updated_at_unix"] = time.time()
        self.job.update(valeurs)
        self.derniere_etape = detail or self.derniere_etape
        logger.info("[%s] %s %s%% — %s", self.job_id, statut, self.job.get("progress", 0), detail)

    def annule(self) -> bool:
        return bool(self.job.get("cancel_requested"))

    def restant(self) -> float:
        return self.delai - (time.monotonic() - self.debut)

    def rapporteur(self) -> Rapporteur:
        return Rapporteur(self.update, self.annule, self.restant)



def _purger_jobs() -> None:
    """Expire états, rendus et dossiers orphelins sans toucher aux travaux actifs."""
    maintenant = time.monotonic()
    expires = [
        identifiant for identifiant, job in JOBS.items()
        if job.get("status") in {"completed", "failed", "cancelled"}
        and maintenant - float(job.get("updated_at", maintenant)) > DUREE_VIE_JOB
    ]
    for identifiant in expires:
        job = JOBS.pop(identifiant, {})
        JOB_TASKS.pop(identifiant, None)
        empreinte = job.get("fingerprint")
        if empreinte and JOB_INDEX.get(empreinte) == identifiant:
            JOB_INDEX.pop(empreinte, None)
        # Vidéo, script .txt et voix off .mp3 expirent ensemble avec leur travail.
        for cle in ("url", "script_url", "voix_url"):
            url = str(job.get(cle, ""))
            nom = Path(urlparse(url).path).name
            if (
                urlparse(url).path == f"/videos/{nom}"
                and re.fullmatch(r"[a-f0-9]{32}\.(mp4|txt|mp3)", nom)
            ):
                (DOSSIER_VIDEOS / nom).unlink(missing_ok=True)

    seuil = time.time() - max(DUREE_VIE_JOB, CONFIG.delai_job * 2)
    for dossier in DOSSIER_TRAVAIL.iterdir():
        try:
            if dossier == DOSSIER_VOIX_OFF:
                continue  # les voix off ont leur propre purge, par fichier
            if dossier.is_dir() and dossier.stat().st_mtime < seuil:
                shutil.rmtree(dossier, ignore_errors=True)
        except OSError:
            logger.warning("Nettoyage impossible pour %s", dossier)

    _purger_voix_off()



def _resoudre_voix_off(session_id: str, identifiant: str) -> Optional[Path]:
    """Traduit l'identifiant renvoyé par /api/voixoff en fichier réellement présent."""
    valeur = str(identifiant or "").strip()
    if not valeur:
        return None
    chemin = _chemin_voix_off(session_id, valeur)
    if not chemin:
        raise ErreurApp(
            "La voix off importée n'est plus disponible (expirée après 6 h). Réimporte le fichier."
        )
    return chemin


def _valider_requete_video(requete: RequeteVideo) -> None:
    if not requete.hook.strip() or not requete.corps.strip():
        raise ErreurApp("Hook et corps du script requis.")
    if not requete.mot_cle_broll.strip():
        raise ErreurApp("Le thème visuel est requis.")



def _valider_requete_montage(requete: RequeteMontage) -> list[str]:
    if not requete.hook.strip() or not requete.corps.strip():
        raise ErreurApp("Accroche et corps du script requis.")
    propres, erreurs, _ = normaliser_liens_tiktok(requete.liens_videos)
    if not propres:
        detail = erreurs[0]["error"] if erreurs else "ajoute au moins un lien"
        raise ErreurApp(f"Aucune vidéo source valide : {detail}")
    if len(propres) > 20:
        raise ErreurApp("20 liens TikTok uniques maximum.")
    if requete.lien_reference_style.strip():
        try:
            normaliser_lien_tiktok(requete.lien_reference_style)
        except ErreurMontage as exc:
            raise ErreurApp(f"Vidéo de référence invalide : {exc}") from exc
    if requete.resolution == "1080" and not CONFIG.autoriser_export_1080:
        raise ErreurApp("L’export 1080 × 1920 est désactivé sur cette instance.")
    return propres


async def _produire_video(
    requete: RequeteVideo, contexte: Optional[ContexteJob] = None, session_id: str = ""
) -> dict[str, Any]:
    _valider_requete_video(requete)
    voix_off = _resoudre_voix_off(session_id, requete.voix_off)
    if contexte:
        contexte.update(statut="selecting", progress=8, detail="Recherche du B-roll vertical")
    cues = _decouper_en_cues(requete.hook, requete.corps)
    duree_totale = max(
        DUREE_MIN_VIDEO_SECONDES,
        cues[-1]["fin"] if cues else float(CONFIG.duree_cible),
    )
    liens_broll = await chercher_broll(requete.mot_cle_broll, duree_totale)
    if contexte:
        contexte.update(statut="editing", progress=45, detail="Téléchargement et montage du B-roll")
    resolution = "1080" if CONFIG.autoriser_export_1080 else "720"
    chemin = await construire_video(
        liens_broll, cues, duree_totale, requete.style,
        resolution=resolution, crf=21,
        voix_off=voix_off,
    )
    await envoyer_script_et_video(requete.hook, requete.corps, chemin)
    return {"url": f"/videos/{chemin.name}", "path": chemin}


async def _produire_montage(
    requete: RequeteMontage, contexte: ContexteJob, session_id: str = ""
) -> dict[str, Any]:
    _valider_requete_montage(requete)
    voix_off = _resoudre_voix_off(session_id, requete.voix_off)
    # Sans référence explicite, la première source devient automatiquement la
    # référence de montage : cadence, coupes, transitions, zooms et sous-titres.
    lien_reference = requete.lien_reference_style.strip() or requete.liens_videos[0]
    resultat = await construire_montage_professionnel(
        liens=requete.liens_videos,
        lien_reference=lien_reference,
        hook=requete.hook.strip(),
        corps=requete.corps.strip(),
        resolution=requete.resolution,
        style_sous_titres=requete.style,
        config=_configuration_montage(requete.mode),
        rapporteur=contexte.rapporteur(),
        resolveur=_resoudre_video_tiktok,
        telechargeur=_telecharger_fichier,
        appel_gemini=_appel_gemini_brut,
        intensite_transitions=requete.intensite_transitions,
        voix_off=voix_off,
    )
    await envoyer_script_et_video(requete.hook, requete.corps, resultat["path"])
    return resultat


async def _produire_reference(requete: RequeteReference, contexte: Optional[ContexteJob] = None) -> dict[str, Any]:
    if not requete.lien.strip():
        raise ErreurApp("Lien vide.")
    if contexte:
        contexte.update(statut="analysing", progress=15, detail="Transcription de la référence")
    return await generer_script_depuis_reference(requete.lien.strip())


async def _produire_analyser(requete: RequeteAnalyser, contexte: Optional[ContexteJob] = None) -> dict[str, Any]:
    if not requete.lien.strip():
        raise ErreurApp("Lien vide.")
    if contexte:
        contexte.update(statut="analysing", progress=15, detail="Lecture et analyse du contenu")
    source_texte = await extraire_source(requete.lien.strip())
    return await generer_script(source_texte)


async def _sauvegarde_auto_drive(
    contexte: ContexteJob, session_id: str, chemin: Path
) -> dict[str, Any]:
    if not SESSIONS_INTEGRATIONS.get(session_id, {}).get("google_drive"):
        return {"status": "not_connected", "message": "Google Drive n’est pas connecté."}
    contexte.update(
        statut="uploading", progress=97, detail="Sauvegarde automatique sur Google Drive",
        drive={"status": "uploading", "message": "Sauvegarde en cours…"},
    )
    try:
        donnees = await _sauvegarder_sur_drive(session_id, chemin)
        return {"status": "completed", **donnees, "message": "Sauvegarde Drive réussie."}
    except Exception as exc:  # la vidéo locale reste disponible
        logger.exception("[%s] sauvegarde Drive échouée", contexte.job_id)
        return {"status": "failed", "error": str(exc), "message": "La sauvegarde Drive a échoué."}


async def _executer_job(job_id: str, fabrique, session_id: str) -> None:
    job = JOBS[job_id]
    contexte = ContexteJob(job_id, job)
    try:
        async with JOB_SEMAPHORE:
            contexte.debut = time.monotonic()  # la limite globale commence après la file d'attente
            contexte.update(statut="validating", progress=1, detail="Démarrage et validation")
            resultat = await asyncio.wait_for(fabrique(contexte), timeout=CONFIG.delai_job)
            chemin = resultat.pop("path", None)
            if chemin and not isinstance(chemin, Path):
                chemin = Path(chemin)
            if chemin:
                # Drive fait partie de la limite globale, même si son échec ne détruit pas le rendu.
                resultat["drive"] = await asyncio.wait_for(
                    _sauvegarde_auto_drive(contexte, session_id, chemin),
                    timeout=max(0.2, contexte.restant()),
                )
            contexte.update(**resultat, statut="completed", progress=100, detail="Travail terminé")
    except TravailAnnule as exc:
        contexte.update(statut="cancelled", detail=str(exc), error=str(exc))
    except asyncio.CancelledError:
        contexte.update(
            statut="cancelled", detail="Travail annulé à la demande de l’utilisateur.",
            error="Travail annulé à la demande de l’utilisateur.",
        )
    except asyncio.TimeoutError:
        contexte.update(
            statut="failed",
            detail=f"Limite globale atteinte pendant « {contexte.derniere_etape} »",
            error=(
                f"Le travail a dépassé la limite globale de {CONFIG.delai_job // 60} min "
                f"{CONFIG.delai_job % 60:02d} pendant « {contexte.derniere_etape} ». "
                "Augmente JOB_TIMEOUT_SECONDES sur l'hébergeur (jusqu'à 3600 s) "
                "ou réduis le nombre ou la durée des sources."
            ),
        )
    except (ErreurApp, ErreurMontage) as exc:
        contexte.update(statut="failed", detail="Travail interrompu", error=str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("[%s] erreur inattendue", job_id)
        contexte.update(
            statut="failed", detail="Erreur serveur détaillée",
            error=f"Erreur inattendue pendant « {contexte.derniere_etape} » : {exc}",
        )
    finally:
        JOB_TASKS.pop(job_id, None)



def _empreinte_job(type_job: str, donnees: dict[str, Any], session_id: str) -> str:
    copie = {k: v for k, v in donnees.items() if k != "idempotency_key"}
    cle_client = str(donnees.get("idempotency_key") or "")
    materiau = json.dumps(
        {"type": type_job, "session": session_id, "client": cle_client, "payload": copie},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(materiau.encode("utf-8")).hexdigest()



def _demarrer_job(
    type_job: str,
    modele: BaseModel,
    session_id: str,
    fabrique,
    *,
    batch_id: str = "",
    batch_index: int = 0,
    batch_total: int = 1,
) -> tuple[str, bool]:
    _purger_jobs()
    donnees = modele.model_dump()
    empreinte = _empreinte_job(type_job, donnees, session_id)
    existant = JOB_INDEX.get(empreinte)
    if existant and existant in JOBS and JOBS[existant].get("status") not in {"failed", "cancelled"}:
        logger.info("Job idempotent réutilisé : %s", existant)
        return existant, True

    job_id = uuid.uuid4().hex
    maintenant = time.monotonic()
    titre = str(getattr(modele, "titre", "") or "").strip()
    titre_defaut = "Création RsT" if type_job == "rst" else ("Création SsT" if type_job == "sst" else "Création vidéo")
    JOBS[job_id] = {
        "job_id": job_id, "type": type_job, "owner": session_id, "status": "queued",
        "title": titre or (f"Création {batch_index + 1}" if batch_total > 1 else titre_defaut),
        "batch_id": batch_id, "batch_index": batch_index, "batch_total": batch_total,
        "progress": 0,
        "detail": (
            f"En attente · projet {batch_index + 1}/{batch_total}"
            if batch_total > 1 else "En attente sur Render"
        ),
        "created_at": maintenant, "updated_at": maintenant,
        "created_at_unix": time.time(), "updated_at_unix": time.time(),
        "source_errors": [], "sources": [],
        "drive": {"status": "pending"}, "fingerprint": empreinte,
    }
    JOB_INDEX[empreinte] = job_id
    JOB_TASKS[job_id] = asyncio.create_task(_executer_job(job_id, fabrique, session_id))
    return job_id, False



def _reponse_nouveau_job(
    request: Request, session_id: str, nouveau_cookie: bool, job_id: str, reused: bool
) -> JSONResponse:
    response = JSONResponse(
        {"job_id": job_id, "status": JOBS[job_id]["status"], "reused": reused}, status_code=202
    )
    if nouveau_cookie:
        _poser_cookie(response, session_id, request)
    return response



def _job_autorise(job_id: str, request: Request) -> dict[str, Any]:
    _purger_jobs()
    job = JOBS.get(job_id)
    session_id = request.cookies.get("creator_session", "")
    if not job or not session_id or job.get("owner") != session_id:
        raise HTTPException(404, "Travail introuvable ou expiré. Relance un diagnostic.")
    return job


@app.get("/")
async def racine() -> FileResponse:
    return FileResponse(
        RACINE / "static" / "index.html",
        headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
    )


@app.get("/api/sante")
async def sante() -> dict[str, Any]:
    return {
        "ok": True,
        "gemini_configure": bool(CONFIG.gemini_api_keys),
        "pexels_configure": bool(CONFIG.pexels_api_key),
        "ffmpeg_installe": shutil.which("ffmpeg") is not None,
        "ffprobe_installe": shutil.which("ffprobe") is not None,
        "tiktok_configure": bool(CONFIG.tiktok_client_key and CONFIG.tiktok_client_secret),
        "google_drive_configure": bool(CONFIG.google_client_id and CONFIG.google_client_secret),
        "job_timeout_seconds": CONFIG.delai_job,
        "max_source_seconds": CONFIG.duree_max_source,
        # Synthèse vocale gratuite et sans clé : on signale seulement si le module est là.
        "voix_off_generee_disponible": importlib.util.find_spec("edge_tts") is not None,
    }


@app.get("/api/config")
async def configuration_publique() -> dict[str, Any]:
    return {
        "max_links": 20,
        "max_source_seconds": CONFIG.duree_max_source,
        "job_timeout_seconds": CONFIG.delai_job,
        "analysis_fps": 6,
        "analysis_resolution": "360p",
        "minimum_video_seconds": DUREE_MIN_VIDEO_SECONDES,
        # Fenêtre de temps partagée par l'estimation, les plafonds RsT/SsT et la
        # confirmation de risque demandée au navigateur.
        "plafond_estimation": PLAFOND_ESTIMATION_MONTAGE,
        "seuil_estimation_risque": SEUIL_ESTIMATION_RISQUE,
        # Contrôle qualité final : relecture obligatoire de chaque plan retenu.
        "controle_qualite_final": {
            "obligatoire": True,
            "tours_max": TOURS_MAX_CONTROLE_QUALITE,
            "secondes_par_plan": SECONDES_CONTROLE_QUALITE_PLAN,
            "echec_ferme": True,
        },
        "ai_source_validation": {
            "required": True,
            "embedded_subtitles_allowed": True,
            "people_allowed": False,
            "other_text_or_overlays_allowed": False,
            "logo_allowed": False,
            "watermark_allowed": False,
            "minimum_sharpness": 0.65,
            "exact_subject_match_required": True,
        },
        "default_mode": "qualite",
        "default_export": "720x1280@24",
        "allow_1080": CONFIG.autoriser_export_1080,
        "rst": {
            "candidats_max": CONFIG.rst_candidats_max,
            "sources_max": CONFIG.rst_sources_max,
            "liens_par_lancement": RST_LIENS_PAR_LANCEMENT,
            "noms_choix": list(RST_NOMS_CHOIX),
            "noms_defaut": RST_NOMS_DEFAUT,
            "duree_max_plan": RST_DUREE_MAX_PLAN,
        },
        "sst": {
            "noms_choix": list(RST_NOMS_CHOIX),
            "noms_defaut": RST_NOMS_DEFAUT,
            "duree_max_plan": RST_DUREE_MAX_PLAN,
            "source_video_reference_only": True,
        },
        "training_profiles": {"persistent": True, "server_storage": "private_session_json"},
        "voix_off_max_mo": VOIX_OFF_MAX_MO,
        "voix_off_extensions": sorted(VOIX_OFF_EXTENSIONS),
        "voix_off_generee": {
            "moteur": "edge-tts",
            "voix": EDGE_TTS_VOIX,
            "gratuit": True,
            "livraison": "separee",
        },
        "gemini_modeles": _modeles_gemini(),
    }


@app.get("/api/rst/sources")
async def diagnostic_sources_rst(auteur: str = "", requete: str = "") -> dict[str, Any]:
    """Vérifie en direct, depuis ce serveur, chaque source publique de découverte RsT.

    Aucune donnée n'est inventée : pour chaque source, ce diagnostic dit si elle a
    répondu depuis l'IP de ce serveur, combien de vrais liens vidéo TikTok elle a
    donnés, et montre un exemple. C'est l'outil de vérification du déploiement :
    en production, il révèle immédiatement quelles sources passent depuis Render.
    """
    auteur = auteur.strip().lstrip("@")[:40]
    requete = requete.strip().lstrip("#")[:60]
    if not auteur and not requete:
        auteur = "tiktok"

    async def sonder_source(session: aiohttp.ClientSession, nom: str) -> dict[str, Any]:
        rapport: dict[str, Any] = {
            "source": nom, "statut": "", "mode": "", "liens": 0,
            "exemple": "", "detail": "", "duree_ms": 0,
        }
        debut = time.monotonic()
        try:
            if nom == "wayback":
                liens, mode, detail = await asyncio.wait_for(
                    _decouvrir_wayback(session, auteur=auteur, limite=5), timeout=35.0
                )
            elif nom == "urlebird":
                liens, mode, detail = await asyncio.wait_for(
                    _source_urlebird(session, auteur=auteur, requete=requete, limite=5), timeout=50.0
                )
            else:
                liens, mode, detail = await asyncio.wait_for(
                    _decouvrir_moteur(session, nom, auteur=auteur, requete=requete, limite=5),
                    timeout=70.0,
                )
        except Exception as exc:  # noqa: BLE001 — le diagnostic rapporte, il ne plante jamais
            rapport["statut"] = "bloque"
            rapport["detail"] = str(exc)[:300]
        else:
            rapport["mode"] = mode
            rapport["liens"] = len(liens)
            rapport["exemple"] = liens[0] if liens else ""
            rapport["statut"] = "ok" if liens else "vide"
            rapport["detail"] = detail[:300]
        rapport["duree_ms"] = int((time.monotonic() - debut) * 1000)
        return rapport

    async def sonder_tikwm(session: aiohttp.ClientSession, chemin: str, parametres: dict) -> dict[str, Any]:
        debut = time.monotonic()
        rapport: dict[str, Any] = {
            "endpoint": chemin, "statut": "", "videos": 0, "detail": "",
            "duree_ms": 0,
        }
        try:
            donnees = await asyncio.wait_for(
                _donnees_tikwm(session, chemin, parametres), timeout=30.0
            )
            rapport["statut"] = "ok"
            if chemin == "/":
                rapport["videos"] = 1 if donnees.get("id") else 0
            else:
                rapport["videos"] = len((donnees or {}).get("videos") or [])
        except ErreurTikwm403:
            rapport["statut"] = "bloque (403)"
        except Exception as exc:  # noqa: BLE001
            rapport["statut"] = "erreur"
            rapport["detail"] = str(exc)[:200]
        rapport["duree_ms"] = int((time.monotonic() - debut) * 1000)
        return rapport

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=75, connect=15)
    ) as session:
        sondes_sources = [
            sonder_source(session, nom)
            for nom in ("searxng", "duckduckgo", "ecosia", "bing", "wayback", "urlebird")
        ]
        sondes_tikwm = [
            sonder_tikwm(session, "/", {"url": "https://www.tiktok.com/@tiktok/video/7106594312292453675", "hd": "1"}),
            sonder_tikwm(session, "/user/posts", {"unique_id": auteur or "tiktok", "count": "5"}),
            sonder_tikwm(session, "/feed/search", {"keywords": requete or "paris", "count": "5"}),
        ]
        sources, tikwm = await asyncio.gather(
            asyncio.gather(*sondes_sources), asyncio.gather(*sondes_tikwm)
        )

    return {
        "ok": True,
        "auteur": auteur,
        "requete": requete,
        "relais": RELAIS_LECTURE,
        "tikwm": list(tikwm),
        "sources": list(sources),
    }


@app.post("/api/voixoff")
async def televerser_voix_off(request: Request, nom: str = ""):
    """Importe un fichier audio (corps HTTP brut) qui servira de voix off au montage.

    Aucun service payant : l'utilisateur fournit son propre enregistrement. Le fichier
    reste lié à sa session, limité à 25 Mo, vérifié par FFprobe et purgé au bout de 6 h.
    """
    _purger_voix_off()
    extension = Path(str(nom or "")).suffix.lower()
    if extension not in VOIX_OFF_EXTENSIONS:
        raise HTTPException(
            400,
            "Format audio non pris en charge. Extensions acceptées : "
            + ", ".join(sorted(VOIX_OFF_EXTENSIONS))
            + ".",
        )

    annonce = request.headers.get("content-length")
    if annonce and annonce.isdigit() and int(annonce) > VOIX_OFF_MAX_OCTETS:
        raise HTTPException(413, f"Voix off trop lourde : {VOIX_OFF_MAX_MO} Mo maximum.")

    corps = await request.body()
    if not corps:
        raise HTTPException(400, "Fichier vide : aucun audio reçu.")
    if len(corps) > VOIX_OFF_MAX_OCTETS:
        raise HTTPException(413, f"Voix off trop lourde : {VOIX_OFF_MAX_MO} Mo maximum.")

    session_id, nouveau = _session_id(request)
    dossier = _dossier_voix_off(session_id)
    dossier.mkdir(parents=True, exist_ok=True)
    identifiant = uuid.uuid4().hex
    chemin = dossier / f"{identifiant}{extension}"
    chemin.write_bytes(corps)

    try:
        duree = await _sonder_audio(chemin)
    except ErreurApp as exc:
        chemin.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        chemin.unlink(missing_ok=True)
        logger.exception("Analyse de la voix off impossible")
        raise HTTPException(400, f"Fichier audio refusé : {exc}") from exc

    response = JSONResponse({
        "voix_off": identifiant,
        "nom": Path(str(nom)).name,
        "extension": extension,
        "taille_octets": len(corps),
        "duree": round(duree, 2),
        "expire_dans_heures": DUREE_VIE_VOIX_OFF // 3600,
    })
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/integrations/status")
async def statut_integrations(request: Request):
    session_id, nouveau = _session_id(request)
    session = SESSIONS_INTEGRATIONS[session_id]
    tiktok = session.get("tiktok") or {}
    drive = session.get("google_drive") or {}
    response = JSONResponse({
        "tiktok": {
            "connected": bool(tiktok), "configured": bool(CONFIG.tiktok_client_key and CONFIG.tiktok_client_secret),
            "display_name": tiktok.get("display_name", ""), "avatar_url": tiktok.get("avatar_url", ""),
        },
        "google_drive": {
            "connected": bool(drive), "configured": bool(CONFIG.google_client_id and CONFIG.google_client_secret),
            "email": drive.get("email", ""), "name": drive.get("name", ""),
        },
    })
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/oauth/tiktok/start")
async def connecter_tiktok(request: Request):
    if not (CONFIG.tiktok_client_key and CONFIG.tiktok_client_secret):
        raise HTTPException(503, "TikTok OAuth n’est pas configuré : ajoute les variables serveur requises.")
    session_id, nouveau = _session_id(request)
    etat = _nouvel_etat_oauth(session_id, "tiktok")
    callback = f"{_url_publique(request)}/api/oauth/tiktok/callback"
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": CONFIG.tiktok_client_key, "scope": "user.info.basic",
        "response_type": "code", "redirect_uri": callback, "state": etat,
    })
    response = RedirectResponse(url)
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/oauth/tiktok/callback")
async def callback_tiktok(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?integration=tiktok&status=error")
    session_id = _consommer_etat_oauth(state, "tiktok")
    callback = f"{_url_publique(request)}/api/oauth/tiktok/callback"
    try:
        jetons = await _requete_json(
            "POST", "https://open.tiktokapis.com/v2/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"client_key": CONFIG.tiktok_client_key, "client_secret": CONFIG.tiktok_client_secret,
                  "code": code, "grant_type": "authorization_code", "redirect_uri": callback},
        )
        profil = await _requete_json(
            "GET", "https://open.tiktokapis.com/v2/user/info/?fields=open_id,union_id,avatar_url,display_name",
            headers={"Authorization": f"Bearer {jetons['access_token']}"},
        )
        utilisateur = profil.get("data", {}).get("user", {})
        SESSIONS_INTEGRATIONS.setdefault(session_id, {})["tiktok"] = {
            **jetons, "display_name": utilisateur.get("display_name", "Compte TikTok"),
            "avatar_url": utilisateur.get("avatar_url", ""), "open_id": utilisateur.get("open_id", ""),
        }
    except ErreurApp as exc:
        logger.warning("Connexion TikTok échouée : %s", exc)
        return RedirectResponse("/?integration=tiktok&status=error")
    response = RedirectResponse("/?integration=tiktok&status=connected")
    _poser_cookie(response, session_id, request)
    return response


@app.get("/api/oauth/google/start")
async def connecter_google(request: Request):
    if not (CONFIG.google_client_id and CONFIG.google_client_secret):
        raise HTTPException(503, "Google Drive OAuth n’est pas configuré : ajoute les variables serveur requises.")
    session_id, nouveau = _session_id(request)
    etat = _nouvel_etat_oauth(session_id, "google")
    callback = f"{_url_publique(request)}/api/oauth/google/callback"
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": CONFIG.google_client_id, "redirect_uri": callback, "response_type": "code",
        "scope": "openid email profile https://www.googleapis.com/auth/drive.file",
        "access_type": "offline", "prompt": "consent", "state": etat,
    })
    response = RedirectResponse(url)
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/oauth/google/callback")
async def callback_google(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        return RedirectResponse("/?integration=drive&status=error")
    session_id = _consommer_etat_oauth(state, "google")
    callback = f"{_url_publique(request)}/api/oauth/google/callback"
    try:
        jetons = await _requete_json(
            "POST", "https://oauth2.googleapis.com/token",
            data={"client_id": CONFIG.google_client_id, "client_secret": CONFIG.google_client_secret,
                  "code": code, "grant_type": "authorization_code", "redirect_uri": callback},
        )
        profil = await _requete_json(
            "GET", "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {jetons['access_token']}"},
        )
        SESSIONS_INTEGRATIONS.setdefault(session_id, {})["google_drive"] = {
            **jetons, "expires_at": time.time() + int(jetons.get("expires_in", 3600)),
            "email": profil.get("email", ""), "name": profil.get("name", "Google Drive"),
        }
    except ErreurApp as exc:
        logger.warning("Connexion Google échouée : %s", exc)
        return RedirectResponse("/?integration=drive&status=error")
    response = RedirectResponse("/?integration=drive&status=connected")
    _poser_cookie(response, session_id, request)
    return response


@app.post("/api/integrations/{service}/disconnect")
async def deconnecter_integration(service: str, request: Request):
    if service not in {"tiktok", "google-drive"}:
        raise HTTPException(404, "Intégration inconnue.")
    session_id, nouveau = _session_id(request)
    SESSIONS_INTEGRATIONS[session_id].pop("tiktok" if service == "tiktok" else "google_drive", None)
    response = JSONResponse({"ok": True})
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.post("/api/integrations/google-drive/backup")
async def sauvegarder_drive(requete: RequeteSauvegardeDrive, request: Request):
    session_id, _ = _session_id(request)
    chemin_url = urlparse(requete.url).path
    nom = Path(chemin_url).name
    if chemin_url != f"/videos/{nom}" or not re.fullmatch(r"[a-zA-Z0-9_.-]+\.mp4", nom):
        raise HTTPException(400, "URL de vidéo invalide.")
    chemin = DOSSIER_VIDEOS / nom
    if not chemin.is_file():
        raise HTTPException(404, "Vidéo introuvable ou expirée.")
    try:
        return await _sauvegarder_sur_drive(session_id, chemin)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/styles")
async def styles() -> dict[str, Any]:
    return {"styles": list(STYLES_SOUS_TITRES.keys())}


async def _json_request(request: Request) -> dict[str, Any]:
    try:
        donnees = await request.json()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, "Corps JSON invalide.") from exc
    if not isinstance(donnees, dict):
        raise HTTPException(400, "Un objet JSON est attendu.")
    return donnees


def _reponse_profil(payload: dict[str, Any], request: Request, session_id: str, nouveau: bool) -> JSONResponse:
    response = JSONResponse(payload)
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/profils-entrainement")
@app.get("/api/training-profiles")
async def lister_profils_entrainement(request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    profils = [_profil_public(profil) for profil in _profils_session(session_id)]
    response = JSONResponse({"profiles": profils, "profils": profils, "updated_at": time.time()})
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.post("/api/profils-entrainement/synchroniser")
@app.post("/api/training-profiles/sync")
async def synchroniser_profils_entrainement(request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    payload = await _json_request(request)
    entrants = payload.get("profiles", payload.get("profils", []))
    if not isinstance(entrants, list) or len(entrants) > 50:
        raise HTTPException(400, "50 profils maximum.")
    existants = {profil["id"]: profil for profil in _profils_session(session_id)}
    for brut in entrants:
        try:
            profil = _profil_depuis_payload(brut if isinstance(brut, dict) else {})
        except ErreurApp:
            continue
        ancien = existants.get(profil["id"])
        if ancien and float(ancien.get("updated_at", 0)) > float(profil.get("updated_at", 0)):
            continue
        existants[profil["id"]] = profil
    profils = list(existants.values())[:50]
    _ecrire_profils_session(session_id, profils)
    publics = [_profil_public(profil) for profil in profils]
    return _reponse_profil({"profiles": publics, "profils": publics, "synchronized": True}, request, session_id, nouveau)


@app.post("/api/profils-entrainement")
@app.post("/api/training-profiles")
async def creer_profil_entrainement(request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    try:
        profil = _profil_depuis_payload(await _json_request(request))
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    profils = _profils_session(session_id)
    profils = [item for item in profils if item.get("id") != profil["id"]]
    profils.insert(0, profil)
    _ecrire_profils_session(session_id, profils[:50])
    return _reponse_profil({"profile": _profil_public(profil), "profil": _profil_public(profil)}, request, session_id, nouveau)


@app.put("/api/profils-entrainement/{profile_id}")
@app.put("/api/training-profiles/{profile_id}")
async def modifier_profil_entrainement(profile_id: str, request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    profils = _profils_session(session_id)
    ancien = next((item for item in profils if item.get("id") == profile_id), None)
    if ancien is None:
        raise HTTPException(404, "Profil d'entraînement introuvable.")
    payload = await _json_request(request)
    payload["id"] = profile_id
    payload["updated_at"] = time.time()
    try:
        profil = _profil_depuis_payload(payload, ancien)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    _ecrire_profils_session(session_id, [profil if item.get("id") == profile_id else item for item in profils])
    return _reponse_profil({"profile": _profil_public(profil), "profil": _profil_public(profil)}, request, session_id, nouveau)


@app.delete("/api/profils-entrainement/{profile_id}")
@app.delete("/api/training-profiles/{profile_id}")
async def supprimer_profil_entrainement(profile_id: str, request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    profils = _profils_session(session_id)
    restants = [item for item in profils if item.get("id") != profile_id]
    if len(restants) == len(profils):
        raise HTTPException(404, "Profil d'entraînement introuvable.")
    _ecrire_profils_session(session_id, restants)
    return _reponse_profil({"ok": True, "deleted": profile_id}, request, session_id, nouveau)


@app.post("/api/profils-entrainement/{profile_id}/analyser")
@app.post("/api/training-profiles/{profile_id}/analyze")
async def analyser_profil_endpoint(profile_id: str, request: Request) -> dict[str, Any]:
    session_id, nouveau = _session_id(request)
    profils = _profils_session(session_id)
    profil = next((item for item in profils if item.get("id") == profile_id), None)
    if profil is None:
        raise HTTPException(404, "Profil d'entraînement introuvable.")
    try:
        resultat = await analyser_profil_entrainement(profil)
    except (ErreurApp, ErreurMontage, asyncio.TimeoutError) as exc:
        raise HTTPException(400, str(exc)) from exc
    _ecrire_profils_session(session_id, [resultat if item.get("id") == profile_id else item for item in profils])
    return _reponse_profil({"profile": _profil_public(resultat), "profil": _profil_public(resultat)}, request, session_id, nouveau)


@app.post("/api/montage/diagnostic")
async def diagnostic_montage(requete: RequeteDiagnosticMontage) -> dict[str, Any]:
    try:
        return await diagnostiquer_montage(
            requete.liens_videos, requete.lien_reference_style,
            _configuration_montage(), _resoudre_video_tiktok,
        )
    except ErreurMontage as exc:
        raise HTTPException(400, str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, "Le diagnostic a dépassé son délai. Réessaie avec moins de sources.") from exc


@app.post("/api/jobs/analyser", status_code=202)
async def lancer_job_analyser(requete: RequeteAnalyser, request: Request):
    if not requete.lien.strip():
        raise HTTPException(400, "Lien vide.")
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "analyser", requete, session_id, lambda contexte: _produire_analyser(requete, contexte)
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.post("/api/jobs/video", status_code=202)
async def lancer_job_video(requete: RequeteVideo, request: Request):
    try:
        _valider_requete_video(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "video", requete, session_id,
        lambda contexte: _produire_video(requete, contexte, session_id),
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.post("/api/jobs/montage", status_code=202)
async def lancer_job_montage(requete: RequeteMontage, request: Request):
    try:
        _valider_requete_montage(requete)
    except (ErreurApp, ErreurMontage) as exc:
        raise HTTPException(400, str(exc)) from exc
    if requete.estimated_seconds > SEUIL_ESTIMATION_RISQUE and not requete.accepter_risque:
        raise HTTPException(
            409,
            "Le diagnostic prévoit plus de "
            f"{SEUIL_ESTIMATION_RISQUE // 60} minutes. Confirme le risque ou réduis les "
            "sources avant le lancement.",
        )
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "montage", requete, session_id,
        lambda contexte: _produire_montage(requete, contexte, session_id),
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.post("/api/jobs/sst", status_code=202)
async def lancer_job_sst(requete: RequeteSst, request: Request):
    """SsT : TOP 3/TOP 5 strictement saisi, distinct du pipeline RsT."""
    try:
        normaliser_lien_tiktok(requete.lien.strip())
        _noms_sst_valides(requete.noms, requete.nombre_noms)
    except (ErreurMontage, ErreurApp) as exc:
        raise HTTPException(400, str(exc)) from exc
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "sst", requete, session_id,
        lambda contexte: _produire_sst(requete, contexte, session_id),
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.post("/api/jobs/rst", status_code=202)
async def lancer_job_rst(requete: RequeteRst, request: Request):
    """Mode RsT : un lien TikTok de départ, puis recherche et montage automatiques."""
    lien = requete.lien.strip()
    if not lien:
        raise HTTPException(400, "Lien vide.")
    try:
        normaliser_lien_tiktok(lien)
    except ErreurMontage as exc:
        raise HTTPException(400, f"Lien TikTok de départ invalide : {exc}") from exc
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "rst", requete, session_id,
        lambda contexte: _produire_rst(requete, contexte, session_id),
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.post("/api/jobs/montage/batch", status_code=202)
async def lancer_lot_montages(requete: RequeteLotMontages, request: Request):
    """Place jusqu’à six projets indépendants dans la file séquentielle Render Free."""
    for projet in requete.projets:
        try:
            _valider_requete_montage(projet)
        except (ErreurApp, ErreurMontage) as exc:
            raise HTTPException(
                400,
                f"Projet « {projet.titre or 'sans titre'} » invalide : {exc}",
            ) from exc
        if projet.estimated_seconds > SEUIL_ESTIMATION_RISQUE and not projet.accepter_risque:
            raise HTTPException(
                409,
                f"Le projet « {projet.titre or 'sans titre'} » est estimé à plus de "
                f"{SEUIL_ESTIMATION_RISQUE // 60} minutes. "
                "Confirme le risque avant de lancer le lot.",
            )

    session_id, nouveau = _session_id(request)
    batch_id = uuid.uuid4().hex
    jobs: list[dict[str, Any]] = []
    total = len(requete.projets)
    for index, projet in enumerate(requete.projets):
        if not projet.idempotency_key and requete.idempotency_key:
            projet = projet.model_copy(
                update={"idempotency_key": f"{requete.idempotency_key}:{index}"}
            )
        job_id, reused = _demarrer_job(
            "montage", projet, session_id,
            lambda contexte, p=projet: _produire_montage(p, contexte, session_id),
            batch_id=batch_id, batch_index=index, batch_total=total,
        )
        jobs.append({
            "job_id": job_id, "title": JOBS[job_id]["title"],
            "status": JOBS[job_id]["status"], "reused": reused,
        })

    response = JSONResponse(
        {"batch_id": batch_id, "count": len(jobs), "jobs": jobs}, status_code=202
    )
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.post("/api/jobs/reference", status_code=202)
async def lancer_job_reference(requete: RequeteReference, request: Request):
    if not requete.lien.strip():
        raise HTTPException(400, "Lien vide.")
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "reference", requete, session_id, lambda contexte: _produire_reference(requete, contexte)
    )
    return _reponse_nouveau_job(request, session_id, nouveau, job_id, reused)


@app.get("/api/jobs")
async def historique_jobs(request: Request):
    """Historique de six heures, utilisable après fermeture ou actualisation de la page."""
    _purger_jobs()
    session_id, nouveau = _session_id(request)
    file_ids = [
        identifiant for identifiant, valeur in sorted(
            JOBS.items(), key=lambda item: float(item[1].get("created_at", 0))
        )
        if valeur.get("status") == "queued"
    ]
    positions = {identifiant: position + 1 for position, identifiant in enumerate(file_ids)}
    champs = {
        "job_id", "type", "title", "batch_id", "batch_index", "batch_total",
        "status", "progress", "detail", "url", "error", "drive",
        "created_at_unix", "updated_at_unix", "elapsed_seconds", "reference_warning",
    }
    historique = []
    for identifiant, job in JOBS.items():
        if job.get("owner") != session_id:
            continue
        resume = {cle: job[cle] for cle in champs if cle in job}
        resume["queue_position"] = positions.get(identifiant, 0)
        resume["source_error_count"] = len(job.get("source_errors") or [])
        if job.get("type") == "rst":
            resume["found_count"] = len(job.get("found_videos") or [])
        historique.append(resume)
    historique.sort(key=lambda item: float(item.get("created_at_unix", 0)), reverse=True)
    response = JSONResponse({"jobs": historique, "retention_seconds": DUREE_VIE_JOB})
    if nouveau:
        _poser_cookie(response, session_id, request)
    return response


@app.get("/api/jobs/{job_id}")
async def etat_job(job_id: str, request: Request) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise HTTPException(404, "Identifiant de travail invalide.")
    job = _job_autorise(job_id, request)
    return {
        key: value for key, value in job.items()
        if key not in {"created_at", "updated_at", "owner", "fingerprint", "cancel_requested"}
    }


@app.post("/api/jobs/{job_id}/cancel")
async def annuler_job(job_id: str, request: Request) -> dict[str, Any]:
    job = _job_autorise(job_id, request)
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return {"ok": True, "status": job["status"], "message": "Ce travail est déjà terminé."}
    job["cancel_requested"] = True
    tache = JOB_TASKS.get(job_id)
    if tache:
        tache.cancel()
    return {"ok": True, "status": "cancelling", "message": "Annulation demandée."}


# Endpoints synchrones historiques conservés pour compatibilité. Le tableau de bord utilise
# les jobs afin de survivre à une actualisation et aux réponses temporaires 502/503 de Render.
@app.post("/api/analyser")
async def analyser(requete: RequeteAnalyser) -> dict[str, Any]:
    try:
        return await _produire_analyser(requete)
    except (ErreurApp, ErreurMontage) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/reference")
async def reference(requete: RequeteReference) -> dict[str, Any]:
    try:
        return await _produire_reference(requete)
    except (ErreurApp, ErreurMontage) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/video")
async def video(requete: RequeteVideo) -> dict[str, Any]:
    try:
        resultat = await _produire_video(requete)
        resultat.pop("path", None)
        return resultat
    except (ErreurApp, ErreurMontage) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/montage")
async def montage(requete: RequeteMontage, request: Request) -> dict[str, Any]:
    session_id, _ = _session_id(request)
    temporaire = {
        "status": "validating", "progress": 0, "detail": "Démarrage",
        "created_at": time.monotonic(), "updated_at": time.monotonic(),
    }
    contexte = ContexteJob("legacy-" + uuid.uuid4().hex[:8], temporaire)
    try:
        resultat = await asyncio.wait_for(
            _produire_montage(requete, contexte, session_id), timeout=CONFIG.delai_job
        )
        resultat.pop("path", None)
        return resultat
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, f"Limite globale dépassée pendant « {contexte.derniere_etape} ».") from exc
    except (ErreurApp, ErreurMontage) as exc:
        raise HTTPException(400, str(exc)) from exc
