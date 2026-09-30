"""Pipeline multi-source économe en mémoire pour le studio vidéo.

Le module ne connaît ni FastAPI ni les secrets OAuth. Il reçoit les appels réseau
(TikWM/téléchargement/Gemini) depuis :mod:`app` et ne manipule qu'une preview à la
fois par slot d'analyse. Les originaux restent sur disque et sont utilisés par
l'unique encodage final FFmpeg.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import re
import shutil
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger("videoapp.montage")


class ErreurMontage(Exception):
    """Erreur de pipeline qui peut être affichée à l'utilisateur."""


class TravailAnnule(ErreurMontage):
    """Le propriétaire a demandé l'annulation du travail."""


@dataclass(frozen=True)
class ConfigurationMontage:
    dossier_travail: Path
    dossier_videos: Path
    duree_max_source: float = 180.0
    delai_ffmpeg: float = 240.0
    delai_gemini: float = 120.0
    delai_job: float = 570.0
    taille_max_apercu: int = 12 * 1024 * 1024
    budget_disque_sources: int = 700 * 1024 * 1024
    analyses_concurrentes: int = 1
    largeur: int = 720
    hauteur: int = 1280
    fps: int = 24
    preset: str = "ultrafast"
    crf: int = 23
    threads_ffmpeg: int = 1
    autoriser_1080: bool = False


class Rapporteur:
    """Petit adaptateur vers l'état d'un job, sans dépendance à FastAPI."""

    def __init__(
        self,
        mise_a_jour: Callable[..., None],
        est_annule: Callable[[], bool],
        temps_restant: Callable[[], float],
    ) -> None:
        self._mise_a_jour = mise_a_jour
        self._est_annule = est_annule
        self._temps_restant = temps_restant
        self.etape = "initialisation"

    def update(self, statut: str, progression: int, detail: str, **extras: Any) -> None:
        self.etape = detail
        self._mise_a_jour(statut=statut, progress=max(0, min(100, int(progression))), detail=detail, **extras)
        self.checkpoint()

    def checkpoint(self) -> None:
        if self._est_annule():
            raise TravailAnnule("Travail annulé à la demande de l’utilisateur.")
        if self._temps_restant() <= 0:
            raise ErreurMontage(
                f"Limite globale dépassée pendant « {self.etape} ». "
                "Réduis le nombre ou la durée des sources, puis relance le diagnostic."
            )

    def timeout(self, maximum: float) -> float:
        self.checkpoint()
        return max(0.2, min(maximum, self._temps_restant()))


# --------------------------------------------------------------------------------------
# Liens et métadonnées
# --------------------------------------------------------------------------------------


def normaliser_lien_tiktok(valeur: str) -> str:
    """Valide un lien TikTok et retire requête, fragment, port et slash final."""
    brut = valeur.strip()
    try:
        morceaux = urlsplit(brut)
        hote = (morceaux.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ErreurMontage("Lien TikTok illisible.") from exc
    if morceaux.scheme not in {"http", "https"} or not hote:
        raise ErreurMontage("Le lien doit commencer par http:// ou https://.")
    if hote != "tiktok.com" and not hote.endswith(".tiktok.com"):
        raise ErreurMontage("Ce lien ne pointe pas vers le domaine TikTok.")
    try:
        autorite_interdite = morceaux.username or morceaux.password or morceaux.port
    except ValueError as exc:
        raise ErreurMontage("Le lien TikTok contient un port invalide.") from exc
    if autorite_interdite:
        raise ErreurMontage("Le lien TikTok contient une autorité non autorisée.")
    chemin = re.sub(r"/{2,}", "/", morceaux.path).rstrip("/")
    if not chemin or chemin == "/":
        raise ErreurMontage("Le lien TikTok ne contient aucune vidéo.")
    return urlunsplit(("https", hote, chemin, "", ""))


def normaliser_liens_tiktok(liens: list[str]) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Retourne les liens uniques, les erreurs de forme et les doublons retirés."""
    valides: list[str] = []
    erreurs: list[dict[str, Any]] = []
    doublons: list[str] = []
    vus: set[str] = set()
    for index, lien in enumerate(liens):
        if not lien.strip():
            continue
        try:
            propre = normaliser_lien_tiktok(lien)
        except ErreurMontage as exc:
            erreurs.append({"index": index, "url": lien.strip(), "error": str(exc), "status": "invalid"})
            continue
        if propre in vus:
            doublons.append(propre)
            continue
        vus.add(propre)
        valides.append(propre)
    return valides, erreurs, doublons


async def executer_commande(
    commande: list[str], *, etape: str, timeout: float, sortie_attendue: Optional[Path] = None
) -> tuple[bytes, bytes]:
    """Exécute FFmpeg/FFprobe avec timeout et tue réellement le processus à l'annulation."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *commande, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise ErreurMontage(
            f"Outil système absent pendant « {etape} » : {commande[0]}. "
            "Utilise le Dockerfile fourni (FFmpeg et FFprobe y sont installés)."
        ) from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        proc.kill()
        await proc.communicate()
        if sortie_attendue:
            sortie_attendue.unlink(missing_ok=True)
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise ErreurMontage(f"Délai dépassé pendant « {etape} » ({int(timeout)} s maximum).") from exc
    if proc.returncode != 0:
        if sortie_attendue:
            sortie_attendue.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", errors="ignore").strip().splitlines()
        fin = " | ".join(detail[-3:])[-500:] if detail else "aucun détail FFmpeg"
        raise ErreurMontage(f"Échec pendant « {etape} » : {fin}")
    return stdout, stderr


