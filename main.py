#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — Bot Discord : pipeline d'automatisation TikTok -> B-roll

Tout le B-roll provient aussi de TikTok (pas de Pexels/Pixabay) : une recherche TikTok
est relancée sur le thème détecté par l'IA, et les vidéos trouvées sont téléchargées
sans watermark, exactement comme la vidéo source.

Sommaire du fichier :
  1. Logging
  2. Exceptions + retry générique
  3. Configuration (.env)
  4. TikTokAutomationPipeline (toute la logique métier)
  5. Bot Discord (commandes /broll et /tiktok)
  6. Serveur keep-alive (requis par Render)
  7. Points d'entrée (bot ou CLI)

Deux modes d'exécution :
  - Bot Discord persistant (par défaut) : `python main.py`
      -> Exécution AUTOMATIQUE en arrière-plan sur la/les niche(s) définie(s) (NICHES)
      -> /broll niche:<mot-clé>   : pipeline complet à la demande, B-roll inclus
      -> /tiktok urls:<lien(s)>   : télécharge une ou plusieurs vidéos TikTok précises
  - Exécution unique en CLI : `python main.py --niche "productivité"`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import aiofiles
import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# ======================================================================================
# 1. LOGGING
# ======================================================================================


def configurer_logging() -> logging.Logger:
    """Logger console + fichier, format horodaté clair."""
    Path("logs").mkdir(exist_ok=True)
    logger_ = logging.getLogger("tiktok_pipeline")
    logger_.setLevel(logging.INFO)
    logger_.handlers.clear()

    formatteur = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for handler in (logging.StreamHandler(), logging.FileHandler("logs/pipeline.log", encoding="utf-8")):
        handler.setFormatter(formatteur)
        logger_.addHandler(handler)
    return logger_


logger = configurer_logging()

# Reconnaît une page TikTok classique (tiktok.com/@user/video/123...) ; tout le reste
# (ex. un lien direct vers un fichier CDN) est traité comme déjà prêt à télécharger.
_RE_PAGE_TIKTOK = re.compile(r"tiktok\.com/(@[\w.\-]+/video/\d+|v/\d+)", re.IGNORECASE)


# ======================================================================================
# 2. EXCEPTIONS + RETRY
# ======================================================================================


class PipelineError(Exception):
    """Erreur métier levée par une étape du pipeline, avec un message de contexte clair."""


async def _avec_retry(coro_factory, *, tentatives: int = 3, delai_initial: float = 1.5, etape: str = ""):
    """Exécute coro_factory() avec plusieurs tentatives et un backoff progressif."""
    derniere_erreur: Optional[Exception] = None
    for tentative in range(1, tentatives + 1):
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001 - on veut vraiment tout capturer ici
            derniere_erreur = exc
            logger.warning("[%s] tentative %s/%s échouée : %s", etape, tentative, tentatives, exc)
            if tentative < tentatives:
                await asyncio.sleep(delai_initial * tentative)
    raise PipelineError(f"Échec définitif de l'étape « {etape} » après {tentatives} tentatives.") from derniere_erreur


# ======================================================================================
# 3. CONFIGURATION
# ======================================================================================


def _env(nom: str, defaut: str = "") -> str:
    """os.getenv, mais en retirant espaces/retours à la ligne accidentels (copier-coller depuis Render)."""
    return os.getenv(nom, defaut).strip()


