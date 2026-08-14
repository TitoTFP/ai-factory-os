from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .models import Repository


class RepositoryError(RuntimeError):
    pass


REPOSITORY_ROOT = Path(__file__).resolve().parents[2] / "data" / "factories"
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+$")
_MAX_OUTPUT = 200_000
_MAX_COMMAND_SECONDS = 600
_GITHUB_API = "https://api.github.com"


def _component(value: str, label: str) -> str:
    value = str(value or "")
    if not _COMPONENT.fullmatch(value):
        raise RepositoryError(f"invalid {label}")
    return value


def validate_github_url(url: str, owner: str, name: str) -> str:
    parsed = urlparse(url)
    expected_path = f"/{owner}/{name}.git"
    if parsed.scheme != "https" or parsed.netloc.casefold() != "github.com" or parsed.path.rstrip("/") != expected_path:
        raise RepositoryError("repository remote must be the configured GitHub HTTPS repository")
    return url


def repository_root(factory_id: str, repository_id: str) -> Path:
    factory = _component(factory_id, "factory identifier")
    repository = _component(repository_id, "repository identifier")
    base = REPOSITORY_ROOT.resolve()
    factory_path = REPOSITORY_ROOT / factory
    if factory_path.is_symlink():
        raise RepositoryError("factory repository root cannot be a symlink")
    root = (factory_path / "repositories" / repository).resolve()
    if base not in root.parents or root == base:
        raise RepositoryError("repository path escapes the factory data boundary")
    for parent in (factory_path, factory_path / "repositories", factory_path / "repositories" / repository):
        if parent.is_symlink():
            raise RepositoryError("repository path cannot contain symlinks")
    return root


def safe_repository_path(worktree: str | Path, relative: str) -> Path:
    root = Path(worktree).resolve()
    raw = str(relative).strip()
    parts = Path(raw).parts
    if not raw or Path(raw).is_absolute() or ".." in parts:
        raise RepositoryError("repository path must be relative to the worktree")
    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise RepositoryError("repository path escapes the worktree boundary")
    if candidate.is_symlink():
        raise RepositoryError("repository path cannot target a symlink")
    return candidate


