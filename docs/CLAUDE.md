# Native Claude worker

Claude Code runs as `claude-runner` using `--print --output-format stream-json`, `--json-schema`,
`--safe-mode`, `--no-session-persistence`, `--permission-mode dontAsk`, identical bounded
`--tools`/`--allowed-tools` lists, and a strict empty MCP configuration. No dangerous permission
bypass is used. Read-only tasks cannot enable Edit, Write, or NotebookEdit. Events, structured
output, usage where exposed, timeouts, cancellation, and session ID are normalized.

The worker environment strips Anthropic API keys/tokens, custom base URLs, Bedrock, Vertex,
Foundry, OpenAI API keys, and AWS credentials. There is no API-paid fallback.

The worker identity is independently subscription-authenticated. Verify without displaying auth
material:

```bash
ssh worker 'sudo -u claude-runner -H env -i HOME=/var/lib/engineering-control/identities/claude-runner PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/claude auth status >/dev/null'
```

Never copy controller Claude credentials. If authentication expires, the bootstrap creates
`/var/lib/engineering-control/MANUAL_AUTH_REQUIRED_CLAUDE` and other platform work continues.

Run `sudo /opt/engineering-control/source/scripts/validate-native-providers.sh` after both native
logins. It passed both maker/checker directions in isolated synthetic worktrees and persisted only
normalized redacted evidence.