@dataclass
class PipelineConfig:
    """Toute la configuration, lue depuis les variables d'environnement (.env)."""

    # --- TikTok / RapidAPI (recherche, réutilisée pour la vidéo source ET le B-roll) ---
    rapidapi_key: str = ""
    rapidapi_host: str = "tiktok-scraper7.p.rapidapi.com"
    min_vues: int = 1_000_000          # seuil "viral" pour la vidéo source
    min_vues_broll: int = 0            # seuil (facultatif) pour les vidéos B-roll

    # --- Résolution + téléchargement (sans watermark) ---
    tikwm_base_url: str = "https://www.tikwm.com/api/"
    cobalt_api_url: str = ""           # instance auto-hébergée/autorisée ; vide = repli désactivé
    quantite_broll: int = 15
    concurrence_telechargement: int = 5

    # --- Transcription ---
    transcription_backend: str = "openai_api"  # "openai_api" ou "local_whisper"
    openai_api_key: str = ""
    whisper_local_model: str = "base"

    # --- Réécriture IA ---
    ai_provider: str = "openai"        # "openai", "anthropic" ou "gemini"
    anthropic_api_key: str = ""
    openai_model: str = "gpt-4o"
    anthropic_model: str = "claude-3-5-sonnet-20241022"
    gemini_api_keys: list[str] = field(default_factory=list)  # plusieurs clés -> rotation anti-quota
    gemini_model: str = "gemini-2.5-flash"

    # --- Notification ---
    notifier_actif: bool = True
    canal_notification: str = "discord"  # "discord" ou "telegram"
    discord_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @classmethod
    def depuis_environnement(cls) -> "PipelineConfig":
        """Charge la configuration depuis .env et valide les clés requises selon les backends choisis."""
        load_dotenv()

        config = cls(
            rapidapi_key=_env("RAPIDAPI_KEY"),
            rapidapi_host=_env("RAPIDAPI_HOST", cls.rapidapi_host),
            min_vues=int(_env("MIN_VUES", "1000000")),
            min_vues_broll=int(_env("MIN_VUES_BROLL", "0")),
            cobalt_api_url=_env("COBALT_API_URL"),
            quantite_broll=int(_env("BROLL_COUNT", "15")),
            concurrence_telechargement=int(_env("DOWNLOAD_CONCURRENCY", "5")),
            transcription_backend=_env("TRANSCRIPTION_BACKEND", "openai_api"),
            openai_api_key=_env("OPENAI_API_KEY"),
            whisper_local_model=_env("WHISPER_LOCAL_MODEL", "base"),
            ai_provider=_env("AI_PROVIDER", "openai"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            openai_model=_env("OPENAI_MODEL", "gpt-4o"),
            anthropic_model=_env("ANTHROPIC_MODEL", "claude-3-5-sonnet-20241022"),
            gemini_api_keys=[cle.strip() for cle in _env("GEMINI_API_KEYS", _env("GEMINI_API_KEY")).split(",") if cle.strip()],
            gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
            notifier_actif=_env("NOTIFIER_ACTIF", "true").lower() in ("1", "true", "yes", "oui"),
            canal_notification=_env("NOTIFICATION_CHANNEL", "discord"),
            discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
        )
        config._valider()
        return config

    def _valider(self) -> None:
        manquantes: list[str] = []
        if not self.rapidapi_key:
            manquantes.append("RAPIDAPI_KEY")
        if self.transcription_backend == "openai_api" and not self.openai_api_key:
            manquantes.append("OPENAI_API_KEY (transcription via l'API OpenAI)")
        if self.ai_provider == "openai" and not self.openai_api_key:
            manquantes.append("OPENAI_API_KEY (réécriture GPT-4o)")
        if self.ai_provider == "anthropic" and not self.anthropic_api_key:
            manquantes.append("ANTHROPIC_API_KEY (réécriture Claude)")
        if self.ai_provider == "gemini" and not self.gemini_api_keys:
            manquantes.append("GEMINI_API_KEYS (une ou plusieurs clés, séparées par des virgules)")
        if self.notifier_actif and self.canal_notification == "discord" and not self.discord_webhook_url:
            manquantes.append("DISCORD_WEBHOOK_URL (ou mettez NOTIFIER_ACTIF=false)")
        if self.notifier_actif and self.canal_notification == "telegram" and not (
            self.telegram_bot_token and self.telegram_chat_id
        ):
            manquantes.append("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (ou mettez NOTIFIER_ACTIF=false)")

        if manquantes:
            raise PipelineError("Variables d'environnement manquantes dans .env : " + ", ".join(manquantes))


# ======================================================================================
# 4. PIPELINE PRINCIPAL
# ======================================================================================


class TikTokAutomationPipeline:
    """
    Orchestre le pipeline complet :
      1. rechercher_video_virale   -> trouve la vidéo TikTok la plus vue sur une niche
      2. telecharger_audio         -> télécharge la vidéo + extrait l'audio (ffmpeg)
      3. transcrire_audio          -> Whisper (API OpenAI ou modèle local)
      4. reecrire_script           -> réécriture + détection du thème B-roll (IA au choix)
      5. envoyer_notification      -> Discord (webhook) ou Telegram
      6. rechercher_broll_tiktok   -> cherche des vidéos TikTok sur le thème détecté
      7. telecharger_plusieurs_videos_tiktok -> télécharge tout, sans watermark

    NOTE IMPORTANTE : la marketplace RapidAPI propose des dizaines de "TikTok Scraper"
    différents, chacun avec son propre schéma JSON. `_extraire_liste_videos` cible un
    format courant (type "tiktok-scraper7") — adaptez-le à l'API que vous souscrivez.
    """

    PROMPT_SYSTEME_REECRITURE = (
        "Tu es un scénariste spécialisé dans les vidéos courtes virales (TikTok/Reels/Shorts). "
        "On te donne la transcription brute d'une vidéo. Tu dois répondre UNIQUEMENT avec un "
        "objet JSON valide, sans texte avant ni après, sans balises markdown, avec exactement "
        "deux clés :\n"
        '- "script_modifie" : une réécriture du script, optimisée pour la rétention '
        "(accroche forte dans les 3 premières secondes, rythme soutenu, phrases courtes), "
        "dans la même langue que la transcription d'origine.\n"
        '- "mot_cle_broll" : un thème visuel court (2 à 5 mots) décrivant l\'objet ou le '
        "concept principal à illustrer en B-roll, SANS aucune mention de texte à l'écran.\n"
        "Réponds strictement avec ce JSON, rien d'autre."
    )

    def __init__(self, config: PipelineConfig):
        self.config = config
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self) -> "TikTokAutomationPipeline":
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90))
        return self

    async def __aexit__(self, *_exc) -> None:
        if self.session:
            await self.session.close()

    def _s(self) -> aiohttp.ClientSession:
        if self.session is None:
            raise PipelineError("Utilisez le pipeline via `async with TikTokAutomationPipeline(...) as p:`.")
        return self.session

    # ---------------------------------------------------------------------------- RECHERCHE TIKTOK
    async def _rechercher_videos_tiktok(self, mot_cle: str, limite: int, min_vues: int = 0) -> list[dict[str, Any]]:
        """Recherche RapidAPI générique, triée par vues décroissantes. Base commune aux étapes 1 et 6."""
        url = f"https://{self.config.rapidapi_host}/feed/search"
        headers = {
            "X-RapidAPI-Key": self.config.rapidapi_key,
            "X-RapidAPI-Host": self.config.rapidapi_host,
        }
        params = {"keywords": mot_cle, "count": str(max(limite, 20)), "cursor": "0", "region": "FR"}

        async def _appel():
            async with self._s().get(url, headers=headers, params=params) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"RapidAPI a répondu {resp.status} : {texte[:300]}")
                return await resp.json()

        data = await _avec_retry(_appel, etape=f"recherche TikTok « {mot_cle} »")

        videos = [v for v in self._extraire_liste_videos(data) if v["vues"] >= min_vues]
        videos.sort(key=lambda v: v["vues"], reverse=True)
        return videos[:limite]

    @staticmethod
    def _extraire_liste_videos(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalise plusieurs formats de réponse RapidAPI courants. À ajuster selon votre fournisseur."""
        bruts = (
            data.get("data", {}).get("videos")
            or data.get("data", {}).get("aweme_list")
            or data.get("videos")
            or []
        )
        resultats = []
        for item in bruts:
            try:
                video_id = str(item.get("video_id") or item.get("aweme_id") or item.get("id"))
                auteur = (
                    item.get("author", {}).get("unique_id")
                    or item.get("author", {}).get("uniqueId")
                    or item.get("author_unique_id")
                    or ""
                )
                vues = int(
                    item.get("play_count")
                    or item.get("statistics", {}).get("play_count")
                    or item.get("stats", {}).get("playCount")
                    or 0
                )
                lien = item.get("play") or item.get("video_url")
                if not lien and auteur and video_id and video_id != "None":
                    lien = f"https://www.tiktok.com/@{auteur}/video/{video_id}"
                if lien:
                    resultats.append({"id": video_id, "url": lien, "vues": vues, "auteur": auteur})
            except (TypeError, ValueError, AttributeError):
                continue
        return resultats

    # ================================================================== 1. VIDÉO VIRALE
    async def rechercher_video_virale(self, niche: str) -> dict[str, Any]:
        """Trouve la vidéo TikTok la plus vue sur `niche`, au-dessus de `min_vues`."""
        logger.info("Recherche de la vidéo virale pour la niche : %s", niche)
        videos = await self._rechercher_videos_tiktok(niche, limite=20, min_vues=self.config.min_vues)
        if not videos:
            seuil = f"{self.config.min_vues:,}".replace(",", " ")
            raise PipelineError(f"Aucune vidéo au-dessus de {seuil} vues pour « {niche} ».")

        meilleure = videos[0]  # déjà trié par vues décroissantes
        vues_fmt = f"{meilleure['vues']:,}".replace(",", " ")
        logger.info("Vidéo retenue : %s (%s vues)", meilleure["url"], vues_fmt)
        return meilleure

    # ================================================================== 2. TÉLÉCHARGEMENT + AUDIO
    async def telecharger_video_tiktok(self, tiktok_url: str, destination: Optional[Path] = None) -> Path:
        """Résout puis télécharge une vidéo TikTok d'origine, sans watermark."""
        lien_media, extension = await self._resoudre_video(tiktok_url)

        if destination is None:
            horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
            dossier = Path("downloads") / f"tiktok_{horodatage}"
            dossier.mkdir(parents=True, exist_ok=True)
            destination = dossier / f"video{extension}"

        await self._telecharger_fichier(lien_media, destination)
        logger.info("Vidéo TikTok téléchargée : %s", destination)
        return destination

    async def telecharger_plusieurs_videos_tiktok(self, urls: list[str], prefixe_dossier: str = "tiktok") -> list[Path]:
        """
        Télécharge plusieurs vidéos TikTok en parallèle (sémaphore borné) dans un dossier
        horodaté commun `./downloads/{prefixe_dossier}_YYYYMMDD_HHMMSS/`. Les échecs
        individuels sont journalisés et ignorés ; seules les réussites sont retournées.
        """
        horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
        dossier = Path("downloads") / f"{prefixe_dossier}_{horodatage}"
        dossier.mkdir(parents=True, exist_ok=True)
        logger.info("Téléchargement de %s vidéo(s) TikTok dans %s", len(urls), dossier)

        semaphore = asyncio.Semaphore(self.config.concurrence_telechargement)

        async def _telecharger_une(index: int, url: str) -> Optional[Path]:
            async with semaphore:
                try:
                    return await self.telecharger_video_tiktok(url, dossier / f"video_{index:02d}.mp4")
                except PipelineError as exc:
                    logger.error("Échec de téléchargement pour %s : %s", url, exc)
                    return None

        resultats = await asyncio.gather(*(_telecharger_une(i, u) for i, u in enumerate(urls, start=1)))
        reussies = [r for r in resultats if r is not None]

        logger.info("%s/%s vidéo(s) téléchargée(s) avec succès dans %s", len(reussies), len(urls), dossier)
        if not reussies:
            raise PipelineError("Aucune des vidéos TikTok fournies n'a pu être téléchargée.")
        return reussies

    async def telecharger_audio(self, tiktok_url: str, dossier_travail: Path) -> Path:
        """Télécharge la vidéo TikTok source puis en extrait la piste audio complète (ffmpeg)."""
        dossier_travail.mkdir(parents=True, exist_ok=True)
        chemin_brut = await self.telecharger_video_tiktok(tiktok_url, dossier_travail / "source.mp4")

        chemin_audio = dossier_travail / "audio.mp3"
        if chemin_brut.suffix in (".mp3", ".m4a", ".wav", ".ogg"):
            chemin_brut.rename(chemin_audio)
        else:
            await self._extraire_audio_ffmpeg(chemin_brut, chemin_audio)

        logger.info("Audio prêt pour transcription : %s", chemin_audio)
        return chemin_audio

    async def _resoudre_video(self, tiktok_url: str) -> tuple[str, str]:
        """
        Tente TikWM puis Cobalt en repli. Retourne (lien_direct, extension_fichier).

        Certains fournisseurs RapidAPI renvoient déjà un lien direct vers le fichier vidéo
        (CDN, ex. tiktokcdn-us.com/...) plutôt qu'une page tiktok.com/@user/video/123 : dans
        ce cas on télécharge directement, TikWM/Cobalt n'acceptant que les pages classiques.
        """
        if not _RE_PAGE_TIKTOK.search(tiktok_url):
            logger.info("Lien déjà direct (pas une page tiktok.com) : téléchargement immédiat.")
            return tiktok_url, ".mp4"

        try:
            return await self._resoudre_via_tikwm(tiktok_url)
        except Exception as exc:
            logger.warning("TikWM a échoué (%s), tentative via Cobalt...", exc)
            if not self.config.cobalt_api_url:
                raise PipelineError(
                    "TikWM a échoué et aucune instance Cobalt (COBALT_API_URL) n'est configurée."
                ) from exc
            return await self._resoudre_via_cobalt(tiktok_url)

    async def _resoudre_via_tikwm(self, tiktok_url: str) -> tuple[str, str]:
        async def _appel():
            async with self._s().get(self.config.tikwm_base_url, params={"url": tiktok_url, "hd": "1"}) as resp:
                data = await resp.json()
            if data.get("code") != 0 or "data" not in data:
                raise PipelineError(f"Réponse TikWM invalide : {data.get('msg', data)}")
            return data["data"]

        data = await _avec_retry(_appel, etape="résolution TikWM")
        lien = data.get("play") or data.get("wmplay")
        if not lien:
            raise PipelineError("TikWM n'a renvoyé aucun lien vidéo exploitable.")
        return lien, ".mp4"

    async def _resoudre_via_cobalt(self, tiktok_url: str) -> tuple[str, str]:
        """
        Repli via une instance Cobalt auto-hébergée ou explicitement autorisée (l'instance
        publique api.cobalt.tools impose une protection anti-bot et n'est plus utilisable
        librement) : self-hostez la vôtre et renseignez COBALT_API_URL.
        """
        headers = {"Accept": "application/json", "Content-Type": "application/json"}

        async def _appel():
            payload = {"url": tiktok_url, "downloadMode": "auto"}
            async with self._s().post(self.config.cobalt_api_url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"Cobalt a répondu {resp.status} : {texte[:300]}")
                return await resp.json()

        data = await _avec_retry(_appel, etape="résolution Cobalt")
        lien = data.get("url")
        if not lien:
            raise PipelineError(f"Cobalt n'a renvoyé aucun lien exploitable : {data}")
        return lien, ".mp4"

    async def _extraire_audio_ffmpeg(self, source: Path, destination: Path) -> None:
        """Extrait la piste audio d'un fichier vidéo via le binaire système ffmpeg (préinstallé sur Render)."""
        commande = ["ffmpeg", "-y", "-i", str(source), "-vn", "-acodec", "libmp3lame", "-q:a", "2", str(destination)]
        processus = await asyncio.create_subprocess_exec(*commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        _, stderr = await processus.communicate()
        if processus.returncode != 0:
            raise PipelineError(
                "Échec de l'extraction audio via ffmpeg (est-il installé ? `ffmpeg -version`) : "
                + stderr.decode(errors="ignore")[-500:]
            )

    async def _telecharger_fichier(self, url: str, destination: Path) -> None:
        """Télécharge un fichier en streaming via aiohttp, avec retry automatique."""

        async def _appel():
            async with self._s().get(url) as resp:
                if resp.status != 200:
                    raise PipelineError(f"Téléchargement échoué ({resp.status}) pour {url}")
                async with aiofiles.open(destination, "wb") as f:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        await f.write(chunk)

        await _avec_retry(_appel, etape=f"téléchargement {destination.name}")

    # ================================================================== 3. TRANSCRIPTION
    async def transcrire_audio(self, audio_path: Path) -> str:
        logger.info("Transcription de %s (backend: %s)", audio_path, self.config.transcription_backend)
        if self.config.transcription_backend == "local_whisper":
            texte = await asyncio.to_thread(self._transcrire_whisper_local, audio_path)
        else:
            texte = await self._transcrire_whisper_api(audio_path)

        if not texte.strip():
            raise PipelineError("La transcription a renvoyé un texte vide.")
        logger.info("Transcription obtenue (%s caractères).", len(texte))
        return texte.strip()

    async def _transcrire_whisper_api(self, audio_path: Path) -> str:
        url = "https://api.openai.com/v1/audio/transcriptions"
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}"}
        contenu_audio = audio_path.read_bytes()

        async def _appel():
            data = aiohttp.FormData()
            data.add_field("model", "whisper-1")
            data.add_field("file", contenu_audio, filename=audio_path.name, content_type="audio/mpeg")
            async with self._s().post(url, headers=headers, data=data) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"Whisper API a répondu {resp.status} : {texte[:300]}")
                reponse = await resp.json()
            return reponse.get("text", "")

        return await _avec_retry(_appel, etape="transcription Whisper API")

    def _transcrire_whisper_local(self, audio_path: Path) -> str:
        """Exécuté dans un thread séparé : bloquant, CPU/GPU intensif (package `openai-whisper`)."""
        import whisper  # import tardif : dépendance lourde et optionnelle

        modele = whisper.load_model(self.config.whisper_local_model)
        return modele.transcribe(str(audio_path)).get("text", "")

    # ================================================================== 4. RÉÉCRITURE IA
    async def reecrire_script(self, script_brut: str) -> dict[str, str]:
        logger.info("Réécriture du script via %s", self.config.ai_provider)
        appels = {
            "anthropic": self._appel_anthropic,
            "gemini": self._appel_gemini,
        }
        brut = await appels.get(self.config.ai_provider, self._appel_openai_chat)(script_brut)

        resultat = self._parser_json_strict(brut)
        for cle in ("script_modifie", "mot_cle_broll"):
            if cle not in resultat or not str(resultat[cle]).strip():
                raise PipelineError(f"Réponse IA invalide : clé « {cle} » manquante ou vide.")

        logger.info("Thème B-roll détecté : %s", resultat["mot_cle_broll"])
        return resultat

    async def _appel_openai_chat(self, script_brut: str) -> str:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.config.openai_api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.config.openai_model,
            "messages": [
                {"role": "system", "content": self.PROMPT_SYSTEME_REECRITURE},
                {"role": "user", "content": script_brut},
            ],
            "temperature": 0.7,
            "response_format": {"type": "json_object"},
        }

        async def _appel():
            async with self._s().post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"OpenAI a répondu {resp.status} : {texte[:300]}")
                data = await resp.json()
            return data["choices"][0]["message"]["content"]

        return await _avec_retry(_appel, etape="réécriture GPT-4o")

    async def _appel_anthropic(self, script_brut: str) -> str:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.config.anthropic_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.config.anthropic_model,
            "max_tokens": 2000,
            "system": self.PROMPT_SYSTEME_REECRITURE,
            "messages": [{"role": "user", "content": script_brut}],
        }

        async def _appel():
            async with self._s().post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"Anthropic a répondu {resp.status} : {texte[:300]}")
                data = await resp.json()
            return data["content"][0]["text"]

        return await _avec_retry(_appel, etape="réécriture Claude")

    async def _appel_gemini(self, script_brut: str) -> str:
        """
        Si plusieurs clés sont configurées (GEMINI_API_KEYS), elles sont tentées dans un
        ordre aléatoire : si une clé est à quota (429) ou invalide, la suivante prend le
        relais — utile pour cumuler plusieurs quotas gratuits.
        """
        cles = self.config.gemini_api_keys
        if not cles:
            raise PipelineError("Aucune clé Gemini configurée (GEMINI_API_KEYS).")

        derniere_erreur: Optional[Exception] = None
        for cle in random.sample(cles, len(cles)):
            try:
                return await self._appel_gemini_avec_cle(script_brut, cle)
            except Exception as exc:  # noqa: BLE001
                derniere_erreur = exc
                logger.warning("Clé Gemini indisponible (%s...), tentative avec la clé suivante.", cle[:6])

        raise PipelineError(f"Toutes les clés Gemini ont échoué : {derniere_erreur}") from derniere_erreur

    async def _appel_gemini_avec_cle(self, script_brut: str, cle_api: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.gemini_model}:generateContent"
        headers = {"x-goog-api-key": cle_api, "Content-Type": "application/json"}
        payload = {
            "systemInstruction": {"parts": [{"text": self.PROMPT_SYSTEME_REECRITURE}]},
            "contents": [{"role": "user", "parts": [{"text": script_brut}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7},
        }

        async def _appel():
            async with self._s().post(url, headers=headers, json=payload) as resp:
                if resp.status != 200:
                    texte = await resp.text()
                    raise PipelineError(f"Gemini a répondu {resp.status} : {texte[:300]}")
                data = await resp.json()
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError) as exc:
                raise PipelineError(f"Réponse Gemini inattendue : {data}") from exc

        return await _avec_retry(_appel, tentatives=2, etape="réécriture Gemini")

    @staticmethod
    def _parser_json_strict(brut: str) -> dict[str, Any]:
        """Parse le JSON renvoyé par le LLM, en nettoyant d'éventuelles balises ```json résiduelles."""
        nettoye = brut.strip()
        if nettoye.startswith("```"):
            nettoye = nettoye.strip("`")
            if "\n" in nettoye:
                nettoye = nettoye.split("\n", 1)[-1]
            if nettoye.lower().startswith("json"):
                nettoye = nettoye[4:]
        try:
            return json.loads(nettoye)
        except json.JSONDecodeError as exc:
            raise PipelineError(f"Le modèle n'a pas renvoyé un JSON valide : {brut[:300]}") from exc

    # ================================================================== 5. NOTIFICATION
    async def envoyer_notification(self, script_modifie: str) -> None:
        logger.info("Envoi de la notification via %s", self.config.canal_notification)
        if self.config.canal_notification == "telegram":
            await self._notifier_telegram(script_modifie)
        else:
            await self._notifier_discord(script_modifie)
        logger.info("Notification envoyée.")

    async def _notifier_discord(self, texte: str) -> None:
        for morceau in self._decouper_texte(texte, 1900):
            async def _appel(morceau=morceau):
                payload = {"content": f"📝 **Nouveau script généré**\n\n{morceau}"}
                async with self._s().post(self.config.discord_webhook_url, json=payload) as resp:
                    if resp.status not in (200, 204):
                        corps = await resp.text()
                        raise PipelineError(f"Webhook Discord a répondu {resp.status} : {corps[:300]}")

            await _avec_retry(_appel, etape="notification Discord")

    async def _notifier_telegram(self, texte: str) -> None:
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"
        for morceau in self._decouper_texte(texte, 3800):
            async def _appel(morceau=morceau):
                payload = {"chat_id": self.config.telegram_chat_id, "text": morceau}
                async with self._s().post(url, json=payload) as resp:
                    if resp.status != 200:
                        corps = await resp.text()
                        raise PipelineError(f"Telegram a répondu {resp.status} : {corps[:300]}")

            await _avec_retry(_appel, etape="notification Telegram")

    @staticmethod
    def _decouper_texte(texte: str, taille_max: int) -> list[str]:
        return [texte[i:i + taille_max] for i in range(0, len(texte), taille_max)] or [""]

    # ================================================================== 6. B-ROLL (TikTok, sans watermark)
    async def rechercher_broll_tiktok(self, mot_cle: str) -> list[str]:
        """Cherche des vidéos TikTok sur le thème détecté par l'IA, à utiliser comme B-roll."""
        logger.info("Recherche de %s vidéo(s) TikTok B-roll pour « %s »", self.config.quantite_broll, mot_cle)
        videos = await self._rechercher_videos_tiktok(
            mot_cle, limite=self.config.quantite_broll, min_vues=self.config.min_vues_broll
        )
        if not videos:
            raise PipelineError(f"Aucune vidéo TikTok trouvée pour le thème B-roll « {mot_cle} ».")
        return [v["url"] for v in videos]

    # ================================================================== ORCHESTRATION COMPLÈTE
    async def run(self, niche: str) -> dict[str, Any]:
        """Exécute le pipeline complet, du repérage de la vidéo virale au B-roll téléchargé."""
        resultats: dict[str, Any] = {"niche": niche}
        try:
            video = await self.rechercher_video_virale(niche)
            resultats["video_source"] = video

            dossier_travail = Path("downloads") / f"travail_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            audio_path = await self.telecharger_audio(video["url"], dossier_travail)

            script_brut = await self.transcrire_audio(audio_path)
            resultats["script_brut"] = script_brut

            reecriture = await self.reecrire_script(script_brut)
            resultats.update(reecriture)

            if self.config.notifier_actif:
                await self.envoyer_notification(reecriture["script_modifie"])
            else:
                logger.info("Notification désactivée (NOTIFIER_ACTIF=false).")

            liens_broll = await self.rechercher_broll_tiktok(reecriture["mot_cle_broll"])
            chemins_broll = await self.telecharger_plusieurs_videos_tiktok(liens_broll, prefixe_dossier="broll")
            resultats["dossier_broll"] = str(chemins_broll[0].parent)
            resultats["nb_broll_telecharges"] = len(chemins_broll)

            logger.info("Pipeline terminé avec succès pour la niche « %s ».", niche)
            return resultats

        except PipelineError as exc:
            logger.error("Le pipeline s'est arrêté : %s", exc)
            resultats["erreur"] = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Erreur inattendue dans le pipeline.")
            resultats["erreur"] = str(exc)
            raise PipelineError(f"Erreur inattendue : {exc}") from exc


# ======================================================================================
# 5. BOT DISCORD
# ======================================================================================

# Intents par défaut : suffisant, on n'utilise que des commandes slash (pas de lecture des
# messages), donc aucun intent privilégié à activer sur le portail développeur Discord.
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

MAX_FICHIERS_PAR_MESSAGE = 10  # limite Discord pour les pièces jointes d'un même message


def _extraire_urls(texte: str) -> list[str]:
    """Extrait une liste d'URLs distinctes depuis un texte (séparateurs : espace, virgule, retour à la ligne)."""
    vues: set[str] = set()
    urls: list[str] = []
    for morceau in re.split(r"[\s,]+", texte.strip()):
        if morceau.startswith("http") and morceau not in vues:
            vues.add(morceau)
            urls.append(morceau)
    return urls


def _limite_upload(interaction: discord.Interaction) -> int:
    return interaction.guild.filesize_limit if interaction.guild else 25 * 1024 * 1024


def _lire_niches() -> list[str]:
    return [n.strip() for n in _env("NICHES", "voitures").split(",") if n.strip()]


@tasks.loop(hours=float(_env("AUTO_RUN_INTERVAL_HOURS", "24")))
async def boucle_automatique() -> None:
    """Exécute le pipeline tout seul, à intervalle régulier, en tournant sur la liste de niches."""
    niches = _lire_niches()
    if not niches:
        return
    niche = niches[boucle_automatique.current_loop % len(niches)]
    logger.info("Exécution automatique programmée pour la niche : %s", niche)
    try:
        config = PipelineConfig.depuis_environnement()
        async with TikTokAutomationPipeline(config) as pipeline:
            await pipeline.run(niche)
    except PipelineError as exc:
        logger.error("Exécution automatique échouée pour « %s » : %s", niche, exc)
    except Exception:  # noqa: BLE001
        logger.exception("Erreur inattendue lors de l'exécution automatique.")


_boucle_demarree = False


@bot.event
async def on_ready() -> None:
    global _boucle_demarree
    guild_id = _env("DISCORD_GUILD_ID")
    try:
        if guild_id:
            guilde = discord.Object(id=int(guild_id))
            bot.tree.copy_global_to(guild=guilde)
            await bot.tree.sync(guild=guilde)
            logger.info("Commandes slash synchronisées sur le serveur %s (instantané).", guild_id)
        else:
            await bot.tree.sync()
            logger.info("Commandes slash synchronisées globalement (peut prendre jusqu'à 1h).")
    except Exception:
        logger.exception("Échec de la synchronisation des commandes slash.")
    logger.info("Bot connecté en tant que %s.", bot.user)

    auto_actif = _env("AUTO_RUN_ENABLED", "true").lower() in ("1", "true", "yes", "oui")
    if auto_actif and not _boucle_demarree:
        boucle_automatique.start()
        _boucle_demarree = True
        logger.info(
            "Exécution automatique activée : niches=%s, toutes les %sh.",
            _lire_niches(), _env("AUTO_RUN_INTERVAL_HOURS", "24"),
        )


async def _lancer_pipeline_et_repondre(interaction: discord.Interaction, niche: str) -> None:
    """Lance le pipeline pour `niche` et répond dans l'interaction (utilisé par /broll et /start)."""
    try:
        config = PipelineConfig.depuis_environnement()
    except PipelineError as exc:
        await interaction.followup.send(f"❌ Configuration invalide : {exc}")
        return

    try:
        async with TikTokAutomationPipeline(config) as pipeline:
            resultats = await pipeline.run(niche)
    except PipelineError as exc:
        await interaction.followup.send(f"❌ Le pipeline a échoué : {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue pendant l'exécution du pipeline.")
        await interaction.followup.send(f"❌ Erreur inattendue : {exc}")
        return

    script = resultats.get("script_modifie", "") or ""
    embed = discord.Embed(
        title=f"✅ Pipeline terminé — {niche}",
        description=script[:4000] if script else "(script vide)",
        color=discord.Color.green(),
    )
    embed.add_field(name="🎯 Thème B-roll", value=resultats.get("mot_cle_broll", "?"), inline=False)
    embed.add_field(
        name="📁 B-roll téléchargé",
        value=f"{resultats.get('nb_broll_telecharges', 0)} vidéo(s) dans `{resultats.get('dossier_broll', '?')}`",
        inline=False,
    )
    if len(script) > 4000:
        embed.set_footer(text="Script tronqué dans cet aperçu.")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="broll", description="Lance le pipeline TikTok -> B-roll pour une niche donnée")
