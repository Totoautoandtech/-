import ast
import base64
import json
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
    _purger_profils_tests()
    yield
    for task in list(app.JOB_TASKS.values()):
        task.cancel()
    app.JOBS.clear(); app.JOB_TASKS.clear(); app.JOB_INDEX.clear()
    app.SESSIONS_INTEGRATIONS.clear()
    _purger_profils_tests()


def _purger_profils_tests():
    """Retire les fichiers de profils d'entraînement créés par les tests."""
    for fichier in app.DOSSIER_PROFILS_ENTRAINEMENT.glob("*.json"):
        fichier.unlink(missing_ok=True)
    for fichier in app.DOSSIER_PROFILS_ENTRAINEMENT.glob("*.tmp"):
        fichier.unlink(missing_ok=True)


@pytest.fixture
def client():
    with TestClient(app.app) as test_client:
        yield test_client



async def decouverte_publique_vide(_session, **_kwargs):
    """Doublure : aucune source publique (moteurs, archive, miroirs) n'est contactée
    dans les tests unitaires — la chaîne réelle a ses propres tests dédiés."""
    return []


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



# ======================================================================================
# TIKWM : les recherches publiques exigent une identification de navigateur sur Render
# ======================================================================================


def test_entetes_tikwm_identifient_honnetement_un_navigateur():
    assert app.ENTETES_TIKWM["User-Agent"].startswith("Mozilla/5.0")
    assert app.ENTETES_TIKWM["Accept"] == "application/json, text/plain, */*"
    assert app.ENTETES_TIKWM["Accept-Language"].startswith("fr-FR")
    assert app.ENTETES_TIKWM["Referer"] == "https://www.tikwm.com/"


class _ReponseTikwm:
    status = 200

    def __init__(self, donnees):
        self.donnees = donnees

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.donnees


class _SessionTikwm:
    def __init__(self, reponses):
        self.reponses = list(reponses)
        self.appels = []

    def get(self, url, **kwargs):
        self.appels.append({"url": url, **kwargs})
        return _ReponseTikwm(self.reponses.pop(0))


def test_appels_tikwm_transmettent_les_entetes_navigateur():
    session = _SessionTikwm([
        {"code": 0, "data": {"play": "https://cdn.example/video.mp4"}},
        {"code": 0, "data": {"title": "Une vraie légende"}},
        {"code": 0, "data": {"videos": []}},
    ])

    assert asyncio.run(app._resoudre_video_tiktok(
        session, "https://www.tiktok.com/@demo/video/123"
    )) == "https://cdn.example/video.mp4"
    assert asyncio.run(app._extraire_texte_tiktok(
        session, "https://www.tiktok.com/@demo/video/123"
    )) == "Une vraie légende"
    assert asyncio.run(app._donnees_tikwm(session, "/feed/search", {"keywords": "demo"})) == {
        "videos": []
    }
    assert len(session.appels) == 3
    assert all(appel["headers"] == app.ENTETES_TIKWM for appel in session.appels)


def test_aucun_appel_tikwm_ne_peut_oublier_les_entetes_navigateur():
    """Garde-fou source : les trois accès TikWM doivent rester protégés à l'avenir."""
    arbre = ast.parse(Path(app.__file__).read_text(encoding="utf-8"))
    fonctions = {
        noeud.name: noeud for noeud in arbre.body
        if isinstance(noeud, ast.AsyncFunctionDef)
        and noeud.name in {"_resoudre_video_tiktok", "_extraire_texte_tiktok", "_donnees_tikwm"}
    }
    assert set(fonctions) == {"_resoudre_video_tiktok", "_extraire_texte_tiktok", "_donnees_tikwm"}
    for nom, fonction in fonctions.items():
        appels_get = [
            noeud for noeud in ast.walk(fonction)
            if isinstance(noeud, ast.Call)
            and isinstance(noeud.func, ast.Attribute)
            and isinstance(noeud.func.value, ast.Name)
            and noeud.func.value.id == "session"
            and noeud.func.attr == "get"
        ]
        assert len(appels_get) == 1, nom
        entete = next((mot for mot in appels_get[0].keywords if mot.arg == "headers"), None)
        assert isinstance(entete.value, ast.Name) and entete.value.id == "ENTETES_TIKWM", nom


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
            for i in range(12):
                videos.append({
                    "video_id": f"1{i + 12}", "title": f"Recette numéro {i}",
                    "duration": 12 + i % 5, "author": {"unique_id": "chef"},
                })
            videos.append({"video_id": "900", "title": "trop long", "duration": 400, "author": {"unique_id": "chef"}})
            videos.append({"video_id": "901", "title": "trop court", "duration": 2, "author": {"unique_id": "chef"}})
            videos.append({"video_id": "902", "title": "durée inconnue", "duration": None, "author": {"unique_id": "chef"}})
            return {"videos": videos}
        if chemin == "/feed/search":
            # Chaque nom du TOP N ramène ses propres vidéos, jamais les mêmes.
            mot = params["keywords"]
            return {"videos": [
                {"video_id": f"2{abs(hash(mot)) % 900:03d}{i}", "title": f"{mot} {i}",
                 "duration": 14 + i % 3, "author": {"unique_id": f"auteur_{i}"}}
                for i in range(4)
            ]}
        raise AssertionError(f"endpoint TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

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
    # Pipeline TOP N : trois noms par défaut, une recherche TikTok par nom.
    assert job["nombre_noms"] == 3
    assert 1 <= len(job["noms"]) <= 3
    recherches = [params["keywords"] for chemin, params in appels if chemin == "/feed/search"]
    assert recherches == job["noms"]
    assert all(v["video_id"] != "111" for v in trouves)
    assert all(v["url"].startswith("https://www.tiktok.com/@") for v in trouves)
    retenues = [v for v in trouves if v["selected"]]
    assert 4 <= len(retenues) <= 12
    assert job["plafond_reel"] <= 540
    ecartees_temps = [
        v for v in trouves
        if not v["selected"] and "limite de temps Render" in v.get("rejet", "")
    ]
    assert ecartees_temps

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

    # La vidéo de départ devient RÉFÉRENCE DE STYLE (pas de contenu) et le mode
    # « pertinence visuelle stricte » est actif : lien normalisé, plans ≤ 5 s.
    assert montages[0]["lien_reference"] == "https://www.tiktok.com/@chef/video/111"
    assert montages[0]["reference_optionnelle"] is True
    assert montages[0]["config"].exiger_pertinence_visuelle is True
    assert montages[0]["config"].duree_max_plan == app.RST_DUREE_MAX_PLAN == 5.0
    # Aucune voix importée : la vidéo finale reste muette (export sans piste audio).
    assert montages[0]["voix_off"] is None

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


def test_rst_ecarte_les_videos_generiques_pour_un_sujet_telephone(client, monkeypatch):
    """Échec réel du 3 octobre : un plan de skyline de Dubaï (Burj Khalifa) monté sur
    un script « TOP 3 téléphone ». Pour un sujet téléphone, une source voyage/skyline
    sans aucun mot tech ne doit jamais être retenue, même si elle est « jolie »."""
    telephone = [
        {"video_id": "p1", "title": "TOP 3 telephone a acheter #telephone #samsung",
         "duration": 14, "author": {"unique_id": "actu"}},
        {"video_id": "p2", "title": "test smartphone en main", "duration": 15,
         "author": {"unique_id": "actu"}},
        {"video_id": "p3", "title": "comparatif ecran iphone vs galaxy", "duration": 13,
         "author": {"unique_id": "actu"}},
        {"video_id": "p4", "title": "batterie du telephone test", "duration": 16,
         "author": {"unique_id": "actu"}},
    ]
    generiques = [
        {"video_id": "g1", "title": "DUBAI 4K skyline travel vlog", "duration": 14,
         "author": {"unique_id": "actu"}},
        {"video_id": "g2", "title": "voyage dubai burj khalifa sunset", "duration": 15,
         "author": {"unique_id": "actu"}},
    ]

    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {
                "id": "77", "title": "TOP 3 telephones a eviter #telephone #iphone",
                "duration": 21, "author": {"unique_id": "actu"},
            }
        if chemin == "/user/posts":
            return {"videos": [*telephone, *generiques]}
        if chemin == "/feed/search":
            return {"videos": []}
        raise AssertionError(f"endpoint TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "Hook téléphone", "corps": "Corps du script téléphone.",
                "mot_cle_broll": "téléphone en main"}

    monkeypatch.setattr(app, "generer_script", script)

    montages = []

    async def montage_fake(**kwargs):
        montages.append(kwargs)
        return {"url": "/videos/rst_tel.mp4", "path": app.DOSSIER_VIDEOS / "rst_tel.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    response = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@actu/video/77",
    })
    assert response.status_code == 202, response.text
    job = _attendre_job(client, response.json()["job_id"])
    assert job["status"] == "completed", job.get("error")

    # La carte « Vidéos trouvées par RsT » garde l'origine réelle et explique les rejets.
    trouves = job["found_videos"]
    par_id = {v["video_id"]: v for v in trouves}
    assert set(par_id) == {"p1", "p2", "p3", "p4", "g1", "g2"}
    for vid in ("g1", "g2"):
        assert par_id[vid]["selected"] is False
        assert "hors sujet" in par_id[vid]["rejet"]
        assert par_id[vid]["origin"]  # origine réelle conservée
    # Les sources téléphone sont retenues et envoyées au montage, jamais les autres.
    retenues = [v for v in trouves if v["selected"]]
    assert {v["video_id"] for v in retenues} == {"p1", "p2", "p3", "p4"}
    assert montages[0]["liens"] == [v["url"] for v in retenues]
    assert all("tiktok.com/@" in lien for lien in montages[0]["liens"])
    # Référence de style = vidéo de départ normalisée, mode strict actif.
    assert montages[0]["lien_reference"] == "https://www.tiktok.com/@actu/video/77"
    assert montages[0]["config"].exiger_pertinence_visuelle is True


def test_rst_aucune_video_trouvee_echoue_honnetement(client, monkeypatch):
    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {"id": "111", "title": "Sujet très pointu #rare", "duration": 12,
                    "author": {"unique_id": "auteur"}}
        return {"videos": []}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

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


# ======================================================================================
# PIPELINE RsT « TOP N » : 3 ou 5 noms, une recherche par nom, plans de 5 s maximum
# ======================================================================================


@pytest.mark.parametrize("demande,attendu", [(3, 3), (5, 5), (4, 3), (0, 3), (None, 3), ("x", 3)])
def test_borner_nombre_noms_n_accepte_que_trois_ou_cinq(demande, attendu):
    assert app.borner_nombre_noms_rst(demande) == attendu


def test_noms_rst_nettoyes_dedupliques_et_ordonnes():
    noms = app._deduplique_noms(
        ["  #Paris ", "@Paris", "PARIS", "", "  ", "x", "a" * 60, "Tour Eiffel", "Louvre"], 3
    )
    # Casse et préfixes ignorés pour la déduplication, ordre d'importance préservé.
    assert noms == ["Paris", "Tour Eiffel", "Louvre"]


def test_rst_refuse_un_nombre_de_noms_hors_trois_et_cinq(client):
    response = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@chef/video/111", "nombre_noms": 4,
    })
    assert response.status_code == 422


