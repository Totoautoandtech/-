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
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ======================================================================================
# LOGGING & CONFIG
# ======================================================================================

logging.basicConfig(format="%(asctime)s | %(levelname)-8s | %(message)s", level=logging.INFO)
logger = logging.getLogger("videoapp")


def _env(nom: str, defaut: str = "") -> str:
    return os.getenv(nom, defaut).strip()


@dataclass
class Config:
    gemini_api_keys: list[str]
    gemini_model: str
    pexels_api_key: str
    duree_cible: int
    gmail_adresse: str
    gmail_mot_de_passe: str
    destinataire_email: str

    @classmethod
    def charger(cls) -> "Config":
        """Charge la configuration sans empêcher le serveur de démarrer.

        Les clés externes sont vérifiées au moment où l'endpoint concerné est appelé.
        Cela permet notamment à Render, aux tests et à la page d'accueil de démarrer
        avec une réponse d'erreur explicite plutôt qu'un crash à l'import du module.
        """
        cles = [c.strip() for c in _env("GEMINI_API_KEYS").split(",") if c.strip()]
        pexels = _env("PEXELS_API_KEY")

        try:
            duree_cible = int(_env("DUREE_CIBLE_SECONDES", "30"))
            if duree_cible <= 0:
                raise ValueError
        except ValueError:
            logger.warning("DUREE_CIBLE_SECONDES invalide : 30 secondes utilisées.")
            duree_cible = 30

        if not cles:
            logger.warning("GEMINI_API_KEYS absent : les endpoints IA seront indisponibles.")
        if not pexels:
            logger.warning("PEXELS_API_KEY absent : la génération B-roll sera indisponible.")

        return cls(
            gemini_api_keys=cles,
            gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
            pexels_api_key=pexels,
            duree_cible=duree_cible,
            gmail_adresse=_env("GMAIL_ADRESSE"),
            gmail_mot_de_passe=_env("GMAIL_MOT_DE_PASSE_APP"),
            destinataire_email=_env("DESTINATAIRE_EMAIL", "tomheude8@gmail.com"),
        )


CONFIG = Config.charger()

# Tous les chemins sont ancrés sur le dossier du dépôt, pas sur le répertoire depuis
# lequel uvicorn a été lancé. C'est important pour les déploiements et les tests.
RACINE = Path(__file__).resolve().parent
DOSSIER_VIDEOS = RACINE / "videos"
DOSSIER_VIDEOS.mkdir(exist_ok=True)
DOSSIER_TRAVAIL = RACINE / "travail"
DOSSIER_TRAVAIL.mkdir(exist_ok=True)

TAILLE_MAX_TELECHARGEMENT = 100 * 1024 * 1024  # évite de remplir le disque avec un lien distant

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

    await _avec_retry(_appel, etape=f"téléchargement {destination.name}")


async def _resoudre_video_tiktok(session: aiohttp.ClientSession, url: str) -> str:
    """Résout un lien TikTok vers son fichier vidéo direct (sans watermark), via TikWM."""
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
            return await resp.text()

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


