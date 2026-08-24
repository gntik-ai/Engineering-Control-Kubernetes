# Native Codex worker

Codex runs as `codex-runner` through the native subscription-authenticated CLI. The adapter uses
noninteractive `codex exec --json`, an output JSON Schema, ephemeral sessions, explicit working
directory, and either read-only or workspace-write sandboxing. JSONL events, token usage where
exposed, timeouts, cancellation, and provider session ID are normalized.

The execution environment removes `OPENAI_API_KEY`, custom OpenAI base URLs, Anthropic keys,
Bedrock/Vertex/Foundry switches, and AWS credentials. Paid API fallback is disabled. Rate limits
produce `RATE_LIMITED` plus the seven-file handoff bundle.

Authentication must be established independently on worker:

```bash
ssh -t worker 'sudo -u codex-runner -H /usr/local/bin/codex login'
ssh worker 'sudo -u codex-runner -H env -i HOME=/var/lib/engineering-control/identities/codex-runner PATH=/usr/local/bin:/usr/bin:/bin /usr/local/bin/codex login status >/dev/null'
```

Never copy `.codex/auth.json` from controller. A missing login leaves
`/var/lib/engineering-control/MANUAL_AUTH_REQUIRED_CODEX` and does not block scanner, browser,
schema, API, or orchestration installation.

Each native call uses `/srv/engineering/tmp/codex/<run-id>/` as its mode-`0700` bounded scratch
parent and an ephemeral child for the individual attempt. The daily retention workflow may remove
only that exact per-run parent after the Run is terminal and expired; active and rate-limited runs
are always protected.

Official structured CLI behavior is documented in the [Codex CLI command reference](https://developers.openai.com/codex/cli/reference/).

After both provider logins, run the bounded two-direction acceptance on worker:

```bash
sudo /opt/engineering-control/source/scripts/validate-native-providers.sh
```

It uses synthetic worktrees only, performs Codex maker -> Claude checker and Claude maker -> Codex
checker, persists a redacted SHA256 evidence manifest, and never touches product repositories. It
also exports a mode-restricted copy under
`/home/hermes/engineering-bootstrap/provider-acceptance/` for the controller's C-01 gate. Fetch
that copy with `scripts/fetch-provider-acceptance.sh`; never copy either provider's authentication
files.
