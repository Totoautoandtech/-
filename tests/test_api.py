import os
import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
import studio_montage


@pytest.fixture(autouse=True)
def nettoyer_etat():
    for task in list(app.JOB_TASKS.values()):
        task.cancel()
    app.JOBS.clear(); app.JOB_TASKS.clear(); app.JOB_INDEX.clear()
    app.SESSIONS_INTEGRATIONS.clear()
    yield
    for task in list(app.JOB_TASKS.values()):
        task.cancel()
    app.JOBS.clear(); app.JOB_TASKS.clear(); app.JOB_INDEX.clear()
    app.SESSIONS_INTEGRATIONS.clear()


@pytest.fixture
def client():
    with TestClient(app.app) as test_client:
        yield test_client


def test_configuration_docker_et_render_contient_les_garde_fous():
    dockerfile = (Path(__file__).parents[1] / "Dockerfile").read_text()
    render = (Path(__file__).parents[1] / "render.yaml").read_text()
    assert "ffmpeg" in dockerfile and "fonts-dejavu-core" in dockerfile and "fonts-liberation" in dockerfile
    assert "--host 0.0.0.0" in dockerfile
    assert "healthCheckPath: /api/sante" in render
    assert 'JOB_TIMEOUT_SECONDES' in render and 'value: "570"' in render


def test_smoke_accueil_sante_et_statiques(client):
    accueil = client.get("/")
    assert accueil.status_code == 200
    assert "Montage multi-source" in accueil.text
    assert "RsT" in accueil.text
    assert "Lien → vidéo" in accueil.text
    assert "Trouver les vidéos et créer" in accueil.text
    assert "Vidéos trouvées par RsT" in accueil.text
    assert "Mes créations" in accueil.text and "Connexions" in accueil.text and "Paramètres" in accueil.text
    sante = client.get("/api/sante")
    assert sante.status_code == 200
    assert sante.json()["ok"] is True
    assert "ffprobe_installe" in sante.json()
    assert client.get("/static/main.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/job-utils.js").status_code == 200
    config = client.get("/api/config").json()
    assert config["max_links"] == 20
    assert config["analysis_fps"] == 6
    assert config["rst"]["candidats_max"] >= 10
    assert config["rst"]["sources_max"] >= 5


@pytest.mark.parametrize("nombre", [1, 4, 20])
def test_api_diagnostic_un_plusieurs_vingt(client, monkeypatch, nombre):
    async def resolveur(_session, url):
        return url

    async def probe(_source, timeout=25):
        return {"duration": 20.0, "width": 720, "height": 1280, "fps": 30.0, "codec": "h264", "size": 10}

    monkeypatch.setattr(app, "_resoudre_video_tiktok", resolveur)
    monkeypatch.setattr(studio_montage, "sonder_video", probe)
    liens = [f"https://www.tiktok.com/@test/video/{10000 + i}?q=x" for i in range(nombre)]
    response = client.post("/api/montage/diagnostic", json={"liens_videos": liens})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["valid_count"] == nombre
    assert data["invalid_count"] == 0
    assert all("?" not in url for url in data["links"])
    assert data["estimated_seconds"] > 0


def test_une_source_indisponible_ne_condamne_pas_les_autres(client, monkeypatch):
    async def resolveur(_session, url):
        if url.endswith("/222"):
            raise app.ErreurApp("TikWM inaccessible (503)")
        return url

    async def probe(_source, timeout=25):
        return {"duration": 15.0, "width": 720, "height": 1280, "fps": 24.0, "codec": "h264", "size": 10}

    monkeypatch.setattr(app, "_resoudre_video_tiktok", resolveur)
    monkeypatch.setattr(studio_montage, "sonder_video", probe)
    response = client.post("/api/montage/diagnostic", json={"liens_videos": [
        "https://www.tiktok.com/@test/video/111",
        "https://www.tiktok.com/@test/video/222",
        "https://www.tiktok.com/@test/video/333",
    ]})
    assert response.status_code == 200
    data = response.json()
    assert data["valid_count"] == 2
    assert data["invalid_count"] == 1
    assert "TikWM inaccessible" in data["errors"][0]["error"]


def test_api_rejette_lien_invalide_avant_job(client):
    response = client.post("/api/jobs/montage", json={
        "hook": "Accroche", "corps": "Corps.", "liens_videos": ["https://example.com/video/1"]
    })
    assert response.status_code == 400
    assert "Aucune vidéo source valide" in response.json()["detail"]


