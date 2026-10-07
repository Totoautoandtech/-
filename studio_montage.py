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
    # Aucun plan ne dépasse cette durée : le montage reste nerveux et lisible.
    duree_max_plan: float = 5.0
    delai_ffmpeg: float = 240.0
    delai_gemini: float = 120.0
    # Limite globale du travail : 30 min par défaut, alignée sur JOB_TIMEOUT_SECONDES.
    delai_job: float = 1800.0
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
    # En production, chaque source doit avoir été réellement validée par l'IA :
    # aucune sélection de repli, aucun texte/logo et aucune image médiocre.
    exiger_validation_ia: bool = False
    # Mode RsT strict : refuser un plan « beau mais hors sujet » plutôt que le monter.
    exiger_pertinence_visuelle: bool = False
    # Toutes les sorties doivent durer au moins 1 min 1 s, transitions comprises.
    duree_min_video: float = 61.0


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


# Fenêtre de temps du pipeline. La limite globale est passée de 9 min 30 à 30 min
# (JOB_TIMEOUT_SECONDES sur Render) : l'estimation, les plafonds RsT/SsT et la
# confirmation de risque suivent tous la même fenêtre.
PLAFOND_ESTIMATION_MONTAGE = 1140  # 19 min : plafond prudent d'un montage
SEUIL_ESTIMATION_RISQUE = 1140  # au-delà, le lancement exige une confirmation

# CONTRÔLE QUALITÉ FINAL : chaque plan retenu est relu UNE SECONDE FOIS par Gemini sur
# un extrait de sa fenêtre exacte. Ce contrôle est obligatoire — un plan non confirmé
# n'est jamais monté. ~18 s d'appel par plan, extrait FFmpeg compris.
SECONDES_CONTROLE_QUALITE_PLAN = 18.0
# Durée typique d'un plan de corps : c'est le « plancher plein » appliqué par
# selectionner_plan, pas le plafond absolu. Elle sert à compter les plans à vérifier.
DUREE_PLAN_QUALITE = 4.5
# Tours de sélection et de relecture maximum avant l'échec final, détaillé.
TOURS_MAX_CONTROLE_QUALITE = 3
# Borne haute d'un appel de relecture : le coût typique est ~18 s, ce plafond évite
# qu'un appel bloqué par la saturation Gemini consomme toute la fenêtre du job.
DELAI_CONTROLE_QUALITE_PLAN = 75.0
# Un extrait de vérification couvre quelques secondes : la limite mémoire est basse.
TAILLE_MAX_EXTRAIT_VERIFICATION = 4 * 1024 * 1024


def estimer_plans_qualite(config: ConfigurationMontage) -> int:
    """Nombre de plans à recontrôler, estimé sur la durée minimale garantie.

    Un plan est produit par segment de script, donc le compte suit la durée du montage
    final — pas le nombre de sources. On retient donc la durée plancher du montage et
    la durée typique d'un plan de corps.
    """
    duree_plan = max(1.0, min(float(config.duree_max_plan), DUREE_PLAN_QUALITE))
    duree_montage = max(1.0, float(config.duree_min_video))
    return max(1, int(math.ceil(duree_montage / duree_plan)))


