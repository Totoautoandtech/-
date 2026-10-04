import asyncio
import sys
from pathlib import Path

import pytest

import studio_montage as montage


@pytest.mark.parametrize("nombre", [1, 3, 20])
def test_accepte_un_plusieurs_et_vingt_liens(nombre):
    liens = [f"https://www.tiktok.com/@compte/video/{1000 + i}" for i in range(nombre)]
    propres, erreurs, doublons = montage.normaliser_liens_tiktok(liens)
    assert len(propres) == nombre
    assert erreurs == []
    assert doublons == []


def test_nettoie_parametres_et_doublons():
    liens = [
        "https://www.tiktok.com/@demo/video/123?q=abc&t=9",
        "https://www.tiktok.com/@demo/video/123?is_from_webapp=1",
    ]
    propres, erreurs, doublons = montage.normaliser_liens_tiktok(liens)
    assert propres == ["https://www.tiktok.com/@demo/video/123"]
    assert erreurs == []
    assert doublons == propres


def test_rejette_lien_invalide():
    propres, erreurs, _ = montage.normaliser_liens_tiktok(["javascript:alert(1)", "https://example.com/video/1"])
    assert propres == []
    assert len(erreurs) == 2
    assert all(item["status"] == "invalid" for item in erreurs)


def _config(tmp_path: Path) -> montage.ConfigurationMontage:
    work = tmp_path / "travail"
    videos = tmp_path / "videos"
    work.mkdir(); videos.mkdir()
    return montage.ConfigurationMontage(dossier_travail=work, dossier_videos=videos)


def _reporter():
    return montage.Rapporteur(lambda **_: None, lambda: False, lambda: 300.0)