def test_idempotence_double_clic_et_reprise_apres_actualisation(client, monkeypatch):
    gate = asyncio.Event()

    async def production(_requete, contexte, _session_id=""):
        contexte.update(statut="analysing", progress=50, detail="Analyse 1/1")
        await gate.wait()
        return {"url": "/videos/factice.mp4", "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "_produire_montage", production)
    payload = {
        "hook": "Une accroche forte",
        "corps": "Voici le corps du script.",
        "liens_videos": ["https://www.tiktok.com/@test/video/123"],
        "idempotency_key": "clic-unique"
    }
    first = client.post("/api/jobs/montage", json=payload)
    second = client.post("/api/jobs/montage", json=payload)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["reused"] is True

    job_id = first.json()["job_id"]
    # Une nouvelle requête GET avec le cookie représente le suivi repris après refresh.
    status = client.get(f"/api/jobs/{job_id}")
    assert status.status_code == 200
    assert status.json()["job_id"] == job_id
    assert status.json()["status"] in {"queued", "validating", "analysing"}

    # Libère proprement la coroutine sur la boucle du TestClient via l'annulation API.
    cancelled = client.post(f"/api/jobs/{job_id}/cancel", json={})
    assert cancelled.status_code == 200


def test_lot_de_six_projets_independants_et_historique(client, monkeypatch):
    gate = asyncio.Event()

    async def production(requete, contexte, _session_id=""):
        contexte.update(statut="analysing", progress=35, detail=f"Analyse de {requete.titre}")
        await gate.wait()
        return {"url": f"/videos/{requete.idempotency_key}.mp4"}

    monkeypatch.setattr(app, "_produire_montage", production)
    projets = [{
        "titre": f"Projet {index + 1}",
        "hook": f"Accroche {index + 1}",
        "corps": f"Corps différent {index + 1}.",
        "liens_videos": [f"https://www.tiktok.com/@test/video/{1000 + index}"],
        "idempotency_key": f"projet-{index + 1}",
    } for index in range(6)]
    response = client.post("/api/jobs/montage/batch", json={
        "projets": projets, "idempotency_key": "lot-six"
    })
    assert response.status_code == 202, response.text
    assert response.json()["count"] == 6
    assert len({job["job_id"] for job in response.json()["jobs"]}) == 6

    history = client.get("/api/jobs")
    assert history.status_code == 200
    jobs = history.json()["jobs"]
    assert len(jobs) == 6
    assert {job["title"] for job in jobs} == {f"Projet {index + 1}" for index in range(6)}
    assert all(job["batch_total"] == 6 for job in jobs)
    assert history.json()["retention_seconds"] >= 6 * 60 * 60

    # L'historique reste accessible après l'équivalent d'une heure.
    for job in app.JOBS.values():
        job["created_at"] -= 3600
        job["updated_at"] -= 3600
    assert len(client.get("/api/jobs").json()["jobs"]) == 6

    for job_id in list(app.JOBS):
        client.post(f"/api/jobs/{job_id}/cancel", json={})


def test_lot_refuse_plus_de_six_projets(client):
    projet = {
        "titre": "Projet", "hook": "Accroche", "corps": "Corps.",
        "liens_videos": ["https://www.tiktok.com/@test/video/123"],
    }
    response = client.post("/api/jobs/montage/batch", json={"projets": [projet] * 7})
    assert response.status_code == 422


def test_limite_estimee_demande_confirmation(client):
    response = client.post("/api/jobs/montage", json={
        "hook": "Accroche", "corps": "Corps.",
        "liens_videos": ["https://www.tiktok.com/@test/video/123"],
        "estimated_seconds": 600, "accepter_risque": False,
    })
    assert response.status_code == 409
    assert "Confirme" in response.json()["detail"]


def test_drive_automatique_reussite_et_echec(tmp_path, monkeypatch):
    session_id = "a" * 48
    app.SESSIONS_INTEGRATIONS[session_id] = {"google_drive": {"access_token": "secret"}}
    fichier = tmp_path / "video.mp4"; fichier.write_bytes(b"video")

    async def succes(_session, _chemin):
        return {"id": "drive-id", "name": "video.mp4", "url": "https://drive.google.com/open?id=drive-id"}

    monkeypatch.setattr(app, "_sauvegarder_sur_drive", succes)
    contexte = app.ContexteJob("job-drive-ok", {"status": "uploading", "progress": 0})
    resultat = asyncio.run(app._sauvegarde_auto_drive(contexte, session_id, fichier))
    assert resultat["status"] == "completed"
    assert resultat["url"].startswith("https://drive.google.com/")

    async def echec(_session, _chemin):
        raise app.ErreurApp("quota Drive temporairement indisponible")

    monkeypatch.setattr(app, "_sauvegarder_sur_drive", echec)
    contexte = app.ContexteJob("job-drive-ko", {"status": "uploading", "progress": 0})
    resultat = asyncio.run(app._sauvegarde_auto_drive(contexte, session_id, fichier))
    assert resultat["status"] == "failed"
    assert "quota Drive" in resultat["error"]


def test_mode_qualite_affine_l_encodage():
    rapide = app._configuration_montage("rapide")
    qualite = app._configuration_montage("qualite")
    assert qualite.crf < rapide.crf
    assert qualite.crf >= 14


def _attendre_job(client, job_id, delai=30.0):
    """Interroge un job jusqu'à un état final (les recherches RsT sont cadencées)."""
    limite = time.time() + delai
    while time.time() < limite:
        etat = client.get(f"/api/jobs/{job_id}")
        assert etat.status_code == 200, etat.text
        if etat.json()["status"] in {"completed", "failed", "cancelled"}:
            return etat.json()
        time.sleep(0.25)
    raise AssertionError("Le job n'a pas atteint d'état final dans le délai imparti.")


def test_rst_trouve_de_vraies_videos_puis_monte(client, monkeypatch):
    appels = []

    async def donnees_tikwm(_session, chemin, params):
        appels.append((chemin, dict(params)))
        if chemin == "/":
            return {
                "id": "111", "title": "Recette de pancakes faciles #cuisine #food",
                "duration": 21, "author": {"unique_id": "chef", "nickname": "Chef"},
            }
        if chemin == "/user/posts":
            videos = [{
                # La vidéo de départ ne doit jamais devenir candidate.
                "video_id": "111", "title": "seed", "duration": 21,
                "author": {"unique_id": "chef"},
            }]
            for i in range(20):
                videos.append({
                    "video_id": f"1{i + 12}", "title": f"Recette numéro {i}",
                    "duration": 12 + i % 5, "author": {"unique_id": "chef"},
                })
            videos.append({"video_id": "900", "title": "trop long", "duration": 400, "author": {"unique_id": "chef"}})
            videos.append({"video_id": "901", "title": "trop court", "duration": 2, "author": {"unique_id": "chef"}})
            videos.append({"video_id": "902", "title": "durée inconnue", "duration": None, "author": {"unique_id": "chef"}})
            return {"videos": videos}
        if chemin == "/feed/search":
            return {"videos": [
                {"video_id": f"2{i:03d}", "title": f"vidéo de recherche {i}",
                 "duration": 14 + i % 3, "author": {"unique_id": f"auteur{i}"}}
                for i in range(20)
            ]}
        raise AssertionError(f"endpoint TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)

    async def script(_texte):
        return {"hook": "Hook RsT", "corps": "Corps du script RsT.", "mot_cle_broll": "cuisine maison"}

    monkeypatch.setattr(app, "generer_script", script)

    montages = []

    async def montage_fake(**kwargs):
        montages.append(kwargs)
        return {
            "url": "/videos/rst.mp4", "path": app.DOSSIER_VIDEOS / "rst.mp4",
            "sources": [], "source_errors": [],
        }

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    response = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@chef/video/111?is_copy_url=1",
        "mode": "rapide", "intensite_transitions": 1,
    })
    assert response.status_code == 202, response.text
    job = _attendre_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job.get("error")
    assert job["url"] == "/videos/rst.mp4"
    assert job["script"]["hook"] == "Hook RsT"

    # Vidéos réellement trouvées : seed exclue, plafonnées au maximum configuré.
    trouves = job["found_videos"]
    assert 20 <= len(trouves) <= app.CONFIG.rst_candidats_max
    assert all(v["video_id"] != "111" for v in trouves)
    assert all(v["url"].startswith("https://www.tiktok.com/@") for v in trouves)
    retenues = [v for v in trouves if v["selected"]]
    assert len(retenues) == min(20, app.CONFIG.rst_sources_max)

    # Les durées hors limites sont signalées honnêtement, jamais retenues.
    hors_limites = [v for v in trouves if v["video_id"] in {"900", "901", "902"}]
    assert hors_limites
    assert all(not v["selected"] and v["rejet"] for v in hors_limites)

    # Le montage reçoit exactement les sources retenues et les réglages demandés.
    assert len(montages) == 1
    assert montages[0]["liens"] == [v["url"] for v in retenues]
    assert montages[0]["intensite_transitions"] == 1
    assert montages[0]["resolution"] == "720"
    assert montages[0]["hook"] == "Hook RsT"

    # Les recherches réellement effectuées sont exposées, l'historique aussi.
    assert any("cuisine" in requete for requete in job["search_queries"])
    historique = client.get("/api/jobs").json()["jobs"]
    assert any(
        entree["type"] == "rst" and entree.get("found_count") == len(trouves)
        for entree in historique
    )


