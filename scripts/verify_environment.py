from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys


def main() -> int:
    missing = [name for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL") if not os.getenv(name)]
    docker = next((name for name in ("docker", "podman", "nerdctl") if shutil.which(name)), None)
    print(f"container_runtime={docker or 'missing'}")
    print(f"postgres_client={'available' if shutil.which('psql') else 'missing'}")
    try:
        print(f"postgres_server={'ready' if subprocess.run(['pg_isready'], capture_output=True, timeout=5).returncode == 0 else 'not-ready'}")
    except (FileNotFoundError, subprocess.SubprocessError):
        print("postgres_server=unknown")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        print(f"{name}={'set' if os.getenv(name) else 'missing'}")
    try:
        print(f"provider_dns={socket.gethostbyname('api.openai.com')}")
    except OSError:
        print("provider_dns=unavailable")
    if missing:
        print(f"live_provider_gate=blocked ({', '.join(missing)})")
        return 2
    if not docker:
        print("deployment_gate=blocked (no Docker-compatible runtime)")
        return 3
    print("environment_gate=ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
