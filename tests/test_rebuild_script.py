# path: tests/test_rebuild_script.py
"""The rebuild script must deploy through compose, not by hand.

Regression coverage for: rebuild.sh ran `docker run --name plexbie` with a
hand-maintained argument list that had drifted from docker-compose.yml. Running it
would have mounted 3 volumes where compose mounts 10 - dropping every /watch and
/library path, so bookshelf processing would stop - and published -p 8081:8081
instead of network_mode: host, re-exposing every /webhook/* route to the LAN.
"""
import re
from pathlib import Path

import conftest  # noqa: F401

SCRIPT = Path(conftest.PROJECT_ROOT) / "rebuild.sh"


def _code_lines():
    """Script lines with comments and blanks removed."""
    lines = []
    for raw in SCRIPT.read_text().splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(raw)
    return lines


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "rebuild.sh should be executable"


def test_no_hand_rolled_docker_run():
    """The whole class of drift: a second, divergent definition of the container."""
    offenders = [line for line in _code_lines() if re.search(r"\bdocker\s+run\b", line)]
    assert offenders == [], (
        "rebuild.sh must deploy via docker compose so the compose file stays the "
        f"single source of truth; found: {offenders}"
    )


def test_no_hand_rolled_volume_or_port_flags():
    """-v and -p here mean the mount/network config has been duplicated."""
    code = "\n".join(_code_lines())
    assert not re.search(r"\s-v\s+/mnt", code), "volume mounts belong in compose"
    assert not re.search(r"\s-p\s+\d+:\d+", code), (
        "publishing ports here would override network_mode: host and re-expose "
        "the webhook routes"
    )


def test_it_uses_compose_and_scopes_to_one_service():
    code = "\n".join(_code_lines())
    assert "docker compose" in code
    assert "--no-deps" in code, "must not touch other services in the project"
    assert "--force-recreate" in code


def test_it_tags_a_rollback_point():
    code = "\n".join(_code_lines())
    assert "docker tag plexbie:latest" in code, (
        "a rebuild should leave a way back to the previous image"
    )


def test_it_fails_fast():
    assert re.search(r"set -[euo]+", SCRIPT.read_text()), (
        "without set -e a failed build would be followed by a deploy anyway"
    )


def test_it_reports_the_startup_summary():
    """So a broken deploy is visible at the point of deploying."""
    code = "\n".join(_code_lines())
    assert "PLEXBIE DISCORD BOT - READY" in code
    assert "ERROR" in code


def test_compose_file_is_the_only_container_definition():
    """Nothing else in the repo should define how the bot container runs."""
    root = Path(conftest.PROJECT_ROOT)
    offenders = []
    for path in root.rglob("*.sh"):
        if "backups" in path.as_posix():
            continue
        for raw in path.read_text().splitlines():
            stripped = raw.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\bdocker\s+run\b.*--name\s+plexbie\b", raw):
                offenders.append(f"{path.name}: {stripped[:70]}")
    assert offenders == [], offenders