def test_rst_rejette_lien_non_tiktok(client):
    response = client.post("/api/jobs/rst", json={"lien": "https://example.com/article"})
    assert response.status_code == 400
    assert "invalide" in response.json()["detail"]


def test_rst_aucune_video_trouvee_echoue_honnetement(client, monkeypatch):
    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {"id": "111", "title": "Sujet très pointu #rare", "duration": 12,
                    "author": {"unique_id": "auteur"}}
        return {"videos": []}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)

    async def script(_texte):
        return {"hook": "Hook", "corps": "Corps.", "mot_cle_broll": "rare"}

    monkeypatch.setattr(app, "generer_script", script)

    response = client.post("/api/jobs/rst", json={"lien": "https://www.tiktok.com/@auteur/video/111"})
    assert response.status_code == 202
    job = _attendre_job(client, response.json()["job_id"])
    assert job["status"] == "failed"
    assert "aucune autre vidéo TikTok" in job["error"]


# ======================================================================================
# RÉSILIENCE GEMINI : 503 « high demand », chaîne de modèles et backoff
# ======================================================================================


class _FausseReponseGemini:
    def __init__(self, statut: int, texte: str) -> None:
        self.status = statut
        self._texte = texte

    async def text(self) -> str:
        return self._texte

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FausseSessionGemini:
    """Remplace aiohttp.ClientSession : renvoie une réponse par modèle appelé."""

    appels: list[str] = []
    reponses_par_modele: dict[str, tuple[int, str]] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        modele = url.rsplit("/", 1)[-1].split(":", 1)[0]
        type(self).appels.append(modele)
        statut, corps = type(self).reponses_par_modele.get(
            modele, (503, '{"error":{"code":503,"message":"high demand"}}')
        )
        return _FausseReponseGemini(statut, corps)


