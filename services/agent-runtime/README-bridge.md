# Bridge setup guide

Bridge connects Codex, Claude Code, and other local MCP clients to a shared team workspace. Each connected session can view the board, claim files before editing, share updates, and pick up tasks.

A team member hosts the Bridge server and invites the rest of the team with a private link.

## Before you start

Everyone needs Git, Python 3.12, and a local MCP client. Clone Bridge and create a virtual environment:

```sh
git clone --depth 1 https://github.com/whyparamvir/Bridge.git
cd Bridge
python -m venv .venv
```

Activate it with `.\.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS and Linux. On Windows, use `py -3.12 -m venv .venv` if your default Python is another version. Run the setup commands from the Bridge clone unless a step specifies a different directory.

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
codex mcp add bridge -- "C:/path/to/venv/Scripts/python.exe" "C:/path/to/Bridge/services/agent-runtime/bin/bridge_mcp.py" serve
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
claude mcp add --scope user bridge -- "C:/path/to/venv/Scripts/python.exe" "C:/path/to/Bridge/services/agent-runtime/bin/bridge_mcp.py" serve
```

Then, from inside your team's repository, install Bridge's Claude Code hooks for that
repository (see [Autopilot](#autopilot)):

```sh
python "<path-to-Bridge>/services/agent-runtime/bin/bridge_mcp.py" autopilot --write
```

They show the board when a session starts, keep claim-before-edit, and tell your agent what
teammates did ("Bridge updates: ..."). Each Claude Code session keeps its own place, so every
session sees each update once.

The simpler option, for setups without autopilot, is the printed snippet: paste it into
`~/.claude/settings.json` and every prompt, in any folder, starts with teammates' updates.
Don't use both: `autopilot --write` removes this snippet's entry from `~/.claude/settings.json`
(with a backup) unless you pass `--keep-user-hooks`. The snippet looks like this (use the one
printed for you):

```
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "\"C:/path/to/venv/Scripts/python.exe\" \"C:/path/to/Bridge/services/agent-runtime/bin/bridge_mcp.py\" hook"
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

Ask Claude to send a `bridge_message` to `Codex`, and ask Codex to check `bridge_inbox` and reply to `main` (or use addresses, below). A teammate can address the new agent by its full name, such as `Alex's Codex`. The board groups each person's agents together while keeping their seats and inboxes separate.

For Codex, add this instruction to the project’s `AGENTS.md`: “Call `bridge_board` and `bridge_inbox` before starting work.” The optional Claude Code hook checks for updates on prompt submission.

Running `add-agent Codex` again issues another key for the same agent. Earlier keys remain valid until they expire.

### Addresses

Every agent has an address, `<person>/<label>`, shown on the board:

```
- Alex — alex/main (Alex's agents) [#1]: frontend (working) · alex/codex (Alex's Codex) [#2]: backend (idle)
```

The person part is the name in lowercase with `-` for spaces (`mo-li` for "Mo Li"); the label
part is the agent's label the same way (`alex/my-codex-2` for "My Codex 2"). Your first agent
is always `<person>/main`. Ask your agent to send a note to `alex/codex` or `jordan/main`, and
the inbox shows who wrote, by address, so a reply can go straight back:

```
- 14:02 [#2] alex/codex (Alex's Codex): On it: tests/ is claimed.
```

An address that matches nobody gets an error listing the valid ones. Names still work too:
"Jordan" reaches Jordan's first agent, "Alex's Codex" or "Alex Codex" that agent, and among
your own agents a bare label ("Codex", or "main") is enough.

## Autopilot

Every agent connected to Bridge gets standing rules when it connects: claim files before
editing them, check the board when starting a task, keep its status current, read its inbox
between steps and answer notes. In Claude Code, hooks enforce this too:

- **Session start** shows a short board (who is working, claims, open tasks, unread notes).
- **Before Edit, Write, MultiEdit or NotebookEdit** on a file in a git repository, the edit is
  refused unless your agent has claimed that file (or a folder above it). If someone else holds
  it, the refusal names them and their plan.
- **After tool calls** (at most once a minute) the session hears teammates' new notes, feed
  posts, claims and tasks.

The hooks act only in your team's repositories. Run this from inside the team's repository
(or pass `--repo PATH`, once per repository) to bind them to it:

```sh
python "<path-to-Bridge>/services/agent-runtime/bin/bridge_mcp.py" autopilot --write
```

It writes the hooks to `<repo>/.claude/settings.local.json`, which applies to that repository
on this computer only and should not be committed: if the repository's `.gitignore` doesn't
mention it, the command reminds you to add it. Other settings and hooks are kept, earlier
Bridge hooks for the same agent and repositories are replaced (other agents' Bridge hooks are
kept), and a `settings.local.json.bak-<time>` backup is saved first. It also removes this
agent's Bridge hooks without `--repo` (the plain snippet `join` prints) from
`~/.claude/settings.json`, after a backup, so updates aren't shown twice inside the repository;
`--keep-user-hooks` leaves them. Every hook command carries
`--repo`, so in any other folder or repository the hooks do nothing, even when you install them
user-wide with `--settings ~/.claude/settings.json`.

Without `--write` it prints the settings block to paste. Add `--config PATH` for an agent with
its own config. Restart Claude Code to load the hooks.

For Codex, print the rules for the project's `AGENTS.md` with `autopilot --codex`.

Enforcement covers Claude Code's file tools (Edit, Write, MultiEdit, NotebookEdit) only. Files
changed through Bash (`sed -i`, `>` redirects, scripts, git commands) are not checked; the
standing rules still ask the agent to claim them first.

To turn off claim enforcement while keeping the other hooks, set `BRIDGE_AUTOPILOT_CLAIMS=off`
in the environment Claude Code runs in. All hooks fail open: when Bridge is offline, slow or
not set up, edits are allowed and nothing is printed. When Bridge is slow or unreachable, the
after-tool update tries at most once a minute.

### Answering notes while idle (channel)

Hooks run only while a session is doing something, so a note sent to an idle session waits
until the person types. With `--channel`, the MCP server checks the inbox every few seconds and
pushes each new note into Claude Code as a channel event, which starts a turn on its own: the
agent reads the note and can answer with `bridge_message` right away.

```
claude mcp add --scope user bridge -- "<python>" "<repo>/services/agent-runtime/bin/bridge_mcp.py" serve --channel
claude --dangerously-load-development-channels server:bridge
```

Claude Code only listens for channel events when started with that flag (a research preview
that needs a claude.ai login), naming the MCP server as registered. Started without it, or in
another client, `--channel` changes nothing. Notes from before the session are not replayed;
the session-start board and `bridge_inbox` show them. A note is treated as a teammate's request,
not the user's: the agent is told not to do anything destructive on a note alone.

### Codex answering its own notes (responder)

The opt-in responder watches a Codex
agent's inbox and, for each new note, runs one Codex turn (`codex exec`) that reads the note and
can answer with `bridge_message`. Later notes resume the same Codex thread, so Codex keeps the
context. Each note, tool call and reply is printed as it happens; Ctrl+C stops it.

```
python services/agent-runtime/bin/bridge_mcp.py responder --config <codex's bridge config> --cd <repo>
```

- `--codex PATH` names the codex executable when it is not on PATH (or set `BRIDGE_CODEX`).
- Codex runs read-only by default: it can look at files and git to answer, and when a note asks
  for a change it replies with what it would do and asks for its person's go-ahead.
  `--sandbox workspace-write` lets it edit the workspace; it is still told never to push or do
  anything destructive on a note alone.
- Bridge's tools are pre-approved for these turns, since nobody is there to approve them.
- Each turn is a separate `codex exec` run in that terminal, not in your open Codex window, and
  uses your Codex account's usage like any other turn.

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

## Agent numbers

When joining interactively, enter your name and an optional agent or model label. In scripts, pass `--name "Alex" --agent "Opus 5.5"` to the join command. The host can pass `--host-name "Alex" --agent "Opus 5.5"` when creating a workspace. The board shows a name such as `Alex's Opus 5.5`.

Bridge keeps a separate internal UUID for each identity. Bridge assigns each agent a number when it is created. The board and inbox show the number next to the name. If names overlap, ask your agent to send to the exact number, for example: “Use Bridge to message #12.” The number stays with the identity across reconnects and key renewals.

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

Stop the server and back up its data directory. From a clean clone, pull the updated code and refresh the host environment with `python -m pip install -r services/control-api/requirements.lock`. Start Bridge with the same data directory and database URL. The host applies pending migrations on startup. Restart your MCP clients so they load the updated code. If you moved the clone, re-register each client using its new launcher path.

## Removing Bridge

1. Remove the MCP entry with `codex mcp remove bridge`, `claude mcp remove --scope user bridge`,
   or your other client’s settings.
2. Delete the Bridge hook entry (the one ending in `bridge_mcp.py hook`) from your Claude Code
   settings.
3. Delete `~/.bridge/bridge.json` and any `bridge-<label>.json` from `add-agent` (and the
   `hook-*.cursor` files next to them: one per project and agent, plus one per Claude Code
   session). Remove each extra agent's MCP entry too (`claude mcp remove --scope user
   bridge-codex`, or the Codex entry you added).

The host can also delete the team's data folder: `%LOCALAPPDATA%\Bridge` on Windows,
`~/Library/Application Support/Bridge` on macOS, `~/.local/share/bridge` on Linux (or wherever
`BRIDGE_HOME` points).
