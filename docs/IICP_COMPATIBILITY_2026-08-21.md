# IICP compatibility review — 21 August 2026

This review compares the original monitor commit `3cec2ac2` with current public
IICP sources. It separates protocol, directory, runtime, operational and
deployment behavior. The monitor remains independently maintained and is not an
official IICP component or conformance implementation.

## Evidence baseline

| Repository | Reviewed commit |
|---|---|
| `michaeloboyle/iicp-node-monitor` | `3cec2ac2aea2b95b6711d34cac5f3e6aef05684d` |
| `RobLe3/IICP` | `a2e559f` |
| `RobLe3/iicp-directory-php` | `697f33d` |
| `RobLe3/iicp-directory-rust` | `2f99797` |
| `RobLe3/iicp-client-rust` | `8a9d592` |

## Compatibility matrix

| Monitor assumption | Current behavior and source | Classification | Patch |
|---|---|---|---|
| Registration failure is `register_error` | Rust writes `register_fail` in `src/node_log.rs` and `src/bin/iicp_node.rs` | Monitor lag | Treat `register_fail` as current and retain `register_error` as a legacy alias. |
| Unknown events are heartbeats | Rust's event vocabulary can grow; unknown does not mean success | Monitor bug | Classify as neutral `unknown`. |
| Discovery calls enumerate the mesh | PHP `RegistryController` and Rust `registry.rs` expose stats, intents and paginated public nodes | Monitor lag | Prefer Registry APIs; retain an explicitly partial discovery fallback. |
| Genesis is the only directory | Rust accepts `IICP_DIRECTORY_URL`; restricted/local modes must not fall back publicly | Security boundary | Add CLI/environment configuration and prohibit implicit Genesis use in restricted modes. |
| Logs always live under `~/.iicp/logs` | Rust supports `IICP_LOG_DIR` | Monitor lag | Follow CLI, environment, then the legacy default. |
| Log freshness is runtime health | Rust exposes `/iicp/health` and implementation-level `health-v1.json` snapshots | Monitor lag | Prefer endpoint, then snapshot, then labelled log inference. |
| A directory task-counter delta is an original task event | A counter has no original task timestamp | Monitor bug | Emit one `directory_task_delta` observation at the monitor observation time. |
| Cloudflare, origin masking, tool refusal and model licensing are universal | These are deployment or policy facts, not universal protocol facts | Monitor bug | Report only measured/configured/directory facts and otherwise show unavailable. |
| Secret encryption can be checked by opening `operator.json` | Secret files are not an observability API | Security issue | Never open the file; consume non-secret reference metadata or report unavailable. |
| Directory/provider strings are safe HTML | Public metadata is externally controlled | Security issue | Escape server-rendered values and client-rendered table fields. |

## 28 August 2026 route-readiness addendum

The official Rust service generator already resolves `iicp-node` to an absolute
path, and an unset `IICP_TUNNEL` keeps automatic route selection enabled. The
remaining supervisor mismatch is older clients' PATH-only `cloudflared`
discovery. The compatibility plist now documents an explicit absolute
`IICP_CLOUDFLARED_PATH` rather than replacing the whole service environment or
forcing every node through a tunnel.

Directory-reported reachability, local process health and externally usable
route health are separate observations. The monitor now measures the advertised
HTTPS `/iicp/health` route independently and reports disagreements without
changing node or directory state.

## Interface authority

- `/iicp/health` is an IICP node surface with a normative minimal health check;
  expanded fields remain version-sensitive.
- `/v1/registry/stats`, `/v1/registry/intents`, and `/v1/registry/nodes` are
  privacy-bounded directory inventory APIs in both maintained directory flavors.
- `health-v1.json` is a Rust implementation-level runtime snapshot, explicitly
  not an IICP wire Profile.
- `events.jsonl` is a local operational interface. Its full vocabulary is not
  currently an implementation-neutral stable protocol contract.

The patch changes no IICP protocol semantics. Stable observability questions are
tracked separately by the IICP project rather than being decided by this monitor.

## Security results

- Restricted modes cannot silently contact public Genesis.
- Local-only mode performs no directory request.
- Operator secret material is not opened or exposed.
- Unknown events cannot appear as healthy heartbeats.
- Derived counter observations are distinguishable from node events.
- External metadata is escaped before HTML rendering.
- Public-route checks reject private addresses, redirects, credentials and
  oversized responses, and do not expose raw network exceptions.
- Loopback remains the default; non-loopback operation emits an exposure warning.

## Validation

Run:

```sh
python3 -m unittest -v
python3 -m py_compile node-stats-server.py
```

The tests are offline and use deterministic fixtures/mocks. A separate bounded
manual check verifies that current public Registry responses are parsed without
using the production network as a CI dependency.
