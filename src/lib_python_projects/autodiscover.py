"""Git-remote → `ProjectConfig` auto-discovery.

Reads `.git/config` walking outward from a directory, parses the `origin`
remote URL, and synthesises a `ProjectConfig` for github.com / gitlab.com /
dev.azure.com URLs.

This is the implementation behind the "CWD repo is always reachable" rule:
even if the active project-list config omits the repo the agent is sitting
in, auto-discovery appends a `source="git-remote"` entry so basic
inspection (`list_projects`, `list_tickets`) still works.
"""
from __future__ import annotations

import logging
import re
from configparser import ConfigParser
from pathlib import Path

from lib_python_config import walk_up

from lib_python_projects.models import ProjectConfig

log = logging.getLogger("lib_python_projects.autodiscover")


_SSH_RE = re.compile(r"^git@([^:]+):(.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(r"^https?://(?:[^/@]+@)?([^/]+)/(.+?)(?:\.git)?/?$")


def _parse_remote_url(url: str) -> tuple[str, str] | None:
    """Return (host, path) where `path` is owner/repo or group/sub/repo."""
    url = url.strip()
    m = _SSH_RE.match(url)
    if m:
        return m.group(1).lower(), m.group(2)
    m = _HTTPS_RE.match(url)
    if m:
        return m.group(1).lower(), m.group(2)
    return None


def _normalise_azure_devops_path(host: str, path: str) -> str | None:
    """Convert a host+path parsed from a git remote into the canonical
    `organization/project/repository` form, or `None` if this doesn't
    look like an Azure DevOps remote.
    """
    segs = [s for s in path.split("/") if s]
    if host == "dev.azure.com":
        # /<org>/<project>/_git/<repo>
        if len(segs) >= 4 and segs[2] == "_git":
            return f"{segs[0]}/{segs[1]}/{segs[3]}"
        return None
    if host == "ssh.dev.azure.com":
        # v3/<org>/<project>/<repo>
        if len(segs) >= 4 and segs[0] == "v3":
            return f"{segs[1]}/{segs[2]}/{segs[3]}"
        return None
    return None


def _autodiscover_from_git(start: Path) -> ProjectConfig | None:
    git_config_path = walk_up(start, (".git/config",))
    if not git_config_path:
        return None
    cp = ConfigParser()
    try:
        cp.read(git_config_path, encoding="utf-8-sig")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not parse %s: %s", git_config_path, exc)
        return None
    section = 'remote "origin"'
    if section not in cp.sections():
        log.info("no [remote \"origin\"] in %s — skipping auto-discovery", git_config_path)
        return None
    url = cp.get(section, "url", fallback=None)
    if not url:
        return None
    parsed = _parse_remote_url(url)
    if not parsed:
        log.info("origin remote URL not recognised: %s", url)
        return None
    host, path = parsed
    if host == "github.com":
        if "/" not in path:
            return None
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="github",
            path=path,
            token_env="GITHUB_TOKEN",
            source="git-remote",
        )
    if host == "gitlab.com":
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="gitlab",
            path=path,
            token_env="GITLAB_TOKEN",
            source="git-remote",
        )
    # Azure DevOps remote shapes:
    #   HTTPS:  https://dev.azure.com/{org}/{project}/_git/{repo}
    #   HTTPS:  https://{org}@dev.azure.com/{org}/{project}/_git/{repo}
    #   SSH:    git@ssh.dev.azure.com:v3/{org}/{project}/{repo}
    # All normalise to canonical "organization/project/repository".
    ado_path = _normalise_azure_devops_path(host, path)
    if ado_path:
        return ProjectConfig(
            id="_auto",
            description=f"Auto-discovered from git remote ({url.strip()})",
            provider="azuredevops",
            path=ado_path,
            token_env="AZURE_DEVOPS_TOKEN",
            source="git-remote",
        )
    log.info(
        "auto-discovery skipped — host %s is not github.com / gitlab.com / "
        "dev.azure.com",
        host,
    )
    return None
