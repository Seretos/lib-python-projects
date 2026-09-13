#ai-generated

## Observed problem (user-facing behavior)

On a machine with no pre-existing Playwright browser cache, the very first call to the `playwright` MCP server's `browser_navigate` tool fails:

```
Error: Browser "chrome-for-testing" is not installed; expected executable at
<...>\ms-playwright\chromium-<rev>\chrome-win64\chrome.exe.
Run `npx @playwright/mcp install-browser chrome-for-testing` to install
```

An agent that only has the MCP tool surface available (no shell access — the normal situation for a consuming agent) has no path forward. It can only relay the shell command to a human and wait. This makes the plugin unusable end-to-end on a fresh machine without a manual, out-of-band step.

## Testable acceptance criterion

On a machine/state where the Playwright browser cache is absent (or does not contain whatever revision the running server needs), calling `browser_navigate` through the plugin's actual exposed MCP tool surface — no manual shell command, no assumed tool name, no prior setup — returns a valid navigation result (or at minimum a single well-defined recovery call that a black-box agent can discover and invoke from the tool surface it was actually given).

This must be demonstrated with a real call against the real running server in that state. A prose/documentation change that references a tool name is not sufficient evidence and does not satisfy this criterion by itself — confirm any referenced tool actually appears in a live `tools/list` response before relying on it in any plan.

## Prior failed attempt — read before implementing

#20 (closed as completed, first attempt via PR #23) added only instruction text to `agents/page-scanner.md` / `skills/web-tester/SKILL.md` telling a subagent to call a tool named `browser_install`. That tool does not exist anywhere in the running server's tool surface (verified: absent from a live `tools/list`). The fix was validated purely by markdown/string assertions (`validate_agents.py`) and CI's `lint` workflow — none of which ever invoked the real MCP tools. Result: CI green, merged, closed as completed — and the original problem is completely unfixed.

The same unverified assumption ("the server's own `browser_install` tool handles first-run provisioning") was already present, also unverified, in the original foundational ticket #1's clarification phase — this is the second time it has gone unchecked through the full pipeline. Whatever this ticket's implementation ends up being, it must be exercised against the live server as part of its own driving test, not just asserted in prose.