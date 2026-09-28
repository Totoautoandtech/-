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
    assert all(4.5 <= p["duree"] <= 5.5 for p in plan if not p["hook"])
    assert all(plan[i]["source"] != plan[i - 1]["source"] for i in range(1, min(3, len(plan))))
    assert all(p["transition_duree"] <= 0.4 for p in plan)
    assert len({p["transition"] for p in plan[1:]}) > 1
