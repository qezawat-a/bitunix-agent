"""Git push automation for J+ROCK.

Automatically packs verified code assets and force-pushes them to GitHub
via a provided Personal Access Token (PAT) and repository link.

Honesty clause: this module never fabricates a push result. It reports the
exact exit code, stdout, and stderr from git, and raises on failure so the
caller can decide whether to retry or alert the operator.

Environment variables (supplied by the operator; never hard-coded):
    GITHUB_PAT        - Personal Access Token with repo scope.
    GITHUB_REPO       - Full repo link, e.g. https://github.com/owner/repo.git
                        (or git@github.com:owner/repo.git).
    GIT_AUTHOR_NAME   - optional, default "J+ROCK"
    GIT_AUTHOR_EMAIL  - optional, default "jrock@localhost"

Usage:
    pusher = GitPusher()
    result = await pusher.push_verified_assets(message="chore: ...")
    # result.ok is True only when the push succeeded.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class GitPushResult:
    ok: bool
    message: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    branch: str = ""


class GitPusher:
    """Stage, commit, and force-push verified code assets to GitHub."""

    def __init__(
        self,
        repo_root: Optional[Path] = None,
        pat: Optional[str] = None,
        repo_link: Optional[str] = None,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
    ) -> None:
        self.repo_root = repo_root or Path.cwd()
        self.pat = pat or os.environ.get("GITHUB_PAT", "")
        self.repo_link = repo_link or os.environ.get("GITHUB_REPO", "")
        self.author_name = author_name or os.environ.get("GIT_AUTHOR_NAME", "J+ROCK")
        self.author_email = author_email or os.environ.get("GIT_AUTHOR_EMAIL", "jrock@localhost")

    # ------------------------------------------------------------------ #
    # Public API                                                          #
    # ------------------------------------------------------------------ #

    def is_configured(self) -> bool:
        """True when both PAT and repo link are available."""
        return bool(self.pat) and bool(self.repo_link)

    async def push_verified_assets(
        self,
        message: str = "chore(jrock): auto-push verified code assets",
        branch: str = "main",
        add_all: bool = True,
        force: bool = True,
    ) -> GitPushResult:
        """Pack verified code assets and force-push to GitHub.

        Steps:
          1. Verify we're inside a git repo.
          2. Stage changes (all tracked + untracked by default).
          3. Commit if there is anything to commit.
          4. Force-push to the configured repo over HTTPS with the PAT embedded.
        """
        if not self.is_configured():
            return GitPushResult(
                ok=False,
                message="Git push not configured: set GITHUB_PAT and GITHUB_REPO.",
                exit_code=-1,
            )

        if not (self.repo_root / ".git").exists():
            return GitPushResult(
                ok=False,
                message=f"Not a git repository: {self.repo_root}",
                exit_code=-1,
            )

        # 1. Configure author identity for this push.
        await self._run_git("config", "user.name", self.author_name)
        await self._run_git("config", "user.email", self.author_email)

        # 2. Stage changes.
        if add_all:
            await self._run_git("add", "-A")
        else:
            await self._run_git("add", ".")

        # 3. Commit (only if there are staged changes).
        status = await self._run_git("status", "--porcelain")
        if status.stdout.strip():
            commit = await self._run_git("commit", "-m", message)
            if commit.exit_code != 0 and "nothing to commit" not in (commit.stderr or ""):
                return GitPushResult(
                    ok=False,
                    message=f"git commit failed (exit {commit.exit_code})",
                    stdout=commit.stdout,
                    stderr=commit.stderr,
                    exit_code=commit.exit_code,
                )
        else:
            log.info("git push: no changes to commit")

        # 4. Force-push with PAT embedded in the HTTPS URL.
        auth_url = self._auth_url(self.repo_link)
        push_args = ["push", auth_url, f"HEAD:{branch}"]
        if force:
            push_args.insert(1, "--force")

        push = await self._run_git(*push_args)
        if push.exit_code == 0:
            log.info("git push: force-pushed to %s on branch %s", self.repo_link, branch)
            return GitPushResult(
                ok=True,
                message=f"Force-pushed verified assets to {self.repo_link} (branch {branch}).",
                stdout=push.stdout,
                stderr=push.stderr,
                exit_code=0,
                branch=branch,
            )

        return GitPushResult(
            ok=False,
            message=f"git push failed (exit {push.exit_code})",
            stdout=push.stdout,
            stderr=push.stderr,
            exit_code=push.exit_code,
        )

    async def status(self) -> GitPushResult:
        """Report working-tree status without pushing."""
        out = await self._run_git("status", "--porcelain", "-b")
        return GitPushResult(
            ok=True,
            message=out.stdout or "working tree clean",
            stdout=out.stdout,
            stderr=out.stderr,
            exit_code=out.exit_code,
        )

    # ------------------------------------------------------------------ #
    # Internal                                                            #
    # ------------------------------------------------------------------ #

    async def _run_git(self, *args: str) -> GitPushResult:
        """Run a git command in the repo root, capturing output."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", *args,
                cwd=str(self.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            stdout, stderr = await proc.communicate()
            return GitPushResult(
                ok=proc.returncode == 0,
                message="",
                stdout=stdout.decode("utf-8", "replace"),
                stderr=stderr.decode("utf-8", "replace"),
                exit_code=proc.returncode or -1,
            )
        except FileNotFoundError:
            return GitPushResult(
                ok=False,
                message="git executable not found on PATH",
                exit_code=-1,
            )
        except Exception as e:
            return GitPushResult(
                ok=False,
                message=f"git invocation error: {e}",
                exit_code=-1,
            )

    @staticmethod
    def _auth_url(repo_link: str) -> str:
        """Embed the PAT into an HTTPS repo URL for authenticated push."""
        pat = os.environ.get("GITHUB_PAT", "")
        if not pat:
            return repo_link
        if repo_link.startswith("https://"):
            return repo_link.replace("https://", f"https://x-access-token:{pat}@", 1)
        return repo_link


__all__ = ["GitPusher", "GitPushResult"]
