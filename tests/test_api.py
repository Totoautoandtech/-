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
    sante = client.get("/api/sante")
    assert sante.status_code == 200
    assert sante.json()["ok"] is True
    assert "ffprobe_installe" in sante.json()
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/job-utils.js").status_code == 200
    config = client.get("/api/config").json()
    assert config["max_links"] == 20
    assert config["analysis_fps"] == 6


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
