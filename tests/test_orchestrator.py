"""Tests for `app.orchestrator` — flow control only, every service is mocked.

Strategy: monkeypatch every `app.services.*` entry point imported INTO
`app.orchestrator`. The orchestrator's job is orchestration + cache checks +
status-writing; none of the tests touch real APIs, ffmpeg, or the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import orchestrator
from app.models import Scene, Script
from app.services.tts_service import TTSError
from app.services.visual_service import VisualGenerationError
from app.utils import cache, filepaths


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _make_script(job_id: str = "test-job") -> Script:
    return Script(
        job_id=job_id,
        title="Curiosidades",
        description="Três curiosidades incríveis #Shorts",
        tags=["curiosidades", "ciência"],
        narration="Esta é a narração de teste para o Short.",
        scenes=[
            Scene(id=1, scene_text="Cena 1", image_prompt="prompt um", duration_seconds=3),
            Scene(id=2, scene_text="Cena 2", image_prompt="prompt dois", duration_seconds=3),
            Scene(id=3, scene_text="Cena 3", image_prompt="prompt três", duration_seconds=3),
        ],
    )


@pytest.fixture
def redirected_dirs(monkeypatch, tmp_path: Path) -> Path:
    """Redirect GENERATED_DIR / RENDERED_DIR into a per-test tmp_path."""
    generated = tmp_path / "generated"
    rendered = tmp_path / "rendered"
    monkeypatch.setattr("app.utils.filepaths.GENERATED_DIR", generated)
    monkeypatch.setattr("app.utils.filepaths.RENDERED_DIR", rendered)
    return tmp_path


@pytest.fixture
def mocked_services(monkeypatch, redirected_dirs: Path):
    """Install call-counting fakes for every service imported by orchestrator.

    Each fake also writes the artifact the real service would have written, so
    subsequent stages (and cache checks) see consistent on-disk state.
    """
    calls: dict[str, int] = {k: 0 for k in (
        "script", "tts", "captions", "clips", "render", "upload",
    )}

    script_holder: dict[str, Script] = {}

    def fake_generate_script(prompt: str, *, job_id: str | None = None) -> Script:
        calls["script"] += 1
        resolved = job_id or filepaths.make_job_id(prompt)
        script = _make_script(resolved)
        filepaths.ensure_job_dirs(resolved)
        filepaths.script_path(resolved).write_text(
            script.model_dump_json(indent=2), encoding="utf-8"
        )
        script_holder["script"] = script
        return script

    def fake_synthesize_narration(narration: str, job_id: str, *, voice_name: str = "Kore") -> Path:
        calls["tts"] += 1
        filepaths.ensure_job_dirs(job_id)
        wav = filepaths.narration_path(job_id)
        wav.write_bytes(b"RIFFfakewavdata")
        cache.write_hash_file(filepaths.narration_hash_path(job_id), cache.hash_text(narration))
        return wav

    def fake_transcribe(
        narration_wav: Path,
        job_id: str,
        *,
        words_per_chunk: int = 6,
        narration_hash: str | None = None,
    ) -> Path:
        calls["captions"] += 1
        filepaths.ensure_job_dirs(job_id)
        ass = filepaths.captions_path(job_id)
        ass.write_text(
            "[Script Info]\nScriptType: v4.00+\n\n[Events]\n"
            "Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\k100}hi\n",
            encoding="utf-8",
        )
        # Mirror the real service: persist the hash so the next run cache-hits.
        if narration_hash is not None:
            cache.write_hash_file(filepaths.captions_hash_path(job_id), narration_hash)
        return ass

    def fake_generate_all_scene_images(script: Script) -> list[Path]:
        calls["clips"] += 1
        filepaths.ensure_job_dirs(script.job_id)
        paths: list[Path] = []
        for scene in script.scenes:
            img = filepaths.scene_image_path(script.job_id, scene.id)
            img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024)
            cache.write_hash_file(
                filepaths.scene_image_hash_path(script.job_id, scene.id),
                cache.hash_text(scene.image_prompt),
            )
            paths.append(img)
        return paths

    def fake_render_short(spec) -> Path:
        calls["render"] += 1
        out = Path(spec.output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        return out

    def fake_upload_short(video_path: Path, script: Script, *, privacy_status: str | None = None) -> str:
        calls["upload"] += 1
        rec = filepaths.upload_record_path(script.job_id)
        rec.parent.mkdir(parents=True, exist_ok=True)
        rec.write_text(json.dumps({"video_id": "vid-123"}), encoding="utf-8")
        return "vid-123"

    monkeypatch.setattr(orchestrator, "generate_script", fake_generate_script)
    monkeypatch.setattr(orchestrator, "synthesize_narration", fake_synthesize_narration)
    monkeypatch.setattr(orchestrator, "transcribe", fake_transcribe)
    monkeypatch.setattr(orchestrator, "generate_all_scene_images", fake_generate_all_scene_images)
    monkeypatch.setattr(orchestrator, "render_short", fake_render_short)
    monkeypatch.setattr(orchestrator, "upload_short", fake_upload_short)

    return calls


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_happy_path_full_pipeline(mocked_services, redirected_dirs):
    """All 6 stages run exactly once; success=True; video_id present."""
    result = orchestrator.run_pipeline("prompt de teste", job_id="job-happy")

    assert result["success"] is True
    assert result["video_id"] == "vid-123"
    assert len(result["stages_completed"]) == 6
    assert result["error"] is None

    assert mocked_services == {
        "script": 1, "tts": 1, "captions": 1, "clips": 1, "render": 1, "upload": 1,
    }

    # status.json on disk should have ok=True (internal format preserved)
    on_disk = json.loads(filepaths.status_path("job-happy").read_text(encoding="utf-8"))
    assert on_disk["ok"] is True
    assert on_disk["job_id"] == "job-happy"
    assert on_disk["prompt"] == "prompt de teste"


def test_skip_upload(mocked_services, redirected_dirs):
    """`skip_upload=True` short-circuits the upload stage and leaves success=True."""
    result = orchestrator.run_pipeline("prompt skip", job_id="job-skip", skip_upload=True)

    assert result["success"] is True
    assert result["video_id"] is None
    assert "upload" in result["stages_completed"]
    assert mocked_services["upload"] == 0
    # The other stages still ran.
    on_disk = json.loads(filepaths.status_path("job-skip").read_text(encoding="utf-8"))
    for stage in ("script", "tts", "captions", "clips", "render"):
        assert on_disk["stages"][stage]["status"] == "done"
    assert on_disk["stages"]["upload"]["status"] == "skipped"


def test_resume_from_script(mocked_services, redirected_dirs):
    """Pre-existing script.json → script stage is a cache-hit; others still run."""
    job_id = "job-resume"
    filepaths.ensure_job_dirs(job_id)
    pre = _make_script(job_id)
    filepaths.script_path(job_id).write_text(
        pre.model_dump_json(indent=2), encoding="utf-8"
    )

    result = orchestrator.run_pipeline("any prompt", job_id=job_id, skip_upload=True)

    assert result["success"] is True
    on_disk = json.loads(filepaths.status_path(job_id).read_text(encoding="utf-8"))
    assert on_disk["stages"]["script"]["status"] == "cache-hit"
    assert mocked_services["script"] == 0
    # Remaining stages still ran.
    assert mocked_services["tts"] == 1
    assert mocked_services["captions"] == 1
    assert mocked_services["clips"] == 1
    assert mocked_services["render"] == 1


def test_failure_mid_pipeline(monkeypatch, mocked_services, redirected_dirs):
    """tts raises → pipeline halts; status shows tts=failed, later stages pending."""
    def boom(*_a, **_kw):
        raise TTSError("boom")

    monkeypatch.setattr(orchestrator, "synthesize_narration", boom)

    with pytest.raises(TTSError, match="boom"):
        orchestrator.run_pipeline("failure prompt", job_id="job-fail")

    status = json.loads(filepaths.status_path("job-fail").read_text(encoding="utf-8"))
    assert status["ok"] is False
    assert status["stages"]["script"]["status"] == "done"
    assert status["stages"]["tts"]["status"] == "failed"
    assert status["stages"]["tts"]["error"] == "boom"
    for later in ("captions", "clips", "render", "upload"):
        assert status["stages"][later]["status"] == "pending"

    # Downstream services were not called.
    assert mocked_services["captions"] == 0
    assert mocked_services["clips"] == 0
    assert mocked_services["render"] == 0
    assert mocked_services["upload"] == 0


def test_failure_in_clips_skips_render_and_upload(
    monkeypatch, mocked_services, redirected_dirs
):
    def boom(*_a, **_kw):
        raise VisualGenerationError("no pixels today")

    monkeypatch.setattr(orchestrator, "generate_all_scene_images", boom)

    with pytest.raises(VisualGenerationError):
        orchestrator.run_pipeline("clips fail", job_id="job-vfail")

    assert mocked_services["render"] == 0
    assert mocked_services["upload"] == 0
    status = json.loads(filepaths.status_path("job-vfail").read_text(encoding="utf-8"))
    assert status["stages"]["clips"]["status"] == "failed"
    assert status["stages"]["render"]["status"] == "pending"
    assert status["stages"]["upload"]["status"] == "pending"
    assert status["ok"] is False


def test_job_id_override_places_artifacts_under_custom_dir(
    mocked_services, redirected_dirs: Path
):
    result = orchestrator.run_pipeline(
        "custom", job_id="custom-id", skip_upload=True
    )
    assert result["success"] is True
    on_disk = json.loads(filepaths.status_path("custom-id").read_text(encoding="utf-8"))
    assert on_disk["job_id"] == "custom-id"
    generated = redirected_dirs / "generated" / "custom-id"
    rendered = redirected_dirs / "rendered" / "custom-id"
    assert (generated / "script.json").exists()
    assert (generated / "narration.wav").exists()
    assert (generated / "captions.ass").exists()
    assert (generated / "status.json").exists()
    assert (rendered / "final.mp4").exists()


def test_return_format_includes_required_keys(mocked_services, redirected_dirs):
    """Verify run_pipeline returns dict with success, video_id, stages_completed, error."""
    result = orchestrator.run_pipeline("prompt", job_id="job-format")

    # Check required keys are present
    assert "success" in result
    assert "video_id" in result
    assert "stages_completed" in result
    assert "error" in result

    # Check types
    assert isinstance(result["success"], bool)
    assert isinstance(result["video_id"], (str, type(None)))
    assert isinstance(result["stages_completed"], list)
    assert isinstance(result["error"], (str, type(None)))

    # Success case: all stages completed, no error
    assert result["success"] is True
    assert result["video_id"] == "vid-123"
    assert len(result["stages_completed"]) == 6
    assert result["error"] is None


def test_account_parameter_accepted(mocked_services, redirected_dirs):
    """Verify account parameter is accepted (for future multi-account support)."""
    # Should not raise an exception when account parameter is passed
    result = orchestrator.run_pipeline(
        "prompt",
        job_id="job-account",
        account={"oauth_token": "test-token"},
    )
    assert result["success"] is True


def test_cli_help_exits_zero_and_mentions_prompt():
    """`python -m app.orchestrator --help` must succeed and document --prompt."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.orchestrator", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "prompt" in proc.stdout.lower()