REPONSE_GEMINI_OK = '{"candidates":[{"content":{"parts":[{"text":"réponse modèle de secours"}]}}]}'
REPONSE_GEMINI_503 = (
    '{"error":{"code":503,"status":"UNAVAILABLE",'
    '"message":"This model is currently experiencing high demand."}}'
)


@pytest.fixture
def gemini_simule(monkeypatch):
    """Isole les appels Gemini : pas de réseau, pas d'attente réelle."""
    _FausseSessionGemini.appels = []
    _FausseSessionGemini.reponses_par_modele = {}
    monkeypatch.setattr(app.aiohttp, "ClientSession", _FausseSessionGemini)
    monkeypatch.setattr(app, "GEMINI_BACKOFF", (0, 0, 0, 0))
    monkeypatch.setattr(app.CONFIG, "gemini_api_keys", ["cle-de-test"])
    monkeypatch.setattr(
        app.CONFIG, "gemini_modeles", ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
    )
    return _FausseSessionGemini


def test_chaine_de_modeles_par_defaut_et_variable_denvironnement(monkeypatch):
    monkeypatch.delenv("GEMINI_MODELES", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    defaut = app.Config.charger()
    assert defaut.gemini_modeles == ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]

    monkeypatch.setenv("GEMINI_MODELES", "modele-a, modele-b ,")
    personnalise = app.Config.charger()
    assert personnalise.gemini_modeles[:2] == ["modele-a", "modele-b"]

    render = (Path(__file__).parents[1] / "render.yaml").read_text(encoding="utf-8")
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "GEMINI_MODELES" in render and "GEMINI_MODELES" in readme