def estimer_duree_traitement(
    durees: list[float], duree_reference: float, config: ConfigurationMontage
) -> dict[str, Any]:
    """Estimation conservatrice pour un petit CPU Render ; ce n'est jamais une promesse."""
    total = sum(min(d, config.duree_max_source) for d in durees)
    nb_analyses = len(durees) + (1 if duree_reference else 0)
    total_analyse = total + min(duree_reference, config.duree_max_source)
    # Aperçus 6 fps + uploads/latence Gemini + téléchargements + export vertical ~30 s.
    # Sur Render Free, une analyse Gemini complète peut prendre ~60 s quand l'API
    # sature (réessais 2/4/8/16 s). On réserve donc 30 s d'analyse + 8 s de
    # transfert/latence par source au lieu de sous-estimer à 10 s par analyse.
    # Le contrôle qualité final s'ajoute : il relit chaque plan retenu une seconde fois.
    controle_qualite = estimer_plans_qualite(config) * SECONDES_CONTROLE_QUALITE_PLAN
    secondes = (
        15
        + total_analyse * 0.38
        + nb_analyses * 30
        + nb_analyses * 8
        + total * 0.08
        + 30 * 3.0
        + controle_qualite
    )
    secondes = int(math.ceil(secondes / 5.0) * 5)
    budget = min(PLAFOND_ESTIMATION_MONTAGE, max(60, int(config.delai_job) - 20))
    dans_budget = secondes <= budget
    return {
        "estimated_seconds": secondes,
        "estimated_label": f"environ {max(1, math.ceil(secondes / 60))} min",
        "likely_within_budget": dans_budget,
        "budget_seconds": budget,
        # Conservé tel quel pour les clients existants et le navigateur déjà déployé.
        "likely_under_10_minutes": dans_budget,
        "warning": "" if dans_budget else (
            f"Ce montage risque de dépasser {budget // 60} minutes sur Render gratuit. "
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
    # Drapeaux d'interdiction : ils sont OBLIGATOIRES. Un drapeau omis invalide la
    # réponse entière — c'est le comportement fail-closed attendu : mieux vaut rejeter
    # toute l'analyse qu'accepter une scène dont l'overlay n'a pas été contrôlé.
    # Le texte TikTok n'est accepté que s'il s'agit de vrais sous-titres.
    texte_sous_titres: bool
    personne_visible: bool
    watermark: bool
    logo_visible: bool
    autre_element_superpose: bool
    pertinence_script: list[PertinenceScript]
    rythme: str = Field(min_length=1, max_length=100)
    transition_recommandee: str = Field(min_length=1, max_length=100)
    score_pertinence: float = Field(ge=0, le=1)


class ReponseAnalyse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenes: list[SceneAnalyse] = Field(max_length=120)


# --------------------------------------------------------------------------------------
# CONTRÔLE QUALITÉ FINAL — relecture de chaque plan retenu sur sa fenêtre exacte
# --------------------------------------------------------------------------------------

PROMPT_VERIFICATION = """Tu es le contrôleur qualité FINAL d'un montage TikTok.
Tu reçois un extrait EXACT de la fenêtre qui va réellement être montée dans la vidéo
(début {debut:.2f} s, fin {fin:.2f} s).
Texte du script que ce plan doit illustrer : {segment}
Retourne UNIQUEMENT cet objet JSON strict :
{{"conforme":false,"personnes_visibles":false,"watermark":false,"logo_ajoute":false,
"autre_element_superpose":false,"texte_non_sous_titre":false,"flou_ou_illisible":false,
"hors_sujet":false,"raisons":[]}}
Un plan n'est conforme QUE SI les sept drapeaux valent false. Écris les raisons en français,
courtes et concrètes.
RÈGLE DU DOUTE ABSOLUE : au moindre doute, le drapeau passe à true. Le doute exclut le plan.
- personnes_visibles : une personne, même partiellement cadrée, une main, un visage, un
  reflet ou une silhouette suffisent.
- watermark : filigrane, bandeau, "@…" ou pseudo d'auteur incrusté par la plateforme.
- logo_ajoute : logo ajouté ou incrusté à l'image. L'emblème physique normal du produit
  ou du véhicule filmé n'en est pas un.
- autre_element_superpose : sticker, emoji, bouton, badge, chrono, bordure ou décoration.
- texte_non_sous_titre : tout texte qui n'est PAS la transcription des paroles dites dans
  l'extrait. Les vrais sous-titres TikTok qui recopient les paroles sont AUTORISÉS ; un
  titre, une légende, une phrase écrite ou une typographie décorative ne le sont pas.
- flou_ou_illisible : image floue, trop sombre, pixellisée ou sans sujet identifiable.
- hors_sujet : le plan ne montre pas exactement le sujet annoncé par le texte du script.
  La beauté, le cadrage et l'esthétique ne remplacent jamais la correspondance.
N'invente rien, n'ajoute aucune clé et ne commente pas en dehors du JSON."""


class ReponseVerification(BaseModel):
    """Verdict du contrôle qualité final — échecs fermés par construction.

    Tous les drapeaux d'interdiction valent « élément présent » par défaut et
    `conforme` vaut « non conforme » par défaut : une réponse incomplète, contradictoire
    ou illisible ne peut donc jamais faire passer un plan.
    """

    model_config = ConfigDict(extra="forbid")
    conforme: bool = False
    personnes_visibles: bool = True
    watermark: bool = True
    logo_ajoute: bool = True
    autre_element_superpose: bool = True
    texte_non_sous_titre: bool = True
    flou_ou_illisible: bool = True
    hors_sujet: bool = True
    raisons: list[str] = Field(default_factory=list, max_length=12)


MOTIFS_VERIFICATION: tuple[tuple[str, str], ...] = (
    ("personnes_visibles", "une personne, une main, un visage ou un reflet est visible"),
    ("watermark", "un watermark, un bandeau ou un pseudo d'auteur est incrusté"),
    ("logo_ajoute", "un logo ajouté ou incrusté à l'image est visible"),
    ("autre_element_superpose", "un sticker, un pseudo, un bouton ou une décoration est superposé"),
    ("texte_non_sous_titre", "du texte qui n'est pas un vrai sous-titre TikTok est visible"),
    ("flou_ou_illisible", "l'image est trop floue, trop sombre ou sans sujet identifiable"),
    ("hors_sujet", "le plan ne montre pas le sujet exact demandé par le script"),
)


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
{{"scenes":[{{"debut":0.0,"fin":5.0,"sujet":"...","action_mouvement":"...","qualite":"bonne|moyenne|faible","nettete":0.8,"cadrage":"...","texte_visible":false,"texte_sous_titres":false,"personne_visible":false,"watermark":false,"logo_visible":false,"autre_element_superpose":false,"pertinence_script":[{{"id":0,"score":0.8}}],"rythme":"dynamique|modéré|statique","transition_recommandee":"cut|fade|slide|zoom","score_pertinence":0.8}}]}}
Contraintes : temps réels dans [0,{duree:.2f}], scènes intéressantes seulement, actions complètes si possible, score 0..1. Décris le sujet, l'action, la qualité/netteté et le cadrage. Distingue précisément : texte_visible = tout texte, texte_sous_titres = uniquement une transcription de paroles, personne_visible = une personne même partielle, watermark, logo_visible = logo ajouté/incrusté à l'image (pas l'emblème physique normal du produit filmé), autre_element_superpose = stickers, pseudos, boutons ou décorations. Indique la pertinence POUR CHAQUE partie concernée, le rythme et la transition. N'invente rien et n'ajoute aucune clé.
RÈGLE DU DOUTE (non négociable, elle prime sur tout le reste) : au moindre doute sur un
drapeau d'interdiction, mets-le à true. Le doute EXCLUT la scène, il ne la sauve jamais.
- texte_sous_titres n'est vrai que si le texte visible est une VRAIE transcription des paroles
  dites dans la scène. Un titre, une légende, une phrase écrite, un slogan ou une typographie
  décorative ne sont pas des sous-titres : dans ce doute, texte_sous_titres = false.
- personne_visible : le moindre fragment suffit — main, visage, silhouette, reflet ou une partie du corps.
- logo_visible : le moindre logo ajouté ou incrusté suffit ; dans le doute, true.
- autre_element_superpose : le moindre sticker, pseudo, bouton, badge, chrono ou décoration suffit.
- watermark : le moindre filigrane, bandeau ou pseudo incrusté par la plateforme suffit.
Remplis TOUJOURS les sept drapeaux : un drapeau absent sera traité comme « élément présent ».
PERTINENCE VISUELLE ET IDENTITÉ STRICTES (critères les plus importants) :
- Identifie exactement le modèle, produit, personne, lieu ou objet demandé. Si le script donne un modèle ou un identifiant exact, toute variante ou image générique est hors sujet et reçoit 0.15 MAXIMUM.
- N'accepte aucune personne visible sur les plans d'objet/voiture/produit, même si elle ne cache qu'une petite partie du sujet.
- Les sous-titres TikTok déjà incrustés sont autorisés. Tout autre texte, logo, watermark, sticker, pseudo ou élément graphique superposé est interdit.
- N'accorde JAMAIS un bon score à un plan seulement parce qu'il est esthétique ou bien filmé : la beauté ne fait pas la pertinence.
- Un paysage, bâtiment, skyline, ville, voyage ou b-roll générique, sans personne, objet ou action lié au script, doit recevoir un score_pertinence et des pertinence_script à 0.25 MAXIMUM.
- Sujet tech/téléphone/produit : privilégie uniquement les plans montrant clairement l'objet exact, son écran, une démonstration sans personne ou une comparaison réelle sans mains ni visage.
- Un plan de skyline, gratte-ciel ou tour (par exemple Burj Khalifa, Dubaï) sur un script de téléphone doit être noté FAIBLE, sauf si le script parle explicitement de Dubaï ou de ce bâtiment.
- pertinence_script mesure le lien DIRECT entre l'image et le texte du segment, jamais la qualité technique de l'image.
"""

PROMPT_STYLE = """Analyse uniquement la GRAMMAIRE VISUELLE de cette vidéo de référence, jamais son contenu créatif. Ne propose pas d'en recopier les images, le son, le logo ou le watermark.
Le montage final doit reproduire le plus fidèlement possible la grammaire de la source : même cadence moyenne, même famille et intensité de coupes/transitions, même fréquence de zooms et même style de sous-titres (position, taille, couleurs, accent). Ses images, son sujet, son son, son texte incrusté, son logo et son watermark ne doivent jamais être réutilisés.
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
        "texte_visible": False, "texte_sous_titres": False,
        "personne_visible": False, "watermark": False, "logo_visible": False,
        "autre_element_superpose": False,
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
            segments=json.dumps([
                {"id": s["id"], "texte": s.get("texte_analyse") or s["texte"]}
                for s in segments
            ], ensure_ascii=False),
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
            if config.exiger_validation_ia:
                raise ErreurMontage(f"Validation IA invalide pour {source.name} : {exc}") from exc
            avertissement = f"Réponse Gemini invalide, sélection de repli utilisée : {exc}"
            return _repli_scenes(duree, segments), avertissement
    except (TravailAnnule, asyncio.CancelledError):
        raise
    except asyncio.TimeoutError as exc:
        if config.exiger_validation_ia:
            raise ErreurMontage(f"Validation IA trop longue pour {source.name}.") from exc
        return _repli_scenes(duree, segments), "Délai Gemini dépassé, sélection de repli utilisée."
    except Exception as exc:  # une analyse en échec ne condamne pas les autres sources
        if config.exiger_validation_ia:
            if isinstance(exc, ErreurMontage):
                raise
            raise ErreurMontage(f"Validation IA indisponible pour {source.name} : {exc}") from exc
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


# Durée maximale d'un plan, en secondes. Le montage RsT s'appuie dessus pour garder
# un rythme court : aucun plan ne reste à l'écran plus longtemps que cette valeur.
DUREE_MAX_PLAN_DEFAUT = 5.0
DUREE_MIN_PLAN = 0.55

# Mode RsT « pertinence visuelle stricte » : pertinence minimale (0..1) exigée d'une
# scène pour rester candidate, côté accroche puis côté corps du script. En dessous, la
# scène est jugée hors sujet — un plan beau mais sans rapport avec le script ne doit
# jamais être monté. Ces seuils s'appliquent à la pertinence brute renvoyée par
# l'analyse (pertinence_script du segment, sinon score_pertinence), pas au score de
# classement qui mélange esthétique et alternance des sources.
SEUIL_PERTINENCE_HOOK = 0.36
SEUIL_PERTINENCE_CORPS = 0.45


def _borner_duree_plan(valeur: Any) -> float:
    """Ramène une durée de plan demandée dans une plage réellement exploitable."""
    try:
        duree = float(valeur)
    except (TypeError, ValueError):
        return DUREE_MAX_PLAN_DEFAUT
    if not math.isfinite(duree) or duree <= 0:
        return DUREE_MAX_PLAN_DEFAUT
    return max(DUREE_MIN_PLAN, min(30.0, duree))


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


def creer_segments_script(
    hook: str, corps: str, duree_max_plan: float = DUREE_MAX_PLAN_DEFAUT
) -> list[dict[str, Any]]:
    """Découpe le script en segments dont aucun ne vise plus de `duree_max_plan` secondes."""
    plafond = _borner_duree_plan(duree_max_plan)
    mots_hook = hook.strip().split()
    segments: list[dict[str, Any]] = []
    if mots_hook:
        duree_hook = min(plafond, max(min(3.0, plafond), len(mots_hook) / 2.3))
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
                "duree_cible": plafond,
            })
    return segments


def garantir_duree_minimale_segments(
    segments: list[dict[str, Any]], duree_min: float, duree_max_plan: float
) -> list[dict[str, Any]]:
    """Ajoute des plans visuels sans sous-titre jusqu'à la durée minimale garantie.

    On réserve 0,55 s par transition (le pire cas autorisé). Le texte n'est jamais
    répété : les plans ajoutés prolongent seulement l'illustration professionnelle.
    """
    resultat = [dict(segment) for segment in segments]
    plafond = _borner_duree_plan(duree_max_plan)
    cible = max(61.0, float(duree_min or 0.0))

    def duree_prudente() -> float:
        total = sum(
            float(segment["duree_cible"])
            if segment.get("hook")
            else min(float(segment["duree_cible"]), min(4.5, plafond))
            for segment in resultat
        )
        return total - max(0, len(resultat) - 1) * 0.55

    contexte_visuel = " ".join(
        str(segment.get("texte", "")).strip() for segment in resultat
        if str(segment.get("texte", "")).strip()
    )[-500:]
    while duree_prudente() < cible:
        resultat.append({
            "id": len(resultat), "texte": "", "texte_analyse": contexte_visuel,
            "hook": False, "duree_cible": plafond, "prolongation": True,
        })
    return resultat


def _interdit_present(scene: dict[str, Any], cle: str) -> bool:
    """Fail-closed : un drapeau d'interdiction ABSENT vaut « élément présent ».

    Les scènes proviennent de Gemini ou d'un repli ; une clé manquante signifie
    qu'aucun contrôle n'a été rendu sur elle. Dans le doute, l'élément est là.
    """
    return bool(scene.get(cle, True))


def _scene_professionnelle_sans_texte(scene: dict[str, Any]) -> bool:
    """Scène nette sans personne/overlay ; seuls les sous-titres TikTok sont tolérés.

    Fail-closed : un drapeau d'interdiction absent vaut « élément présent », donc la
    scène est écartée. Seuls les sous-titres TikTok — vraie transcription des paroles —
    autorisent un texte visible.
    """
    qualite = str(scene.get("qualite", "")).strip().lower()
    try:
        nettete = float(scene.get("nettete", 0.0))
    except (TypeError, ValueError):
        nettete = 0.0
    texte_interdit = bool(scene.get("texte_visible")) and not bool(scene.get("texte_sous_titres", False))
    return (
        not texte_interdit
        and not _interdit_present(scene, "personne_visible")
        and not _interdit_present(scene, "watermark")
        and not _interdit_present(scene, "logo_visible")
        and not _interdit_present(scene, "autre_element_superpose")
        and qualite in {"bonne", "excellent", "excellente", "professionnelle"}
        and nettete >= 0.65
    )


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


def _pertinence_scene(scene: dict[str, Any], segment_id: int) -> float:
    """Pertinence DIRECTE d'une scène pour un segment du script, sans bonus esthétique.

    Utilise la pertinence du segment si l'analyse la fournit, sinon le score global de
    la scène. C'est la valeur soumise aux seuils du mode strict — jamais le score de
    classement, qui mélange netteté, alternance et pénalités de rythme.
    """
    for entree in scene.get("pertinence_script") or []:
        try:
            if int(entree.get("id", -1)) == segment_id:
                return float(entree.get("score", 0.0))
        except (TypeError, ValueError):
            continue
    try:
        return float(scene.get("score_pertinence", 0.0))
    except (TypeError, ValueError):
        return 0.0


def selectionner_plan(
    segments: list[dict[str, Any]],
    analyses: dict[str, list[dict[str, Any]]],
    metadonnees: dict[str, dict[str, Any]],
    style: StyleReference,
    intensite_transitions: int = 2,
    duree_max_plan: float = DUREE_MAX_PLAN_DEFAUT,
    exiger_pertinence_visuelle: bool = False,
) -> list[dict[str, Any]]:
    if not metadonnees:
        raise ErreurMontage("Aucune source valide n’est disponible pour le montage.")
    plafond = _borner_duree_plan(duree_max_plan)
    # En dessous du plafond, on vise des plans « pleins » sans jamais le dépasser.
    plancher_plein = min(4.5, plafond)
    sources = list(metadonnees)
    plan: list[dict[str, Any]] = []
    precedente = ""
    transition_precedente = ""
    # Compteur par fenêtre temporelle : une même source peut revenir plusieurs fois,
    # mais on utilise d'abord ses moments différents (début, milieu, fin).
    utilisations: dict[tuple[str, int], int] = {}
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
        cible = min(plafond, float(segment["duree_cible"]))
        if not segment["hook"]:
            cible = min(plafond, max(plancher_plein, style.duree_moyenne_plans))
        candidats: list[tuple[float, str, dict[str, Any], float, tuple[str, int]]] = []
        for source, scenes in analyses.items():
            for scene in scenes:
                # Condition non négociable : l'IA doit confirmer une image propre,
                # sans personne ni overlay interdit. Les sous-titres sont autorisés.
                if not _scene_professionnelle_sans_texte(scene):
                    continue
                debut_scene = max(0.0, float(scene.get("debut", 0)))
                fin_scene = min(
                    float(metadonnees[source]["duration"]), float(scene.get("fin", 0))
                )
                disponible = fin_scene - debut_scene
                if disponible + 1e-6 < cible:
                    continue
                score_base = _score_scene(scene, int(segment["id"]), source, precedente)
                if not segment["hook"] and disponible >= 4.5:
                    score_base += 0.18
                if segment["hook"] and "stat" not in str(scene.get("rythme", "")).lower():
                    score_base += 0.12

                # Découpe virtuellement une longue scène en fenêtres distinctes de
                # 5 s maximum. Les fenêtres jamais utilisées passent avant les redites.
                pas = max(cible, 0.6)
                positions: list[float] = []
                position = debut_scene
                while position + cible <= fin_scene + 1e-6:
                    positions.append(position)
                    position += pas
                derniere = max(debut_scene, fin_scene - cible)
                if not positions or derniere - positions[-1] >= max(0.5, cible * 0.5):
                    positions.append(derniere)
                for debut_possible in positions:
                    cle_fenetre = (source, int(round(debut_possible * 1000)))
                    repetitions = utilisations.get(cle_fenetre, 0)
                    candidats.append((
                        score_base - repetitions * 1.25,
                        source, scene, debut_possible, cle_fenetre,
                    ))
        texte_pertinence = str(
            segment.get("texte_analyse") or segment.get("texte", "")
        ).strip()
        if exiger_pertinence_visuelle and texte_pertinence:
            # Mode strict : chaque plan, y compris une prolongation sans sous-titre,
            # doit montrer exactement le sujet demandé par le script.
            seuil = SEUIL_PERTINENCE_HOOK if segment["hook"] else SEUIL_PERTINENCE_CORPS
            candidats = [
                element for element in candidats
                if _pertinence_scene(element[2], int(segment["id"])) >= seuil
            ]
            if not candidats:
                extrait = " ".join(texte_pertinence.split())
                if len(extrait) > 60:
                    extrait = extrait[:57].rstrip() + "…"
                raise ErreurMontage(
                    f"Aucune scène assez pertinente pour le segment « {extrait} ». "
                    "RsT arrête le rendu plutôt que de produire une vidéo hors sujet. "
                    "Relance avec une vidéo de départ plus précise ou colle manuellement "
                    "de meilleures sources."
                )
        candidats.sort(key=lambda element: element[0], reverse=True)

        if candidats:
            _, source, scene, debut, cle_fenetre = candidats[0]
            fin_scene = min(float(metadonnees[source]["duration"]), float(scene["fin"]))
            disponible = max(0.55, fin_scene - debut)
            duree = min(cible, disponible)
            if not segment["hook"] and disponible >= plancher_plein:
                duree = min(plafond, max(plancher_plein, min(cible, disponible)))
            utilisations[cle_fenetre] = utilisations.get(cle_fenetre, 0) + 1
            recommandation = str(scene.get("transition_recommandee", "cut")).lower()
        else:
            raise ErreurMontage(
                "Aucune scène cohérente, professionnelle, sans personne, sans texte ni watermark, "
                "ou sans logo ajouté n'est disponible. Les sous-titres intégrés sont les seuls overlays autorisés. "
            )

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
            **segment,
            "source": source,
            # Garde une provenance explicite avec le plan, en plus des métadonnées source.
            "source_url": str(metadonnees[source].get("url") or ""),
            "debut": round(debut, 3),
            # Garde-fou final : aucun plan ne franchit le plafond demandé.
            "duree": round(min(plafond, max(DUREE_MIN_PLAN, duree)), 3),
            "transition": transition,
            "transition_duree": round(transition_duree, 3),
        })
        precedente = source
        transition_precedente = transition
    return plan