def test_stage_clips_uses_hook_flow_when_configured(monkeypatch, redirected_dirs, tmp_path):
    """USE_VIDEO=false + USE_VIDEO_HOOK=true → hook flow, not all-image, not all-video."""
    from app import config, orchestrator
    from app.services import visual_service, video_service

    monkeypatch.setattr(config, "USE_VIDEO", False)
    monkeypatch.setattr(config, "USE_VIDEO_HOOK", True)

    called = {"hook": 0, "all_video": 0, "all_images": 0}

    def fake_hook(script):
        called["hook"] += 1
        return [tmp_path / "scene_01.mp4", tmp_path / "scene_02.png", tmp_path / "scene_03.png"]

    def fake_all_video(script):
        called["all_video"] += 1
        return []

    def fake_all_images(script):
        called["all_images"] += 1
        return []

    monkeypatch.setattr(
        "app.services.video_service.generate_hook_plus_images", fake_hook
    )
    monkeypatch.setattr(
        "app.services.video_service.generate_all_scene_clips", fake_all_video
    )
    monkeypatch.setattr(
        "app.orchestrator.generate_all_scene_images", fake_all_images
    )

    script = _make_script("orch-hook-test")
    result = orchestrator._stage_clips(script)

    assert called == {"hook": 1, "all_video": 0, "all_images": 0}
    assert len(result) == 3