def test_extraire_noms_rst_prefere_l_ia_puis_retombe_sur_la_legende(monkeypatch):
    async def ia_ok(_parts, **_kwargs):
        return '{"noms": ["Kylian Mbappé", "Real Madrid", "Bernabéu"]}'

    monkeypatch.setattr(app, "_appel_gemini_brut", ia_ok)
    noms = asyncio.run(app.extraire_noms_rst("Mbappé au Real #football", 3))
    assert noms == ["Kylian Mbappé", "Real Madrid", "Bernabéu"]

    # Gemini indisponible : on retombe sur les mots réellement présents, sans rien inventer.
    async def ia_ko(_parts, **_kwargs):
        raise app.ErreurApp("Gemini saturé")

    monkeypatch.setattr(app, "_appel_gemini_brut", ia_ko)
    replis = asyncio.run(app.extraire_noms_rst("Recette de pancakes #cuisine #food", 3))
    assert replis
    assert all(isinstance(nom, str) and nom.strip() for nom in replis)
    assert "cuisine" in [nom.lower() for nom in replis]


def test_repartir_par_nom_alterne_les_noms_et_relegue_le_fil_auteur():
    candidats = [
        {"video_id": "a1", "nom": "Paris"}, {"video_id": "a2", "nom": "Paris"},
        {"video_id": "b1", "nom": "Lyon"},
        {"video_id": "z1", "nom": ""},
        {"video_id": "c1", "nom": "Nice"}, {"video_id": "c2", "nom": "Nice"},
    ]
    ordonnes = app._repartir_par_nom(candidats, ["Paris", "Lyon", "Nice"])
    # Un tour complet par rang : chaque nom est servi avant que le premier ne se resserve.
    assert [c["video_id"] for c in ordonnes] == ["a1", "b1", "c1", "a2", "c2", "z1"]


def test_rst_score_texte_priorise_le_sujet_et_penalise_le_generique():
    """Score texte simple : le nom recherché et les mots tech montent, une skyline de
    Dubaï sans mot tech coule — uniquement pour un sujet téléphone/produit."""
    assert app._sujet_telephone_rst("TOP 3 téléphones à éviter #samsung", "") is True
    assert app._sujet_telephone_rst("Recette de pancakes faciles #cuisine", "cuisine maison") is False

    def candidate(titre, origine, nom=""):
        return {"title": titre, "origin": origine, "nom": nom}

    sujet = True
    assert app._score_texte_candidat_rst(
        candidate("Test iPhone 15 en main", "recherche « iPhone 15 »", "iPhone 15"), sujet
    ) == 0.95  # nom dans le titre + mot tech
    assert app._score_texte_candidat_rst(
        candidate("top 3 telephone a eviter", "publications de @actu"), sujet
    ) == 0.55   # mot tech, sans nom
    assert app._score_texte_candidat_rst(
        candidate("un lundi comme les autres", "recherche « iPhone 15 »"), sujet
    ) == 0.30   # neutre : aucune promesse inventée
    # Hors sujet : générique (titre OU origine) sans mot tech ni nom dans le titre.
    assert app._score_texte_candidat_rst(
        candidate("DUBAI 4K skyline travel", "recherche « iPhone 15 »", "iPhone 15"), sujet
    ) == 0.0
    assert app._score_texte_candidat_rst(
        candidate("coucher de soleil", "publications de @travel_life"), sujet
    ) == 0.0
    # Un sujet NON téléphone ne subit pas la pénalité voyage : hors périmètre.
    assert app._score_texte_candidat_rst(
        candidate("DUBAI 4K skyline travel", "recherche « pancakes »", "pancakes"), False
    ) == 0.30

    # Le classement met le téléphone devant la skyline, la sélection l'écarte.
    classes = app._classer_candidats_rst([
        {"video_id": "g", "title": "voyage dubai", "origin": "recherche « iPhone 15 »",
         "nom": "iPhone 15", "duration": 12},
        {"video_id": "p", "title": "test iphone 15", "origin": "recherche « iPhone 15 »",
         "nom": "iPhone 15", "duration": 12},
    ], True)
    assert [c["video_id"] for c in classes] == ["p", "g"]
    retenues, tous = app._selectionner_sources_rst([dict(c) for c in classes], 5, 180.0)
    assert [c["video_id"] for c in retenues] == ["p"]
    assert tous[1]["rejet"].startswith("titre hors sujet")


def test_rst_cinq_noms_lance_cinq_recherches_et_couvre_chaque_nom(client, monkeypatch):
    noms_ia = ["Mbappé", "Real Madrid", "Bernabéu", "Vinicius", "Ancelotti"]

    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {"id": "42", "title": "Mbappé au Real Madrid", "duration": 18,
                    "author": {"unique_id": "foot"}}
        if chemin == "/user/posts":
            return {"videos": []}
        if chemin == "/feed/search":
            mot = params["keywords"]
            rang = noms_ia.index(mot)
            return {"videos": [
                {"video_id": f"{rang}{i}", "title": f"{mot} {i}", "duration": 12,
                 "author": {"unique_id": f"src{rang}{i}"}}
                for i in range(3)
            ]}
        raise AssertionError(chemin)

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "Hook", "corps": "Corps.", "mot_cle_broll": "football"}

    monkeypatch.setattr(app, "generer_script", script)

    async def noms(_legende, _nombre, _broll=""):
        return list(noms_ia)

    monkeypatch.setattr(app, "extraire_noms_rst", noms)

    recus = []

    async def montage_fake(**kwargs):
        recus.append(kwargs)
        return {"url": "/videos/top5.mp4", "path": app.DOSSIER_VIDEOS / "top5.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    reponse = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@foot/video/42", "nombre_noms": 5,
    })
    assert reponse.status_code == 202, reponse.text
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")

    assert job["nombre_noms"] == 5
    assert job["noms"] == noms_ia
    # Le fil de l'auteur ouvre la marche, puis une recherche par nom, dans l'ordre.
    assert job["search_queries"] == ["@foot", *noms_ia]
    assert sorted(job["noms_couverts"]) == sorted(noms_ia)  # aucun nom laissé de côté
    # Le montage RsT impose des plans de 5 s maximum, la vidéo de départ sert de
    # référence de style, et le mode pertinence visuelle stricte est actif.
    assert recus[0]["config"].duree_max_plan == app.RST_DUREE_MAX_PLAN == 5.0
    assert recus[0]["lien_reference"] == "https://www.tiktok.com/@foot/video/42"
    assert recus[0]["config"].exiger_pertinence_visuelle is True
    assert recus[0]["voix_off"] is None  # vidéo finale muette sans voix importée


def test_config_publique_expose_le_top_n(client):
    rst = client.get("/api/config").json()["rst"]
    assert rst["noms_choix"] == [3, 5]
    assert rst["noms_defaut"] == 3
    assert rst["duree_max_plan"] == 5.0


def test_interface_propose_le_choix_du_top_n():
    racine = Path(__file__).parents[1]
    html = (racine / "static" / "index.html").read_text(encoding="utf-8")
    js = (racine / "static" / "main.js").read_text(encoding="utf-8")
    assert 'id="rst-noms-choices"' in html
    assert 'data-noms="3"' in html and 'data-noms="5"' in html
    assert "nombre_noms" in js and "rstNombreNoms" in js


# ======================================================================================
# LIVRAISON SÉPARÉE : vidéo muette + script .txt + voix off .mp3 générée par edge-tts
# ======================================================================================


def test_script_txt_ne_contient_que_des_donnees_reelles():
    texte = app.composer_script_txt(
        {"hook": "Le hook", "corps": "Le corps du script.", "mot_cle_broll": "cuisine"},
        {"url": "https://www.tiktok.com/@chef/video/1", "title": "Pancakes", "author": "chef"},
        ["Pancakes", "Chef"],
    )
    assert "Le hook" in texte and "Le corps du script." in texte
    assert "Pancakes, Chef" in texte
    assert "@chef" in texte and "https://www.tiktok.com/@chef/video/1" in texte
    assert "cuisine" in texte
    # La livraison séparée est rappelée dans le fichier lui-même.
    assert "muette" in texte


def test_texte_a_dire_enchaine_hook_et_corps_et_reste_borne():
    assert app.texte_a_dire({"hook": "A", "corps": "B."}) == "A B."
    long = app.texte_a_dire({"hook": "x" * 10_000, "corps": "y" * 10_000})
    assert len(long) == app.VOIX_OFF_TEXTE_MAX


def test_synthese_vocale_refuse_un_script_vide():
    with pytest.raises(app.ErreurApp, match="rien à lire"):
        asyncio.run(app.synthetiser_voix_off("   ", app.DOSSIER_VIDEOS / "vide.mp3"))


def test_synthese_vocale_injoignable_ne_laisse_aucun_fichier(tmp_path, monkeypatch):
    """edge-tts est bloqué sur certains réseaux : l'échec doit être propre et explicite."""
    class CommunicateKo:
        def __init__(self, *_args, **_kwargs):
            pass

        async def save(self, chemin):
            Path(chemin).write_bytes(b"")          # fichier partiel laissé par le module
            raise OSError("speech.platform.bing.com injoignable")

    monkeypatch.setitem(
        __import__("sys").modules, "edge_tts", type("M", (), {"Communicate": CommunicateKo})
    )
    cible = tmp_path / "voix.mp3"
    with pytest.raises(app.ErreurApp, match="Synthèse vocale indisponible"):
        asyncio.run(app.synthetiser_voix_off("Bonjour", cible))
    assert not cible.exists()


def test_livraison_ecrit_le_script_meme_si_la_voix_echoue(monkeypatch):
    async def synthese_ko(_texte, chemin, budget=app.EDGE_TTS_DELAI):
        raise app.ErreurApp("speech.platform.bing.com injoignable")

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese_ko)
    livraison = asyncio.run(app.livrer_script_et_voix({"hook": "H", "corps": "C."}))

    assert livraison["script_url"].startswith("/videos/") and livraison["script_url"].endswith(".txt")
    fichier = app.DOSSIER_VIDEOS / Path(livraison["script_url"]).name
    assert fichier.is_file() and "H" in fichier.read_text(encoding="utf-8")
    # Aucun MP3 factice : l'échec est annoncé tel quel.
    assert livraison["voix_url"] == ""
    assert "injoignable" in livraison["voix_erreur"]
    fichier.unlink(missing_ok=True)


def test_livraison_complete_expose_script_et_mp3(monkeypatch):
    async def synthese_ok(_texte, chemin, budget=app.EDGE_TTS_DELAI):
        Path(chemin).write_bytes(b"ID3fauxmp3")
        return 10

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese_ok)
    livraison = asyncio.run(app.livrer_script_et_voix({"hook": "H", "corps": "C."}))

    assert livraison["voix_url"].endswith(".mp3")
    assert livraison["voix_nom"] == "voix-off.mp3"
    assert livraison["voix_erreur"] == ""
    assert "edge-tts" in livraison["voix_moteur"]
    # Script et audio sont deux fichiers distincts, livrés côte à côte.
    assert Path(livraison["script_url"]).stem == Path(livraison["voix_url"]).stem
    for url in (livraison["script_url"], livraison["voix_url"]):
        (app.DOSSIER_VIDEOS / Path(url).name).unlink(missing_ok=True)