# --------------------------------------------------------------------------------------
# Contrôle qualité final : chaque plan retenu est relu une seconde fois par Gemini
# --------------------------------------------------------------------------------------


def _texte_attendu_plan(clip: dict[str, Any]) -> str:
    """Texte du segment que ce plan doit illustrer.

    Un plan de prolongation n'a pas de sous-titre : `texte_analyse` porte alors le
    contexte visuel de la vidéo, sinon le contrôleur n'a rien contre quoi juger.
    """
    for cle in ("texte_analyse", "texte"):
        valeur = " ".join(str(clip.get(cle) or "").split())
        if valeur:
            return valeur[:400]
    return "(plan de prolongation : le plan doit montrer le sujet du montage, rien d'autre)"


def _detail_rejets(rejets: list[dict[str, Any]], limite: int = 6) -> str:
    """Échec final détaillé : URL, timestamps exacts et raison de chaque refus."""
    lignes: list[str] = []
    for verdict in rejets[:limite]:
        url = str(verdict.get("url") or verdict.get("source") or "source inconnue")
        raisons = " ; ".join(verdict.get("raisons") or []) or "non confirmé"
        lignes.append(
            f"{url} [{float(verdict.get('debut', 0)):.2f} s → "
            f"{float(verdict.get('fin', 0)):.2f} s] : {raisons}"
        )
    reste = len(rejets) - limite
    if reste > 0:
        lignes.append(f"… et {reste} autre(s) plan(s) écarté(s)")
    return " | ".join(lignes) if lignes else "aucun détail disponible"


