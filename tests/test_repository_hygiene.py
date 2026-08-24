from __future__ import annotations

from pathlib import Path


def test_sensitive_runtime_files_are_not_present():
    root = Path(__file__).resolve().parents[1]
    forbidden_names = {
        ".env",
        "config.yml",
        "cookies.txt",
        "secrets.yml",
        "secrets.yaml",
    }
    present = {
        path.name
        for path in root.rglob("*")
        if ".git" not in path.parts and ".venv" not in path.parts
    }
    assert not forbidden_names & present


def test_private_workspace_identifiers_are_absent():
    root = Path(__file__).resolve().parents[1]
    forbidden = (
        "Social" + "Scrapers",
        "Research" + "Memory",
        "Invest" + "meows",
        "Toshi" + "Terminal",
    )
    offenders = []
    for path in root.rglob("*"):
        if not path.is_file() or ".git" in path.parts or ".venv" in path.parts:
            continue
        try:
            text = path.read_text("utf-8")
        except UnicodeDecodeError:
            continue
        if any(marker in text for marker in forbidden):
            offenders.append(path.relative_to(root))
    assert offenders == []