def test_synthese_vocale_borne_son_delai_au_budget(tmp_path, monkeypatch):
    delais = []

    class DelaiObserve:
        def __init__(self, delai):
            delais.append(delai)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class CommunicateOk:
        def __init__(self, *_args, **_kwargs):
            pass

        async def save(self, chemin):
            Path(chemin).write_bytes(b"ID3fauxmp3")

    monkeypatch.setitem(
        __import__("sys").modules, "edge_tts", type("M", (), {"Communicate": CommunicateOk})
    )
    monkeypatch.setattr(app.asyncio, "timeout", DelaiObserve)
    cible = tmp_path / "voix.mp3"
    assert asyncio.run(app.synthetiser_voix_off("Bonjour", cible, budget=7.5)) == 10
    assert delais == [7.5]


def test_livraison_signale_erreur_ecriture_script_sans_lever(tmp_path, monkeypatch):
    def ecriture_impossible(*_args, **_kwargs):
        raise OSError("disque plein")

    async def synthese_ok(_texte, chemin, budget=app.EDGE_TTS_DELAI):
        Path(chemin).write_bytes(b"ID3fauxmp3")
        return 10

    monkeypatch.setattr(Path, "write_text", ecriture_impossible)
    monkeypatch.setattr(app, "synthetiser_voix_off", synthese_ok)
    livraison = asyncio.run(app.livrer_script_et_voix({"hook": "H", "corps": "C."}))

    assert livraison["script_url"] == ""
    assert "disque plein" in livraison["script_erreur"]
    assert livraison["voix_url"].endswith(".mp3")
    (app.DOSSIER_VIDEOS / Path(livraison["voix_url"]).name).unlink(missing_ok=True)


def test_livraison_saute_la_voix_si_le_budget_est_trop_court(monkeypatch):
    appels = []

    async def synthese_ne_doit_pas_etre_appelee(*_args, **_kwargs):
        appels.append(True)
        raise AssertionError("edge-tts ne doit pas être appelé")

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese_ne_doit_pas_etre_appelee)
    livraison = asyncio.run(app.livrer_script_et_voix(
        {"hook": "H", "corps": "C."}, budget=app.EDGE_TTS_MINIMUM - 0.1
    ))

    assert appels == []
    assert livraison["script_url"].endswith(".txt")
    assert livraison["voix_url"] == ""
    assert "ignorée" in livraison["voix_erreur"]
    (app.DOSSIER_VIDEOS / Path(livraison["script_url"]).name).unlink(missing_ok=True)


def test_rst_conserve_le_rendu_si_la_livraison_leve(client, monkeypatch):
    """Un souci tardif de livraison ne doit jamais faire échouer une vidéo déjà rendue."""
    async def donnees_tikwm(_session, chemin, _params):
        if chemin == "/":
            return {
                "id": "7", "title": "Paris prépare les Jeux", "duration": 15,
                "author": {"unique_id": "sport"},
            }
        if chemin == "/user/posts":
            return {"videos": []}
        return {"videos": [
            {"video_id": "source-1", "title": "source", "duration": 11,
             "author": {"unique_id": "auteur"}},
        ]}

    async def script(_texte):
        return {"hook": "Paris se prépare", "corps": "Le compte à rebours commence.", "mot_cle_broll": "Paris"}

    async def noms(_legende, _nombre, _broll=""):
        return ["Paris"]

    async def montage_fake(**_kwargs):
        return {
            "url": "/videos/muet-deja-rendu.mp4",
            "path": app.DOSSIER_VIDEOS / "muet-deja-rendu.mp4",
            "sources": [], "source_errors": [],
        }

    async def livraison_ko(*_args, **_kwargs):
        raise OSError("stockage complémentaire indisponible")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)
    monkeypatch.setattr(app, "generer_script", script)
    monkeypatch.setattr(app, "extraire_noms_rst", noms)
    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)
    monkeypatch.setattr(app, "livrer_script_et_voix", livraison_ko)

    reponse = client.post("/api/jobs/rst", json={"lien": "https://www.tiktok.com/@sport/video/7"})
    job = _attendre_job(client, reponse.json()["job_id"])

    assert job["status"] == "completed", job.get("error")
    assert job["url"] == "/videos/muet-deja-rendu.mp4"
    assert "stockage complémentaire indisponible" in job["livraison_erreur"]


def test_rst_livre_video_muette_script_et_voix_off(client, monkeypatch):
    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {"id": "7", "title": "Match du PSG", "duration": 15,
                    "author": {"unique_id": "sport"}}
        if chemin == "/user/posts":
            return {"videos": []}
        return {"videos": [
            {"video_id": f"s{i}", "title": "source", "duration": 11,
             "author": {"unique_id": f"a{i}"}} for i in range(4)
        ]}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "Le PSG gagne", "corps": "Un but décisif.", "mot_cle_broll": "football"}

    monkeypatch.setattr(app, "generer_script", script)

    async def noms(_legende, _nombre, _broll=""):
        return ["PSG"]

    monkeypatch.setattr(app, "extraire_noms_rst", noms)

    async def montage_fake(**kwargs):
        # La vidéo produite reste muette : aucune voix off n'est passée au montage.
        assert kwargs["voix_off"] is None
        return {"url": "/videos/muet.mp4", "path": app.DOSSIER_VIDEOS / "muet.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    async def synthese_ok(texte, chemin, budget=app.EDGE_TTS_DELAI):
        assert "Le PSG gagne" in texte
        Path(chemin).write_bytes(b"ID3fauxmp3")
        return 10

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese_ok)

    reponse = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@sport/video/7",
    })
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")

    # Trois fichiers distincts, réellement servis par l'application.
    assert job["url"] == "/videos/muet.mp4"
    assert client.get(job["script_url"]).status_code == 200
    assert "Le PSG gagne" in client.get(job["script_url"]).text
    assert client.get(job["voix_url"]).status_code == 200
    for url in (job["script_url"], job["voix_url"]):
        (app.DOSSIER_VIDEOS / Path(url).name).unlink(missing_ok=True)


def test_purge_supprime_aussi_le_script_et_la_voix_off():
    base = "b" * 32
    fichiers = {}
    for extension in ("mp4", "txt", "mp3"):
        chemin = app.DOSSIER_VIDEOS / f"{base}.{extension}"
        chemin.write_bytes(b"x")
        fichiers[extension] = chemin

    app.JOBS["vieux"] = {
        "status": "completed", "updated_at": time.monotonic() - app.DUREE_VIE_JOB - 10,
        "url": f"/videos/{base}.mp4",
        "script_url": f"/videos/{base}.txt",
        "voix_url": f"/videos/{base}.mp3",
    }
    app._purger_jobs()
    assert "vieux" not in app.JOBS
    assert not any(chemin.exists() for chemin in fichiers.values())


def test_config_et_sante_annoncent_la_voix_off_generee(client):
    config = client.get("/api/config").json()
    assert config["voix_off_generee"]["moteur"] == "edge-tts"
    assert config["voix_off_generee"]["gratuit"] is True
    assert config["voix_off_generee"]["livraison"] == "separee"
    assert "voix_off_generee_disponible" in client.get("/api/sante").json()


def test_interface_annonce_la_livraison_separee():
    racine = Path(__file__).parents[1]
    html = (racine / "static" / "index.html").read_text(encoding="utf-8")
    js = (racine / "static" / "main.js").read_text(encoding="utf-8")
    requirements = (racine / "requirements.txt").read_text(encoding="utf-8")
    assert "edge-tts" in html and "edge-tts" in requirements
    assert "script_url" in js and "voix_url" in js
    assert "livraison_erreur" in js and "script_erreur" in js
    assert "Script .txt" in js and "Voix off .mp3" in js
    assert "Vidéo (muette)" in js


# ======================================================================================
# RECHERCHE VIDE : diagnostic honnête et repli sur les mots-clés réels de la légende
# ======================================================================================


def test_rst_recherche_vide_detaille_les_tentatives(client, monkeypatch):
    """Un échec doit dire ce qui a été cherché et ce que ça a donné, sans rien inventer."""
    async def donnees_tikwm(_session, chemin, _params):
        if chemin == "/":
            return {"id": "111", "title": "Sujet pointu sur Bordeaux", "duration": 12,
                    "author": {"unique_id": "auteur"}}
        return {"videos": []}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "Hook", "corps": "Corps.", "mot_cle_broll": "bordeaux"}

    monkeypatch.setattr(app, "generer_script", script)

    async def noms(_legende, _nombre, _broll=""):
        return ["Bordeaux", "Gironde"]

    monkeypatch.setattr(app, "extraire_noms_rst", noms)

    reponse = client.post("/api/jobs/rst", json={"lien": "https://www.tiktok.com/@auteur/video/111"})
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "failed"
    erreur = job["error"]
    assert "aucune autre vidéo TikTok" in erreur
    assert "recherche(s) tentée(s)" in erreur
    # Les noms réellement cherchés apparaissent dans le diagnostic.
    assert "Bordeaux" in erreur and "aucun résultat" in erreur


def test_rst_sans_aucun_nom_elargit_la_recherche_aux_mots_cles(client, monkeypatch):
    """Légende sans nom propre : le repli mots-clés évite de condamner le travail."""
    recherches = []

    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            return {"id": "5", "title": "Astuce rangement #maison #diy", "duration": 14,
                    "author": {"unique_id": "brico"}}
        if chemin == "/user/posts":
            return {"videos": []}
        recherches.append(params["keywords"])
        return {"videos": [
            {"video_id": f"r{i}", "title": "trouvée", "duration": 10,
             "author": {"unique_id": f"u{i}"}} for i in range(3)
        ]}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "H", "corps": "C.", "mot_cle_broll": "rangement"}

    monkeypatch.setattr(app, "generer_script", script)

    async def aucun_nom(_legende, _nombre, _broll=""):
        return []                                  # l'IA n'a trouvé aucun nom exploitable

    monkeypatch.setattr(app, "extraire_noms_rst", aucun_nom)

    async def montage_fake(**_kwargs):
        return {"url": "/videos/repli.mp4", "path": app.DOSSIER_VIDEOS / "repli.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    async def synthese(_texte, chemin, budget=app.EDGE_TTS_DELAI):
        Path(chemin).write_bytes(b"ID3")
        return 3

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese)

    reponse = client.post("/api/jobs/rst", json={"lien": "https://www.tiktok.com/@brico/video/5"})
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")
    assert job["noms"] == []
    # La recherche élargie s'appuie sur les vrais hashtags / mots de la légende.
    assert recherches
    assert any(mot in {"maison", "diy", "rangement", "astuce"} for mot in recherches)
    assert job["found_videos"]
    for url in (job.get("script_url"), job.get("voix_url")):
        if url:
            (app.DOSSIER_VIDEOS / Path(url).name).unlink(missing_ok=True)


def test_rst_sans_requete_possible_le_dit_clairement(client, monkeypatch):
    """Ni auteur ni nom ni mot-clé : le message doit expliquer pourquoi, pas planter."""
    async def donnees_tikwm(_session, chemin, _params):
        if chemin == "/":
            return {"id": "9", "title": "...", "duration": 10, "author": {"unique_id": ""}}
        return {"videos": []}

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    # Aucune source publique n'est contactée dans les tests unitaires.
    monkeypatch.setattr(app, "_decouvrir_publique", decouverte_publique_vide)

    async def script(_texte):
        return {"hook": "H", "corps": "C.", "mot_cle_broll": ""}

    monkeypatch.setattr(app, "generer_script", script)

    async def aucun_nom(_legende, _nombre, _broll=""):
        return []

    monkeypatch.setattr(app, "extraire_noms_rst", aucun_nom)

    reponse = client.post("/api/jobs/rst", json={"lien": "https://www.tiktok.com/@x/video/9"})
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "failed"
    assert "aucune recherche" in job["error"]
    assert "ni nom ni mot-clé" in job["error"]


