# Environment variables

Defaults fail safe: an unset `BRIDGE_ENV` means **production**; production refuses dev
auth and SQLite at startup; live provider calls are off unless explicitly enabled.
`python -m bridge_control check-config` validates and summarizes the configuration.

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_ENV` | `production` | `production`, `staging`, `development`, `test` or `local`. `local` is a self-run team server (`python -m bridge_runtime.team start` sets it): owner session, signed-out invite redemption and the team bootstrap exist only here. Dev auth is still refused. |
| `BRIDGE_DATABASE_URL` | dev/test: `sqlite:///.bridge-dev/bridge.db`; local: `sqlite:///<BRIDGE_HOME>/bridge.db`; otherwise **required** | SQLAlchemy URL. PostgreSQL for staging/production (`postgresql+psycopg://…`). SQLite is rejected outside development/test/local. |
| `BRIDGE_DEV_AUTH` | off | `1` trusts the `X-Bridge-Dev-User: <email>` header (auto-creates the user). **Startup fails** if set while `BRIDGE_ENV` is not development/test. Every response then carries `X-Bridge-Dev-Auth: enabled` and the server logs a warning. |
| `BRIDGE_LIVE_PROVIDER_CALLS` | off (also when `BRIDGE_ENV=local`) | `1` allows HTTP adapters to contact model providers (including local runtimes). Off = every adapter raises `disabled` before sending anything. Turning it on may incur charges. |
| `BRIDGE_MODEL_RATES` | empty | JSON `{"<model_id>": [input_micros_per_Mtok, output_micros_per_Mtok]}`. Live calls to a model with no rate are refused so budgets stay enforceable. No prices are built in. |
| `BRIDGE_ARTIFACT_DIR` | `.bridge-dev/artifacts`; local: `<BRIDGE_HOME>/files` | Directory for the development `LocalBlobStore`. This checkout provides local storage. |
| `BRIDGE_EVENT_RETENTION_HOURS` | `168` | Events older than this are pruned; older cursors get `410 cursor_expired`. |
| `BRIDGE_SESSION_TTL_HOURS` | `12`; local: `720` (30 days) | Lifetime of session and agent credentials, including the bridge credential an invite returns. |
| `BRIDGE_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` (`team start`: `--public-url`, else `http://127.0.0.1:<port>`) | The address teammates use. Invite links are `{BRIDGE_PUBLIC_BASE_URL}/#join={token}`. |
| `BRIDGE_HOME` | Windows `%LOCALAPPDATA%\Bridge`, macOS `~/Library/Application Support/Bridge`, else `$XDG_DATA_HOME/bridge` or `~/.local/share/bridge` | Data folder of a local team server (database and files). |
| `BRIDGE_BIND_HOST` | `127.0.0.1` | Interface a local team server listens on (`team start --bind`). Any non-loopback value needs `BRIDGE_PUBLIC_BASE_URL` to be `https://…`, **or** `BRIDGE_PRIVATE_NETWORK=1` with a private address (10/8, 172.16/12, 192.168/16, Tailscale 100.64/10, fd00::/8) given as an IP literal; host names such as Tailscale MagicDNS are refused for now. `team start` also refuses a non-loopback `--bind` without `--public-url`. |
| `BRIDGE_PRIVATE_NETWORK` | off | `1` allows plain HTTP on such a private address (`team start --private-network`); the server prints that anyone on the network can read the traffic. |
| Provider keys, e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` | unset | Read **only** by the runtime when a provider connection's `secret_ref` is `env:<NAME>`. Never stored in the database, returned by the API, or logged. |
| `PYTHONPATH` | — | Until packaging is integrated: `services/control-api/src` and `services/agent-runtime/src` for `python -m ...`. The launchers `services/agent-runtime/bin/bridge_team.py` and `bin/bridge_mcp.py` set up the path themselves and need no `PYTHONPATH`. |

## Bridge (`python -m bridge_mcp`)

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_CONFIG` | `~/.bridge/bridge.json` | The bridge settings `join` writes (mode 0600 where supported) and `serve`/`hook` read. `team start` writes the host's own here too. The hook keeps its place next to it: `hook-<project>-<session_id>.cursor` per Claude Code session (the id comes from the hook's stdin JSON) and `hook-<project>.cursor`, which new sessions start from. |
| `BRIDGE_URL`, `BRIDGE_TOKEN`, `BRIDGE_PROJECT`, `BRIDGE_SEAT_LABEL` | from the config file | Override the server address, agent credential (`ha_…`), project id and this session's seat label. Without either, a session's seat label is the name of the folder Claude Code runs it in (`join` saves a label only with `--label`; `team start` saves none). |
