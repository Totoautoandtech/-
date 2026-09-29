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
