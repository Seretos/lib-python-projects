# lib-python-projects

Domain library for the Seretos agent-plugin ecosystem. Bundles the
project-list model (whitelist of repos the agent can act on), the provider
abstraction (GitHub / GitLab / Azure DevOps wire layer), git-remote
auto-discovery, and a `load_projects` loader that builds on top of the
generic `lib-python-config` machinery.

Extracted from `agent-project-issues` so other plugins (e.g. release
automation, repo dashboards) can reuse the same project model without
pulling in the MCP server itself.

## Install

`lib-python-projects` depends on the sibling source library
`lib-python-config`. Install it as an editable dep first:

```bash
pip install -e ../lib-python-config
pip install -e .
```

## Public API

```python
from lib_python_projects import (
    ProjectConfig,
    Permissions,
    IssuesPermissions,
    PullsPermissions,
    ConfigDocument,
    ProjectsLoadResult,
    Provider,
    Source,
    load_projects,
    resolve_token,
)

# Providers are exposed through their sub-package:
from lib_python_projects.providers.github import GitHubProvider, GitHubError
from lib_python_projects.providers.gitlab import GitLabProvider, GitLabError
from lib_python_projects.providers.azuredevops import AzureDevOpsProvider
from lib_python_projects.providers.base import (
    Ticket, Comment, PullRequest, ReviewComment, Review,
    TicketFilters, PRFilters, RelationKind, Relation,
    StatusSpec, PipelineRun, FailingJob, PipelineFailure,
    TokenCapabilities, TokenCapabilityProvider,
    RelationKindUnsupported, RelationNotFound, normalize_timestamp,
    WRITABLE_RELATION_KINDS, READ_ONLY_RELATION_KINDS,
)
```

## Usage

```python
from pathlib import Path
from lib_python_projects import load_projects

result = load_projects(Path.cwd())

if result.state == "ok":
    for p in result.projects:
        print(p.id, p.provider, p.path, p.local_path)
elif result.state == "config_error":
    print("config broken:", result.error)
```

`load_projects` defaults match the `agent-project-issues` plugin (config
dir `.seretos/`, filename `project-issues.yml`, env vars
`PROJECT_ISSUES_CONFIG` / `PROJECT_ISSUES_PLUGIN_ROOT` /
`PROJECT_ISSUES_PLUGIN_CWD`). Other consumers pass their own values:

```python
result = load_projects(
    Path.cwd(),
    config_filename="release.yml",
    override_env="RELEASE_PLUGIN_CONFIG",
    plugin_root_env="RELEASE_PLUGIN_ROOT",
    search_env_vars=("RELEASE_PLUGIN_CWD", "CLAUDE_PROJECT_DIR"),
)
```

## What's new in 0.1.0

- `ProjectConfig.local_path: str | None = None` — the local checkout path
  for the project, when known. Auto-populated for `source="git-remote"`
  projects from the discovered git-repo root; readable from YAML for
  `source="config"` projects.
