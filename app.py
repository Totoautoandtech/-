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
import json
import logging
import os
import random
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from studio_montage import (
    ConfigurationMontage,
    ErreurMontage,
    Rapporteur,
    TravailAnnule,
    construire_montage_professionnel,
    diagnostiquer_montage,
    estimer_duree_traitement,
    executer_commande,
    normaliser_lien_tiktok,
    normaliser_liens_tiktok,
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

# Mode RsT : nombre maximum de liens TikTok traités par lancement (un job indépendant chacun).
RST_LIENS_PAR_LANCEMENT = 6


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
            delai_job=entier("JOB_TIMEOUT_SECONDES", 570, 60, 900),
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


def _configuration_montage(mode: str = "rapide") -> ConfigurationMontage:
    """Configuration du pipeline ; le mode « qualite » privilégie un encodage plus fin."""
    qualite = str(mode).lower() == "qualite"
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
        crf=21 if qualite else 23,
        threads_ffmpeg=CONFIG.threads_ffmpeg,
        autoriser_1080=CONFIG.autoriser_export_1080,
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


async def _avec_retry(fabrique, *, tentatives: int = 3, etape: str = ""):
    derniere: Optional[Exception] = None
    for essai in range(1, tentatives + 1):
        try:
            return await fabrique()
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
                async with session.get("https://www.tikwm.com/api/", params={"url": url, "hd": "1"}) as resp:
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
        async with session.get("https://www.tikwm.com/api/", params={"url": url, "hd": "1"}) as resp:
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
    '- "corps" : la suite du script (3 à 6 phrases courtes), qui développe l\'idée, dans la même '
    "langue que le contenu source.\n"
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


# ======================================================================================
# MODE "VIDÉO DE RÉFÉRENCE" : reprendre le script d'une vidéo existante, hook préservé
# ======================================================================================

PROMPT_REFERENCE = (
    "Voici la transcription d'une vidéo virale. Réponds UNIQUEMENT avec un JSON valide, sans "
    "texte avant/après, sans balises markdown, avec exactement trois clés :\n"
    '- "hook" : recopie MOT POUR MOT la toute première phrase de la transcription (l\'accroche '
    "d'origine). Ne la traduis pas, ne la modifie pas, ne la reformule pas.\n"
    '- "corps" : réécris et traduis en français le reste de la transcription, pour que ce soit '
    "fluide et clair, sans en changer le sens ni les informations.\n"
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

    nb_clips = max(3, min(8, -(-int(duree_visee) // 6)))  # ~6s par clip (index de liste : doit rester un int)
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
        duree_par_clip = duree_totale / len(liens_broll)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            bruts = []
            for i, lien in enumerate(liens_broll):
                chemin = dossier / f"brut_{i}.mp4"
                await _telecharger_fichier(session, lien, chemin)
                bruts.append(chemin)

        normalises = []
        for i, brut in enumerate(bruts):
            cible = dossier / f"norm_{i}.mp4"
            await _normaliser_clip(brut, cible, duree_par_clip, largeur=largeur, hauteur=hauteur)
            normalises.append(cible)

        assemble = await _assembler_clips(normalises, dossier)
        return await _incruster_sous_titres(
            assemble, cues, style, dossier, largeur=largeur, hauteur=hauteur, crf=crf,
            voix_off=voix_off,
        )
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
# MODE « RsT » : un seul lien TikTok de départ → script + vraies vidéos trouvées → montage
# ======================================================================================

DUREE_MIN_SOURCE_RST = 5.0

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
        async with session.get(url, params=params) as resp:
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


def _normaliser_candidat_rst(video: Any, origine: str) -> Optional[dict]:
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


def _selectionner_sources_rst(
    candidats: list[dict], limite: int, duree_max: float
) -> tuple[list[dict], list[dict]]:
    """Retient jusqu'à `limite` candidates dont la durée réelle est exploisable."""
    for candidate in candidats:
        candidate.setdefault("selected", False)
        candidate.setdefault("rejet", "")
    retenues: list[dict] = []
    for candidate in candidats:
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
        candidate["selected"] = True
        retenues.append(candidate)
    return retenues, candidats


def _reduire_selon_estimation(
    sources: list[dict], config: ConfigurationMontage, plafond: int = 540
) -> list[dict]:
    """Retire les dernières sources tant que l'estimation dépasse le plafond prudent."""
    while len(sources) > 3:
        estimation = estimer_duree_traitement(
            [float(s.get("duration") or 0) for s in sources], 0.0, config
        )
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
    """Mode RsT : analyse un lien TikTok, trouve de vraies vidéos, monte automatiquement."""
    try:
        lien = normaliser_lien_tiktok(requete.lien.strip())
    except ErreurMontage as exc:
        raise ErreurApp(f"Lien TikTok de départ invalide : {exc}") from exc

    contexte.update(statut="analysing", progress=4, detail="Lecture de la vidéo TikTok de départ")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75, connect=15)) as session:
        donnees = await _donnees_tikwm(session, "/", {"url": lien, "hd": "1"})
        legende = str(donnees.get("title") or "").strip()
        if not legende:
            raise ErreurApp("Cette vidéo TikTok n'a pas de légende exploitable comme point de départ.")
        auteur = str((donnees.get("author") or {}).get("unique_id") or "").strip().lstrip("@")
        identifiant_depart = str(donnees.get("id") or donnees.get("video_id") or "").strip()

        contexte.update(statut="analysing", progress=12, detail="Rédaction du script à partir de la légende")
        script = await generer_script(legende)
        contexte.update(
            statut="analysing", progress=20, detail="Script prêt — recherche de vidéos candidates",
            script=script,
            seed={
                "url": lien, "title": legende[:200], "author": auteur,
                "duration": donnees.get("duration"),
            },
        )

        requetes_reelles: list[str] = []
        candidats: dict[str, dict] = {}

        async def accumuler(videos: Any, origine: str) -> None:
            for video in videos or []:
                candidate = _normaliser_candidat_rst(video, origine)
                if not candidate or candidate["video_id"] == identifiant_depart:
                    continue
                candidats.setdefault(candidate["video_id"], candidate)

        def publier(detail: str, progression: int) -> None:
            contexte.update(
                statut="searching", progress=progression, detail=detail,
                found_videos=list(candidats.values()), search_queries=list(requetes_reelles),
            )

        # 1) Les autres publications réelles du créateur de la vidéo de départ.
        if auteur:
            requetes_reelles.append(f"@{auteur}")
            contexte.update(statut="searching", progress=24, detail=f"Publications de @{auteur}")
            try:
                await asyncio.sleep(1.0)  # cadence respectueuse de l'API publique TikWM
                publications = await _donnees_tikwm(
                    session, "/user/posts",
                    {"unique_id": auteur, "count": str(min(CONFIG.rst_candidats_max, 40))},
                )
                await accumuler(publications.get("videos") or [], f"publications de @{auteur}")
            except ErreurApp as exc:
                logger.warning("[RsT] publications de @%s indisponibles : %s", auteur, exc)
            publier(f"{len(candidats)} vidéo(s) réellement trouvée(s)", 30)

        # 2) Recherches TikTok par mots-clés tirés de la vraie légende.
        for mot_cle in _extraire_mots_cles_rst(legende, script.get("mot_cle_broll", "")):
            if len(candidats) >= CONFIG.rst_candidats_max:
                break
            requetes_reelles.append(mot_cle)
            contexte.update(statut="searching", progress=34, detail=f"Recherche TikTok « {mot_cle} »")
            try:
                await asyncio.sleep(1.0)
                resultats = await _donnees_tikwm(
                    session, "/feed/search", {"keywords": mot_cle, "count": "20"}
                )
                await accumuler(resultats.get("videos") or [], f"recherche « {mot_cle} »")
            except ErreurApp as exc:
                logger.warning("[RsT] recherche « %s » indisponible : %s", mot_cle, exc)
            publier(f"{len(candidats)} vidéo(s) réellement trouvée(s)", 38)

    trouves = list(candidats.values())[: CONFIG.rst_candidats_max]
    contexte.update(
        statut="searching", progress=42,
        detail=f"{len(trouves)} vidéo(s) trouvée(s) — sélection des meilleures sources",
        found_videos=trouves, search_queries=requetes_reelles,
    )
    if not trouves:
        raise ErreurApp("RsT n'a trouvé aucune autre vidéo TikTok exploitable pour ce point de départ.")

    selectionnees, trouves = _selectionner_sources_rst(
        trouves, CONFIG.rst_sources_max, float(CONFIG.duree_max_source)
    )
    selectionnees = _reduire_selon_estimation(selectionnees, _configuration_montage(requete.mode))
    if not selectionnees:
        raise ErreurApp("Aucune vidéo trouvée n'entre dans les limites de durée utilisables.")
    contexte.update(
        statut="selecting", progress=44,
        detail=f"{len(selectionnees)} source(s) retenue(s) sur {len(trouves)} trouvée(s)",
        found_videos=trouves, search_queries=requetes_reelles,
    )

    resolution = "1080" if (requete.mode == "qualite" and CONFIG.autoriser_export_1080) else "720"
    resultat = await construire_montage_professionnel(
        liens=[candidate["url"] for candidate in selectionnees],
        lien_reference="",
        hook=script["hook"].strip(),
        corps=script["corps"].strip(),
        resolution=resolution,
        style_sous_titres="classique",
        config=_configuration_montage(requete.mode),
        rapporteur=_rapporteur_decale(contexte, 45.0, 0.55),
        resolveur=_resoudre_video_tiktok,
        telechargeur=_telecharger_fichier,
        appel_gemini=_appel_gemini_brut,
        intensite_transitions=requete.intensite_transitions,
        voix_off=_resoudre_voix_off(session_id, requete.voix_off),
    )
    await envoyer_script_et_video(script["hook"], script["corps"], resultat["path"])
    resultat.update(script=script, found_videos=trouves, search_queries=requetes_reelles)
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
    mode: str = Field(default="rapide", pattern="^(rapide|qualite)$")
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
    mode: str = Field(default="rapide", pattern="^(rapide|qualite)$")
    intensite_transitions: int = Field(default=2, ge=0, le=3)
    voix_off: str = Field(default="", max_length=64)
    estimated_seconds: int = Field(default=0, ge=0, le=7200)
    accepter_risque: bool = False
    idempotency_key: str = Field(default="", max_length=80)


class RequeteRst(BaseModel):
    """Mode RsT : un seul lien TikTok de départ suffit."""
    titre: str = Field(default="", max_length=120)
    lien: str = Field(min_length=1, max_length=2048)
    mode: str = Field(default="rapide", pattern="^(rapide|qualite)$")
    intensite_transitions: int = Field(default=2, ge=0, le=3)
    voix_off: str = Field(default="", max_length=64)
    idempotency_key: str = Field(default="", max_length=80)


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
        url = str(job.get("url", ""))
        nom = Path(urlparse(url).path).name
        if urlparse(url).path == f"/videos/{nom}" and re.fullmatch(r"[a-f0-9]{32}\.mp4", nom):
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
    duree_totale = cues[-1]["fin"] if cues else float(CONFIG.duree_cible)
    liens_broll = await chercher_broll(requete.mot_cle_broll, duree_totale)
    if contexte:
        contexte.update(statut="editing", progress=45, detail="Téléchargement et montage du B-roll")
    resolution = "1080" if (requete.mode == "qualite" and CONFIG.autoriser_export_1080) else "720"
    chemin = await construire_video(
        liens_broll, cues, duree_totale, requete.style,
        resolution=resolution, crf=21 if requete.mode == "qualite" else 23,
        voix_off=voix_off,
    )
    await envoyer_script_et_video(requete.hook, requete.corps, chemin)
    return {"url": f"/videos/{chemin.name}", "path": chemin}


async def _produire_montage(
    requete: RequeteMontage, contexte: ContexteJob, session_id: str = ""
) -> dict[str, Any]:
    _valider_requete_montage(requete)
    voix_off = _resoudre_voix_off(session_id, requete.voix_off)
    resultat = await construire_montage_professionnel(
        liens=requete.liens_videos,
        lien_reference=requete.lien_reference_style.strip(),
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
                "Réduis le nombre ou la durée des sources."
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
    titre_defaut = "Création RsT" if type_job == "rst" else "Création vidéo"
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
    }


@app.get("/api/config")
async def configuration_publique() -> dict[str, Any]:
    return {
        "max_links": 20,
        "max_source_seconds": CONFIG.duree_max_source,
        "job_timeout_seconds": CONFIG.delai_job,
        "analysis_fps": 6,
        "analysis_resolution": "360p",
        "default_export": "720x1280@24",
        "allow_1080": CONFIG.autoriser_export_1080,
        "rst": {
            "candidats_max": CONFIG.rst_candidats_max,
            "sources_max": CONFIG.rst_sources_max,
            "liens_par_lancement": RST_LIENS_PAR_LANCEMENT,
        },
        "voix_off_max_mo": VOIX_OFF_MAX_MO,
        "voix_off_extensions": sorted(VOIX_OFF_EXTENSIONS),
        "gemini_modeles": _modeles_gemini(),
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
    if requete.estimated_seconds > 540 and not requete.accepter_risque:
        raise HTTPException(
            409,
            "Le diagnostic prévoit plus de 9 minutes. Confirme le risque ou réduis les sources avant le lancement.",
        )
    session_id, nouveau = _session_id(request)
    job_id, reused = _demarrer_job(
        "montage", requete, session_id,
        lambda contexte: _produire_montage(requete, contexte, session_id),
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
        if projet.estimated_seconds > 540 and not projet.accepter_risque:
            raise HTTPException(
                409,
                f"Le projet « {projet.titre or 'sans titre'} » est estimé à plus de 9 minutes. "
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