def _detail_erreurs_sources(erreurs: list[dict[str, Any]], limite: int = 6) -> str:
    """Expose les motifs source réellement observés quand la sélection stricte échoue."""
    lignes: list[str] = []
    for entree in erreurs[:limite]:
        url = str(entree.get("url") or f"source {int(entree.get('index', -1)) + 1}")
        raison = " ".join(str(entree.get("error") or "source rejetée").split())
        if len(raison) > 240:
            raison = raison[:237].rstrip() + "…"
        lignes.append(f"{url} : {raison}")
    reste = len(erreurs) - limite
    if reste > 0:
        lignes.append(f"… et {reste} autre(s) source(s)")
    return " | ".join(lignes)


async def creer_extrait_verification(
    source: Path,
    destination: Path,
    debut: float,
    duree: float,
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
) -> Path:
    """Extrait la fenêtre EXACTE du plan, en définition propre à la décision finale.

    L'aperçu d'analyse (360p / 6 fps) sert au repérage des scènes ; la relecture doit
    juger la fenêtre réellement montée, donc dans une définition plus large.
    """
    # Le redimensionnement avec préservation du ratio peut laisser une dimension impaire
    # (par exemple 405×720 en portrait) ; libx264 en yuv420p exige deux dimensions paires.
    filtre = (
        "scale='if(gt(iw,ih),720,405)':'if(gt(iw,ih),405,720)'"
        ":force_original_aspect_ratio=decrease,"
        "scale='trunc(iw/2)*2':'trunc(ih/2)*2',fps=8"
    )
    commande = [
        "ffmpeg", "-y", "-ss", f"{max(0.0, float(debut)):.3f}", "-i", str(source),
        "-t", f"{max(DUREE_MIN_PLAN, float(duree)):.3f}", "-vf", filtre, "-an",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-maxrate", "800k", "-bufsize", "1600k", "-pix_fmt", "yuv420p",
        "-threads", str(config.threads_ffmpeg), str(destination),
    ]
    await executer_commande(
        commande, etape="extraction de la fenêtre exacte du plan pour contrôle qualité",
        timeout=rapporteur.timeout(config.delai_ffmpeg), sortie_attendue=destination,
    )
    taille = destination.stat().st_size if destination.is_file() else 0
    if taille <= 0:
        destination.unlink(missing_ok=True)
        raise ErreurMontage("L’extrait de vérification est vide : contrôle qualité impossible.")
    if taille > TAILLE_MAX_EXTRAIT_VERIFICATION:
        destination.unlink(missing_ok=True)
        raise ErreurMontage("L’extrait de vérification dépasse la limite mémoire.")
    return destination