def test_urlebird_extrait_vrais_liens_tiktok_sans_inventer_de_donnees():
    """Urlebird : le parseur HTML découvre de vrais liens TikTok par auteur et par mot-clé."""
    html_auteur = """
    <div class="video-listing">
        <div class="thumb">
            <a href="/video/recette-crepes-faciles-7341000000000000001/">
                <img src="thumb1.jpg" />
            </a>
            <div class="info">
                <a href="/user/chef/">@chef</a>
            </div>
        </div>
        <div class="thumb">
            <a href="https://urlebird.com/video/crepes-sucrees-7341000000000000002/"></a>
        </div>
        <div class="thumb">
            <a href="https://www.tiktok.com/@autre_chef/video/7341000000000000003"></a>
        </div>
    </div>
    """
    liens = app._extraire_liens_urlebird(html_auteur, auteur_defaut="chef")
    assert "https://www.tiktok.com/@chef/video/7341000000000000001" in liens
    assert "https://www.tiktok.com/@chef/video/7341000000000000002" in liens
    assert "https://www.tiktok.com/@autre_chef/video/7341000000000000003" in liens
    assert len(liens) == 3


def test_tikwm_403_ne_declenche_pas_de_retry_inutile():
    """Si TikWM répond 403, l'appel échoue immédiatement sans réessais inutiles."""
    appels = []

    class _Session403:
        def get(self, url, **kwargs):
            appels.append(url)
            reponse = _ReponseTikwm({})
            reponse.status = 403
            return reponse

    session = _Session403()
    debut = time.monotonic()
    with pytest.raises(app.ErreurTikwm403):
        asyncio.run(app._donnees_tikwm(session, "/feed/search", {"keywords": "android"}))
    duree = time.monotonic() - debut

    # 403 = IP bloquée : un seul appel, pas de sleep/retry
    assert len(appels) == 1
    assert duree < 0.5


def test_rst_bascule_sur_les_sources_publiques_si_tikwm_recherche_repond_403(client, monkeypatch):
    """En cas de 403 TikWM (blocage IP Render), RsT bascule sur la chaîne publique
    et revalide chaque lien découvert via TikWM /api/ — origines réelles conservées."""
    appels_tikwm = []
    appels_moteurs = []

    async def donnees_tikwm(_session, chemin, params):
        appels_tikwm.append((chemin, dict(params)))
        if chemin == "/":
            url = params.get("url", "")
            if "111" in url:
                # Vidéo de départ
                return {
                    "id": "111", "title": "Comparatif galaxya56 et honormagic7pro #tech",
                    "duration": 20, "author": {"unique_id": "techreview", "nickname": "Tech Review"},
                }
            catalogue = {
                "7341000000000000001": ("techreview", "Unboxing Samsung Galaxy A56", 18),
                "7341000000000000002": ("reviewer2", "Galaxy A56 test complet", 16),
                "7341000000000000003": ("reviewer3", "Honor Magic 7 Pro camera test", 22),
            }
            for vid, (pseudo, titre, duree) in catalogue.items():
                if vid in url:
                    return {"id": vid, "title": titre, "duration": duree,
                            "author": {"unique_id": pseudo, "nickname": pseudo}}
            raise app.ErreurApp(f"Vidéo inconnue : {url}")

        if chemin in {"/user/posts", "/feed/search"}:
            # Simule le 403 réel observé sur Render
            raise app.ErreurTikwm403("TikWM inaccessible (403)")
        raise AssertionError(f"Chemin TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)

    async def decouvrir_moteur(_session, moteur, *, auteur="", requete="", limite=20):
        appels_moteurs.append({"moteur": moteur, "auteur": auteur, "requete": requete})
        if moteur != "duckduckgo":
            return [], "", "aucun résultat"
        if auteur == "techreview":
            return (["https://www.tiktok.com/@techreview/video/7341000000000000001"],
                    "relais jina", "relais de lecture : 1 lien")
        if requete == "Galaxy A56":
            return (["https://www.tiktok.com/@reviewer2/video/7341000000000000002"],
                    "direct", "direct : 1 lien")
        if requete == "Honor Magic 7 Pro":
            return (["https://www.tiktok.com/@reviewer3/video/7341000000000000003"],
                    "relais traduction", "relais de traduction : 1 lien")
        return [], "", "aucun résultat"

    monkeypatch.setattr(app, "_decouvrir_moteur", decouvrir_moteur)

    async def wayback_bloque(_session, *, auteur="", limite=20):
        raise app.ErreurApp("archive web indisponible")

    monkeypatch.setattr(app, "_decouvrir_wayback", wayback_bloque)

    async def urlebird_bloque(_session, *, auteur="", requete="", limite=20):
        raise app.ErreurApp("Urlebird inaccessible (403)")

    monkeypatch.setattr(app, "_source_urlebird", urlebird_bloque)

    async def script(_texte):
        return {"hook": "Le duel des smartphones 2026", "corps": "Galaxy A56 face au Magic 7 Pro.", "mot_cle_broll": "smartphone"}

    monkeypatch.setattr(app, "generer_script", script)

    async def noms(_legende, _nombre, _broll=""):
        return ["Galaxy A56", "Honor Magic 7 Pro"]

    monkeypatch.setattr(app, "extraire_noms_rst", noms)

    async def montage_fake(**kwargs):
        return {
            "url": "/videos/rst-publique.mp4",
            "path": app.DOSSIER_VIDEOS / "rst-publique.mp4",
            "sources": [], "source_errors": [],
        }

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    async def synthese(texte, chemin, budget=app.EDGE_TTS_DELAI):
        Path(chemin).write_bytes(b"ID3mp3")
        return 5

    monkeypatch.setattr(app, "synthetiser_voix_off", synthese)

    reponse = client.post("/api/jobs/rst", json={
        "lien": "https://www.tiktok.com/@techreview/video/111",
        "nombre_noms": 3,
    })
    assert reponse.status_code == 202, reponse.text
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")
    assert job["url"] == "/videos/rst-publique.mp4"

    # La chaîne publique a bien été utilisée : DuckDuckGo a répondu pour l'auteur
    # et pour chaque nom du TOP N.
    assert any(a.get("auteur") == "techreview" for a in appels_moteurs)
    assert any(a.get("requete") == "Galaxy A56" for a in appels_moteurs)
    assert any(a.get("requete") == "Honor Magic 7 Pro" for a in appels_moteurs)

    # Chaque candidate porte son origine réelle (source + mode + recherche).
    trouvees = job["found_videos"]
    assert len(trouvees) == 3
    assert {v["origin"] for v in trouvees} == {
        "DuckDuckGo via relais : publications de @techreview",
        "DuckDuckGo : recherche « Galaxy A56 »",
        "DuckDuckGo via relais de traduction : recherche « Honor Magic 7 Pro »",
    }
    # Les métadonnées viennent de TikWM /api/, jamais de la source de découverte.
    assert {v["duration"] for v in trouvees} == {18.0, 16.0, 22.0}

    # Chaque lien découvert a été revalidé par TikWM /api/ (la vidéo de départ incluse).
    validations = [params for chemin, params in appels_tikwm if chemin == "/"]
    assert len(validations) == 4

    # Après le premier 403, les endpoints de recherche TikWM ne sont plus réessayés.
    appels_search = [c for c, _ in appels_tikwm if c in {"/user/posts", "/feed/search"}]
    assert len(appels_search) == 1

    for url in (job.get("script_url"), job.get("voix_url")):
        if url:
            (app.DOSSIER_VIDEOS / Path(url).name).unlink(missing_ok=True)


# ======================================================================================
# DÉCOUVERTE PUBLIQUE MULTI-SOURCES : parseurs, relais, archive et chaîne complète
# ======================================================================================


class _PageWeb:
    def __init__(self, statut=200, texte=""):
        self.status = statut
        self._texte = texte

    async def text(self, **_kwargs):
        return self._texte

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _SessionWeb:
    """Session aiohttp factuelle : sert une page selon un motif d'URL, sans réseau."""

    def __init__(self, pages):
        self.pages = list(pages)  # liste de (motif d'URL, _PageWeb)
        self.appels = []

    def get(self, url, **_kwargs):
        self.appels.append(url)
        for motif, page in self.pages:
            if motif in url:
                return page
        return _PageWeb(404, "")


def test_page_bloquee_reconnait_les_defis_anti_bot():
    assert app._page_bloquee("<html><title>Just a moment...</title></html>")
    assert app._page_bloquee("Please complete the CAPTCHA to continue")
    assert not app._page_bloquee("<html>10 résultats de recherche honnêtes</html>")


def test_extraire_liens_tiktok_texte_decode_moteurs_sans_inventer():
    # 1) Lien direct dans une page de résultats.
    texte = '<a href="https://www.tiktok.com/@chef/video/7341000000000000001">vidéo</a>'
    assert app._extraire_liens_tiktok_texte(texte) == [
        "https://www.tiktok.com/@chef/video/7341000000000000001"
    ]

    # 2) DuckDuckGo emballe la destination en percent-encoding (uddg=…).
    ddg = (
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tiktok.com%2F%40chef%2Fvideo%2F"
        "7341000000000000002&rut=abc123"
    )
    assert app._extraire_liens_tiktok_texte(ddg) == [
        "https://www.tiktok.com/@chef/video/7341000000000000002"
    ]

    # 3) Bing emballe la destination en base64 (u=a1…).
    cible = "https://www.tiktok.com/@review/video/7341000000000000003"
    jeton = "a1" + base64.urlsafe_b64encode(cible.encode()).decode().rstrip("=")
    bing = f"https://www.bing.com/ck/a?!&&p=x&u={jeton}&ntb=1"
    assert app._extraire_liens_tiktok_texte(bing) == [
        "https://www.tiktok.com/@review/video/7341000000000000003"
    ]

    # 4) Filtre par auteur attendu : seules les vidéos de cet auteur sont retenues.
    melange = (
        "https://www.tiktok.com/@autre/video/7341000000000000004 "
        "https://www.tiktok.com/@chef/video/7341000000000000005"
    )
    assert app._extraire_liens_tiktok_texte(melange, auteur_attendu="chef") == [
        "https://www.tiktok.com/@chef/video/7341000000000000005"
    ]

    # 5) Aucun identifiant plausible : aucun lien, rien n'est fabriqué.
    assert app._extraire_liens_tiktok_texte("tiktok.com/@x/video/123456 tiktok.com/@x/video/2.0.0.21") == []
    assert app._extraire_liens_tiktok_texte("") == []


def test_decouvrir_moteur_bascule_sur_le_relais_quand_l_ip_est_bloquee():
    """IP datacenter bloquée (403) : la même recherche passe par le relais public."""
    relais = _PageWeb(200, (
        "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tiktok.com%2F%40parisjet%2Fvideo%2F"
        "7680000000000000001&rut=xyz"
    ))
    session = _SessionWeb([
        ("r.jina.ai/", relais),
        ("lite.duckduckgo.com", _PageWeb(403, "")),
    ])
    liens, mode, detail = asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="paris", limite=5))
    assert mode == "relais jina"
    assert liens == ["https://www.tiktok.com/@parisjet/video/7680000000000000001"]
    assert "relais de lecture" in detail
    assert len(session.appels) == 2  # direct bloqué puis relais