def test_reponse_gemini_invalide_utilise_repli_et_nettoie_apercu(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")

    async def fake_preview(_source, destination, _duration, _config, _reporter):
        destination.write_bytes(b"preview")
        return destination

    async def fake_gemini(*_args, **_kwargs):
        return "pas du json"

    monkeypatch.setattr(montage, "creer_apercu", fake_preview)
    scenes, warning = asyncio.run(montage.analyser_video(
        source, 12.0, [{"id": 0, "texte": "bonjour"}], _config(tmp_path), _reporter(), fake_gemini
    ))
    assert scenes[0]["score_pertinence"] == 0.25
    assert "invalide" in warning
    assert not (tmp_path / "source_analyse.mp4").exists()


def test_timeout_gemini_utilise_repli(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")

    async def fake_preview(_source, destination, _duration, _config, _reporter):
        destination.write_bytes(b"preview")
        return destination

    async def timeout_gemini(*_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch.setattr(montage, "creer_apercu", fake_preview)
    scenes, warning = asyncio.run(montage.analyser_video(
        source, 8.0, [{"id": 0, "texte": "test"}], _config(tmp_path), _reporter(), timeout_gemini
    ))
    assert scenes
    assert "Délai Gemini" in warning


def test_ffmpeg_en_erreur_retourne_etape_precise():
    async def lancer():
        await montage.executer_commande(
            [sys.executable, "-c", "import sys; print('codec cassé', file=sys.stderr); sys.exit(2)"],
            etape="export test", timeout=5,
        )

    with pytest.raises(montage.ErreurMontage, match="export test"):
        asyncio.run(lancer())


def test_nettoyage_dossier_temporaire_meme_en_echec(tmp_path):
    racine = tmp_path / "travail"
    chemin = None
    with pytest.raises(RuntimeError):
        with montage.dossier_temporaire(racine) as dossier:
            chemin = dossier
            (dossier / "partiel.mp4").write_bytes(b"x")
            raise RuntimeError("échec simulé")
    assert chemin is not None
    assert not chemin.exists()


def test_plan_accroche_rapide_puis_plans_cinq_secondes():
    segments = montage.creer_segments_script(
        "Voici le secret incroyable",
        "La première scène explique le contexte. La seconde montre le résultat final."
    )
    hook = [s for s in segments if s["hook"]]
    corps = [s for s in segments if not s["hook"]]
    assert len(hook) >= 2
    assert all(0.6 <= s["duree_cible"] <= 1.5 for s in hook)
    assert all(s["duree_cible"] == 5.0 for s in corps)

    analyses = {}
    metadata = {}
    for index in range(3):
        nom = f"source_{index}"
        metadata[nom] = {"duration": 30.0}
        analyses[nom] = [{
            "debut": 1.0, "fin": 8.0, "sujet": "test", "action_mouvement": "dynamique",
            "qualite": "bonne", "nettete": 0.9, "cadrage": "vertical", "texte_visible": False,
            "watermark": False,
            "pertinence_script": [{"id": s["id"], "score": 0.9} for s in segments],
            "rythme": "dynamique", "transition_recommandee": "cut", "score_pertinence": 0.9,
        }]
    plan = montage.selectionner_plan(segments, analyses, metadata, montage.STYLE_DEFAUT)
    assert all(0.55 <= p["duree"] <= 1.5 for p in plan if p["hook"])
    assert all(2.5 <= p["duree"] <= 5.5 for p in plan if not p["hook"])
    assert all(plan[i]["source"] != plan[i - 1]["source"] for i in range(1, min(3, len(plan))))
    assert all(p["transition_duree"] <= 0.4 for p in plan)
    assert len({p["transition"] for p in plan[1:]}) > 1


def test_intensite_transitions_0_ne_garde_que_les_coupes():
    segments = montage.creer_segments_script(
        "Voici le secret incroyable",
        "La première scène explique le contexte. La seconde montre le résultat final."
    )
    analyses, metadata = {}, {}
    for index in range(3):
        nom = f"source_{index}"
        metadata[nom] = {"duration": 30.0}
        analyses[nom] = [{
            "debut": 1.0, "fin": 8.0, "sujet": "test", "action_mouvement": "dynamique",
            "qualite": "bonne", "nettete": 0.9, "cadrage": "vertical", "texte_visible": False,
            "watermark": False,
            "pertinence_script": [{"id": s["id"], "score": 0.9} for s in segments],
            "rythme": "dynamique", "transition_recommandee": "fade", "score_pertinence": 0.9,
        }]

    assert montage.borner_intensite_transitions(-3) == 0
    assert montage.borner_intensite_transitions("2") == 2
    assert montage.borner_intensite_transitions(9) == 3
    assert montage.borner_intensite_transitions(None) == 2

    plan = montage.selectionner_plan(segments, analyses, metadata, montage.STYLE_DEFAUT, 0)
    assert plan[0]["transition"] == "none"
    assert all(p["transition"] == "cut" for p in plan[1:])
    assert all(p["transition_duree"] <= 0.06 for p in plan[1:])


def test_intensite_transitions_forte_allonge_les_transitions():
    segments = montage.creer_segments_script("Une accroche marquante", "Une phrase complète. Une autre phrase.")
    scene = [{
        "debut": 0.0, "fin": 30.0, "sujet": "test", "action_mouvement": "dynamique",
        "qualite": "bonne", "nettete": 0.9, "cadrage": "vertical", "texte_visible": False,
        "watermark": False,
        "pertinence_script": [{"id": s["id"], "score": 0.9} for s in segments],
        "rythme": "dynamique", "transition_recommandee": "fade", "score_pertinence": 0.9,
    }]
    analyses = {"source_0": scene}
    metadata = {"source_0": {"duration": 30.0}}

    legere = montage.selectionner_plan(segments, analyses, metadata, montage.STYLE_DEFAUT, 1)
    forte = montage.selectionner_plan(segments, analyses, metadata, montage.STYLE_DEFAUT, 3)
    fondu_legers = [p["transition_duree"] for p in legere[1:] if p["transition"] != "cut"]
    fondu_forts = [p["transition_duree"] for p in forte[1:] if p["transition"] != "cut"]
    assert fondu_legers and fondu_forts
    assert max(fondu_forts) > max(fondu_legers)
    assert all(p["transition_duree"] <= 0.55 for p in forte)


def test_arguments_audio_sans_voix_off_reste_muet():
    entrees, filtres, sortie = montage._arguments_audio(None, 3)
    assert entrees == [] and filtres == []
    assert sortie == ["-an"]


def test_arguments_audio_avec_voix_off_mappe_une_piste_aac():
    from pathlib import Path as _Path

    entrees, filtres, sortie = montage._arguments_audio(_Path("/tmp/voix.mp3"), 4)
    assert entrees == ["-i", "/tmp/voix.mp3"]
    assert filtres == ["[4:a]apad[a]"]
    assert sortie == ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-shortest"]


def _analyses_longues(segments, nombre=3, duree=30.0):
    """Sources longues et parfaitement pertinentes : rien ne bride la durée des plans."""
    analyses, metadata = {}, {}
    for index in range(nombre):
        nom = f"source_{index}"
        metadata[nom] = {"duration": duree}
        analyses[nom] = [{
            "debut": 0.0, "fin": duree, "sujet": "test", "action_mouvement": "dynamique",
            "qualite": "bonne", "nettete": 0.9, "cadrage": "vertical", "texte_visible": False,
            "watermark": False,
            "pertinence_script": [{"id": s["id"], "score": 0.9} for s in segments],
            "rythme": "dynamique", "transition_recommandee": "cut", "score_pertinence": 0.9,
        }]
    return analyses, metadata


def test_aucun_plan_ne_depasse_cinq_secondes_meme_avec_un_style_lent():
    """Un style de référence très lent ne doit jamais produire de plan de plus de 5 s."""
    segments = montage.creer_segments_script(
        "Voici le secret incroyable",
        "La première scène explique le contexte. La seconde montre le résultat final.",
    )
    assert all(s["duree_cible"] <= montage.DUREE_MAX_PLAN_DEFAUT for s in segments)

    style_lent = montage.STYLE_DEFAUT.model_copy(deep=True, update={"duree_moyenne_plans": 9.5})
    analyses, metadata = _analyses_longues(segments)
    plan = montage.selectionner_plan(segments, analyses, metadata, style_lent)
    assert plan
    assert all(p["duree"] <= montage.DUREE_MAX_PLAN_DEFAUT for p in plan)
    # Les plans du corps restent « pleins » : on plafonne sans raboter le rythme.
    assert all(len(s["texte"].split()) / montage.MOTS_PAR_SECONDE_PAROLE - 0.2 <= p["duree"] <= montage.DUREE_MAX_PLAN_DEFAUT for p, s in zip(plan, segments) if not p["hook"])


def test_duree_max_plan_personnalisee_borne_segments_et_plan():
    """Le plafond est configurable de bout en bout : segments, plan et garde-fou final."""
    segments = montage.creer_segments_script(
        "Une accroche marquante", "Une phrase complète. Une autre phrase.", 3.0
    )
    assert all(s["duree_cible"] <= 3.0 for s in segments)

    analyses, metadata = _analyses_longues(segments)
    plan = montage.selectionner_plan(
        segments, analyses, metadata, montage.STYLE_DEFAUT, 2, 3.0
    )
    assert all(p["duree"] <= 3.0 for p in plan)


@pytest.mark.parametrize("valeur", [0, -4, None, "abc", float("nan")])
def test_duree_max_plan_invalide_retombe_sur_cinq_secondes(valeur):
    assert montage._borner_duree_plan(valeur) == montage.DUREE_MAX_PLAN_DEFAUT


def test_configuration_montage_porte_la_duree_max_plan(tmp_path):
    config = montage.ConfigurationMontage(dossier_travail=tmp_path, dossier_videos=tmp_path)
    assert config.duree_max_plan == montage.DUREE_MAX_PLAN_DEFAUT
    # Le mode « pertinence visuelle stricte » est désactivé par défaut : seuls les
    # montages RsT l'activent, les autres modes ne changent pas de comportement.
    assert config.exiger_pertinence_visuelle is False
    strict = montage.ConfigurationMontage(
        dossier_travail=tmp_path, dossier_videos=tmp_path, exiger_pertinence_visuelle=True
    )
    assert strict.exiger_pertinence_visuelle is True


# -------------------------------------------------------------------------------------
# Mode RsT « pertinence visuelle stricte » : un plan beau mais hors sujet est refusé
# -------------------------------------------------------------------------------------


def _scene(segments, sujet, action, score):
    """Scène d'analyse : superbe (nettete 0.95) mais avec la pertinence donnée."""
    return {
        "debut": 0.0, "fin": 20.0, "sujet": sujet, "action_mouvement": action,
        "qualite": "bonne", "nettete": 0.95, "cadrage": "vertical", "texte_visible": False,
        "watermark": False,
        "pertinence_script": [{"id": s["id"], "score": score} for s in segments],
        "rythme": "dynamique", "transition_recommandee": "cut", "score_pertinence": score,
    }


def test_mode_strict_refuse_les_plans_trop_peu_pertinents():
    """Échec réel du 3 octobre : une skyline de Dubaï montée sur un script téléphone.

    En mode strict, un plan esthétique mais sans rapport avec le script (pertinence
    0.22, sous les seuils hook 0.36 / corps 0.45) doit faire échouer le rendu plutôt
    que de glisser dans le montage.
    """
    segments = montage.creer_segments_script(
        "Top 3 des téléphones", "Ce téléphone a un écran incroyable."
    )
    analyses = {"source_0": [_scene(segments, "gratte-ciel à Dubai", "panoramique aérien de la skyline", 0.22)]}
    metadata = {"source_0": {"duration": 20.0}}
    with pytest.raises(montage.ErreurMontage) as excinfo:
        montage.selectionner_plan(
            segments, analyses, metadata, montage.STYLE_DEFAUT,
            exiger_pertinence_visuelle=True,
        )
    message = str(excinfo.value)
    assert "Aucune scène assez pertinente" in message
    assert "segment" in message
    assert "hors sujet" in message
    assert "meilleures sources" in message


def test_mode_strict_garde_les_plans_vraiment_pertinents():
    """Une scène vraiment pertinente (smartphone en main, 0.86) passe le filtre strict.

    Une scène hors sujet reste disponible sur une autre source : elle ne doit jamais
    être choisie tant qu'un plan pertinent existe.
    """
    segments = montage.creer_segments_script(
        "Top 3 des téléphones", "Ce téléphone a un écran incroyable."
    )
    analyses = {
        "source_0": [_scene(segments, "smartphone tenu en main", "démonstration de l'écran", 0.86)],
        "source_1": [_scene(segments, "skyline de Dubai", "vue aérienne", 0.22)],
    }
    metadata = {"source_0": {"duration": 20.0}, "source_1": {"duration": 20.0}}
    plan = montage.selectionner_plan(
        segments, analyses, metadata, montage.STYLE_DEFAUT,
        exiger_pertinence_visuelle=True,
    )
    assert plan
    # Seule la scène réellement pertinente est montée — jamais la skyline.
    assert all(p["source"] == "source_0" for p in plan)
    assert all(p["duree"] <= montage.DUREE_MAX_PLAN_DEFAUT for p in plan)
    # Hors mode strict, le comportement historique est inchangé (la skyline reste
    # candidate, simplement moins bien classée).
    plan_souplet = montage.selectionner_plan(segments, analyses, metadata, montage.STYLE_DEFAUT)
    assert plan_souplet and all(p["duree"] <= montage.DUREE_MAX_PLAN_DEFAUT for p in plan_souplet)