async def verifier_conformite_extrait(
    clip: dict[str, Any],
    chemin: Optional[Path],
    url: str,
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    appel_gemini: AppelGemini,
    tour: int = 1,
) -> dict[str, Any]:
    """Relit UNE DEUXIÈME FOIS un plan sur un extrait de sa fenêtre exacte.

    Verdict échoué par construction : réponse illisible, incomplète, contradictoire ou
    appel impossible valent tous « non conforme ». Un contrôle impossible écarte le plan,
    il ne le laisse jamais passer.
    """
    source = str(clip.get("source", ""))
    url_source = str(url or "").strip()
    debut = max(0.0, float(clip.get("debut", 0.0)))
    duree = max(DUREE_MIN_PLAN, float(clip.get("duree", 0.0)))
    verdict: dict[str, Any] = {
        "index": int(clip.get("id", 0)),
        "source": source,
        "url": url_source,
        "debut": round(debut, 3),
        "fin": round(debut + duree, 3),
        "duree": round(duree, 3),
        "hook": bool(clip.get("hook")),
        "tour": int(tour),
        "conforme": False,
        "raisons": [],
    }
    if not url_source:
        verdict["raisons"] = ["URL source inconnue : provenance du plan impossible"]
        return verdict
    if chemin is None or not chemin.is_file():
        verdict["raisons"] = ["source téléchargée introuvable : contrôle qualité impossible"]
        return verdict

    extrait = chemin.with_name(f"{chemin.stem}_verif_{int(round(debut * 1000))}.mp4")
    try:
        try:
            await creer_extrait_verification(chemin, extrait, debut, duree, config, rapporteur)
        except (TravailAnnule, asyncio.CancelledError):
            raise
        except ErreurMontage as exc:
            # Une limite globale épuisée doit remonter telle quelle ; un extrait
            # simplement inutilisable écarte le plan sans casser tout le montage.
            if "Limite globale dépassée" in str(exc):
                raise
            verdict["raisons"] = [f"extrait de vérification impossible : {exc}"]
            return verdict
        except Exception as exc:  # noqa: BLE001
            verdict["raisons"] = [
                f"extrait de vérification impossible : {exc or type(exc).__name__}"
            ]
            return verdict

        contenu = await asyncio.to_thread(extrait.read_bytes)
        video_b64 = base64.b64encode(contenu).decode("ascii")
        del contenu
        prompt = PROMPT_VERIFICATION.format(
            segment=_texte_attendu_plan(clip), debut=debut, fin=debut + duree
        )
        try:
            brut = await asyncio.wait_for(
                appel_gemini(
                    [
                        {"inline_data": {"mime_type": "video/mp4", "data": video_b64}},
                        {"text": prompt},
                    ],
                    temperature=0.0, json_mode=True,
                ),
                timeout=rapporteur.timeout(
                    min(float(config.delai_gemini), DELAI_CONTROLE_QUALITE_PLAN)
                ),
            )
        except asyncio.TimeoutError:
            verdict["raisons"] = ["Gemini n’a pas répondu à temps : contrôle qualité impossible"]
            return verdict
        del video_b64
        try:
            objet = json.loads(brut.strip().removeprefix("```json").removesuffix("```").strip())
            analyse = ReponseVerification.model_validate(objet).model_dump()
        except Exception as exc:  # noqa: BLE001 — illisible = non conforme
            verdict["raisons"] = [
                f"contrôle qualité final illisible : {exc or type(exc).__name__}"
            ]
            return verdict
    except (TravailAnnule, asyncio.CancelledError):
        raise
    finally:
        extrait.unlink(missing_ok=True)

    raisons = [
        motif for cle, motif in MOTIFS_VERIFICATION if analyse.get(cle)
    ]
    raisons.extend(str(r).strip() for r in analyse.get("raisons") or [] if str(r).strip())
    verdict["raisons"] = raisons
    verdict["conforme"] = bool(analyse.get("conforme")) and not raisons
    return verdict