def test_decouvrir_moteur_relais_apres_page_anti_bot():
    """Une page 200 qui n'est qu'un défi anti-bot ne vaut pas un résultat : relais."""
    antibot = _PageWeb(200, "<title>Just a moment...</title>challenge")
    relais = _PageWeb(200, "tiktok.com/@chef/video/7341000000000000006")
    session = _SessionWeb([
        ("r.jina.ai/", relais),
        ("lite.duckduckgo.com", antibot),
    ])
    liens, mode, _detail = asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="chef", limite=5))
    assert (liens, mode) == (["https://www.tiktok.com/@chef/video/7341000000000000006"], "relais jina")


def test_decouvrir_moteur_sans_resultat_n_appelle_pas_le_relais():
    """Le moteur a répondu « aucun résultat » : inutile de consommer le relais."""
    session = _SessionWeb([("lite.duckduckgo.com", _PageWeb(200, "<html>No results found.</html>"))])
    liens, mode, detail = asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="xyzrarissime", limite=5))
    assert (liens, mode) == ([], "")
    assert "aucun résultat" in detail
    assert len(session.appels) == 1


def test_requete_moteur_adapte_le_format_au_moteur():
    """DuckDuckGo et SearXNG ciblent TikTok par mots-clés : « site: » rend la page
    vide sur DDG servi via le relais de traduction, et leurs moteurs agrégés le
    perdent. Ecosia et Bing gardent « site: »."""
    assert app._requete_moteur("chef", "", "duckduckgo") == "tiktok.com @chef video"
    assert app._requete_moteur("chef", "", "searxng") == "tiktok.com @chef video"
    assert app._requete_moteur("chef", "", "ecosia") == "site:tiktok.com/@chef video"
    assert app._requete_moteur("chef", "", "bing") == "site:tiktok.com/@chef video"
    assert app._requete_moteur("", "Galaxy A56", "bing") == "site:tiktok.com Galaxy A56 video"
    assert app._requete_moteur("", "Galaxy A56", "searxng") == "tiktok.com Galaxy A56 video"
    assert app._requete_moteur("", "Galaxy A56", "duckduckgo") == "tiktok.com Galaxy A56 video"
    assert app._requete_moteur("", "  #paris  ", "ecosia") == "site:tiktok.com paris video"
    assert app._requete_moteur("", "", "searxng") == ""


def test_apercu_resultats_nomme_les_destinations_de_la_page():
    page = (
        '<a href="https://wikipedia.org/x">1</a> <a href="https://paris.fr/y">2</a> '
        '<a href="https://wikipedia.org/z">3</a> <a href="https://exemple.org/w">4</a>'
    )
    assert app._apercu_resultats(page) == "wikipedia.org, paris.fr, exemple.org"
    assert app._apercu_resultats("") == ""


def test_url_translate_construit_le_relais_google():
    url = app._url_translate("https://lite.duckduckgo.com/lite/?q=site%3Atiktok.com+paris+video")
    assert url == (
        "https://lite-duckduckgo-com.translate.goog/lite/"
        "?q=site%3Atiktok.com+paris+video&_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"
    )
    sans_requete = app._url_translate("https://www.exemple.fr/page/")
    assert sans_requete == "https://www-exemple-fr.translate.goog/page/?_x_tr_sl=auto&_x_tr_tl=en&_x_tr_hl=en"


def test_decouvrir_moteur_bascule_sur_le_relais_de_traduction():
    """Blocage doux : direct renvoie une page vide sans explication, le relais de
    lecture reste vide aussi — le relais de traduction Google finit par passer."""
    vide_suspect = _PageWeb(200, "<html><body>DuckDuckGo</body></html>")  # ni lien ni « no results »
    jina_vide = _PageWeb(200, "Title: x\n\nMarkdown Content:\nrien ici")
    traduction = _PageWeb(
        200,
        "<html>résultats traduits " * 8 + " tiktok.com/@chef/video/7341000000000000007",
    )
    session = _SessionWeb([
        ("translate.goog", traduction),
        ("r.jina.ai/", jina_vide),
        ("lite.duckduckgo.com", vide_suspect),
    ])
    liens, mode, detail = asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="chef", limite=5))
    assert mode == "relais traduction"
    assert liens == ["https://www.tiktok.com/@chef/video/7341000000000000007"]
    assert "direct vide" in detail and "relais de lecture vide" in detail
    assert len(session.appels) == 3  # direct, relais de lecture, relais de traduction


def test_decouvrir_moteur_searxng_tente_chaque_instance_puis_normalise_le_miroir():
    """SearXNG public : la première instance est bloquée, la deuxième répond ; le
    miroir sticktock.com (mêmes auteurs et identifiants que TikTok) est normalisé
    en vrai lien tiktok.com — revalidé ensuite par TikWM /api/."""
    page_opnxng = _PageWeb(403, "")
    page_inetol = _PageWeb(200, (
        '<a href="https://sticktock.com/@chef/video/7505962928663809310">vidéo</a> '
        '<a href="https://sticktock.com/@chef/video/7673952670075358495">vidéo</a>'
    ))
    session = _SessionWeb([
        ("search.inetol.net", page_inetol),
        ("opnxng.com", page_opnxng),
    ])
    liens, mode, detail = asyncio.run(app._decouvrir_moteur(session, "searxng", requete="chef", limite=5))
    assert mode == "direct"
    assert liens == [
        "https://www.tiktok.com/@chef/video/7505962928663809310",
        "https://www.tiktok.com/@chef/video/7673952670075358495",
    ]
    assert "direct" in detail
    # Deux appels directs seulement : les relais ne sont pas consommés.
    assert len(session.appels) == 2
    assert "opnxng.com" in session.appels[0] and "inetol.net" in session.appels[1]


def test_decouvrir_moteur_injoignable_partout_est_declare_bloque():
    """Direct 403, relais de lecture 403, relais de traduction 403 : aucune page
    obtenue — le moteur est injoignable, pas vide. L'ErreurApp déclenche le
    disjoncteur de la chaîne (la source ne sera plus tentée pendant le travail)."""
    session = _SessionWeb([_ for _ in []] or [
        ("lite.duckduckgo.com", _PageWeb(403, "")),
        ("r.jina.ai/", _PageWeb(403, "")),
        ("translate.goog", _PageWeb(403, "")),
    ])
    with pytest.raises(app.ErreurApp, match="injoignable par tous les chemins"):
        asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="chef", limite=5))


class _SessionSequentielle:
    """Session factuelle qui sert des pages différentes à chaque appel successif."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.appels = []

    def get(self, url, **_kwargs):
        page = self.pages[min(len(self.appels), len(self.pages) - 1)]
        self.appels.append(url)
        return page


def test_relais_de_traduction_reessaie_apres_un_202():
    """Google répond « 202 Accepted » pendant qu'il prépare la page : on réessaie
    une fois, et la deuxième réponse est servie telle quelle."""
    premiere = _PageWeb(202, "<html>" + "translation in progress " * 6 + "</html>")
    deuxieme = _PageWeb(200, "<html>" + "vraie page de résultats de recherche " * 6 + "</html>")
    session = _SessionSequentielle([premiere, deuxieme])
    texte = asyncio.run(app._lire_page_translate(session, "https://lite.duckduckgo.com/lite/?q=test"))
    assert texte == deuxieme._texte
    assert len(session.appels) == 2


def test_relais_de_traduction_accepte_un_202_deja_servi():
    """Un 202 dont le corps contient déjà la page complète est accepté tel quel."""
    page = _PageWeb(202, "x" * 2000 + " https://www.tiktok.com/@chef/video/7505962928663809310")
    session = _SessionSequentielle([page])
    texte = asyncio.run(app._lire_page_translate(session, "https://lite.duckduckgo.com/lite/?q=test"))
    assert "tiktok.com/@chef" in texte
    assert len(session.appels) == 1


def test_relais_de_traduction_abandonne_apres_deux_202_vides():
    session = _SessionSequentielle([_PageWeb(202, "<html>wait</html>")])
    with pytest.raises(app.ErreurApp, match="relais de traduction inaccessible"):
        asyncio.run(app._lire_page_translate(session, "https://lite.duckduckgo.com/lite/?q=test"))
    assert len(session.appels) == 2


def test_extraire_liens_reconnait_le_miroir_sticktock():
    """Le miroir sticktock.com partage les identifiants vidéo de TikTok : les liens
    y sont lus puis normalisés — jamais pris pour un lien sticktock à livrer."""
    texte = "https://sticktock.com/@parishilton/video/7505962928663809310 fin"
    assert app._extraire_liens_tiktok_texte(texte) == [
        "https://www.tiktok.com/@parishilton/video/7505962928663809310"
    ]


def test_decouvrir_moteur_serp_vide_via_relais_arrete_la():
    """Le relais de lecture obtient une vraie page « aucun résultat » : le moteur a
    répondu, inutile de consommer le second relais pour la même recherche."""
    vide_suspect = _PageWeb(200, "<html>en-tête seul</html>")
    jina_vide_honnete = _PageWeb(200, "No results found for your search")
    session = _SessionWeb([
        ("r.jina.ai/", jina_vide_honnete),
        ("lite.duckduckgo.com", vide_suspect),
    ])
    liens, mode, _detail = asyncio.run(app._decouvrir_moteur(session, "duckduckgo", requete="zzz", limite=5))
    assert (liens, mode) == ([], "")
    assert len(session.appels) == 2  # direct puis relais de lecture, pas de traduction


def test_decouvrir_wayback_ne_garde_que_les_vraies_videos_les_plus_recentes():
    lignes = [
        ["original", "timestamp"],
        ["https://www.tiktok.com/@chef/video/2.0.0.21", "20250219135252"],           # bruit
        ["https://www.tiktok.com/@chef/video/7341000000000000001", "20240101000000"],
        ["https://www.tiktok.com/@chef/video/7341000000000000002?is_copy_url=1", "20260601120000"],
        ["https://www.tiktok.com/@chef/video/7341000000000000001", "20250303000000"],  # doublon
        ["https://www.tiktok.com/@chef/video/7341000000000000003", "20260202000000"],
    ]
    session = _SessionWeb([("web.archive.org", _PageWeb(200, json.dumps(lignes)))])
    liens, mode, _detail = asyncio.run(app._decouvrir_wayback(session, auteur="chef", limite=2))
    assert mode == "archive"
    # Snapshots les plus récents d'abord, doublons fusionnés, bruit écarté.
    assert liens == [
        "https://www.tiktok.com/@chef/video/7341000000000000002",
        "https://www.tiktok.com/@chef/video/7341000000000000003",
    ]


def test_chaine_publique_bloque_les_sources_en_echec_et_reessaie_la_gagnante(monkeypatch):
    """Une source bloquée n'est plus tentée ; la source gagnante passe en premier."""
    ordre = []

    async def moteur(_session, nom, *, auteur="", requete="", limite=20):
        ordre.append(nom)
        if nom == "duckduckgo":
            raise app.ErreurApp("403")
        if nom == "ecosia":
            return (["https://www.tiktok.com/@ecoloi/video/7341000000000000001"],
                    "direct", "direct : 1 lien")
        return [], "", "aucun résultat"

    async def wayback(_session, *, auteur="", limite=20):
        ordre.append("wayback")
        raise app.ErreurApp("archive indisponible")

    async def urlebird(_session, *, auteur="", requete="", limite=20):
        ordre.append("urlebird")
        raise app.ErreurApp("Urlebird inaccessible (403)")

    monkeypatch.setattr(app, "_decouvrir_moteur", moteur)
    monkeypatch.setattr(app, "_decouvrir_wayback", wayback)
    monkeypatch.setattr(app, "_source_urlebird", urlebird)

    etat = app.EtatSourcesDecouverte()
    liens = asyncio.run(app._decouvrir_publique(None, requete="paris", limite=5, etat=etat))
    assert ordre == ["duckduckgo", "searxng", "ecosia", "bing", "urlebird"]
    assert etat.bloquees == {"duckduckgo", "urlebird"}
    assert etat.gagnante == "ecosia"
    assert [l["origin"] for l in liens] == ["Ecosia : recherche « paris »"]

    ordre.clear()
    liens2 = asyncio.run(app._decouvrir_publique(None, requete="lyon", limite=5, etat=etat))
    # La gagnante passe en tête, les bloquées (duckduckgo, urlebird) ne le sont plus.
    assert ordre == ["ecosia", "searxng", "bing"]
    assert [l["origin"] for l in liens2] == ["Ecosia : recherche « lyon »"]