async def _appel_gemini_brut(
    parts: list[dict], *, temperature: float, system: Optional[str] = None, json_mode: bool = False
) -> str:
    cles = CONFIG.gemini_api_keys
    if not cles:
        raise ErreurApp("GEMINI_API_KEYS n'est pas configuré sur le serveur.")

    derniere: Optional[Exception] = None
    for cle in random.sample(cles, len(cles)):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{CONFIG.gemini_model}:generateContent"
        headers = {"x-goog-api-key": cle, "Content-Type": "application/json"}
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature},
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=180)) as session:
                async def _appel():
                    async with session.post(url, headers=headers, json=payload) as resp:
                        if resp.status != 200:
                            texte = await resp.text()
                            raise ErreurApp(f"Gemini a répondu {resp.status} : {texte[:200]}")
                        return await resp.json()

                data = await _avec_retry(_appel, tentatives=2, etape="appel Gemini")
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as exc:  # noqa: BLE001
            derniere = exc
            logger.warning("Clé Gemini indisponible (%s...), clé suivante.", cle[:6])

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
    """Extrait l'audio (ffmpeg) et transcrit mot pour mot via Gemini."""
    audio_path = video_path.with_suffix(".mp3")
    commande = ["ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(audio_path)]
    proc = await asyncio.create_subprocess_exec(*commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ErreurApp(f"Extraction audio échouée : {stderr.decode(errors='ignore')[-200:]}")

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
            candidats.append(fichiers[0]["link"])

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


async def _analyser_video(video_path: Path, segments: list[dict]) -> list[dict]:
    """Demande à Gemini de repérer, dans cette vidéo, les passages qui illustrent le script."""
    taille_mo = video_path.stat().st_size / 1_048_576
    if taille_mo > 18:
        logger.warning("%s fait %.1f Mo : trop lourd pour l'analyse inline, ignorée.", video_path.name, taille_mo)
        return []

    video_b64 = base64.b64encode(video_path.read_bytes()).decode("ascii")
    segments_json = json.dumps([{"id": s["id"], "texte": s["texte"]} for s in segments], ensure_ascii=False)
    prompt = PROMPT_ANALYSE_VIDEO.format(segments=segments_json)

    try:
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


async def _normaliser_clip(source: Path, destination: Path, duree: float, debut: float = 0.0) -> None:
    commande = [
        "ffmpeg", "-y", "-ss", str(debut), "-i", str(source), "-t", str(duree),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(destination),
    ]
    proc = await asyncio.create_subprocess_exec(*commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ErreurApp(f"Normalisation vidéo échouée : {stderr.decode(errors='ignore')[-200:]}")


async def _assembler_clips(normalises: list[Path], dossier: Path) -> Path:
    liste = dossier / "liste.txt"
    liste.write_text("".join(f"file '{c.resolve()}'\n" for c in normalises), encoding="utf-8")
    assemble = dossier / "assemble.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(liste), "-c", "copy", str(assemble),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ErreurApp(f"Assemblage vidéo échoué : {stderr.decode(errors='ignore')[-200:]}")
    return assemble


async def _incruster_sous_titres(assemble: Path, cues: list[dict], style: str, dossier: Path) -> Path:
    """Fichiers texte par cue (textfile= évite tout souci d'échappement des apostrophes/accents)."""
    if style not in STYLES_SOUS_TITRES:
        style = "classique"
    reglage = STYLES_SOUS_TITRES[style]

    police = _police()
    filtres = []
    positions = {"bas": "h-320", "centre": "(h-text_h)/2", "haut": "160"}
    y = positions.get(reglage["position"], "h-320")
    for i, cue in enumerate(cues):
        fichier_texte = dossier / f"cue_{i}.txt"
        fichier_texte.write_text(cue["texte"], encoding="utf-8")
        filtres.append(
            f"drawtext=fontfile={police}:textfile={fichier_texte}:fontsize=64:"
            f"fontcolor={reglage['couleur']}:borderw=4:bordercolor={reglage['contour']}:"
            f"x=(w-text_w)/2:y={y}:enable='between(t,{cue['debut']:.2f},{cue['fin']:.2f})'"
        )

    sortie = DOSSIER_VIDEOS / f"{uuid.uuid4().hex}.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(assemble),
        "-vf", ",".join(filtres),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-an", str(sortie),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise ErreurApp(f"Incrustation des sous-titres échouée : {stderr.decode(errors='ignore')[-200:]}")
    return sortie


async def construire_video(liens_broll: list[str], cues: list[dict], duree_totale: float, style: str) -> Path:
    """Mode « thème libre » : B-roll cherché sur Pexels."""
    _verifier_ffmpeg()
    dossier = DOSSIER_TRAVAIL / uuid.uuid4().hex
    dossier.mkdir(parents=True, exist_ok=True)

    try:
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
            await _normaliser_clip(brut, cible, duree_par_clip)
            normalises.append(cible)

        assemble = await _assembler_clips(normalises, dossier)
        return await _incruster_sous_titres(assemble, cues, style, dossier)
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

            await asyncio.gather(*(_telecharger_une(i, lien) for i, lien in enumerate(liens_videos)))

        if not chemins_videos:
            raise ErreurApp("Aucune des vidéos fournies n'a pu être téléchargée.")

        # 2. L'IA analyse chaque vidéo pour y repérer les passages pertinents
        # Analyse en parallèle (plutôt qu'une vidéo après l'autre) : réduit fortement
        # le temps total, donc le risque de dépasser le délai autorisé par Render.
        noms = list(chemins_videos.keys())
        resultats_analyse = await asyncio.gather(
            *(_analyser_video(chemins_videos[nom], segments) for nom in noms)
        )
        candidats_par_video = dict(zip(noms, resultats_analyse))

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
# API
# ======================================================================================

app = FastAPI(title="Générateur de vidéos")
app.mount("/static", StaticFiles(directory=str(RACINE / "static")), name="static")
app.mount("/videos", StaticFiles(directory=str(DOSSIER_VIDEOS)), name="videos")


class RequeteAnalyser(BaseModel):
    lien: str = Field(min_length=1, max_length=2048)


class RequeteReference(BaseModel):
    lien: str = Field(min_length=1, max_length=2048)


class RequeteVideo(BaseModel):
    hook: str = Field(min_length=1, max_length=4000)
    corps: str = Field(min_length=1, max_length=12000)
    mot_cle_broll: str = Field(min_length=1, max_length=200)
    style: str = Field(default="classique", max_length=32)


class RequeteMontage(BaseModel):
    hook: str = Field(min_length=1, max_length=4000)
    corps: str = Field(min_length=1, max_length=12000)
    liens_videos: list[str] = Field(min_length=1, max_length=20)
    style: str = Field(default="classique", max_length=32)


# Render limite la durée de vie des requêtes HTTP. Les générations vidéo sont donc
# lancées en arrière-plan et l'interface suit leur état avec un petit polling.
JOBS: dict[str, dict[str, Any]] = {}
JOB_TASKS: dict[str, asyncio.Task] = {}
JOB_SEMAPHORE = asyncio.Semaphore(1)
DUREE_VIE_JOB = 60 * 60


def _purger_jobs() -> None:
    maintenant = time.monotonic()
    expires = [
        job_id for job_id, job in JOBS.items()
        if job.get("status") in {"completed", "failed"} and maintenant - job.get("updated_at", maintenant) > DUREE_VIE_JOB
    ]
    for job_id in expires:
        JOBS.pop(job_id, None)
        JOB_TASKS.pop(job_id, None)


def _valider_requete_video(requete: RequeteVideo) -> None:
    if not requete.hook.strip() or not requete.corps.strip():
        raise ErreurApp("Hook et corps du script requis.")
    if not requete.mot_cle_broll.strip():
        raise ErreurApp("Le thème visuel est requis.")


def _valider_requete_montage(requete: RequeteMontage) -> list[str]:
    if not requete.hook.strip() or not requete.corps.strip():
        raise ErreurApp("Hook et corps du script requis.")
    liens = [lien.strip() for lien in requete.liens_videos if lien.strip()]
    if not liens:
        raise ErreurApp("Ajoute au moins un lien de vidéo source.")
    return [_valider_url(lien, "Lien vidéo") for lien in liens]


async def _produire_video(requete: RequeteVideo) -> dict[str, str]:
    _valider_requete_video(requete)
    cues = _decouper_en_cues(requete.hook, requete.corps)
    duree_totale = cues[-1]["fin"] if cues else float(CONFIG.duree_cible)
    liens_broll = await chercher_broll(requete.mot_cle_broll, duree_totale)
    chemin = await construire_video(liens_broll, cues, duree_totale, requete.style)
    await envoyer_script_et_video(requete.hook, requete.corps, chemin)
    return {"url": f"/videos/{chemin.name}"}


async def _produire_montage(requete: RequeteMontage) -> dict[str, str]:
    liens = _valider_requete_montage(requete)
    chemin = await construire_montage(liens, requete.hook, requete.corps, requete.style)
    await envoyer_script_et_video(requete.hook, requete.corps, chemin)
    return {"url": f"/videos/{chemin.name}"}


async def _produire_reference(requete: RequeteReference) -> dict[str, str]:
    if not requete.lien.strip():
        raise ErreurApp("Lien vide.")
    return await generer_script_depuis_reference(requete.lien.strip())


async def _produire_analyser(requete: RequeteAnalyser) -> dict[str, str]:
    if not requete.lien.strip():
        raise ErreurApp("Lien vide.")
    source_texte = await extraire_source(requete.lien.strip())
    return await generer_script(source_texte)


async def _executer_job(job_id: str, fabrique) -> None:
    job = JOBS[job_id]
    try:
        async with JOB_SEMAPHORE:
            job["status"] = "running"
            job["updated_at"] = time.monotonic()
            resultat = await fabrique()
        job.update(resultat, status="completed", updated_at=time.monotonic())
    except ErreurApp as exc:
        job.update(status="failed", error=str(exc), updated_at=time.monotonic())
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s échoué.", job_id)
        job.update(status="failed", error=f"Erreur inattendue : {exc}", updated_at=time.monotonic())
    finally:
        JOB_TASKS.pop(job_id, None)


def _demarrer_job(fabrique) -> str:
    _purger_jobs()
    job_id = uuid.uuid4().hex
    JOBS[job_id] = {"job_id": job_id, "status": "queued", "created_at": time.monotonic(), "updated_at": time.monotonic()}
    JOB_TASKS[job_id] = asyncio.create_task(_executer_job(job_id, fabrique))
    return job_id


@app.get("/")
async def racine() -> FileResponse:
    return FileResponse(RACINE / "static" / "index.html")


@app.get("/api/sante")
async def sante() -> dict[str, Any]:
    """Endpoint léger pour les sondes de déploiement et le diagnostic."""
    return {
        "ok": True,
        "gemini_configure": bool(CONFIG.gemini_api_keys),
        "pexels_configure": bool(CONFIG.pexels_api_key),
        "ffmpeg_installe": shutil.which("ffmpeg") is not None,
    }


@app.get("/api/styles")
async def styles() -> dict:
    return {"styles": list(STYLES_SOUS_TITRES.keys())}


@app.post("/api/jobs/analyser", status_code=202)
async def lancer_job_analyser(requete: RequeteAnalyser) -> dict[str, str]:
    if not requete.lien.strip():
        raise HTTPException(400, "Lien vide.")
    return {"job_id": _demarrer_job(lambda: _produire_analyser(requete)), "status": "queued"}


@app.post("/api/jobs/video", status_code=202)
async def lancer_job_video(requete: RequeteVideo) -> dict[str, str]:
    try:
        _valider_requete_video(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": _demarrer_job(lambda: _produire_video(requete)), "status": "queued"}


@app.post("/api/jobs/montage", status_code=202)
async def lancer_job_montage(requete: RequeteMontage) -> dict[str, str]:
    try:
        _valider_requete_montage(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"job_id": _demarrer_job(lambda: _produire_montage(requete)), "status": "queued"}


@app.post("/api/jobs/reference", status_code=202)
async def lancer_job_reference(requete: RequeteReference) -> dict[str, str]:
    if not requete.lien.strip():
        raise HTTPException(400, "Lien vide.")
    return {"job_id": _demarrer_job(lambda: _produire_reference(requete)), "status": "queued"}


@app.get("/api/jobs/{job_id}")
async def etat_job(job_id: str) -> dict[str, Any]:
    _purger_jobs()
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Job introuvable ou expiré.")
    return {key: value for key, value in job.items() if key not in {"created_at", "updated_at"}}


@app.post("/api/analyser")
async def analyser(requete: RequeteAnalyser) -> dict:
    if not requete.lien.strip():
        raise HTTPException(400, "Lien vide.")
    try:
        source_texte = await extraire_source(requete.lien.strip())
        script = await generer_script(source_texte)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue dans /api/analyser.")
        raise HTTPException(500, f"Erreur inattendue : {exc}") from exc
    return script


@app.post("/api/reference")
async def reference(requete: RequeteReference) -> dict:
    """Mode « vidéo de référence » : reprend le script d'une vidéo TikTok existante, hook préservé."""
    try:
        return await _produire_reference(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue dans /api/reference.")
        raise HTTPException(500, f"Erreur inattendue : {exc}") from exc


@app.post("/api/video")
async def video(requete: RequeteVideo) -> dict:
    """Mode « thème libre » : B-roll cherché sur Pexels."""
    try:
        return await _produire_video(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue dans /api/video.")
        raise HTTPException(500, f"Erreur inattendue : {exc}") from exc


@app.post("/api/montage")
async def montage(requete: RequeteMontage) -> dict:
    """Mode « montage multi-vidéos » : l'IA repère les bons passages dans TES vidéos."""
    try:
        return await _produire_montage(requete)
    except ErreurApp as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue dans /api/montage.")
        raise HTTPException(500, f"Erreur inattendue : {exc}") from exc
