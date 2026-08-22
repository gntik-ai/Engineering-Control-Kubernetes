# Native Claude worker

Claude Code runs as `claude-runner` using `--print --output-format stream-json`, `--json-schema`,
`--safe-mode`, `--no-session-persistence`, `--permission-mode dontAsk`, and a strict empty MCP
configuration. Read-only tasks cannot enable Edit, Write, or NotebookEdit. Events, structured
output, usage where exposed, timeouts, cancellation, and session ID are normalized.

The worker environment strips Anthropic API keys/tokens, custom base URLs, Bedrock, Vertex,
Foundry, OpenAI API keys, and AWS credentials. There is no API-paid fallback.

Authenticate independently:

```bash
ssh -t worker 'sudo -u claude-runner -H /usr/local/bin/claude auth login'
ssh worker 'sudo -u claude-runner -H /usr/local/bin/claude auth status >/dev/null'
```

Never copy controller Claude credentials. Missing authentication creates
`/var/lib/engineering-control/MANUAL_AUTH_REQUIRED_CLAUDE`; other platform work continues.