def test_gemini_503_bascule_sur_le_modele_suivant(gemini_simule):
    """Le premier modèle est saturé (503) : le suivant prend le relais automatiquement."""
    gemini_simule.reponses_par_modele = {
        "gemini-2.5-flash": (503, REPONSE_GEMINI_503),
        "gemini-2.5-flash-lite": (200, REPONSE_GEMINI_OK),
    }

    texte = asyncio.run(app._appel_gemini_brut([{"text": "bonjour"}], temperature=0.5))

    assert texte == "réponse modèle de secours"
    # 5 tentatives sur le modèle saturé, puis succès immédiat sur le modèle de secours.
    assert gemini_simule.appels.count("gemini-2.5-flash") == app.GEMINI_TENTATIVES_PAR_MODELE
    assert gemini_simule.appels[-1] == "gemini-2.5-flash-lite"


def test_gemini_429_bascule_aussi_sur_le_modele_suivant(gemini_simule):
    gemini_simule.reponses_par_modele = {
        "gemini-2.5-flash": (429, '{"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}'),
        "gemini-2.5-flash-lite": (503, REPONSE_GEMINI_503),
        "gemini-2.0-flash": (200, REPONSE_GEMINI_OK),
    }
    texte = asyncio.run(app._appel_gemini_brut([{"text": "bonjour"}], temperature=0.5))
    assert texte == "réponse modèle de secours"
    assert gemini_simule.appels[-1] == "gemini-2.0-flash"


def test_gemini_503_total_donne_un_message_clair_en_francais(gemini_simule):
    """Tous les modèles et toutes les clés saturés : message explicite, pas de trace brute."""
    gemini_simule.reponses_par_modele = {
        "gemini-2.5-flash": (503, REPONSE_GEMINI_503),
        "gemini-2.5-flash-lite": (503, REPONSE_GEMINI_503),
        "gemini-2.0-flash": (503, REPONSE_GEMINI_503),
    }

    with pytest.raises(app.ErreurApp) as erreur:
        asyncio.run(app._appel_gemini_brut([{"text": "bonjour"}], temperature=0.5))

    assert str(erreur.value) == "Gemini est momentanément saturé (503). Réessaie dans quelques minutes."
    assert str(erreur.value) == app.GEMINI_MESSAGE_SATURE
    # 3 modèles × 5 tentatives : le backoff exponentiel est bien appliqué par modèle.
    assert len(gemini_simule.appels) == 3 * app.GEMINI_TENTATIVES_PAR_MODELE


def test_gemini_erreur_definitive_ne_declenche_pas_cinq_tentatives(gemini_simule):
    """Une erreur 400 n'est pas réessayable : on passe tout de suite au modèle suivant."""
    gemini_simule.reponses_par_modele = {
        "gemini-2.5-flash": (400, '{"error":{"code":400,"message":"clé invalide"}}'),
        "gemini-2.5-flash-lite": (400, '{"error":{"code":400,"message":"clé invalide"}}'),
        "gemini-2.0-flash": (400, '{"error":{"code":400,"message":"clé invalide"}}'),
    }
    with pytest.raises(app.ErreurApp) as erreur:
        asyncio.run(app._appel_gemini_brut([{"text": "bonjour"}], temperature=0.5))
    assert "Toutes les clés Gemini ont échoué" in str(erreur.value)
    assert len(gemini_simule.appels) == 3


# ======================================================================================
# VOIX OFF IMPORTÉE
# ======================================================================================


def test_config_publique_expose_rst_et_voix_off(client):
    config = client.get("/api/config").json()
    assert config["rst"]["liens_par_lancement"] == 6
    assert config["voix_off_max_mo"] == 25
    assert ".mp3" in config["voix_off_extensions"]


def test_voix_off_refuse_une_extension_non_audio(client):
    reponse = client.post("/api/voixoff?nom=script.txt", content=b"abc")
    assert reponse.status_code == 400
    assert "Format audio" in reponse.json()["detail"]


def test_voix_off_refuse_un_fichier_vide_ou_trop_lourd(client):
    assert client.post("/api/voixoff?nom=voix.mp3", content=b"").status_code == 400
    trop = client.post(
        "/api/voixoff?nom=voix.mp3",
        content=b"0",
        headers={"Content-Length": str(app.VOIX_OFF_MAX_OCTETS + 1)},
    )
    assert trop.status_code == 413
    assert "25 Mo" in trop.json()["detail"]


