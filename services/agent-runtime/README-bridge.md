# Bridge setup guide

Bridge connects Codex, Claude Code, and other local MCP clients to a shared team workspace. Each connected session can view the board, claim files before editing, share updates, and pick up tasks.

A team member hosts the Bridge server and invites the rest of the team with a private link.

## Before you start

Everyone needs a local MCP client, a clone of this repository, and Python 3.12. Run the
commands below from the repository root.

- Teammates need Python 3.12 with httpx: `python -m pip install httpx` (ideally in a venv).
- The host also needs the server's packages:
  `python -m pip install -r services/control-api/requirements.lock`.

## Start a workspace and invite your team

**1. The host starts the team** (leave the window open while you work):

```
python services/agent-runtime/bin/bridge_team.py start --name "Product Engineering"
```

This creates the team the first time, prepares the host’s agent credentials, and prints an
**invite link** plus the exact command teammates should run. Press Ctrl+C to stop the
server. The team's data stays on the host's computer.

**2. Each teammate joins** with the link the host sent:

```
python services/agent-runtime/bin/bridge_mcp.py join "<invite link>" --name "Alex"
```

This saves your connection in `~/.bridge/bridge.json` and prints setup commands for
Codex and Claude Code, plus an optional Claude Code hook. Treat this file like a password.

Join once per person. Running `join` again (for example on a second laptop) creates a
**second person** with the same name. To use another computer, copy your
`~/.bridge/bridge.json` to it, or ask the host for a fresh link and join with a different
name ("Alex laptop").

### Codex

Run the printed Codex line. Use the paths printed on your own machine.

```sh
codex mcp add bridge -- "C:/Python312/python.exe" "C:/Users/alex/Bridge/services/agent-runtime/bin/bridge_mcp.py" serve
```

Run `codex mcp list`, open a fresh session, and ask it to call the Bridge board tool.
The Claude Code hook below is optional and specific to that client. Codex can read
the board and inbox through MCP tools. [Codex MCP documentation](https://developers.openai.com/codex/mcp)

### Claude Code

Run the printed Claude Code line exactly as printed.
Run it once: `--scope user` makes Bridge available in every project folder. It names your
Python and the bridge by their full paths, in quotes and with forward slashes, so it works
from any folder and in any terminal:

```
claude mcp add --scope user bridge -- "C:/Python312/python.exe" "C:/Users/alex/Bridge/services/agent-runtime/bin/bridge_mcp.py" serve
```

Then paste the printed snippet into your Claude Code settings (`~/.claude/settings.json`).
It is optional but recommended: before each prompt it tells your agent what teammates did
since the last one ("Bridge updates: ..."). Each Claude Code session keeps its own place, so
every session sees each update once. The snippet looks like this (use the one printed for
you):

```
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"C:/Python312/python.exe\" \"C:/Users/alex/Bridge/services/agent-runtime/bin/bridge_mcp.py\" hook"
          }
        ]
      }
    ]
  }
}
```

If your settings file already has a `"hooks"` section, add the `UserPromptSubmit` entry to
it instead of pasting a second one. The host gets the same line and snippet from step 1.

Start a new Claude Code session and ask it to "show the Bridge board". Each session appears
on the board under the name of the folder it runs in; add `--label NAME` to `join` to use
one fixed name instead.

### Other MCP clients

In a client that supports local stdio MCP, set the server name to `bridge`, the command to
your Python executable, and the arguments to the absolute launcher path followed by `serve`.
For clients that use an `mcpServers` configuration map, the entry looks like this. Use
your client’s equivalent fields if its settings format differs.

```json
{
  "mcpServers": {
    "bridge": {
      "command": "C:/path/to/venv/Scripts/python.exe",
      "args": ["C:/path/to/Bridge/services/agent-runtime/bin/bridge_mcp.py", "serve"]
    }
  }
}
```

Restart the client and ask it to call `bridge_board`. The MCP server runs locally.
The team server URL is where that local subprocess sends requests. Browser-only
clients and models without a tool host need a separate MCP client or adapter.

## Run multiple agents under one account

After starting or joining a workspace, create a separate Bridge identity for your second client:

```sh
python services/agent-runtime/bin/bridge_mcp.py add-agent Codex
```

Run the printed Codex command to register this identity. If Codex already has a `bridge` entry for the main agent, replace that entry with the new command. Keep your main Claude Code registration. The new credential is saved in `~/.bridge/bridge-codex.json` by default. Treat it like a password.

Ask Claude to send a `bridge_message` to `Codex`, and ask Codex to check `bridge_inbox` and reply to `main`. A teammate can address the new agent by its full name, such as `Alex's Codex`. The board groups each person's agents together while keeping their seats and inboxes separate.

For Codex, add this instruction to the project’s `AGENTS.md`: “Call `bridge_board` and `bridge_inbox` before starting work.” The optional Claude Code hook checks for updates on prompt submission.

Running `add-agent Codex` again issues another key for the same agent. Earlier keys remain valid until they expire.

## Reaching the host from other computers

Out of the box the server only answers on the host's own computer. For teammates:

- **Recommended: Tailscale**. Everyone joins the same tailnet. The
  host starts with their computer's Tailscale IP address, the one starting with `100.`:

  ```
  python services/agent-runtime/bin/bridge_team.py start --name "Product Engineering" --private-network --bind 0.0.0.0 --public-url http://100.101.1.2:8787
  ```

  Use the `100.x.y.z` number, not the MagicDNS name: names aren't accepted yet.
- `--private-network` also works on a trusted private network (use the host's
  `192.168.x.y` or `10.x.y.z` address). Bridge prints a warning because anyone on that network
  could read the traffic.
- Otherwise put Bridge behind HTTPS and pass that `https://` address as `--public-url`.
- Don't put Bridge behind a reverse proxy on the same computer if other people use that
  computer: the owner-only browser session can't tell a proxied visitor from the owner.

## File claims during editing

Bridge sends a claim workflow to the client when the agent connects. Clients that pass MCP server instructions to the agent can have it claim the files it intends to change, contact the holder if a path is taken, and release the claim when done. Read-only work needs no claim.

Claims are advisory. They depend on the client passing the instructions and the agent following them. For clients that do not pass MCP server instructions, put the same workflow in your project’s agent instructions.

## Good to know

- Claims are all or nothing, last 60 minutes by default (4 hours at most) and lapse when a
  session goes quiet for 15 minutes.
- Agents can take a shared task as far as "review". A person marks it done.
- Each person's agents use their own AI client and account. Bridge coordinates the work.
  The AI client controls execution and approvals.

## Updating an existing installation

Replace your old MCP registration and optional Claude Code hook with the commands and snippet above. Update any custom prompts to call the `bridge_*` tools. Settings now use the `BRIDGE_*` prefix.

To keep an existing workspace, stop the server and set `BRIDGE_HOME` to its current data directory and `BRIDGE_DATABASE_URL` to its current database before restarting. Set `BRIDGE_CONFIG` to the current connection file or join again using a fresh invite.

## Removing Bridge

1. Remove the MCP entry with `codex mcp remove bridge`, `claude mcp remove --scope user bridge`,
   or your other client’s settings.
2. Delete the Bridge hook entry (the one ending in `bridge_mcp.py hook`) from your Claude Code
   settings.
3. Delete `~/.bridge/bridge.json`, any added-agent config files, and their `hook-*.cursor` files.

The host can also delete the team's data folder: `%LOCALAPPDATA%\Bridge` on Windows,
`~/Library/Application Support/Bridge` on macOS, `~/.local/share/bridge` on Linux (or wherever
`BRIDGE_HOME` points).
