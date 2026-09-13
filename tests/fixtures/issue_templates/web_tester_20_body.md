#ai-generated

During a first-run exploration of the agent-web-tester MCP, the very first tool call failed:

```
browser_navigate({ url: "https://www.jako.com/de-de/" })
```

Error:
```
Error: Browser "chrome-for-testing" is not installed; expected executable at
C:\Users\<user>\AppData\Local\ms-playwright\chromium-1237\chrome-win64\chrome.exe.
Run `npx @playwright/mcp install-browser chrome-for-testing` to install
```

The error message itself is clear and actionable (good), but there is no MCP tool to
recover from it. An agent restricted to the MCP tool surface (no shell access) has no
path forward except to hand the exact shell command back to a human and wait — the
plugin cannot self-heal a missing prerequisite on first use.

Suggested fix: have the MCP server detect a missing browser binary on startup / first
`browser_navigate` call and either (a) auto-run the install step transparently before
proceeding, or (b) expose a dedicated tool (e.g. `ensure_browser_installed` /
`install_browser`) so an agent can resolve the prerequisite itself without shelling out.

Workaround used in this session: ran `npx @playwright/mcp install-browser
chrome-for-testing` manually outside the MCP tool surface, which downloaded ~310 MiB
(Chrome for Testing + Chrome Headless Shell) and resolved the issue for subsequent calls.