async def sonder_video(source: str | Path, timeout: float = 25.0) -> dict[str, Any]:
    """Lit durée, dimensions, cadence et codec avec FFprobe, localement ou à distance."""
    commande = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name,avg_frame_rate,duration:format=duration,size",
        "-of", "json", str(source),
    ]
    stdout, _ = await executer_commande(commande, etape="lecture des informations techniques", timeout=timeout)
    try:
        donnees = json.loads(stdout)
        flux = donnees["streams"][0]
        fmt = donnees.get("format", {})
        duree = float(flux.get("duration") or fmt.get("duration"))
        largeur = int(flux["width"])
        hauteur = int(flux["height"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ErreurMontage("FFprobe n’a trouvé aucun flux vidéo exploitable.") from exc
    if not math.isfinite(duree) or duree <= 0 or largeur <= 0 or hauteur <= 0:
        raise ErreurMontage("Les informations techniques de cette vidéo sont invalides.")
    cadence_brute = str(flux.get("avg_frame_rate") or "0/1")
    try:
        numerateur, denominateur = cadence_brute.split("/", 1)
        cadence = float(numerateur) / max(float(denominateur), 1.0)
    except (ValueError, ZeroDivisionError):
        cadence = 0.0
    return {
        "duration": round(duree, 3),
        "width": largeur,
        "height": hauteur,
        "fps": round(cadence, 3),
        "codec": str(flux.get("codec_name") or "inconnu"),
        "size": int(fmt.get("size") or 0),
    }


Resolveur = Callable[[aiohttp.ClientSession, str], Awaitable[str]]
Telechargeur = Callable[[aiohttp.ClientSession, str, Path], Awaitable[None]]
AppelGemini = Callable[..., Awaitable[str]]


async def diagnostiquer_montage(
    liens: list[str],
    lien_reference: str,
    config: ConfigurationMontage,
    resolveur: Resolveur,
) -> dict[str, Any]:
    """Préflight sans téléchargement complet : TikWM puis FFprobe sur le flux distant."""
    propres, erreurs, doublons = normaliser_liens_tiktok(liens)
    if len(propres) > 20:
        erreurs.append({"url": "", "error": "20 liens uniques maximum.", "status": "invalid"})
        propres = propres[:20]

    resultats: list[Optional[dict[str, Any]]] = [None] * len(propres)
    semaphore = asyncio.Semaphore(2)
    delai_session = aiohttp.ClientTimeout(total=35, connect=12, sock_read=25)

    async with aiohttp.ClientSession(timeout=delai_session) as session:
        async def inspecter(index: int, url: str) -> None:
            async with semaphore:
                try:
                    direct = await resolveur(session, url)
                    infos = await sonder_video(direct, timeout=min(25.0, config.delai_ffmpeg))
                    if infos["duration"] > config.duree_max_source:
                        raise ErreurMontage(
                            f"Durée {infos['duration']:.1f} s : limite configurée "
                            f"à {config.duree_max_source:.0f} s par source."
                        )
                    resultats[index] = {"index": index, "url": url, "status": "valid", **infos}
                except Exception as exc:  # une source ne condamne pas les autres
                    resultats[index] = {
                        "index": index, "url": url, "status": "unavailable",
                        "error": str(exc) or type(exc).__name__,
                    }

        await asyncio.gather(*(inspecter(i, url) for i, url in enumerate(propres)))

        reference: Optional[dict[str, Any]] = None
        if lien_reference.strip():
            try:
                ref_propre = normaliser_lien_tiktok(lien_reference)
                direct = await resolveur(session, ref_propre)
                infos = await sonder_video(direct, timeout=min(25.0, config.delai_ffmpeg))
                if infos["duration"] > config.duree_max_source:
                    raise ErreurMontage(
                        f"La référence dure {infos['duration']:.1f} s, au-delà de la limite "
                        f"de {config.duree_max_source:.0f} s."
                    )
                reference = {"url": ref_propre, "status": "valid", **infos}
            except Exception as exc:
                reference = {"url": lien_reference.strip(), "status": "unavailable", "error": str(exc)}

    sources = [r for r in resultats if r is not None]
    erreurs.extend(r for r in sources if r.get("status") != "valid")
    valides = [r for r in sources if r.get("status") == "valid"]
    estimation = estimer_duree_traitement(
        [float(r["duration"]) for r in valides],
        float(reference.get("duration", 0)) if reference and reference.get("status") == "valid" else 0,
        config,
    )
    return {
        "links": [r["url"] for r in valides],
        "sources": sources,
        "errors": erreurs,
        "duplicates": doublons,
        "reference": reference,
        "valid_count": len(valides),
        "invalid_count": len(erreurs),
        "max_source_seconds": config.duree_max_source,
        **estimation,
    }


def estimer_duree_traitement(
    durees: list[float], duree_reference: float, config: ConfigurationMontage
) -> dict[str, Any]:
    """Estimation conservatrice pour un petit CPU Render ; ce n'est jamais une promesse."""
    total = sum(min(d, config.duree_max_source) for d in durees)
    nb_analyses = len(durees) + (1 if duree_reference else 0)
    total_analyse = total + min(duree_reference, config.duree_max_source)
    # Aperçus 6 fps + uploads/latence Gemini + téléchargements + export vertical ~30 s.
    secondes = 15 + total_analyse * 0.38 + nb_analyses * 10 + total * 0.08 + 30 * 3.0
    secondes = int(math.ceil(secondes / 5.0) * 5)
    plafond_prudent = min(540, max(60, int(config.delai_job - 20)))
    sous_dix = secondes <= plafond_prudent
    return {
        "estimated_seconds": secondes,
        "estimated_label": f"environ {max(1, math.ceil(secondes / 60))} min",
        "likely_under_10_minutes": sous_dix,
        "warning": "" if sous_dix else (
            "Ce montage risque de dépasser 10 minutes sur Render gratuit. "
            "Raccourcis ou réduis les sources ; la limite serveur interrompra le travail proprement."
        ),
    }


# --------------------------------------------------------------------------------------
# Réponses Gemini strictes et repli
# --------------------------------------------------------------------------------------


class PertinenceScript(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0)
    score: float = Field(ge=0, le=1)


class SceneAnalyse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    debut: float = Field(ge=0)
    fin: float = Field(gt=0)
    sujet: str = Field(min_length=1, max_length=300)
    action_mouvement: str = Field(min_length=1, max_length=300)
    qualite: str = Field(min_length=1, max_length=100)
    nettete: float = Field(ge=0, le=1)
    cadrage: str = Field(min_length=1, max_length=200)
    texte_visible: bool
    watermark: bool
    pertinence_script: list[PertinenceScript]
    rythme: str = Field(min_length=1, max_length=100)
    transition_recommandee: str = Field(min_length=1, max_length=100)
    score_pertinence: float = Field(ge=0, le=1)


class ReponseAnalyse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneAnalyse] = Field(max_length=120)


class StyleReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    duree_moyenne_plans: float = Field(ge=0.3, le=10)
    rythme: str = Field(min_length=1, max_length=100)
    coupes: list[str] = Field(min_length=1, max_length=5)
    zooms_legers: bool
    transitions: list[str] = Field(min_length=1, max_length=5)
    style_police: str = Field(pattern="^(sans|serif|mono|arrondie)$")
    position_sous_titres: str = Field(pattern="^(haut|centre|bas)$")
    taille_sous_titres: float = Field(ge=0.03, le=0.10)
    couleur_texte: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    couleur_contour: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    couleur_accent: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    epaisseur_contour: int = Field(ge=1, le=8)
    ombre: bool
    mots_par_ecran: int = Field(ge=1, le=8)
    apparition: str = Field(pattern="^(pop|progressive|fondu|simple)$")
    mots_mis_en_avant: bool


STYLE_DEFAUT = StyleReference(
    duree_moyenne_plans=5.0,
    rythme="accroche rapide puis rythme posé",
    coupes=["cut", "fade"],
    zooms_legers=True,
    transitions=["cut", "fade", "slide", "zoom"],
    style_police="sans",
    position_sous_titres="bas",
    taille_sous_titres=0.047,
    couleur_texte="#FFFFFF",
    couleur_contour="#101010",
    couleur_accent="#FFD43B",
    epaisseur_contour=4,
    ombre=True,
    mots_par_ecran=4,
    apparition="pop",
    mots_mis_en_avant=True,
)

PROMPT_ANALYSE = """Tu analyses un aperçu vidéo léger mais couvrant TOUTE la source ({duree:.2f} secondes, 360p, 6 fps, sans audio).
Parties du script : {segments}
Retourne UNIQUEMENT cet objet JSON strict :
{{"scenes":[{{"debut":0.0,"fin":5.0,"sujet":"...","action_mouvement":"...","qualite":"bonne|moyenne|faible","nettete":0.8,"cadrage":"...","texte_visible":false,"watermark":false,"pertinence_script":[{{"id":0,"score":0.8}}],"rythme":"dynamique|modéré|statique","transition_recommandee":"cut|fade|slide|zoom","score_pertinence":0.8}}]}}
Contraintes : temps réels dans [0,{duree:.2f}], scènes intéressantes seulement, actions complètes si possible, score 0..1. Décris le sujet, l'action, la qualité/netteté, le cadrage, tout texte/watermark, la pertinence POUR CHAQUE partie concernée, le rythme et la transition. N'invente rien et n'ajoute aucune clé."""

PROMPT_STYLE = """Analyse uniquement la GRAMMAIRE VISUELLE de cette vidéo de référence, jamais son contenu créatif. Ne propose pas d'en recopier les images, le son, le logo ou le watermark.
Retourne UNIQUEMENT un objet JSON strict avec : duree_moyenne_plans (secondes), rythme, coupes (cut/fade/slide/zoom), zooms_legers (booléen), transitions, style_police (sans/serif/mono/arrondie), position_sous_titres (haut/centre/bas), taille_sous_titres (ratio 0.03..0.10 de la hauteur), couleur_texte (#RRGGBB), couleur_contour, couleur_accent, epaisseur_contour (1..8), ombre (booléen), mots_par_ecran (1..8), apparition (pop/progressive/fondu/simple), mots_mis_en_avant (booléen). Aucune autre clé."""


async def creer_apercu(
    source: Path, destination: Path, duree: float, config: ConfigurationMontage, rapporteur: Rapporteur
) -> Path:
    filtre = "scale='if(gt(iw,ih),640,360)':'if(gt(iw,ih),360,640)':force_original_aspect_ratio=decrease,fps=6"
    commande = [
        "ffmpeg", "-y", "-i", str(source), "-t", f"{duree:.3f}", "-vf", filtre,
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "33",
        "-maxrate", "320k", "-bufsize", "640k", "-pix_fmt", "yuv420p",
        "-threads", str(config.threads_ffmpeg), str(destination),
    ]
    await executer_commande(
        commande, etape="création de l’aperçu 360p / 6 fps",
        timeout=rapporteur.timeout(config.delai_ffmpeg), sortie_attendue=destination,
    )
    if not destination.is_file() or destination.stat().st_size > config.taille_max_apercu:
        destination.unlink(missing_ok=True)
        raise ErreurMontage(
            "L’aperçu de toute la source dépasse la limite mémoire. Réduis DUREE_MAX_SOURCE_SECONDES."
        )
    return destination


def _repli_scenes(duree: float, segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    fin = max(0.6, min(duree, 8.0))
    return [{
        "debut": 0.0, "fin": fin, "sujet": "contenu de la source",
        "action_mouvement": "mouvement non confirmé (repli sans analyse IA)",
        "qualite": "inconnue", "nettete": 0.5, "cadrage": "inconnu",
        "texte_visible": False, "watermark": False,
        "pertinence_script": [{"id": s["id"], "score": 0.25} for s in segments],
        "rythme": "modéré", "transition_recommandee": "cut", "score_pertinence": 0.25,
    }]


async def analyser_video(
    source: Path,
    duree: float,
    segments: list[dict[str, Any]],
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    appel_gemini: AppelGemini,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Analyse toute la durée ; une réponse invalide produit un repli explicite."""
    apercu = source.with_name(f"{source.stem}_analyse.mp4")
    try:
        await creer_apercu(source, apercu, duree, config, rapporteur)
        donnees_video = await asyncio.to_thread(apercu.read_bytes)
        video_b64 = base64.b64encode(donnees_video).decode("ascii")
        del donnees_video
        prompt = PROMPT_ANALYSE.format(
            duree=duree,
            segments=json.dumps([{"id": s["id"], "texte": s["texte"]} for s in segments], ensure_ascii=False),
        )
        brut = await asyncio.wait_for(
            appel_gemini(
                [{"inline_data": {"mime_type": "video/mp4", "data": video_b64}}, {"text": prompt}],
                temperature=0.1, json_mode=True,
            ),
            timeout=rapporteur.timeout(config.delai_gemini),
        )
        del video_b64
        try:
            objet = json.loads(brut.strip().removeprefix("```json").removesuffix("```").strip())
            valide = ReponseAnalyse.model_validate(objet)
            scenes: list[dict[str, Any]] = []
            for scene in valide.scenes:
                if scene.fin <= scene.debut or scene.debut >= duree:
                    continue
                item = scene.model_dump()
                item["fin"] = min(float(item["fin"]), duree)
                scenes.append(item)
            if not scenes:
                raise ValueError("aucune scène temporelle valide")
            return scenes, None
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            avertissement = f"Réponse Gemini invalide, sélection de repli utilisée : {exc}"
            return _repli_scenes(duree, segments), avertissement
    except (TravailAnnule, asyncio.CancelledError):
        raise
    except asyncio.TimeoutError:
        return _repli_scenes(duree, segments), "Délai Gemini dépassé, sélection de repli utilisée."
    except Exception as exc:  # une analyse en échec ne condamne pas les autres sources
        return _repli_scenes(duree, segments), f"Analyse indisponible, sélection de repli utilisée : {exc}"
    finally:
        apercu.unlink(missing_ok=True)


async def analyser_style_reference(
    source: Path,
    duree: float,
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    appel_gemini: AppelGemini,
) -> tuple[StyleReference, Optional[str]]:
    apercu = source.with_name("reference_style_analyse.mp4")
    try:
        await creer_apercu(source, apercu, duree, config, rapporteur)
        contenu = await asyncio.to_thread(apercu.read_bytes)
        video_b64 = base64.b64encode(contenu).decode("ascii")
        del contenu
        brut = await asyncio.wait_for(
            appel_gemini(
                [{"inline_data": {"mime_type": "video/mp4", "data": video_b64}}, {"text": PROMPT_STYLE}],
                temperature=0.1, json_mode=True,
            ),
            timeout=rapporteur.timeout(config.delai_gemini),
        )
        objet = json.loads(brut.strip().removeprefix("```json").removesuffix("```").strip())
        return StyleReference.model_validate(objet), None
    except (TravailAnnule, asyncio.CancelledError):
        raise
    except (asyncio.TimeoutError, json.JSONDecodeError, ValidationError, ValueError, ErreurMontage) as exc:
        return STYLE_DEFAUT.model_copy(deep=True), f"Style de référence non exploitable, style professionnel utilisé : {exc}"
    finally:
        apercu.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# Plan de montage : hook rapide, scènes ~5 secondes et alternance des sources
# --------------------------------------------------------------------------------------

# Intensité des transitions, de 0 (coupes franches seules) à 3 (transitions appuyées).
ECHELLES_INTENSITE_TRANSITIONS = {0: 0.15, 1: 0.6, 2: 1.0, 3: 1.35}


def borner_intensite_transitions(valeur: Any) -> int:
    try:
        return max(0, min(3, int(valeur)))
    except (TypeError, ValueError):
        return 2


def _groupes_equilibres(mots: list[str], nombre: int) -> list[list[str]]:
    groupes: list[list[str]] = []
    debut = 0
    for i in range(nombre):
        restant = len(mots) - debut
        taille = math.ceil(restant / max(1, nombre - i))
        groupes.append(mots[debut:debut + taille])
        debut += taille
    return groupes


def creer_segments_script(hook: str, corps: str) -> list[dict[str, Any]]:
    mots_hook = hook.strip().split()
    segments: list[dict[str, Any]] = []
    if mots_hook:
        duree_hook = min(5.0, max(3.0, len(mots_hook) / 2.3))
        nombre = max(2, min(len(mots_hook), math.ceil(duree_hook / 1.2))) if len(mots_hook) > 1 else 2
        groupes = _groupes_equilibres(mots_hook, min(nombre, len(mots_hook)))
        # Un hook d'un seul mot garde deux changements visuels sans dupliquer le sous-titre.
        if len(groupes) == 1 and duree_hook > 1.5:
            groupes.append([])
        duree_plan = min(1.5, max(0.6, duree_hook / len(groupes)))
        for groupe in groupes:
            segments.append({
                "id": len(segments), "texte": " ".join(groupe), "hook": True,
                "duree_cible": duree_plan,
            })

    phrases = [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", corps.strip()) if p.strip()]
    for phrase in phrases:
        mots = phrase.split()
        morceaux = [mots[i:i + 18] for i in range(0, len(mots), 18)] or [[]]
        for morceau in morceaux:
            segments.append({
                "id": len(segments), "texte": " ".join(morceau), "hook": False,
                "duree_cible": 5.0,
            })
    return segments


def _score_scene(scene: dict[str, Any], segment_id: int, source: str, precedente: str) -> float:
    pertinences = {int(p.get("id", -1)): float(p.get("score", 0)) for p in scene.get("pertinence_script", [])}
    score = pertinences.get(segment_id, float(scene.get("score_pertinence", 0))) * 0.65
    score += float(scene.get("score_pertinence", 0)) * 0.20
    score += float(scene.get("nettete", 0.5)) * 0.15
    texte = f"{scene.get('action_mouvement', '')} {scene.get('rythme', '')}".lower()
    if any(mot in texte for mot in ("statique", "immobile", "flou")):
        score -= 0.28
    if scene.get("texte_visible"):
        score -= 0.18
    if scene.get("watermark"):
        score -= 0.22
    if source == precedente:
        score -= 0.30
    return score


def selectionner_plan(
    segments: list[dict[str, Any]],
    analyses: dict[str, list[dict[str, Any]]],
    metadonnees: dict[str, dict[str, Any]],
    style: StyleReference,
    intensite_transitions: int = 2,
) -> list[dict[str, Any]]:
    if not metadonnees:
        raise ErreurMontage("Aucune source valide n’est disponible pour le montage.")
    sources = list(metadonnees)
    plan: list[dict[str, Any]] = []
    precedente = ""
    transition_precedente = ""
    curseurs_repli = {nom: 0.0 for nom in sources}
    autorisees = {"cut", "fade", "slide", "zoom"}
    style_cycle = [str(t).lower() for t in style.transitions + style.coupes if str(t).lower() in autorisees]
    intensite = borner_intensite_transitions(intensite_transitions)
    if intensite == 0:
        # Coupes franches uniquement : aucune transition temporelle.
        cycle = ["cut"]
    elif intensite == 1:
        cycle = ["cut", "fade"]
    else:
        cycle = style_cycle or ["cut", "fade", "cut", "zoom", "slide"]
    if intensite > 0 and len(set(cycle)) == 1:
        cycle.append("fade" if cycle[0] != "fade" else "cut")
    echelle_intensite = ECHELLES_INTENSITE_TRANSITIONS[intensite]

    for index, segment in enumerate(segments):
        cible = float(segment["duree_cible"])
        if not segment["hook"]:
            cible = min(5.5, max(4.5, style.duree_moyenne_plans))
        candidats: list[tuple[float, str, dict[str, Any]]] = []
        for source, scenes in analyses.items():
            for scene in scenes:
                disponible = float(scene.get("fin", 0)) - float(scene.get("debut", 0))
                if disponible < min(0.55, cible):
                    continue
                score = _score_scene(scene, int(segment["id"]), source, precedente)
                if not segment["hook"] and disponible >= 4.5:
                    score += 0.18
                if segment["hook"] and "stat" not in str(scene.get("rythme", "")).lower():
                    score += 0.12
                candidats.append((score, source, scene))
        candidats.sort(key=lambda element: element[0], reverse=True)

        if candidats:
            _, source, scene = candidats[0]
            debut_scene = max(0.0, float(scene["debut"]))
            fin_scene = min(float(metadonnees[source]["duration"]), float(scene["fin"]))
            disponible = max(0.55, fin_scene - debut_scene)
            duree = min(cible, disponible)
            if not segment["hook"] and disponible >= 4.5:
                duree = min(5.5, max(4.5, min(cible, disponible)))
            debut = debut_scene
            recommandation = str(scene.get("transition_recommandee", "cut")).lower()
        else:
            choix = [s for s in sources if s != precedente] or sources
            source = choix[index % len(choix)]
            duree_source = float(metadonnees[source]["duration"])
            duree = min(cible, duree_source)
            if not segment["hook"] and duree_source >= 4.5:
                duree = min(5.0, duree_source)
            debut = min(curseurs_repli[source], max(0.0, duree_source - duree))
            curseurs_repli[source] = (debut + duree + 0.5) % max(duree_source, 0.6)
            recommandation = "cut"

        transition = (
            recommandation if recommandation in autorisees and recommandation in cycle
            else cycle[index % len(cycle)]
        )
        if len(cycle) > 1 and transition == transition_precedente:
            transition = next(
                candidate for candidate in cycle[index % len(cycle):] + cycle[:index % len(cycle)]
                if candidate != transition_precedente
            )
        if index == 0:
            transition, transition_duree = "none", 0.0
        elif transition == "cut":
            transition_duree = 0.05  # xfade quasi instantané, visuellement une coupe franche
        else:
            transition_duree = (0.22, 0.28, 0.34)[index % 3] * echelle_intensite
        transition_duree = min(0.55, transition_duree, max(0.02, duree / 3))

        plan.append({
            **segment, "source": source, "debut": round(debut, 3),
            "duree": round(max(0.55, duree), 3), "transition": transition,
            "transition_duree": round(transition_duree, 3),
        })
        precedente = source
        transition_precedente = transition
    return plan


# --------------------------------------------------------------------------------------
# ASS/libass et export final unique
# --------------------------------------------------------------------------------------


def _ass_couleur(hexadecimal: str) -> str:
    valeur = hexadecimal.lstrip("#")
    return f"&H00{valeur[4:6]}{valeur[2:4]}{valeur[0:2]}".upper()


def _ass_temps(secondes: float) -> str:
    secondes = max(0.0, secondes)
    heures = int(secondes // 3600)
    minutes = int(secondes % 3600 // 60)
    reste = secondes % 60
    return f"{heures}:{minutes:02d}:{reste:05.2f}"


def _echapper_ass(texte: str) -> str:
    return texte.replace("\\", r"\backslash").replace("{", r"\{").replace("}", r"\}").replace("\n", r"\N")


def _mettre_accent_ass(texte: str, style: StyleReference) -> str:
    propre = _echapper_ass(texte)
    if not style.mots_mis_en_avant or not propre.strip():
        return propre
    mots = list(re.finditer(r"[\wÀ-ÿ'’\-]+", propre, flags=re.UNICODE))
    if not mots:
        return propre
    fort = max(mots, key=lambda m: len(m.group(0)))
    couleur = _ass_couleur(style.couleur_accent)
    return propre[:fort.start()] + "{\\c" + couleur + "}" + fort.group(0) + "{\\r}" + propre[fort.end():]


def construire_cues(plan: list[dict[str, Any]], mots_par_ecran: int) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    debut_clip = 0.0
    for index, clip in enumerate(plan):
        if index:
            debut_clip -= float(clip["transition_duree"])
        fin_affichage = debut_clip + float(clip["duree"])
        if index + 1 < len(plan):
            fin_affichage -= float(plan[index + 1]["transition_duree"])
        mots = str(clip.get("texte", "")).split()
        groupes = [mots[i:i + mots_par_ecran] for i in range(0, len(mots), mots_par_ecran)]
        if groupes:
            fenetre = max(0.2, fin_affichage - debut_clip)
            pas = fenetre / len(groupes)
            for no, groupe in enumerate(groupes):
                cues.append({
                    "texte": " ".join(groupe), "debut": debut_clip + no * pas,
                    "fin": min(fin_affichage, debut_clip + (no + 1) * pas),
                })
        debut_clip += float(clip["duree"])
    return cues


def ecrire_ass(
    destination: Path,
    plan: list[dict[str, Any]],
    style: StyleReference,
    largeur: int,
    hauteur: int,
    nom_police: Optional[str] = None,
) -> Path:
    polices_installees = {
        "sans": "DejaVu Sans", "serif": "DejaVu Serif",
        "mono": "Liberation Mono", "arrondie": "DejaVu Sans",
    }
    # On reproduit la famille visuelle, jamais une police propriétaire absente ;
    # DejaVu Sans reste le repli garanti par le Dockerfile.
    nom_police = nom_police or polices_installees.get(style.style_police, "DejaVu Sans")
    alignements = {"bas": 2, "centre": 5, "haut": 8}
    marges = {"bas": int(hauteur * 0.13), "centre": 0, "haut": int(hauteur * 0.10)}
    taille = int(max(34, min(88, hauteur * style.taille_sous_titres)))
    ombre = 2 if style.ombre else 0
    entete = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {largeur}
PlayResY: {hauteur}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Principal,{nom_police},{taille},{_ass_couleur(style.couleur_texte)},{_ass_couleur(style.couleur_accent)},{_ass_couleur(style.couleur_contour)},&H80000000,-1,0,0,0,100,100,0,0,1,{style.epaisseur_contour},{ombre},{alignements[style.position_sous_titres]},52,52,{marges[style.position_sous_titres]},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lignes = [entete]
    for cue in construire_cues(plan, style.mots_par_ecran):
        animation = ""
        if style.apparition == "pop":
            animation = r"{\fad(45,70)\fscx108\fscy108\t(0,120,\fscx100\fscy100)}"
        elif style.apparition == "fondu":
            animation = r"{\fad(120,100)}"
        elif style.apparition == "progressive":
            animation = r"{\fad(70,60)\t(0,140,\fsp1)}"
        texte = animation + _mettre_accent_ass(cue["texte"], style)
        lignes.append(
            f"Dialogue: 0,{_ass_temps(cue['debut'])},{_ass_temps(cue['fin'])},Principal,,0,0,0,,{texte}\n"
        )
    destination.write_text("".join(lignes), encoding="utf-8")
    return destination


def _echapper_filtre(chemin: Path) -> str:
    return str(chemin.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _arguments_audio(
    voix_off: Optional[Path], index_entree: int
) -> tuple[list[str], list[str], list[str]]:
    """Arguments FFmpeg pour la voix off importée.

    Sans voix off, le montage reste muet (`-an`) comme avant. Avec une voix off, le
    fichier devient une entrée supplémentaire, `apad` prolonge l'audio si la piste est
    plus courte que l'image, et `-shortest` coupe à la fin du plus court des deux.

    Retourne (entrées supplémentaires, filtres audio, arguments de sortie).
    """
    if not voix_off:
        return [], [], ["-an"]
    return (
        ["-i", str(voix_off)],
        [f"[{index_entree}:a]apad[a]"],
        ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-shortest"],
    )


async def exporter_plan(
    plan: list[dict[str, Any]],
    sources: dict[str, Path],
    style: StyleReference,
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    resolution: str,
    dossier: Path,
    voix_off: Optional[Path] = None,
) -> Path:
    if resolution == "1080" and not config.autoriser_1080:
        raise ErreurMontage("L’export 1080 × 1920 est désactivé sur cette instance pour protéger ses ressources.")
    largeur, hauteur = (1080, 1920) if resolution == "1080" else (config.largeur, config.hauteur)
    ass = ecrire_ass(dossier / "sous_titres.ass", plan, style, largeur, hauteur)
    rapporteur.update("subtitling", 86, "Sous-titres ASS synchronisés préparés")

    commande = ["ffmpeg", "-y"]
    for clip in plan:
        chemin = sources[clip["source"]]
        commande += ["-ss", f"{clip['debut']:.3f}", "-t", f"{clip['duree']:.3f}", "-i", str(chemin)]

    filtres: list[str] = []
    for i, clip in enumerate(plan):
        zoom = style.zooms_legers and (i % 4 == 2 or clip["transition"] == "zoom")
        if zoom:
            largeur_zoom = int(math.ceil(largeur * 1.035 / 2) * 2)
            hauteur_zoom = int(math.ceil(hauteur * 1.035 / 2) * 2)
            visuel = (
                f"scale={largeur_zoom}:{hauteur_zoom}:force_original_aspect_ratio=increase,"
                f"crop={largeur}:{hauteur}"
            )
        else:
            visuel = (
                f"scale={largeur}:{hauteur}:force_original_aspect_ratio=increase,"
                f"crop={largeur}:{hauteur}"
            )
        filtres.append(
            f"[{i}:v]{visuel},fps={config.fps},setsar=1,format=yuv420p,"
            f"settb=AVTB,setpts=PTS-STARTPTS[v{i}]"
        )

    courant = "v0"
    duree_courante = float(plan[0]["duree"])
    transitions_ffmpeg = {"cut": "fade", "fade": "fade", "slide": "slideleft", "zoom": "smoothup"}
    for i in range(1, len(plan)):
        transition = transitions_ffmpeg.get(plan[i]["transition"], "fade")
        duree_transition = float(plan[i]["transition_duree"])
        offset = max(0.0, duree_courante - duree_transition)
        sortie = f"x{i}"
        filtres.append(
            f"[{courant}][v{i}]xfade=transition={transition}:duration={duree_transition:.3f}:"
            f"offset={offset:.3f}[{sortie}]"
        )
        courant = sortie
        duree_courante += float(plan[i]["duree"]) - duree_transition

    filtres.append(f"[{courant}]ass=filename='{_echapper_filtre(ass)}'[final]")

    entrees_audio, filtres_audio, sortie_audio = _arguments_audio(voix_off, len(plan))
    commande += entrees_audio
    filtres += filtres_audio

    sortie = config.dossier_videos / f"{uuid.uuid4().hex}.mp4"
    commande += [
        "-filter_complex", ";".join(filtres), "-map", "[final]", *sortie_audio,
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(max(14, min(30, int(config.crf)))),
        "-pix_fmt", "yuv420p", "-r", str(config.fps), "-threads", str(config.threads_ffmpeg),
        "-movflags", "+faststart", str(sortie),
    ]
    rapporteur.update("editing", 89, f"Export FFmpeg {largeur} × {hauteur} à {config.fps} FPS")
    await executer_commande(
        commande, etape="export FFmpeg final", timeout=rapporteur.timeout(config.delai_ffmpeg),
        sortie_attendue=sortie,
    )
    return sortie


@contextmanager
def dossier_temporaire(racine: Path):
    racine.mkdir(parents=True, exist_ok=True)
    dossier = racine / uuid.uuid4().hex
    dossier.mkdir(mode=0o700)
    try:
        yield dossier
    finally:
        shutil.rmtree(dossier, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Orchestration complète
# --------------------------------------------------------------------------------------


async def construire_montage_professionnel(
    liens: list[str],
    lien_reference: str,
    hook: str,
    corps: str,
    resolution: str,
    style_sous_titres: str,
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    resolveur: Resolveur,
    telechargeur: Telechargeur,
    appel_gemini: AppelGemini,
    intensite_transitions: int = 2,
    voix_off: Optional[Path] = None,
) -> dict[str, Any]:
    debut_global = time.monotonic()
    propres, erreurs_forme, doublons = normaliser_liens_tiktok(liens)
    if not propres:
        detail = erreurs_forme[0]["error"] if erreurs_forme else "aucun lien"
        raise ErreurMontage(f"Aucune source TikTok valide : {detail}")
    if len(propres) > 20:
        raise ErreurMontage("20 liens TikTok uniques maximum.")
    segments = creer_segments_script(hook, corps)
    if not segments:
        raise ErreurMontage("Le script est vide : aucun plan ne peut être préparé.")

    rapporteur.update(
        "validating", 2, f"Validation 0/{len(propres)}", source_errors=erreurs_forme,
        duplicates=doublons, sources=[],
    )

    with dossier_temporaire(config.dossier_travail) as dossier:
        sources: dict[str, Path] = {}
        metadonnees: dict[str, dict[str, Any]] = {}
        rapports: list[dict[str, Any]] = []
        erreurs_sources = list(erreurs_forme)
        directs: list[tuple[int, str, str, dict[str, Any]]] = []
        reference_propre = ""
        reference_direct = ""
        reference_infos: Optional[dict[str, Any]] = None

        timeout_http = aiohttp.ClientTimeout(total=45, connect=12, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout_http) as session:
            # Valider chaque URL avant tout téléchargement complet.
            for i, url in enumerate(propres, start=1):
                rapporteur.checkpoint()
                entree = {"index": i - 1, "url": url, "status": "validating"}
                rapports.append(entree)
                try:
                    direct = await resolveur(session, url)
                    infos = await sonder_video(direct, timeout=rapporteur.timeout(min(25, config.delai_ffmpeg)))
                    if infos["duration"] > config.duree_max_source:
                        raise ErreurMontage(
                            f"Source de {infos['duration']:.1f} s, limite {config.duree_max_source:.0f} s."
                        )
                    entree.update(status="validated", **infos)
                    directs.append((i - 1, url, direct, infos))
                except Exception as exc:
                    entree.update(status="error", error=str(exc))
                    erreurs_sources.append({"index": i - 1, "url": url, "error": str(exc), "status": "unavailable"})
                rapporteur.update(
                    "validating", 2 + int(12 * i / len(propres)),
                    f"Validation {i}/{len(propres)}", sources=rapports, source_errors=erreurs_sources,
                )

            if lien_reference.strip():
                try:
                    reference_propre = normaliser_lien_tiktok(lien_reference)
                    reference_direct = await resolveur(session, reference_propre)
                    reference_infos = await sonder_video(
                        reference_direct, timeout=rapporteur.timeout(min(25, config.delai_ffmpeg))
                    )
                    if reference_infos["duration"] > config.duree_max_source:
                        raise ErreurMontage(
                            f"Référence de {reference_infos['duration']:.1f} s, "
                            f"limite {config.duree_max_source:.0f} s."
                        )
                except Exception as exc:
                    raise ErreurMontage(f"Vidéo de référence inaccessible : {exc}") from exc

            if not directs:
                raise ErreurMontage(
                    "Aucune source accessible. " + "; ".join(e.get("error", "erreur") for e in erreurs_sources[:3])
                )

            # Téléchargement séquentiel, écrit par blocs sur disque par l'appelant.
            total_disque = 0
            for numero, (index, url, direct, infos) in enumerate(directs, start=1):
                rapporteur.update(
                    "downloading", 15 + int(18 * (numero - 1) / len(directs)),
                    f"Téléchargement {numero}/{len(directs)}", sources=rapports,
                    source_errors=erreurs_sources,
                )
                nom = f"source_{index}"
                chemin = dossier / f"{nom}.mp4"
                try:
                    await telechargeur(session, direct, chemin)
                    infos_locales = await sonder_video(
                        chemin, timeout=rapporteur.timeout(min(20, config.delai_ffmpeg))
                    )
                    total_disque += chemin.stat().st_size
                    if total_disque > config.budget_disque_sources:
                        raise ErreurMontage(
                            "Budget disque temporaire dépassé. Réduis la durée ou le nombre de sources."
                        )
                    sources[nom] = chemin
                    metadonnees[nom] = {**infos_locales, "url": url, "index": index}
                    rapports[index].update(status="downloaded", **infos_locales)
                except Exception as exc:
                    chemin.unlink(missing_ok=True)
                    rapports[index].update(status="error", error=str(exc))
                    erreurs_sources.append({"index": index, "url": url, "error": str(exc), "status": "download_error"})
                rapporteur.update(
                    "downloading", 15 + int(18 * numero / len(directs)),
                    f"Téléchargement {numero}/{len(directs)}", sources=rapports,
                    source_errors=erreurs_sources,
                )

            reference_path: Optional[Path] = None
            if reference_direct and reference_infos:
                reference_path = dossier / "reference_style.mp4"
                rapporteur.update("downloading", 34, "Téléchargement de la référence de style")
                await telechargeur(session, reference_direct, reference_path)

        if not sources:
            raise ErreurMontage("Tous les téléchargements ont échoué ; consulte les erreurs par source.")

        # Le sémaphore est borné à deux, configurable à un sur Render gratuit.
        analyses: dict[str, list[dict[str, Any]]] = {}
        verrou = asyncio.Semaphore(max(1, min(2, config.analyses_concurrentes)))
        terminees = 0
        total_analyses = len(sources)

        async def analyser_une(nom: str) -> None:
            nonlocal terminees
            async with verrou:
                rapporteur.checkpoint()
                scenes, avertissement = await analyser_video(
                    sources[nom], float(metadonnees[nom]["duration"]), segments,
                    config, rapporteur, appel_gemini,
                )
                analyses[nom] = scenes
                terminees += 1
                index = int(metadonnees[nom]["index"])
                rapports[index].update(
                    status="analysed", scenes=len(scenes), analysis_warning=avertissement or ""
                )
                if avertissement:
                    erreurs_sources.append({
                        "index": index, "url": metadonnees[nom]["url"],
                        "error": avertissement, "status": "analysis_fallback",
                    })
                rapporteur.update(
                    "analysing", 36 + int(34 * terminees / total_analyses),
                    f"Analyse {terminees}/{total_analyses}", sources=rapports,
                    source_errors=erreurs_sources,
                )

        await asyncio.gather(*(analyser_une(nom) for nom in sources))

        style_reference = STYLE_DEFAUT.model_copy(deep=True)
        avertissement_reference = ""
        if reference_path and reference_infos:
            rapporteur.update("analysing", 72, "Analyse de la grammaire visuelle de la référence")
            style_reference, warning = await analyser_style_reference(
                reference_path, float(reference_infos["duration"]), config, rapporteur, appel_gemini
            )
            avertissement_reference = warning or ""
            reference_path.unlink(missing_ok=True)
        elif style_sous_titres == "jaune":
            style_reference = style_reference.model_copy(update={"couleur_texte": "#FFD43B"})
        elif style_sous_titres == "centre":
            style_reference = style_reference.model_copy(update={"position_sous_titres": "centre"})

        rapporteur.update("selecting", 76, "Sélection et alternance des meilleurs plans")
        plan = selectionner_plan(segments, analyses, metadonnees, style_reference, intensite_transitions)
        rapporteur.update(
            "editing", 82,
            f"Plan prêt : {len(plan)} plans, accroche rapide puis scènes principales de 5 s",
            edit_plan=[{
                "source": p["source"], "start": p["debut"], "duration": p["duree"],
                "transition": p["transition"], "hook": p["hook"],
            } for p in plan],
        )
        sortie = await exporter_plan(
            plan, sources, style_reference, config, rapporteur, resolution, dossier,
            voix_off=voix_off,
        )
        return {
            "path": sortie,
            "url": f"/videos/{sortie.name}",
            "sources": rapports,
            "source_errors": erreurs_sources,
            "duplicates": doublons,
            "reference_style": style_reference.model_dump(),
            "reference_warning": avertissement_reference,
            "elapsed_seconds": round(time.monotonic() - debut_global, 1),
        }