def _retirer_scenes_du_plan(
    analyses: dict[str, list[dict[str, Any]]],
    source: str,
    debut: float,
    fin: float,
    marge: float = 0.35,
) -> int:
    """Retire de l'analyse les scènes qui ont produit le plan écarté.

    Sans cela, la sélection relancée proposerait exactement le même plan et le travail
    bouclerait. La marge couvre les positions alternatives d'une même scène : seule la
    fenêtre refusée disparaît, le reste de la source reste montéable.
    """
    scenes = analyses.get(source)
    if not scenes:
        return 0
    debut_borne = float(debut) - float(marge)
    fin_borne = float(fin) + float(marge)
    restantes = [
        scene for scene in scenes
        if not (
            float(scene.get("debut", 0.0)) < fin_borne
            and float(scene.get("fin", 0.0)) > debut_borne
        )
    ]
    retirees = len(scenes) - len(restantes)
    if retirees:
        analyses[source] = restantes
    return retirees


async def controler_qualite_plan(
    plan: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    analyses: dict[str, list[dict[str, Any]]],
    metadonnees: dict[str, dict[str, Any]],
    style: StyleReference,
    sources: dict[str, Path],
    config: ConfigurationMontage,
    rapporteur: Rapporteur,
    appel_gemini: AppelGemini,
    intensite_transitions: int = 2,
    tours_max: int = TOURS_MAX_CONTROLE_QUALITE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Relit chaque plan retenu et ne renvoie que ceux qui sont confirmés.

    Un plan non confirmé est écarté AVEC sa scène, puis la sélection est relancée. Au
    plus `tours_max` tours : passé ce cap, l'échec est détaillé (URL, timestamps,
    raisons) plutôt qu'une vidéo non conforme.
    """
    derniers_rejets: list[dict[str, Any]] = []
    for tour in range(1, max(1, int(tours_max)) + 1):
        verdicts: list[dict[str, Any]] = []
        for position, clip in enumerate(plan, 1):
            rapporteur.checkpoint()
            source = str(clip.get("source", ""))
            rapporteur.update(
                "validating", 76 + int(6 * (position - 1) / max(1, len(plan))),
                f"Contrôle qualité strict du plan {position}/{len(plan)} (tour {tour})",
            )
            verdicts.append(await verifier_conformite_extrait(
                clip, sources.get(source), str(metadonnees.get(source, {}).get("url") or ""),
                config, rapporteur, appel_gemini, tour,
            ))
        rejets = [verdict for verdict in verdicts if not verdict["conforme"]]
        if not rejets:
            return plan, verdicts
        derniers_rejets = rejets
        rapporteur.update(
            "selecting", 76 + int(6 * tour / max(1, tours_max)),
            f"Contrôle qualité : {len(rejets)} plan(s) écartés — nouvelle sélection",
        )
        for verdict in rejets:
            _retirer_scenes_du_plan(
                analyses, verdict["source"], verdict["debut"], verdict["fin"]
            )
        try:
            plan = selectionner_plan(
                segments, analyses, metadonnees, style, intensite_transitions,
                config.duree_max_plan,
                exiger_pertinence_visuelle=config.exiger_pertinence_visuelle,
            )
        except ErreurMontage as exc:
            raise ErreurMontage(
                "Le contrôle qualité final a écarté tous les plans disponibles : "
                f"{_detail_rejets(rejets)} — {exc}"
            ) from exc
    raise ErreurMontage(
        f"Contrôle qualité final non satisfait après {tours_max} tours de sélection. "
        f"Plans écartés : {_detail_rejets(derniers_rejets)}. "
        "Aucun plan non conforme n'est monté : relance avec d'autres sources."
    )


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
    if not plan:
        raise ErreurMontage("Aucun plan validé à exporter ; aucun fichier vidéo n'a été produit.")
    for position, clip in enumerate(plan, start=1):
        source = str(clip.get("source") or "")
        chemin = sources.get(source)
        if not source or chemin is None or not chemin.is_file():
            raise ErreurMontage(
                f"Plan {position} sans fichier source retenu accessible ; export interrompu sans repli."
            )
        if not str(clip.get("source_url") or "").strip():
            raise ErreurMontage(
                f"Plan {position} sans URL de provenance ; export interrompu pour préserver la traçabilité."
            )
    if resolution == "1080" and not config.autoriser_1080:
        raise ErreurMontage("L’export 1080 × 1920 est désactivé sur cette instance pour protéger ses ressources.")
    largeur, hauteur = (1080, 1920) if resolution == "1080" else (config.largeur, config.hauteur)
    ass = ecrire_ass(dossier / "sous_titres.ass", plan, style, largeur, hauteur)
    rapporteur.update("subtitling", 86, "Sous-titres ASS synchronisés préparés")

    commande = ["ffmpeg", "-y"]
    # `sources` ne contient que les fichiers liés aux URL de montage retenues ;
    # `reference_style.mp4` reste isolée pour l'analyse de grammaire visuelle.
    for clip in plan:
        chemin = sources[str(clip["source"])]
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
    reference_optionnelle: bool = False,
) -> dict[str, Any]:
    debut_global = time.monotonic()
    propres, erreurs_forme, doublons = normaliser_liens_tiktok(liens)
    if not propres:
        detail = erreurs_forme[0]["error"] if erreurs_forme else "aucun lien"
        raise ErreurMontage(f"Aucune source TikTok valide : {detail}")
    if len(propres) > 20:
        raise ErreurMontage("20 liens TikTok uniques maximum.")
    segments = creer_segments_script(hook, corps, config.duree_max_plan)
    if not segments:
        raise ErreurMontage("Le script est vide : aucun plan ne peut être préparé.")
    segments = garantir_duree_minimale_segments(
        segments, config.duree_min_video, config.duree_max_plan
    )

    rapporteur.update(
        "validating", 2, f"Validation 0/{len(propres)}", source_errors=erreurs_forme,
        duplicates=doublons, sources=[],
    )

    with dossier_temporaire(config.dossier_travail) as dossier:
        # Seuls les liens fournis dans `liens` alimentent cette table et l'export.
        # La vidéo de référence est stockée à part, sous `reference_path`.
        sources: dict[str, Path] = {}
        metadonnees: dict[str, dict[str, Any]] = {}
        rapports: list[dict[str, Any]] = []
        erreurs_sources = list(erreurs_forme)
        directs: list[tuple[int, str, str, dict[str, Any]]] = []
        reference_propre = ""
        reference_direct = ""
        reference_infos: Optional[dict[str, Any]] = None
        # Avertissement sur la référence de style : rempli dès qu'une référence
        # optionnelle (mode RsT) devient inutilisable, puis par l'analyse de style.
        avertissement_reference = ""

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
                    if reference_optionnelle:
                        # En RsT, la référence de style est un apport : si elle devient
                        # inaccessible, on garde les sources et le style professionnel
                        # par défaut plutôt que de perdre tout le travail.
                        avertissement_reference = (
                            f"Vidéo de référence de style inutilisable, "
                            f"style professionnel conservé : {exc}"
                        )
                        reference_propre, reference_direct, reference_infos = "", "", None
                    else:
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
                try:
                    await telechargeur(session, reference_direct, reference_path)
                except Exception as exc:
                    if reference_optionnelle:
                        reference_path.unlink(missing_ok=True)
                        reference_path = None
                        avertissement_reference = (
                            f"Vidéo de référence de style inutilisable, "
                            f"style professionnel conservé : {exc}"
                        )
                    else:
                        raise

        if reference_path is not None:
            chemin_reference = reference_path.resolve()
            if any(chemin.resolve() == chemin_reference for chemin in sources.values()):
                raise ErreurMontage(
                    "La vidéo de référence de style a été confondue avec un fichier source ; "
                    "l'export est interrompu sans réutiliser cette vidéo."
                )
        if not sources:
            details = _detail_erreurs_sources(erreurs_sources)
            raise ErreurMontage(
                "Aucun téléchargement de source n'a abouti ; aucun montage n'a été exporté. "
                + (f"Raisons : {details}" if details else "Aucune source accessible.")
            )

        # Le sémaphore est borné à deux, configurable à un sur Render gratuit.
        analyses: dict[str, list[dict[str, Any]]] = {}
        verrou = asyncio.Semaphore(max(1, min(2, config.analyses_concurrentes)))
        terminees = 0
        total_analyses = len(sources)

        async def analyser_une(nom: str) -> None:
            nonlocal terminees
            async with verrou:
                rapporteur.checkpoint()
                index = int(metadonnees[nom]["index"])
                try:
                    scenes, avertissement = await analyser_video(
                        sources[nom], float(metadonnees[nom]["duration"]), segments,
                        config, rapporteur, appel_gemini,
                    )
                    analyses[nom] = scenes
                    rapports[index].update(
                        status="analysed", scenes=len(scenes), analysis_warning=avertissement or ""
                    )
                    if avertissement:
                        erreurs_sources.append({
                            "index": index, "url": metadonnees[nom]["url"],
                            "error": avertissement, "status": "analysis_fallback",
                        })
                except ErreurMontage as exc:
                    # Une source refusée par l'IA (texte, réponse invalide, timeout)
                    # est exclue ; les autres sources propres peuvent continuer.
                    rapports[index].update(status="rejected_by_ai", scenes=0, error=str(exc))
                    erreurs_sources.append({
                        "index": index, "url": metadonnees[nom]["url"],
                        "error": str(exc), "status": "rejected_by_ai",
                    })
                terminees += 1
                rapporteur.update(
                    "analysing", 36 + int(34 * terminees / total_analyses),
                    f"Analyse {terminees}/{total_analyses}", sources=rapports,
                    source_errors=erreurs_sources,
                )

        await asyncio.gather(*(analyser_une(nom) for nom in sources))

        style_reference = STYLE_DEFAUT.model_copy(deep=True)
        if reference_path and reference_infos:
            rapporteur.update("analysing", 72, "Analyse de la grammaire visuelle de la référence")
            style_reference, warning = await analyser_style_reference(
                reference_path, float(reference_infos["duration"]), config, rapporteur, appel_gemini
            )
            avertissement_reference = warning or avertissement_reference
            reference_path.unlink(missing_ok=True)
        elif style_sous_titres == "jaune":
            style_reference = style_reference.model_copy(update={"couleur_texte": "#FFD43B"})
        elif style_sous_titres == "centre":
            style_reference = style_reference.model_copy(update={"position_sous_titres": "centre"})

        rapporteur.update(
            "selecting", 76,
            "Sélection stricte : seuls les plans pertinents pour le script"
            if config.exiger_pertinence_visuelle
            else "Sélection et alternance des meilleurs plans",
        )
        try:
            plan = selectionner_plan(
                segments, analyses, metadonnees, style_reference, intensite_transitions,
                config.duree_max_plan,
                exiger_pertinence_visuelle=config.exiger_pertinence_visuelle,
            )
        except ErreurMontage as exc:
            if not config.exiger_validation_ia:
                raise
            details = _detail_erreurs_sources(erreurs_sources)
            details_sources = f" Raisons sources : {details}." if details else ""
            raise ErreurMontage(
                "Aucun plan source n'a satisfait les contrôles stricts ; aucun montage n'a été exporté."
                f"{details_sources} Détail de sélection : {exc}"
            ) from exc
        # CONTRÔLE QUALITÉ FINAL : chaque plan retenu est relu UNE DEUXIÈME FOIS par
        # Gemini sur un extrait de sa fenêtre exacte. Un plan non confirmé est écarté
        # avec sa scène et la sélection est relancée — jamais monté.
        plan, plan_verification = await controler_qualite_plan(
            plan, segments, analyses, metadonnees, style_reference, sources, config,
            rapporteur, appel_gemini, intensite_transitions,
        )
        rapporteur.update(
            "editing", 82,
            f"Plan prêt : {len(plan)} plans, accroche rapide puis scènes principales de 5 s",
            edit_plan=[{
                "source": p["source"], "url": p["source_url"],
                "start": p["debut"], "end": round(p["debut"] + p["duree"], 3),
                "duration": p["duree"],
                "transition": p["transition"], "hook": p["hook"],
            } for p in plan],
        )
        sortie = await exporter_plan(
            plan, sources, style_reference, config, rapporteur, resolution, dossier,
            voix_off=voix_off,
        )
        infos_sortie = await sonder_video(
            sortie, timeout=rapporteur.timeout(min(25, config.delai_ffmpeg))
        )
        if float(infos_sortie["duration"]) + 0.05 < config.duree_min_video:
            sortie.unlink(missing_ok=True)
            raise ErreurMontage(
                f"La vidéo finale ne dure que {infos_sortie['duration']:.2f} s ; "
                f"minimum requis : {config.duree_min_video:.0f} s."
            )
        return {
            "duration": infos_sortie["duration"],
            "path": sortie,
            "url": f"/videos/{sortie.name}",
            "sources": rapports,
            "source_errors": erreurs_sources,
            "duplicates": doublons,
            "reference_style": style_reference.model_dump(),
            "reference_warning": avertissement_reference,
            # Verdict par plan du contrôle qualité final : conforme,timestamps, raisons.
            "plan_verification": plan_verification,
            "elapsed_seconds": round(time.monotonic() - debut_global, 1),
        }