def test_voix_off_acceptee_puis_utilisable_et_liee_a_la_session(client, monkeypatch):
    async def sonder(_chemin):
        return 12.5

    monkeypatch.setattr(app, "_sonder_audio", sonder)
    reponse = client.post("/api/voixoff?nom=ma voix.mp3", content=b"faux-audio")
    assert reponse.status_code == 200
    donnees = reponse.json()
    assert donnees["duree"] == 12.5 and donnees["extension"] == ".mp3"
    identifiant = donnees["voix_off"]

    session_id = client.cookies.get("creator_session")
    chemin = app._chemin_voix_off(session_id, identifiant)
    assert chemin is not None and chemin.read_bytes() == b"faux-audio"
    # Une autre session ne peut pas réutiliser l'identifiant d'autrui.
    assert app._chemin_voix_off("une-autre-session", identifiant) is None
    assert app._resoudre_voix_off(session_id, identifiant) == chemin
    chemin.unlink(missing_ok=True)


def test_voix_off_expiree_renvoie_un_message_explicite():
    with pytest.raises(app.ErreurApp) as erreur:
        app._resoudre_voix_off("session-inconnue", "a" * 32)
    assert "n'est plus disponible" in str(erreur.value)
    assert app._resoudre_voix_off("session-inconnue", "") is None


def test_voix_off_refusee_si_ffprobe_ne_trouve_aucune_piste(client, monkeypatch):
    async def sonder(_chemin):
        raise app.ErreurApp("Ce fichier ne contient aucune piste audio exploitable.")

    monkeypatch.setattr(app, "_sonder_audio", sonder)
    reponse = client.post("/api/voixoff?nom=image.wav", content=b"pas-de-son")
    assert reponse.status_code == 400
    assert "aucune piste audio" in reponse.json()["detail"]
    # Le fichier refusé n'est pas conservé sur le disque.
    dossier = app._dossier_voix_off(client.cookies.get("creator_session") or "")
    assert not dossier.is_dir() or not list(dossier.iterdir())


def test_requetes_acceptent_le_champ_voix_off():
    assert app.RequeteRst(lien="https://www.tiktok.com/@a/video/1", voix_off="x" * 32).voix_off
    assert app.RequeteVideo(hook="h", corps="c", mot_cle_broll="m", voix_off="y" * 32).voix_off
    assert app.RequeteMontage(
        hook="h", corps="c", liens_videos=["https://www.tiktok.com/@a/video/1"], voix_off="z" * 32
    ).voix_off


def test_purge_voix_off_supprime_les_fichiers_de_plus_de_six_heures():
    dossier = app._dossier_voix_off("session-purge")
    dossier.mkdir(parents=True, exist_ok=True)
    ancien = dossier / f"{'a' * 32}.mp3"
    ancien.write_bytes(b"vieux")
    vieux = time.time() - app.DUREE_VIE_VOIX_OFF - 60
    os.utime(ancien, (vieux, vieux))
    app._purger_voix_off()
    assert not ancien.exists()


# ======================================================================================
# INTERFACE : RsT multiple (jusqu'à 6 liens) et champs voix off
# ======================================================================================


def test_interface_contient_rst_multiple_et_voix_off():
    racine = Path(__file__).parents[1]
    html = (racine / "static" / "index.html").read_text(encoding="utf-8")
    js = (racine / "static" / "main.js").read_text(encoding="utf-8")
    utils = (racine / "static" / "job-utils.js").read_text(encoding="utf-8")
    css = (racine / "static" / "styles.css").read_text(encoding="utf-8")

    assert 'id="rst-liens"' in html and "0/6 liens" in html
    assert 'id="job-list"' in html
    assert html.count("Voix off") >= 3 and "facultatif" in html
    assert 'id="voix-lien"' in html and 'id="voix-rst"' in html and 'id="voix-montage"' in html
    assert "vesper.rstJobs.v1" in utils and "loadRstJobs" in js
    assert "/api/voixoff" in js and "voix_off" in js
    assert "job-card" in js and "found-bloc" in js
    assert "rst-liens" in js and "parseRstLinks" in js

    # Thème noir & blanc minimal : polices Inter + DM Mono, plus de violet ni de cyan.
    assert "Inter" in html and "DM+Mono" in html
    assert '"Inter"' in css and '"DM Mono"' in css
    assert "#7c5cff" not in css and "#43d9ff" not in css
    assert "--accent: #ffffff;" in css