@app_commands.describe(niche="Mot-clé / niche à rechercher sur TikTok")
async def commande_broll(interaction: discord.Interaction, niche: str) -> None:
    # Le pipeline peut prendre plus de 3 secondes : on défère la réponse immédiatement.
    await interaction.response.defer(thinking=True)
    await _lancer_pipeline_et_repondre(interaction, niche)


@bot.tree.command(
    name="start",
    description="Lance tout de suite le pipeline sur la niche configurée (NICHES), sans attendre le cycle automatique",
)
async def commande_start(interaction: discord.Interaction) -> None:
    await interaction.response.defer(thinking=True)
    niches = _lire_niches()
    if not niches:
        await interaction.followup.send("❌ Aucune niche configurée (variable NICHES dans .env).")
        return

    # Reprend la même niche que celle sur laquelle la boucle automatique en est actuellement.
    index = boucle_automatique.current_loop % len(niches) if boucle_automatique.is_running() else 0
    niche = niches[index]
    await interaction.followup.send(f"🚀 Lancement immédiat sur la niche « {niche} »...")
    await _lancer_pipeline_et_repondre(interaction, niche)


@bot.tree.command(
    name="tiktok",
    description="Télécharge une ou plusieurs vidéos TikTok (sans watermark) et les envoie ici",
)
@app_commands.describe(urls="Un ou plusieurs liens TikTok (séparés par un espace, une virgule ou un retour à la ligne)")
async def commande_tiktok(interaction: discord.Interaction, urls: str) -> None:
    await interaction.response.defer(thinking=True)

    liens = _extraire_urls(urls)
    if not liens:
        await interaction.followup.send("❌ Aucun lien TikTok valide détecté dans le message.")
        return
    if len(liens) > MAX_FICHIERS_PAR_MESSAGE:
        await interaction.followup.send(
            f"⚠️ {len(liens)} liens fournis : seuls les {MAX_FICHIERS_PAR_MESSAGE} premiers seront "
            f"traités (limite Discord de {MAX_FICHIERS_PAR_MESSAGE} pièces jointes par message)."
        )
        liens = liens[:MAX_FICHIERS_PAR_MESSAGE]

    try:
        config = PipelineConfig.depuis_environnement()
    except PipelineError as exc:
        await interaction.followup.send(f"❌ Configuration invalide : {exc}")
        return

    try:
        async with TikTokAutomationPipeline(config) as pipeline:
            chemins = await pipeline.telecharger_plusieurs_videos_tiktok(liens)
    except PipelineError as exc:
        await interaction.followup.send(f"❌ Téléchargement échoué : {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Erreur inattendue pendant /tiktok.")
        await interaction.followup.send(f"❌ Erreur inattendue : {exc}")
        return

    limite = _limite_upload(interaction)
    fichiers_ok = [c for c in chemins if c.stat().st_size <= limite]
    fichiers_trop_lourds = [c for c in chemins if c.stat().st_size > limite]

    if fichiers_ok:
        await interaction.followup.send(
            content=f"✅ {len(fichiers_ok)}/{len(liens)} vidéo(s) téléchargée(s).",
            files=[discord.File(c) for c in fichiers_ok],
        )
    if fichiers_trop_lourds:
        noms = "\n".join(f"`{c}`" for c in fichiers_trop_lourds)
        await interaction.followup.send(
            f"⚠️ {len(fichiers_trop_lourds)} vidéo(s) trop lourde(s) pour Discord "
            f"(limite {limite / 1_048_576:.0f} Mo ici), conservée(s) côté serveur :\n{noms}"
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    logger.error("Erreur de commande slash : %s", error)
    message = f"❌ Erreur : {error}"
    if interaction.response.is_done():
        await interaction.followup.send(message)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# ======================================================================================
# 6. SERVEUR KEEP-ALIVE (requis par Render pour un Web Service : doit écouter sur $PORT)
# ======================================================================================


async def _requete_sante(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _demarrer_serveur_keepalive() -> None:
    app = web.Application()
    app.router.add_get("/", _requete_sante)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(_env("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Serveur keep-alive démarré sur le port %s.", port)


# ======================================================================================
# 7. POINTS D'ENTRÉE
# ======================================================================================


async def _executer_pipeline_cli(niche: str) -> None:
    """Exécute le pipeline une seule fois, sans démarrer le bot (utile en local/debug)."""
    config = PipelineConfig.depuis_environnement()
    async with TikTokAutomationPipeline(config) as pipeline:
        resultats = await pipeline.run(niche)
    affichage = {k: v for k, v in resultats.items() if k != "script_brut"}
    print(json.dumps(affichage, indent=2, ensure_ascii=False))


async def _demarrer_bot_discord() -> None:
    """Démarre le bot Discord persistant + le serveur keep-alive (mode par défaut sur Render)."""
    token = _env("DISCORD_BOT_TOKEN")
    if not token:
        raise PipelineError("DISCORD_BOT_TOKEN manquant dans .env : impossible de démarrer le bot.")

    PipelineConfig.depuis_environnement()  # valide la config dès le démarrage (fail-fast)

    await asyncio.gather(_demarrer_serveur_keepalive(), bot.start(token))


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline d'automatisation TikTok -> B-roll")
    parser.add_argument(
        "--niche",
        default=None,
        help="Exécute le pipeline une fois pour cette niche, en CLI, sans démarrer le bot Discord.",
    )
    args = parser.parse_args()

    try:
        if args.niche:
            asyncio.run(_executer_pipeline_cli(args.niche))
        else:
            asyncio.run(_demarrer_bot_discord())
    except PipelineError as exc:
        logger.error("Arrêt : %s", exc)
        raise SystemExit(1)
    except KeyboardInterrupt:
        logger.info("Interrompu par l'utilisateur.")


if __name__ == "__main__":
    main()
