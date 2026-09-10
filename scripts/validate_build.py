"""Validate project structure and Python syntax for npm run build."""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_FILES = [
    "app.py",
    "transcription_pipeline.py",
    "drive_auth.py",
    "requirements.txt",
    "README.md",
    "config.example.env",
    "package.json",
]


def check_files() -> list[str]:
    missing = [name for name in REQUIRED_FILES if not (ROOT / name).exists()]
    return missing


def check_syntax() -> list[str]:
    errors: list[str] = []
    names = [
        "app.py",
        "transcription_pipeline.py",
        "drive_auth.py",
        "scripts/validate_build.py",
    ]
    studio = ROOT / "pitch-studio" / "backend"
    if studio.exists():
        names.extend(str(path.relative_to(ROOT)) for path in studio.rglob("*.py"))
    for name in names:
        path = ROOT / name
        if not path.exists():
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{name}: {exc}")
    return errors


def check_imports() -> list[str]:
    """Import local modules that do not require optional heavy deps at import time."""
    errors: list[str] = []
    for module_name, path in (
        ("drive_auth", ROOT / "drive_auth.py"),
    ):
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            assert spec and spec.loader
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "extract_file_id"):
                errors.append(f"{module_name}: missing extract_file_id")
            elif module.extract_file_id("https://drive.google.com/file/d/abc123XYZ_-/view") != "abc123XYZ_-":
                errors.append(f"{module_name}: extract_file_id failed")
            if not hasattr(module, "get_public_file_metadata"):
                errors.append(f"{module_name}: missing get_public_file_metadata")
            if not hasattr(module, "open_public_drive_stream"):
                errors.append(f"{module_name}: missing open_public_drive_stream")
            if not hasattr(module, "download_oauth_file"):
                errors.append(f"{module_name}: missing download_oauth_file")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{module_name}: {exc}")
    return errors


def main() -> int:
    problems: list[str] = []
    missing = check_files()
    if missing:
        problems.append("Missing files: " + ", ".join(missing))
    problems.extend(check_syntax())
    problems.extend(check_imports())
    studio_backend = ROOT / "pitch-studio" / "backend"
    if not (studio_backend / "main.py").exists():
        problems.append("pitch-studio/backend/main.py missing")
    if not (ROOT / "pitch-studio" / "frontend" / "package.json").exists():
        problems.append("pitch-studio/frontend/package.json missing")
    try:
        python = ROOT / ".venv" / "bin" / "python"
        exe = str(python) if python.exists() else sys.executable
        result = subprocess.run(
            [
                exe,
                "-m",
                "unittest",
                "backend.tests.test_pipeline",
                "backend.tests.test_deck",
                "backend.tests.test_sync_assets",
                "backend.tests.test_transcripts",
                "backend.tests.test_extract",
                "backend.tests.test_resolver_persona",
                "backend.tests.test_media_index",
                "backend.tests.test_worker",
                "backend.tests.test_database",
                "backend.tests.test_youtube_apify",
            ],
            cwd=ROOT / "pitch-studio",
            env={**os.environ, "PYTHONPATH": str(ROOT / "pitch-studio")},
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            problems.append("pitch-studio tests failed:\n" + result.stderr + result.stdout)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"pitch-studio tests: {exc}")

    if problems:
        print("BUILD FAILED")
        for problem in problems:
            print(f" - {problem}")
        return 1

    print("BUILD OK")
    print(" - required files present")
    print(" - Python syntax valid")
    print(" - drive_auth extract_file_id ok")
    print(" - pitch-studio backend tests ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