def _git_env(token: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    if token:
        # Keep the token out of argv, URLs, logs, and audit payloads.
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
        encoded = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: Basic {encoded}"
    return env


def _run_git(args: list[str], *, cwd: Path | None = None, token: str | None = None, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_git_env(token),
            capture_output=True,
            text=True,
            timeout=min(max(timeout, 1), _MAX_COMMAND_SECONDS),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepositoryError(f"git operation failed: {type(exc).__name__}") from exc
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "git command failed")[-2_000:]
        raise RepositoryError(f"git operation failed ({result.returncode}): {detail}")
    return result


def _repo_base(repository: Repository) -> Path:
    validate_github_url(repository.remote_url, repository.owner, repository.name)
    return repository_root(repository.factory_id, repository.id)


def ensure_checkout(repository: Repository, token: str | None = None) -> Path:
    base = _repo_base(repository)
    base.mkdir(parents=True, exist_ok=True)
    checkout = base / "checkout"
    if (checkout / ".git").exists():
        _run_git(["fetch", "--prune", "origin"], cwd=checkout, token=token)
        return checkout
    if checkout.exists():
        try:
            shutil.rmtree(checkout)
        except OSError as exc:
            raise RepositoryError("unable to clear the repository checkout") from exc
    _run_git(["clone", "--no-tags", repository.remote_url, str(checkout)], cwd=base, token=token, timeout=300)
    return checkout


def create_worktree(repository: Repository, cycle_id: str, token: str | None = None) -> tuple[Path, str, str]:
    checkout = ensure_checkout(repository, token)
    _run_git(["fetch", "--prune", "origin", repository.default_branch], cwd=checkout, token=token)
    base_sha = _run_git(["rev-parse", f"refs/remotes/origin/{repository.default_branch}"], cwd=checkout, token=token).stdout.strip()
    branch = f"factory-zero/{_component(cycle_id, 'cycle identifier')[:24]}"
    path = _repo_base(repository) / "worktrees" / _component(cycle_id, "cycle identifier")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RepositoryError("worktree path already exists")
    _run_git(["worktree", "add", "-b", branch, str(path), base_sha], cwd=checkout, token=token, timeout=180)
    return path, branch, base_sha


def cleanup_worktree(repository: Repository, worktree: str | Path, branch: str | None = None, token: str | None = None) -> None:
    checkout = _repo_base(repository) / "checkout"
    path = Path(worktree)
    if (checkout / ".git").exists() and path.exists():
        _run_git(["worktree", "remove", "--force", str(path)], cwd=checkout, token=token, check=False)
    if path.exists():
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise RepositoryError("unable to clean the repository worktree") from exc
    if branch and (checkout / ".git").exists():
        _run_git(["branch", "--delete", "--force", branch], cwd=checkout, token=token, check=False)


def read_file(worktree: str | Path, relative: str, *, max_chars: int = _MAX_OUTPUT) -> dict[str, Any]:
    path = safe_repository_path(worktree, relative)
    if not path.is_file():
        raise RepositoryError("repository file does not exist")
    return {"path": str(Path(relative)), "content": path.read_text(encoding="utf-8")[:max_chars]}


def search_files(worktree: str | Path, query: str, *, max_hits: int = 50) -> dict[str, Any]:
    query = str(query or "").strip().casefold()
    if not query:
        raise RepositoryError("repository search query is required")
    root = Path(worktree).resolve()
    hits: list[dict[str, Any]] = []
    ignored = {".git", "node_modules", ".venv", "dist", "build", "coverage", "__pycache__"}
    for path in root.rglob("*"):
        if len(hits) >= max_hits:
            break
        if any(part in ignored for part in path.parts):
            continue
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(lines, 1):
            if query in line.casefold():
                hits.append({"path": str(path.relative_to(root)), "line": number, "text": line[:500]})
                if len(hits) >= max_hits:
                    break
    return {"query": query, "hits": hits}


def write_file(worktree: str | Path, relative: str, content: str) -> dict[str, Any]:
    path = safe_repository_path(worktree, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise RepositoryError("repository file cannot be a symlink")
    path.write_text(str(content), encoding="utf-8")
    return {"path": str(Path(relative)), "bytes": len(str(content).encode("utf-8"))}


def git_status(worktree: str | Path) -> dict[str, Any]:
    root = Path(worktree).resolve()
    status = _run_git(["status", "--short"], cwd=root).stdout[-_MAX_OUTPUT:]
    diff = _run_git(["diff", "--stat"], cwd=root).stdout[-_MAX_OUTPUT:]
    return {"status": status, "diff_stat": diff}


def git_diff(worktree: str | Path) -> dict[str, Any]:
    root = Path(worktree).resolve()
    return {"diff": _run_git(["diff", "--no-ext-diff"], cwd=root).stdout[-_MAX_OUTPUT:]}


def git_commit(worktree: str | Path, message: str) -> str:
    root = Path(worktree).resolve()
    message = " ".join(str(message or "").split())[:240]
    if not message:
        raise RepositoryError("commit message is required")
    _run_git(["config", "user.name", "Factory Zero"], cwd=root)
    _run_git(["config", "user.email", "factory-zero@users.noreply.github.com"], cwd=root)
    _run_git(["add", "--all"], cwd=root)
    staged = _run_git(["diff", "--cached", "--quiet"], cwd=root, check=False)
    if staged.returncode == 0:
        raise RepositoryError("worktree has no changes to commit")
    _run_git(["commit", "-m", message], cwd=root, timeout=180)
    return _run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def push_branch(worktree: str | Path, branch: str, token: str) -> None:
    if not token:
        raise RepositoryError("GitHub token is required to push a branch")
    _run_git(["push", "--set-upstream", "origin", branch], cwd=Path(worktree).resolve(), token=token, timeout=300)


def run_configured_commands(repository: Repository, worktree: str | Path, kind: str) -> dict[str, Any]:
    commands = getattr(repository, f"{kind}_commands", None)
    if commands is None:
        raise RepositoryError(f"unsupported verification command kind: {kind}")
    results: list[dict[str, Any]] = []
    for raw in commands:
        if not isinstance(raw, list) or not raw or any(not isinstance(part, str) or not part or "\x00" in part for part in raw):
            raise RepositoryError("repository commands must be non-empty argv arrays")
        try:
            result = subprocess.run(
                raw,
                cwd=Path(worktree).resolve(),
                env={**os.environ, "CI": "1"},
                capture_output=True,
                text=True,
                timeout=_MAX_COMMAND_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepositoryError(f"configured {kind} command failed: {type(exc).__name__}") from exc
        output = ((result.stdout or "") + (result.stderr or ""))[-_MAX_OUTPUT:]
        results.append({"command": raw, "returncode": result.returncode, "output": output})
        if result.returncode:
            return {"kind": kind, "passed": False, "commands": results}
    return {"kind": kind, "passed": True, "commands": results}


def _github_headers(token: str) -> dict[str, str]:
    if not token:
        raise RepositoryError("GitHub token is required")
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-factory-os-factory-zero",
    }


async def github_request(token: str, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.startswith("/repos/") or ".." in path or "//" in path:
        raise RepositoryError("invalid GitHub API path")
    async with httpx.AsyncClient(base_url=_GITHUB_API, timeout=45, trust_env=False) as client:
        response = await client.request(method, path, headers=_github_headers(token), json=payload)
    if response.status_code >= 400:
        detail = response.text[:500].replace(token, "[REDACTED]")
        raise RepositoryError(f"GitHub API {response.status_code}: {detail}")
    if not response.content:
        return {}
    try:
        data = response.json()
    except ValueError as exc:
        raise RepositoryError("GitHub API returned invalid JSON") from exc
    return data if isinstance(data, dict) else {"items": data}


async def create_pull_request(repository: Repository, token: str, branch: str, title: str, body: str) -> dict[str, Any]:
    return await github_request(
        token,
        "POST",
        f"/repos/{repository.owner}/{repository.name}/pulls",
        {"title": title[:240], "body": body[:20_000], "head": branch, "base": repository.default_branch, "draft": False},
    )


def _pull_number(value: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise RepositoryError("invalid pull request number") from exc
    if number <= 0:
        raise RepositoryError("invalid pull request number")
    return number


async def merge_pull_request(repository: Repository, token: str, number: int, head_sha: str) -> dict[str, Any]:
    pull_number = _pull_number(number)
    return await github_request(
        token,
        "PUT",
        f"/repos/{repository.owner}/{repository.name}/pulls/{pull_number}/merge",
        {"sha": head_sha, "merge_method": "squash", "commit_title": f"Factory Zero: {head_sha[:12]}"},
    )


async def pull_request(repository: Repository, token: str, number: int) -> dict[str, Any]:
    pull_number = _pull_number(number)
    return await github_request(token, "GET", f"/repos/{repository.owner}/{repository.name}/pulls/{pull_number}")


async def check_runs(repository: Repository, token: str, sha: str) -> dict[str, Any]:
    return await github_request(token, "GET", f"/repos/{repository.owner}/{repository.name}/commits/{sha}/check-runs")
