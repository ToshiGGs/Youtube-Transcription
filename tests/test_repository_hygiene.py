from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

SENSITIVE_ARTIFACTS = (
    "credentials.json",
    "application_default_credentials.json",
    "service-account-prod.json",
    "prod-service-account.json",
    "token.json",
    "private.pem",
    "private.key",
    "certificate.p12",
    "certificate.pfx",
    "truststore.jks",
    "private.keystore",
    "Cookies",
    "Cookies-journal",
    "Cookies-wal",
    "Cookies-shm",
    "Cookies-lock",
    "cookies.sqlite-wal",
)


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
        "invest" + "meows",
        "Stin" + "stack",
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


@pytest.mark.parametrize("filename", SENSITIVE_ARTIFACTS)
def test_common_sensitive_artifact_names_are_excluded(filename):
    root = Path(__file__).resolve().parents[1]
    for ignore_file in (".gitignore", ".dockerignore"):
        patterns = [
            line.strip()
            for line in (root / ignore_file).read_text("utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "!"))
        ]
        assert any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)
