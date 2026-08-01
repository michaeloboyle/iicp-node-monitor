# iicp-node-monitor

A stdlib-only live dashboard for an [IICP](https://iicp.network) mesh node: a
cardiogram of node activity, a whole-mesh topology graph, and a security-posture
panel, served from a single Python file with **no dependencies**.

IICP is the routing layer of the agent-protocol stack (Orchestration = A2A/ACP,
**Routing = IICP**, Tools = MCP). A node advertises an intent (for example
`urn:iicp:intent:llm:chat:v1`), the directory routes typed tasks to it, and this
monitor shows you what your node is actually doing.

![endpoints: / dashboard, /api.json, /events.json, /network.json](#endpoints)

## Why this exists

Most node dashboards draw a pretty waveform whether or not anything is happening.
This one refuses to. Its design rule is a single non-negotiable:

> **The cardiogram is an honest impulse plot. Every mark maps to one real logged
> event at its real timestamp. Nothing synthetic is ever drawn.**

A flat baseline means the node is idle (the truth between 30-second heartbeats).
A green needle is a heartbeat, blue is a served task, amber is a refusal or
recovery check, red is a heartbeat failure, and a red flat line means the node
has actually gone silent (>90s). A decorative squiggle would imply activity that
did not happen, so there isn't one. Derived values are labeled as claims, and
stale state is dropped rather than frozen and shown as current.

## Run it

```sh
python3 node-stats-server.py --port 9486 --host 127.0.0.1
open http://127.0.0.1:9486/
```

It resolves the *current* node dynamically from `~/.iicp/logs/*.log`, so it
survives the node-id and tunnel-URL rotation that happens on every restart.
Nothing is hard-coded to a specific operator or endpoint.

### Endpoints

| Path | What |
|---|---|
| `/` | dashboard: cardiogram + mesh graph + security posture + tables |
| `/?mode=screensaver` | ambient full-screen wall monitor (dark, chrome-less) |
| `/api.json` | unified snapshot (local log + directory + mesh + security) |
| `/events.json` | event timeline that drives the cardiogram |
| `/network.json` | whole-mesh topology + per-node stats |

Light and dark themes both ship (CSS variables + `prefers-color-scheme` + a
`data-theme` override). Canvases are device-pixel-ratio aware so text stays crisp.

## Run at login (macOS launchd)

Copy the example plists in `launchd/`, replace the `__HOME__` placeholders with
your home directory, and load them:

```sh
sed "s#__HOME__#$HOME#g" launchd/local.iicp-stats.plist.example \
  > ~/Library/LaunchAgents/local.iicp-stats.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.iicp-stats.plist
```

The node plist example (`network.iicp.node.default.plist.example`) is the one
`iicp-node service install` writes, plus two fixups it omits: a `PATH` that
includes `cloudflared` and `iicp-node`, and `IICP_TUNNEL=1`. Without those the
node advertises an unreachable endpoint.

## Examples

`examples/draft-plus-check.py` shows a cheap, honest use of a small model served
over IICP: a **draft-plus-check** classifier. A small drafter model labels public
text, an independent verifier model labels it too, agreement is accepted, and
disagreement is escalated. The example also documents its own limitation: two
weak, correlated models can agree on a wrong answer, so an ensemble of small
models is a good first-pass and disagreement detector, not a verifier for
anything with stakes.

## Security notes

- The monitor binds to loopback by default. It reads local logs and the public
  directory; it never touches your operator secret or node token.
- Never commit `~/.iicp/` contents. Tokens and the operator secret live there and
  stay there. This repo references their *location*, never their value.
- A served node should offer only a public, open-weights model and refuse
  `bash` / `write_file` intents (the IICP default). Do not route private prompts
  to public mesh nodes; a remote executor can read every prompt it runs.

## License

MIT. See `LICENSE`.
