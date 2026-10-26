"""VAD model verification against MANIFEST.json."""

from __future__ import annotations

from pathlib import Path

import pytest
from media_helpers import write_models_dir

from va_media.vad_model import VadModelError, VadStatus, verify_vad_model


async def test_matching_model_verifies(models_dir: Path) -> None:
    await verify_vad_model(models_dir)


async def test_missing_model_file(models_dir: Path) -> None:
    (models_dir / "silero_vad.onnx").unlink()
    with pytest.raises(VadModelError, match="missing"):
        await verify_vad_model(models_dir)


async def test_checksum_mismatch(tmp_path: Path) -> None:
    root = write_models_dir(tmp_path / "m", b"tampered", sha256="0" * 64)
    with pytest.raises(VadModelError, match="sha256"):
        await verify_vad_model(root)


async def test_model_not_listed(tmp_path: Path) -> None:
    root = write_models_dir(tmp_path / "m", b"x", name="other_model")
    with pytest.raises(VadModelError, match="not listed"):
        await verify_vad_model(root)


@pytest.mark.parametrize("manifest", [None, "{not json", '["models"]'])
async def test_unreadable_or_malformed_manifest(tmp_path: Path, manifest: str | None) -> None:
    if manifest is not None:
        (tmp_path / "MANIFEST.json").write_text(manifest, encoding="utf-8")
    with pytest.raises(VadModelError):
        await verify_vad_model(tmp_path)


@pytest.fixture
def repo_models() -> tuple[Path, bool]:
    root = Path(__file__).resolve().parents[3] / "models"
    return root, (root / "silero_vad.onnx").exists()


async def test_repository_manifest_entry_is_found(repo_models: tuple[Path, bool]) -> None:
    root, present = repo_models
    if not present:
        with pytest.raises(VadModelError, match="missing"):
            await verify_vad_model(root)
        return
    await verify_vad_model(root)


async def test_status_check() -> None:
    status = VadStatus()
    with pytest.raises(VadModelError, match="not loaded"):
        await status.check()
    status.error = "models/silero_vad.onnx is missing"
    with pytest.raises(VadModelError, match="missing"):
        await status.check()
    status.loaded = True
    await status.check()
