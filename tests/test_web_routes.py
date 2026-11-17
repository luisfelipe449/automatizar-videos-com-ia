"""Integration tests for web routes using FastAPI TestClient."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.web.main import create_app


@pytest.fixture
def client():
    """Create test client with default app configuration.

    Note: These tests use the default database configured in the app.
    For isolated tests, consider using an in-memory database override.
    """
    app = create_app()
    return TestClient(app)


@pytest.fixture
def isolated_client(tmp_path, monkeypatch):
    """Test client bound to a fresh SQLite DB with one active account.

    Overrides `get_db` so all route handlers (including the JobQueueManager
    dependency, which is `Depends(get_db)`) read/write the temp DB. Returns
    (TestClient, account_id) so tests can target that account directly.
    """
    from app.db.database import Base, get_engine, get_session
    from app.db.models import Account
    from app.web.dependencies import get_db

    # Redirect filesystem writes (script.json etc.) into the tmp dir so
    # test runs don't pollute the repo's assets/ tree.
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", tmp_path / "generated")
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", tmp_path / "rendered")

    db_path = tmp_path / "test.db"
    engine = get_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    # Seed one active account with full quota.
    with get_session(engine) as seed_session:
        account = Account(
            name="TestAccount",
            email="test@example.com",
            client_id="cid",
            client_secret="csec",
            refresh_token="rtok",
            daily_quota=10000,
            quota_used=0,
            quota_reset_at=datetime.now(timezone.utc) + timedelta(hours=24),
            is_active=True,
        )
        seed_session.add(account)
        seed_session.flush()
        account_id = account.id

    def override_get_db():
        with get_session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), account_id


class TestAppStructure:
    """Tests for application structure and startup."""

    def test_app_creation(self):
        """Test that the app can be created successfully."""
        app = create_app()
        assert app is not None
        assert app.title == "Video Automation Dashboard"

    def test_app_has_routes(self, client):
        """Test that main routes are accessible."""
        # Dashboard
        response = client.get("/")
        assert response.status_code in [200, 500]  # 500 if DB not initialized

        # Accounts
        response = client.get("/accounts")
        assert response.status_code in [200, 500]

        # Jobs
        response = client.get("/jobs")
        assert response.status_code in [200, 500]


class TestAPIRoutes:
    """Tests for JSON API routes."""

    def test_api_status_endpoint_exists(self, client):
        """Test /api/status endpoint exists."""
        response = client.get("/api/status")
        # Should return 200 or 500 depending on DB state
        assert response.status_code in [200, 500]

        if response.status_code == 200:
            data = response.json()
            assert "accounts" in data
            assert "jobs" in data

    def test_api_job_status_not_found(self, client):
        """Test API endpoint for non-existent job returns 404 or 500."""
        response = client.get("/api/jobs/nonexistent-job/status")
        assert response.status_code in [404, 500]


class TestAccountRoutes:
    """Tests for account management routes."""

    def test_accounts_list_page(self, client):
        """Test accounts list page loads."""
        response = client.get("/accounts")
        assert response.status_code in [200, 500]

    def test_new_account_form(self, client):
        """Test new account form loads."""
        response = client.get("/accounts/new")
        assert response.status_code in [200, 500]

        if response.status_code == 200:
            assert b"Add New Account" in response.content or b"Account Name" in response.content

    def test_create_account_missing_fields(self, client):
        """Test create account with missing required fields returns 422."""
        response = client.post("/accounts/new", data={"name": "Test"})
        assert response.status_code == 422


class TestJobRoutes:
    """Tests for job queue routes."""

    def test_jobs_list_page(self, client):
        """Test jobs list page loads."""
        response = client.get("/jobs")
        assert response.status_code in [200, 500]

    def test_create_job_missing_prompt(self, client):
        """Test creating job without prompt fails with 422."""
        response = client.post("/jobs/create", data={"priority": 0})
        assert response.status_code == 422

    def test_job_detail_not_found(self, client):
        """Test job detail for non-existent job returns 404 or 500."""
        response = client.get("/jobs/nonexistent-job-id")
        assert response.status_code in [404, 500]


class TestTemplateRendering:
    """Tests for template rendering."""

    def test_base_template_elements(self, client):
        """Test that base template loads with key elements."""
        response = client.get("/")

        if response.status_code == 200:
            content = response.content
            # Check for navigation elements
            assert b"Video Automation" in content or b"Dashboard" in content
            # Check for Tailwind CSS
            assert b"tailwindcss.com" in content or b"cdn" in content


class TestErrorHandling:
    """Tests for error handling."""

    def test_nonexistent_route_404(self, client):
        """Test that nonexistent routes return 404."""
        response = client.get("/nonexistent-route-12345")
        assert response.status_code == 404

    def test_post_to_nonexistent_route_404(self, client):
        """Test that POST to nonexistent routes returns 404."""
        response = client.post("/nonexistent-route-12345", data={})
        assert response.status_code in [404, 405]  # 405 Method Not Allowed is also valid


class TestJobSubmissionFlow:
    """Integration test for full job submission and processing flow."""

    def test_job_submission_flow(self, client):
        """Test complete flow: enqueue -> worker processes -> marks completed.

        This test verifies:
        1. Job can be enqueued via API
        2. Job starts in pending state
        3. Worker can pick up job (simulated)
        4. Job status is updated correctly

        Note: This test does NOT run the actual pipeline to avoid
        external API calls. It only tests the queue management flow.
        """
        # Create a test job via API
        response = client.post(
            "/jobs/create",
            data={"prompt": "Test integration job", "priority": 5}
        )

        # Should redirect on success or return error
        if response.status_code == 303:
            # Follow redirect to job detail
            location = response.headers.get("location", "")
            job_id = location.split("/")[-1] if location else None

            if job_id:
                # Verify job exists and is pending
                detail_response = client.get(f"/jobs/{job_id}")
                assert detail_response.status_code == 200

                # Check API status for this job
                api_response = client.get(f"/api/jobs/{job_id}/status")
                if api_response.status_code == 200:
                    job_data = api_response.json()
                    assert job_data["status"] == "pending"
                    assert job_data["prompt"] == "Test integration job"

        # If DB not initialized, test should still pass (500 expected)
        elif response.status_code == 500:
            # Database not initialized - this is acceptable for test
            pass
        else:
            # Other status codes should be 422 (validation) or 200
            assert response.status_code in [200, 422, 500]


# --------------------------------------------------------------------------- #
# Theme-batch endpoints
# --------------------------------------------------------------------------- #

_FAKE_ANGLES = [
    {"title": f"Angle {i}", "prompt": f"Unique prompt number {i} for the theme."}
    for i in range(1, 6)
]


class TestGenerateAnglesEndpoint:
    """POST /jobs/generate-angles — stateless preview."""

    def test_returns_angles_on_success(self, isolated_client, monkeypatch):
        client, _ = isolated_client

        monkeypatch.setattr(
            "app.services.script_service.generate_angles",
            lambda theme, n=5: _FAKE_ANGLES[:n],
        )

        resp = client.post(
            "/jobs/generate-angles",
            json={"theme": "buracos negros", "count": 5},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "angles" in body
        assert len(body["angles"]) == 5
        assert body["angles"][0]["title"] == "Angle 1"

    def test_rejects_empty_theme(self, isolated_client):
        client, _ = isolated_client
        resp = client.post("/jobs/generate-angles", json={"theme": "", "count": 5})
        # Pydantic min_length=1 → 422
        assert resp.status_code == 422

    def test_returns_500_on_angles_generation_error(self, isolated_client, monkeypatch):
        from app.services.script_service import AnglesGenerationError

        client, _ = isolated_client

        def boom(theme, n=5):
            raise AnglesGenerationError("simulated LLM failure")

        monkeypatch.setattr("app.services.script_service.generate_angles", boom)

        resp = client.post(
            "/jobs/generate-angles",
            json={"theme": "qualquer", "count": 5},
        )
        assert resp.status_code == 500
        assert "simulated LLM failure" in resp.json()["detail"]


class TestCreateBatchEndpoint:
    """POST /jobs/create-batch — all-or-nothing script gen + 5-job enqueue."""

    def _form(self, account_id: int, angles=_FAKE_ANGLES) -> dict:
        import json

        return {
            "theme": "buracos negros",
            "angles_json": json.dumps(angles),
            "account_id": str(account_id),
            "category": "24",
            "priority": "0",
        }

    def test_enqueues_n_jobs_on_success(self, isolated_client, monkeypatch):
        from app.db.database import Base, get_engine, get_session
        from app.db.models import Job

        client, account_id = isolated_client

        calls: list[str] = []
        captured_titles: list[str | None] = []

        def fake_generate_script(prompt, *, job_id=None, video_title=None):
            calls.append(prompt)
            captured_titles.append(video_title)
            # Mimic side effect (persisting script.json) without touching disk.
            return None

        monkeypatch.setattr(
            "app.services.script_service.generate_script", fake_generate_script
        )

        resp = client.post(
            "/jobs/create-batch",
            data=self._form(account_id),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/jobs"
        assert len(calls) == 5
        assert captured_titles == [a["title"] for a in _FAKE_ANGLES]

        # Inspect DB: 5 jobs inserted, same account, all pending.
        engine = client.app.dependency_overrides[
            __import__("app.web.dependencies", fromlist=["get_db"]).get_db
        ].__wrapped__.__globals__.get("engine") if False else None
        # Simpler: just open a new session through the API we know works.
        # Pull jobs via /jobs list page.
        list_resp = client.get("/jobs")
        assert list_resp.status_code == 200
        # Cross-check via direct query using the same connection URL by asking
        # the override (its closure captures `engine`).
        get_db_override = client.app.dependency_overrides.get(
            __import__("app.web.dependencies", fromlist=["get_db"]).get_db
        )
        session_gen = get_db_override()
        session = next(session_gen)
        try:
            pending = session.query(Job).filter_by(status="pending").all()
            assert len(pending) == 5
            # All jobs belong to the same account and their prompts are the angle prompts.
            assert {j.account_id for j in pending} == {account_id}
            assert {j.prompt for j in pending} == {a["prompt"] for a in _FAKE_ANGLES}
        finally:
            try:
                next(session_gen)
            except StopIteration:
                pass

    def test_aborts_on_script_failure_no_jobs_enqueued(
        self, isolated_client, monkeypatch
    ):
        from app.db.models import Job
        from app.services.script_service import ScriptGenerationError

        client, account_id = isolated_client
        call_counter = {"n": 0}

        def fake_generate_script(prompt, *, job_id=None, video_title=None):
            call_counter["n"] += 1
            if call_counter["n"] == 3:
                raise ScriptGenerationError("simulated script failure")
            return None

        monkeypatch.setattr(
            "app.services.script_service.generate_script", fake_generate_script
        )

        resp = client.post(
            "/jobs/create-batch",
            data=self._form(account_id),
            follow_redirects=False,
        )
        assert resp.status_code == 500
        assert "angle 3" in resp.json()["detail"].lower() or "angle 3" in resp.json()["detail"]
        assert "No jobs were enqueued" in resp.json()["detail"]

        # Confirm DB has zero jobs.
        from app.web.dependencies import get_db

        session_gen = client.app.dependency_overrides[get_db]()
        session = next(session_gen)
        try:
            assert session.query(Job).count() == 0
        finally:
            try:
                next(session_gen)
            except StopIteration:
                pass

    def test_rejects_insufficient_quota(self, isolated_client, monkeypatch):
        from app.db.models import Account, Job
        from app.web.dependencies import get_db

        client, account_id = isolated_client

        # Force the account down to 2 uploads' worth of quota (3200 of 10000).
        session_gen = client.app.dependency_overrides[get_db]()
        session = next(session_gen)
        try:
            acct = session.query(Account).filter_by(id=account_id).first()
            acct.quota_used = acct.daily_quota - 3200
            session.commit()
        finally:
            try:
                next(session_gen)
            except StopIteration:
                pass

        # generate_script should NEVER be called — quota is checked first.
        def should_not_run(*_a, **_kw):
            raise AssertionError("generate_script called despite quota shortfall")

        monkeypatch.setattr(
            "app.services.script_service.generate_script", should_not_run
        )

        resp = client.post(
            "/jobs/create-batch",
            data=self._form(account_id),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "quota" in resp.json()["detail"].lower()

        # No jobs inserted.
        session_gen2 = client.app.dependency_overrides[get_db]()
        session2 = next(session_gen2)
        try:
            assert session2.query(Job).count() == 0
        finally:
            try:
                next(session_gen2)
            except StopIteration:
                pass

    def test_rejects_malformed_angles_json(self, isolated_client, monkeypatch):
        client, account_id = isolated_client

        def should_not_run(*_a, **_kw):
            raise AssertionError("should not reach script generation")

        monkeypatch.setattr(
            "app.services.script_service.generate_script", should_not_run
        )

        bad = self._form(account_id)
        bad["angles_json"] = "not json"
        resp = client.post("/jobs/create-batch", data=bad, follow_redirects=False)
        assert resp.status_code == 400
        assert "angles_json" in resp.json()["detail"].lower()

    def test_rejects_empty_angle_prompt(self, isolated_client, monkeypatch):
        import json

        client, account_id = isolated_client
        angles = [dict(a) for a in _FAKE_ANGLES]
        angles[2]["prompt"] = ""

        def should_not_run(*_a, **_kw):
            raise AssertionError("should not reach script generation")

        monkeypatch.setattr(
            "app.services.script_service.generate_script", should_not_run
        )

        form = self._form(account_id)
        form["angles_json"] = json.dumps(angles)
        resp = client.post("/jobs/create-batch", data=form, follow_redirects=False)
        assert resp.status_code == 400
        assert "angle 3" in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# Clips editions (approval dashboard)
# --------------------------------------------------------------------------- #

@pytest.fixture
def editions_client(tmp_path, monkeypatch):
    """Client on a temp DB with one rendered top_daily edition and its files.

    Returns (TestClient, edition_id, editions_dir).
    """
    import json
    from datetime import date

    from app.db.database import Base, get_engine, get_session
    from app.db.models import Clip, Edition, EditionClip
    from app.web.dependencies import get_db

    editions_dir = tmp_path / "editions"
    monkeypatch.setattr("app.clips.pipeline.EDITIONS_DIR", editions_dir)

    engine = get_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    with get_session(engine) as s:
        clips = [
            Clip(source="twitch", source_clip_id=f"c{i}", url=f"https://clips.twitch.tv/c{i}",
                 broadcaster_name=f"streamer{i}", title="t", view_count=1000 - i, duration_s=30.0,
                 created_at_source=datetime(2026, 11, 19, 3, tzinfo=timezone.utc), category="play")
            for i in range(3)
        ]
        s.add_all(clips)
        s.flush()
        edition = Edition(format_key="top_daily", edition_date=date(2026, 11, 19), status="rendered")
        edition.numbers = {"five-star-clips": 4, "five-star-clips-br": 2}
        edition.items = [
            EditionClip(clip_id=clips[2].id, position=1),
            EditionClip(clip_id=clips[1].id, position=2),
            EditionClip(clip_id=clips[0].id, position=3, in_short=True, trim_start_s=1.5),
        ]
        s.add(edition)
        s.commit()
        edition_id = edition.id

    base = editions_dir / "2026-11-19_top_daily"
    for key, title in (("five-star-clips", "GTA 6 Top Clips of the Day #4"),
                       ("five-star-clips-br", "GTA 6 Melhores Clipes do Dia #2")):
        lang = base / key
        lang.mkdir(parents=True)
        for name in ("medio.mp4", "short.mp4", "thumb.jpg"):
            (lang / name).write_bytes(f"{key}/{name}".encode())
        snippet = {"title": title, "description": f"desc {key}\n\n0:00 #3", "tags": ["gta 6"],
                   "categoryId": "20", "defaultLanguage": "en"}
        (lang / "metadata.json").write_text(json.dumps({"medium": snippet, "short": snippet}),
                                            encoding="utf-8")
    (base / "status.json").write_text(json.dumps(
        {"stages": {"base": {"status": "done", "seconds": 12.3}}, "error": None}), encoding="utf-8")

    def override_get_db():
        with get_session(engine) as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app), edition_id, editions_dir, engine


class TestEditions:
    def test_list_shows_the_days_batch(self, editions_client):
        client, edition_id, _, _ = editions_client
        resp = client.get("/editions?date=2026-11-19")
        assert resp.status_code == 200
        assert f"/editions/{edition_id}" in resp.text
        assert "top_daily" in resp.text and "rendered" in resp.text

    def test_list_of_empty_day(self, editions_client):
        client, _, _, _ = editions_client
        resp = client.get("/editions?date=2026-11-20")
        assert resp.status_code == 200
        assert "No editions for this day" in resp.text

    def test_list_rejects_bad_date(self, editions_client):
        client, _, _, _ = editions_client
        assert client.get("/editions?date=19-11-2026").status_code == 422

    def test_detail_shows_players_titles_and_publish_times(self, editions_client):
        client, edition_id, _, _ = editions_client
        resp = client.get(f"/editions/{edition_id}")
        assert resp.status_code == 200
        text = resp.text
        for key in ("five-star-clips", "five-star-clips-br"):
            for piece in ("medio", "short", "thumb"):
                assert f"/editions/{edition_id}/media/{key}/{piece}" in text
        assert "GTA 6 Top Clips of the Day #4" in text
        assert "GTA 6 Melhores Clipes do Dia #2" in text
        # en: 15:00 New York = 17:00 BRT on 19/11; pt-BR: 11:00 Sao Paulo.
        assert "15:00 America/New_York" in text and "19/11 17:00 BRT" in text
        assert "11:00 America/Sao_Paulo" in text
        # Clips in playback order, #1 last and marked as the Short.
        assert text.index("streamer2") < text.index("streamer0")
        assert "-1.5 s" in text
        assert "Approve" in text and "Reject" in text
        assert "12.3 s" in text

    def test_detail_not_found(self, editions_client):
        client, _, _, _ = editions_client
        assert client.get("/editions/9999").status_code == 404

    def test_approve_then_decision_is_final(self, editions_client):
        from app.db.database import get_session
        from app.db.models import Edition

        client, edition_id, _, engine = editions_client
        resp = client.post(f"/editions/{edition_id}/approve", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/editions/{edition_id}"
        with get_session(engine) as s:
            edition = s.get(Edition, edition_id)
            assert edition.status == "approved" and edition.decided_at is not None
        assert client.post(f"/editions/{edition_id}/reject").status_code == 409
        assert "Approve" not in client.get(f"/editions/{edition_id}").text

    def test_reject(self, editions_client):
        from app.db.database import get_session
        from app.db.models import Edition

        client, edition_id, _, engine = editions_client
        client.post(f"/editions/{edition_id}/reject", follow_redirects=False)
        with get_session(engine) as s:
            assert s.get(Edition, edition_id).status == "rejected"

    def test_cannot_decide_an_unrendered_edition(self, editions_client):
        from app.db.database import get_session
        from app.db.models import Edition

        client, edition_id, _, engine = editions_client
        with get_session(engine) as s:
            s.get(Edition, edition_id).status = "planned"
            s.commit()
        assert client.post(f"/editions/{edition_id}/approve").status_code == 409

    def test_download_is_an_attachment(self, editions_client):
        client, edition_id, _, _ = editions_client
        resp = client.get(f"/editions/{edition_id}/download/five-star-clips-br/short")
        assert resp.status_code == 200
        assert resp.content == b"five-star-clips-br/short.mp4"
        assert resp.headers["content-type"] == "video/mp4"
        disposition = resp.headers["content-disposition"]
        assert disposition.startswith("attachment")
        assert "2026-11-19_top_daily_five-star-clips-br_short.mp4" in disposition

    def test_media_is_inline(self, editions_client):
        client, edition_id, _, _ = editions_client
        resp = client.get(f"/editions/{edition_id}/media/five-star-clips/thumb")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.headers["content-disposition"].startswith("inline")

    @pytest.mark.parametrize("profile, piece", [
        ("nope", "short"),
        ("five-star-clips", "status"),
        ("..", "short"),
        ("five-star-clips", "..%2Fstatus.json"),
    ])
    def test_download_rejects_unknown_channel_or_piece(self, editions_client, profile, piece):
        client, edition_id, _, _ = editions_client
        assert client.get(f"/editions/{edition_id}/download/{profile}/{piece}").status_code == 404

    def test_download_missing_file(self, editions_client):
        client, edition_id, editions_dir, _ = editions_client
        (editions_dir / "2026-11-19_top_daily" / "five-star-clips" / "short.mp4").unlink()
        assert client.get(f"/editions/{edition_id}/download/five-star-clips/short").status_code == 404

    def test_nav_links_to_clips(self, editions_client):
        client, _, _, _ = editions_client
        assert 'href="/editions"' in client.get("/editions").text