def test_chaine_publique_respecte_son_budget_de_temps():
    """Budget épuisé : plus aucune source n'est contactée, la chaîne rend la main."""

    async def moteur(_session, _nom, **_kwargs):
        raise AssertionError("aucune source ne doit être contactée")

    app_moteur, app_wayback, app_urlebird = app._decouvrir_moteur, app._decouvrir_wayback, app._source_urlebird
    app._decouvrir_moteur = moteur
    try:
        etat = app.EtatSourcesDecouverte(budget=5.0)
        liens = asyncio.run(app._decouvrir_publique(None, requete="paris", etat=etat))
        assert liens == []
    finally:
        app._decouvrir_moteur, app._decouvrir_wayback, app._source_urlebird = app_moteur, app_wayback, app_urlebird


def test_diagnostic_sources_rst_rapporte_l_etat_reel_depuis_le_serveur(client, monkeypatch):
    """Le diagnostic /api/rst/sources dit, source par source, ce qui passe depuis
    l'IP du serveur — sans jamais inventer de résultat."""
    async def moteur(_session, nom, *, auteur="", requete="", limite=20):
        if nom == "duckduckgo":
            return (["https://www.tiktok.com/@tiktok/video/7341000000000000001"],
                    "relais traduction", "relais de traduction : 1 lien")
        return [], "", "direct : aucun résultat affiché par le moteur"

    async def wayback(_session, *, auteur="", limite=20):
        return [], "archive", "0 vidéo archivée"

    async def urlebird(_session, *, auteur="", requete="", limite=20):
        raise app.ErreurApp("Urlebird inaccessible (403)")

    async def donnees_tikwm(_session, chemin, _params):
        if chemin == "/":
            return {"id": "7106594312292453675", "title": "vraie vidéo", "duration": 24,
                    "author": {"unique_id": "tiktok"}}
        if chemin == "/user/posts":
            raise app.ErreurTikwm403("TikWM inaccessible (403)")
        return {"videos": []}

    monkeypatch.setattr(app, "_decouvrir_moteur", moteur)
    monkeypatch.setattr(app, "_decouvrir_wayback", wayback)
    monkeypatch.setattr(app, "_source_urlebird", urlebird)
    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)

    reponse = client.get("/api/rst/sources", params={"auteur": "tiktok"})
    assert reponse.status_code == 200
    rapport = reponse.json()
    assert rapport["relais"].startswith("https://")

    par_source = {s["source"]: s for s in rapport["sources"]}
    assert par_source["duckduckgo"] == {
        "source": "duckduckgo", "statut": "ok", "mode": "relais traduction", "liens": 1,
        "exemple": "https://www.tiktok.com/@tiktok/video/7341000000000000001",
        "detail": "relais de traduction : 1 lien", "duree_ms": par_source["duckduckgo"]["duree_ms"],
    }
    assert "aucun résultat" in par_source["ecosia"]["detail"]
    assert par_source["ecosia"]["statut"] == "vide"
    assert par_source["searxng"]["statut"] == "vide"
    assert par_source["bing"]["statut"] == "vide"
    assert par_source["wayback"]["statut"] == "vide"
    assert par_source["urlebird"]["statut"] == "bloque"
    assert "Urlebird" in par_source["urlebird"]["detail"]

    tikwm = {t["endpoint"]: t for t in rapport["tikwm"]}
    assert tikwm["/"]["statut"] == "ok" and tikwm["/"]["videos"] == 1
    assert tikwm["/user/posts"]["statut"] == "bloque (403)"
    assert tikwm["/feed/search"]["statut"] == "ok" and tikwm["/feed/search"]["videos"] == 0


# ======================================================================================
# SsT : TOP 3 / TOP 5 manuel — recherche indépendante par nom, secours public
# ======================================================================================


def test_sst_exige_exactement_3_ou_5_noms_differents():
    """TOP 3 = exactement 3 noms différents ; TOP 5 = exactement 5 — ni vide ni doublon."""
    valide = ["Alpha One", "Beta S", "Gamma X"]
    assert app._noms_sst_valides(valide, 3) == valide
    cinq = ["Alpha One", "Beta S", "Gamma X", "Delta R", "Epsilon T"]
    assert app._noms_sst_valides(cinq, 5) == cinq

    with pytest.raises(app.ErreurApp, match="uniquement TOP 3 ou TOP 5"):
        app._noms_sst_valides(valide, 4)
    with pytest.raises(app.ErreurApp, match="exactement 3 noms différents"):
        app._noms_sst_valides(["Alpha One", "Beta S"], 3)
    with pytest.raises(app.ErreurApp, match="exactement 5 noms différents"):
        app._noms_sst_valides(["Alpha One", "Beta S", "Gamma X", "Delta R"], 5)
    with pytest.raises(app.ErreurApp, match="exactement 3 noms différents"):
        app._noms_sst_valides(["Alpha One", "", "Gamma X"], 3)
    with pytest.raises(app.ErreurApp, match="tous différents"):
        app._noms_sst_valides(["Alpha One", "alpha one", "Gamma X"], 3)
    with pytest.raises(app.ErreurApp, match="tous différents"):
        app._noms_sst_valides(["Alpha One", "Beta S", "beta S"], 3)


def test_sst_endpoint_refuse_noms_vides_ou_dupliques(client):
    """La validation Pydantic rejette un nom vide ou dupliqué avant tout traitement."""
    cas = [
        {"nombre_noms": 3, "noms": ["Alpha One", "", "Gamma X"]},
        {"nombre_noms": 3, "noms": ["Alpha One", "alpha one", "Gamma X"]},
        {"nombre_noms": 3, "noms": ["Alpha One", "Beta S"]},
        {"nombre_noms": 5, "noms": ["Alpha One", "Beta S", "Gamma X", "Delta R"]},
        {"nombre_noms": 4, "noms": ["Alpha One", "Beta S", "Gamma X", "Delta R"]},
    ]
    for corps in cas:
        reponse = client.post("/api/jobs/sst", json={
            "lien": "https://www.tiktok.com/@chaine/video/9000000000000000001", **corps,
        })
        assert reponse.status_code == 422, (corps, reponse.text)
    assert not app.JOBS  # aucun travail créé : la validation a tout bloqué


def _brancher_tikwm_sst(monkeypatch, *, source_id, catalogue, bloquer_recherche=False):
    """Doublure TikWM : la vidéo source passe par /api/, les vidéos du catalogue aussi,
    et /feed/search peut être bloqué (403) comme constaté depuis certaines IP serveur."""
    appels: list[tuple[str, dict]] = []

    async def donnees_tikwm(_session, chemin, params):
        appels.append((chemin, dict(params)))
        if chemin == "/":
            url = str(params.get("url", ""))
            if source_id in url:
                return {
                    "id": source_id, "title": "Classement de véhicules électriques 2026 #auto",
                    "duration": 21, "author": {"unique_id": "chaineauto", "nickname": "Chaîne Auto"},
                }
            for vid, (pseudo, titre, duree) in catalogue.items():
                if vid in url:
                    return {"id": vid, "title": titre, "duration": duree,
                            "author": {"unique_id": pseudo, "nickname": pseudo}}
            raise app.ErreurApp(f"Vidéo inconnue : {url}")
        if chemin == "/feed/search":
            if bloquer_recherche:
                raise app.ErreurTikwm403("TikWM inaccessible (403)")
            return {"videos": []}
        raise AssertionError(f"Chemin TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)
    return appels


def test_sst_bascule_sur_la_decouverte_publique_si_tikwm_recherche_repond_403(client, monkeypatch):
    """/feed/search répond 403 (blocage IP type Render) : SsT utilise la chaîne publique
    pour chaque nom séparément, revalide chaque lien via TikWM /api/, et ne retient une
    source qu'après la validation visuelle du montage."""
    catalogue = {
        "9000000000000000011": ("critique1", "Essai Alpha One : le bilan complet", 18),
        "9000000000000000012": ("critique2", "Beta S en test : nos mesures", 16),
        "9000000000000000013": ("critique3", "Gamma X premier contact", 22),
    }
    appels_tikwm = _brancher_tikwm_sst(
        monkeypatch, source_id="9000000000000000001",
        catalogue=catalogue, bloquer_recherche=True,
    )
    requetes_publiques: list[dict] = []

    async def decouvrir_publique(_session, *, auteur="", requete="", limite=20, etat=None):
        requetes_publiques.append({"auteur": auteur, "requete": requete, "limite": limite})
        par_nom = {
            "Alpha One": [{"url": "https://www.tiktok.com/@critique1/video/9000000000000000011",
                           "origin": "DuckDuckGo : recherche « Alpha One »"}],
            "Beta S": [{"url": "https://www.tiktok.com/@critique2/video/9000000000000000012",
                        "origin": "DuckDuckGo : recherche « Beta S »"}],
            "Gamma X": [{"url": "https://www.tiktok.com/@critique3/video/9000000000000000013",
                         "origin": "SearXNG via relais : recherche « Gamma X »"}],
        }
        return par_nom.get(requete, [])

    monkeypatch.setattr(app, "_decouvrir_publique", decouvrir_publique)

    montage_kwargs: dict = {}

    async def montage_fake(**kwargs):
        montage_kwargs.update(kwargs)
        return {"url": "/videos/sst-publique.mp4", "path": app.DOSSIER_VIDEOS / "sst-publique.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    noms = ["Alpha One", "Beta S", "Gamma X"]
    reponse = client.post("/api/jobs/sst", json={
        "lien": "https://www.tiktok.com/@chaineauto/video/9000000000000000001",
        "nombre_noms": 3, "noms": noms,
    })
    assert reponse.status_code == 202, reponse.text
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")

    # La chaîne publique a été interrogée séparément pour chaque nom saisi, sans
    # jamais remplacer un nom par un sujet général.
    assert [r["requete"] for r in requetes_publiques] == noms
    assert all(not r["auteur"] for r in requetes_publiques)

    # Chaque lien public a été revalidé par TikWM /api/ (source incluse).
    validations = [params for chemin, params in appels_tikwm if chemin == "/"]
    assert len(validations) == 1 + len(noms)
    assert {v["url"] for v in validations} == {
        "https://www.tiktok.com/@chaineauto/video/9000000000000000001",
        "https://www.tiktok.com/@critique1/video/9000000000000000011",
        "https://www.tiktok.com/@critique2/video/9000000000000000012",
        "https://www.tiktok.com/@critique3/video/9000000000000000013",
    }
    # Après le premier 403, /feed/search n'est plus réessayé pour les noms suivants.
    assert len([c for c, _ in appels_tikwm if c == "/feed/search"]) == 1

    # Les noms saisis sont utilisés tels quels, un par un.
    assert job["noms_saisis"] == noms
    assert job["search_queries"] == noms

    trouvees = job["found_videos"]
    assert len(trouvees) == 3
    assert {v["video_id"] for v in trouvees} == set(catalogue)
    assert {v["nom"] for v in trouvees} == set(noms)
    # Métadonnées réelles issues de TikWM /api/, origines publiques conservées.
    assert {v["origin"] for v in trouvees} == {
        "DuckDuckGo : recherche « Alpha One »",
        "DuckDuckGo : recherche « Beta S »",
        "SearXNG via relais : recherche « Gamma X »",
    }
    assert {v["duration"] for v in trouvees} == {18.0, 16.0, 22.0}
    # La vidéo source n'est jamais une candidate : elle n'est que référence.
    assert all(v["video_id"] != "9000000000000000001" for v in trouvees)
    # Une source n'est « retenue » (selected) qu'après la validation visuelle du montage.
    assert [v["selected"] for v in trouvees] == [True, True, True]
    assert all(v["validation_status"] == "validated" for v in trouvees)
    assert job["source_video_used_only_as_reference"] is True
    # La vidéo source n'est que référence de montage, jamais source de contenu.
    assert job["source_video_reference"] == "https://www.tiktok.com/@chaineauto/video/9000000000000000001"
    assert montage_kwargs["lien_reference"] == "https://www.tiktok.com/@chaineauto/video/9000000000000000001"
    assert montage_kwargs["reference_optionnelle"] is False


def test_sst_chaque_nom_est_recherche_independamment_et_les_doublons_sont_supprimes(client, monkeypatch):
    """Voie nominale : /feed/search répond. Chaque nom a sa propre recherche, et une
    vidéo renvoyée pour deux noms n'apparaît qu'une seule fois (doublon par identifiant)."""
    videos_alpha = [
        {"id": "9000000000000000021", "title": "Alpha One : essai complet", "duration": 19,
         "author": {"unique_id": "critiqueA", "nickname": "Critique A"}},
        # Même identifiant vidéo renvoyé pour « Beta S » : doublon TikTok à écarter.
        {"id": "9000000000000000022", "title": "Alpha One vs Beta S", "duration": 17,
         "author": {"unique_id": "critiqueB", "nickname": "Critique B"}},
    ]
    videos_beta = [
        {"id": "9000000000000000022", "title": "Alpha One vs Beta S", "duration": 17,
         "author": {"unique_id": "critiqueB", "nickname": "Critique B"}},
        {"id": "9000000000000000023", "title": "Beta S : nos mesures", "duration": 15,
         "author": {"unique_id": "critiqueC", "nickname": "Critique C"}},
    ]
    videos_gamma = [
        {"id": "9000000000000000024", "title": "Gamma X premier contact", "duration": 13,
         "author": {"unique_id": "critiqueD", "nickname": "Critique D"}},
    ]
    requetes: list[str] = []

    async def donnees_tikwm(_session, chemin, params):
        if chemin == "/":
            if "9000000000000000001" in str(params.get("url", "")):
                return {"id": "9000000000000000001", "title": "Classement 2026 #auto",
                        "duration": 20, "author": {"unique_id": "chaineauto"}}
            raise app.ErreurApp(f"Vidéo inconnue : {params.get('url')}")
        if chemin == "/feed/search":
            requetes.append(str(params.get("keywords")))
            return {"videos": {
                "Alpha One": videos_alpha, "Beta S": videos_beta, "Gamma X": videos_gamma,
            }.get(str(params.get("keywords")), [])}
        raise AssertionError(f"Chemin TikWM inattendu : {chemin}")

    monkeypatch.setattr(app, "_donnees_tikwm", donnees_tikwm)

    async def decouvrir_interdite(**_kwargs):
        raise AssertionError("La chaîne publique ne doit pas être utilisée quand /feed/search répond.")

    monkeypatch.setattr(app, "_decouvrir_publique", decouvrir_interdite)

    montage_kwargs: dict = {}

    async def montage_fake(**kwargs):
        montage_kwargs.update(kwargs)
        return {"url": "/videos/sst-direct.mp4", "path": app.DOSSIER_VIDEOS / "sst-direct.mp4",
                "sources": [], "source_errors": []}

    monkeypatch.setattr(app, "construire_montage_professionnel", montage_fake)

    noms = ["Alpha One", "Beta S", "Gamma X"]
    reponse = client.post("/api/jobs/sst", json={
        "lien": "https://www.tiktok.com/@chaineauto/video/9000000000000000001",
        "nombre_noms": 3, "noms": noms,
    })
    assert reponse.status_code == 202, reponse.text
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "completed", job.get("error")

    # Une recherche TikWM par nom, dans l'ordre saisi — jamais de remplacement.
    assert requetes == noms
    assert job["search_queries"] == noms

    trouvees = job["found_videos"]
    # 4 vidéos uniques : le doublon 9000000000000000022 n'apparaît qu'une fois.
    assert len(trouvees) == 4
    identifiants = [v["video_id"] for v in trouvees]
    assert len(set(identifiants)) == len(identifiants)
    assert set(identifiants) == {
        "9000000000000000021", "9000000000000000022", "9000000000000000023", "9000000000000000024",
    }
    # Le doublon garde l'origine de sa première découverte (recherche « Alpha One »).
    doublon = next(v for v in trouvees if v["video_id"] == "9000000000000000022")
    assert doublon["origin"] == "recherche SsT « Alpha One »"
    assert doublon["nom"] == "Alpha One"

    # La sélection équilibrée couvre les trois noms, puis complète le quota.
    liens_montage = montage_kwargs["liens"]
    assert len(liens_montage) == 4
    # Le lien de référence de style reste la vidéo source, jamais une candidate.
    assert montage_kwargs["lien_reference"] == "https://www.tiktok.com/@chaineauto/video/9000000000000000001"


def test_sst_message_d_erreur_detaille_les_recherches_echouees(client, monkeypatch):
    """Quand rien n'est trouvé, l'échec liste les noms, les recherches tentées, les
    erreurs TikWM, les erreurs des sources publiques et les raisons de rejet."""
    _brancher_tikwm_sst(
        monkeypatch, source_id="9000000000000000001", catalogue={},
        bloquer_recherche=True,
    )

    async def decouvrir_publique(_session, *, auteur="", requete="", limite=20, etat=None):
        # Aucun moteur public ne donne de lien pour ces noms depuis cette IP.
        return []

    monkeypatch.setattr(app, "_decouvrir_publique", decouvrir_publique)

    noms = ["Alpha One", "Beta S", "Gamma X"]
    reponse = client.post("/api/jobs/sst", json={
        "lien": "https://www.tiktok.com/@chaineauto/video/9000000000000000001",
        "nombre_noms": 3, "noms": noms,
    })
    assert reponse.status_code == 202, reponse.text
    job = _attendre_job(client, reponse.json()["job_id"])
    assert job["status"] == "failed"
    erreur = job["error"]

    assert "SsT n'a trouvé aucune source exploitable" in erreur
    for nom in noms:
        assert nom in erreur
    assert f"Recherches tentées : {', '.join(noms)}" in erreur
    assert "Erreurs TikWM" in erreur and "bloqué (403)" in erreur
    assert "Erreurs des sources publiques" in erreur
    assert "aucune source publique n'a donné de lien" in erreur
    assert "Raisons de rejet" in erreur
    assert "aucune candidate trouvée" in erreur


def test_sst_candidats_de_duree_inconnue_ou_invalide_ne_sont_pas_retenues():
    """Une candidate sans durée valide est rejetée avant l'IA, avec la raison exacte."""
    noms = ["Alpha One", "Beta S", "Gamma X"]
    candidats = [
        {"url": "https://www.tiktok.com/@a/video/1", "video_id": "1", "author": "a",
         "duration": 0.0, "nom": "Alpha One", "origin": "recherche SsT « Alpha One »"},
        {"url": "https://www.tiktok.com/@b/video/2", "video_id": "2", "author": "b",
         "duration": None, "nom": "Beta S", "origin": "recherche SsT « Beta S »"},
        {"url": "https://www.tiktok.com/@c/video/3", "video_id": "3", "author": "c",
         "duration": "invalide", "nom": "Gamma X", "origin": "recherche SsT « Gamma X »"},
        {"url": "https://www.tiktok.com/@d/video/4", "video_id": "4", "author": "d",
         "duration": 12.0, "nom": "Alpha One", "origin": "recherche SsT « Alpha One »"},
    ]
    retenues = app._selectionner_sources_sst(candidats, noms, limite=20, duree_max=600.0)
    assert [c["video_id"] for c in retenues] == ["4"]
    rejets = {c["video_id"]: c["rejet"] for c in candidats if c.get("rejet")}
    assert rejets["1"] == "durée inconnue ou invalide"
    assert rejets["2"] == "durée inconnue ou invalide"
    assert rejets["3"] == "durée inconnue ou invalide"
    assert all(not c["selected"] for c in candidats)


def test_sst_aucune_candidature_n_est_retenue_avant_la_validation_visuelle():
    """La sélection équilibrée marque « awaiting_visual_ai » et jamais selected=True."""
    noms = ["Alpha One", "Beta S", "Gamma X"]
    candidats = [
        {"url": f"https://www.tiktok.com/@a{i}/video/9{i}", "video_id": f"9{i}",
         "author": f"a{i}", "duration": 15.0, "nom": nom, "origin": f"recherche SsT « {nom} »"}
        for i, nom in enumerate(noms)
    ]
    retenues = app._selectionner_sources_sst(list(candidats), noms, limite=20, duree_max=600.0)
    assert len(retenues) == 3
    for candidat in candidats:
        assert candidat["selected"] is False
        assert candidat["validation_status"] == "awaiting_visual_ai"
        assert not candidat.get("rejet")


def test_sst_les_noms_ne_sont_jamais_remplaces_ni_completes_par_l_ia(monkeypatch):
    """L'IA n'ajoute, ne remplace et ne complète jamais les noms : le repli déterministe
    et le complément manquant n'utilisent que les noms fournis par l'utilisateur."""
    noms = ["Alpha One", "Beta S", "Gamma X"]

    async def gemini_indisponible(_parts, **_kwargs):
        raise app.ErreurApp("GEMINI_API_KEYS n'est pas configuré sur le serveur.")

    monkeypatch.setattr(app, "_appel_gemini_brut", gemini_indisponible)
    script = asyncio.run(app.adapter_script_sst("Un classement de véhicules électriques.", noms))
    for nom in noms:
        assert nom.casefold() in f"{script['hook']} {script['corps']}".casefold()

    # L'IA répond en oubliant un nom : le complément n'utilise QUE les noms fournis.
    reponse_ia = json.dumps({"hook": "TOP 3 spécial", "corps": "Découvrez Alpha One et Beta S.", "mot_cle_broll": "essai"})

    async def gemini_oublieuse(_parts, **_kwargs):
        return reponse_ia

    monkeypatch.setattr(app, "_appel_gemini_brut", gemini_oublieuse)
    script = asyncio.run(app.adapter_script_sst("Un classement de véhicules électriques.", noms))
    assert "Gamma X" in script["corps"]  # complété avec le nom fourni, jamais un autre
    assert "Alpha One" in script["corps"] and "Beta S" in script["corps"]
    assert script["hook"] == "TOP 3 spécial"


# ======================================================================================
# ENTRAÎNEMENT IA : profils visuels — persistance serveur, isolation, enrichissement
# ======================================================================================


def test_profil_cree_persiste_dans_le_fichier_prive_de_la_session(client):
    """Un profil créé est écrit dans le JSON privé 0600 de la session serveur."""
    reponse = client.post("/api/profils-entrainement", json={
        "nom_sujet": "Alpha One",
        "aliases": ["alpha", "A1"],
        "good_examples": ["https://www.tiktok.com/@critique1/video/9000000000000000011"],
        "bad_examples": ["https://www.tiktok.com/@critique2/video/9000000000000000012"],
    })
    assert reponse.status_code == 200, reponse.text
    profil = reponse.json()["profile"]
    assert profil["nom_sujet"] == "Alpha One"
    assert profil["aliases"] == ["alpha", "A1"]
    assert profil["source_count"] == 0
    assert profil["sources_automatiquement_ajoutees"] == 0

    session_id = client.cookies.get("creator_session")
    chemin = app._fichier_profils_session(session_id)
    assert chemin.exists()
    assert (chemin.stat().st_mode & 0o777) == 0o600
    profils = json.loads(chemin.read_text(encoding="utf-8"))
    assert len(profils) == 1 and profils[0]["id"] == profil["id"]
    assert profils[0]["bons_exemples"] == ["https://www.tiktok.com/@critique1/video/9000000000000000011"]

    # Le listage renvoie le même profil pour cette session.
    liste = client.get("/api/profils-entrainement").json()
    assert [p["id"] for p in liste["profiles"]] == [profil["id"]]


def test_deux_sessions_ne_voient_pas_les_profils_l_une_de_l_autre():
    """Chaque cookie de session possède son fichier JSON privé : aucune fuite croisée."""
    with TestClient(app.app) as premiere, TestClient(app.app) as seconde:
        r1 = premiere.post("/api/profils-entrainement", json={"nom_sujet": "Sujet session 1"})
        r2 = seconde.post("/api/profils-entrainement", json={"nom_sujet": "Sujet session 2"})
        assert r1.status_code == r2.status_code == 200

        assert [p["nom_sujet"] for p in premiere.get("/api/profils-entrainement").json()["profiles"]] == ["Sujet session 1"]
        assert [p["nom_sujet"] for p in seconde.get("/api/profils-entrainement").json()["profiles"]] == ["Sujet session 2"]

        # Deux fichiers distincts, un par empreinte de session.
        chemin_1 = app._fichier_profils_session(premiere.cookies.get("creator_session"))
        chemin_2 = app._fichier_profils_session(seconde.cookies.get("creator_session"))
        assert chemin_1 != chemin_2 and chemin_1.exists() and chemin_2.exists()

        id_premier = r1.json()["profile"]["id"]
        # La session 2 ne peut ni modifier ni supprimer le profil de la session 1.
        assert seconde.put(f"/api/profils-entrainement/{id_premier}", json={"nom_sujet": "vol"}).status_code == 404
        assert seconde.delete(f"/api/profils-entrainement/{id_premier}").status_code == 404
        assert chemin_1.exists()
        assert any(p["id"] == id_premier for p in json.loads(chemin_1.read_text(encoding="utf-8")))


def test_synchronisation_et_suppression_du_profil_dans_les_deux_emplacements(client):
    """La synchro navigateur/serveur fusionne par date, la suppression vide le fichier."""
    profil_local = {
        "id": "profil-local-1", "nom_sujet": "Alpha One",
        "aliases": ["A1"], "bons_exemples": [], "mauvais_exemples": [],
        "updated_at": 1000.0,
    }
    synchro = client.post("/api/profils-entrainement/synchroniser", json={"profiles": [profil_local]})
    assert synchro.status_code == 200, synchro.text
    assert [p["id"] for p in synchro.json()["profiles"]] == ["profil-local-1"]

    session_id = client.cookies.get("creator_session")
    chemin = app._fichier_profils_session(session_id)
    assert "profil-local-1" in chemin.read_text(encoding="utf-8")

    # Le navigateur (plus récent) l'emporte ; le serveur garde le profil existant.
    mis_a_jour = {**profil_local, "nom_sujet": "Alpha One renommé", "updated_at": 2000.0}
    client.post("/api/profils-entrainement/synchroniser", json={"profiles": [mis_a_jour]})
    assert "Alpha One renommé" in chemin.read_text(encoding="utf-8")

    suppression = client.delete("/api/profils-entrainement/profil-local-1")
    assert suppression.status_code == 200
    assert json.loads(chemin.read_text(encoding="utf-8")) == []
    assert client.delete("/api/profils-entrainement/profil-local-1").status_code == 404


def test_analyser_profil_conserve_exemples_signatures_timestamps_et_refus(client, monkeypatch):
    """« Analyser avec l'IA et alimenter » conserve toutes les données : exemples analysés
    (timestamps compris), signatures positive et négative, raisons de rejet des candidates
    écartées, et seules les candidates réellement validées deviennent des sources."""
    creation = client.post("/api/profils-entrainement", json={
        "nom_sujet": "Alpha One",
        "aliases": ["A1"],
        "good_examples": ["https://www.tiktok.com/@critique1/video/9000000000000000011"],
        "bad_examples": ["https://www.tiktok.com/@critique2/video/9000000000000000012"],
    })
    assert creation.status_code == 200, creation.text
    profil_id = creation.json()["profile"]["id"]

    analyses_appelees: list[str] = []

    async def analyser_exemple(_session, url, profil, *, est_bon=True):
        analyses_appelees.append(url)
        if "000011" in url and est_bon:
            return {
                "url": url, "est_bon_exemple": True, "valide": True, "raison_refus": "",
                "sujet_visible": "Alpha One", "sujet_correspond": True, "confiance": 0.92,
                "personnes": [], "watermarks": [], "logos_ajoutes": [], "textes": [],
                "sous_titres_tiktok": ["sous-titre intégré"], "qualite": "bonne", "nettete": 0.8,
                "passages_propres": [{"debut": 1.5, "fin": 5.0, "raison": "plan produit net"}],
                "signature_positive": "Sujet exact Alpha One, plans produits nets",
                "signature_negative": "",
                "duration": 21.0,
            }
        if "000012" in url and not est_bon:
            return {
                "url": url, "est_bon_exemple": False, "valide": False,
                "raison_refus": "personne visible", "confiance": 0.3,
                "passages_propres": [], "signature_negative": "personne visible",
                "duration": 19.0,
            }
        # Candidate automatique : validée ou rejetée selon l'URL.
        valide = "000031" in url
        return {
            "url": url, "est_bon_exemple": True, "valide": valide,
            "raison_refus": "" if valide else "watermark détecté",
            "sujet_visible": "Alpha One" if valide else "autre sujet",
            "sujet_correspond": valide, "confiance": 0.9 if valide else 0.2,
            "personnes": [], "watermarks": [] if valide else ["logo"],
            "logos_ajoutes": [], "textes": [], "passages_propres":
                [{"debut": 0.0, "fin": 4.5, "raison": "net"}] if valide else [],
            "signature_positive": "Alpha One confirmé" if valide else "",
            "signature_negative": "" if valide else "watermark détecté",
            "duration": 17.0,
        }

    monkeypatch.setattr(app, "analyser_exemple_entrainement", analyser_exemple)

    async def candidates_profil(_session, profil, limite=10):
        return [
            {"url": "https://www.tiktok.com/@x/video/9000000000000000031",
             "video_id": "9000000000000000031", "author": "x", "title": "Alpha One essai",
             "duration": 17.0, "origin": "entraînement « Alpha One »", "nom": "Alpha One"},
            {"url": "https://www.tiktok.com/@y/video/9000000000000000032",
             "video_id": "9000000000000000032", "author": "y", "title": "hors sujet",
             "duration": 12.0, "origin": "entraînement « Alpha One »", "nom": "Alpha One"},
        ]

    monkeypatch.setattr(app, "_candidates_profil", candidates_profil)

    analyse = client.post(f"/api/profils-entrainement/{profil_id}/analyser", json={})
    assert analyse.status_code == 200, analyse.text
    profil = analyse.json()["profile"]

    # Les exemples bons et mauvais ont été analysés, plus les deux candidates.
    assert len(analyses_appelees) == 4
    donnees = profil["donnees"]
    assert donnees["sources_exemples_analysees"] == 2
    assert len(donnees["exemples_analyses"]) == 2
    exemple_bon = next(e for e in donnees["exemples_analyses"] if "000011" in e["url"])
    assert exemple_bon["passages_propres"] == [{"debut": 1.5, "fin": 5.0, "raison": "plan produit net"}]
    exemple_mauvais = next(e for e in donnees["exemples_analyses"] if "000012" in e["url"])
    assert exemple_mauvais["raison_refus"] == "personne visible"

    # Signatures positive et négative construites sur les analyses réelles.
    assert donnees["signature_positive"] == ["Sujet exact Alpha One, plans produits nets"]
    assert "personne visible" in donnees["signature_negative"]

    # Seule la candidate validée devient une source ; la rejetée garde sa raison.
    assert len(profil["sources_validees"]) == 1
    assert profil["sources_validees"][0]["video_id"] == "9000000000000000031"
    assert profil["sources_automatiquement_ajoutees"] == 1
    candidates_analysees = donnees["candidates_analysees"]
    rejetee = next(c for c in candidates_analysees if c["video_id"] == "9000000000000000032")
    assert rejetee["raison_refus"] == "watermark détecté"
    assert rejetee["validation_status"] == "rejected"

    # Tout est persisté dans le fichier privé de la session.
    session_id = client.cookies.get("creator_session")
    persiste = json.loads(app._fichier_profils_session(session_id).read_text(encoding="utf-8"))
    assert persiste[0]["donnees"]["signature_positive"] == ["Sujet exact Alpha One, plans produits nets"]
    assert len(persiste[0]["sources_validees"]) == 1


def test_profil_non_entraiine_est_seulement_enregistre(client):
    """Un profil à 0 source, 0 exemple analysé et 0 % de confiance est simplement
    enregistré : l'API expose ces compteurs sans rien inventer."""
    creation = client.post("/api/profils-entrainement", json={
        "nom_sujet": "Alpha One", "aliases": [], "bons_exemples": [], "mauvais_exemples": [],
    })
    assert creation.status_code == 200, creation.text
    profil = creation.json()["profile"]
    assert profil["source_count"] == 0
    assert profil["sources_automatiquement_ajoutees"] == 0
    assert profil["donnees"] == {}
    assert not profil["sources_validees"]
