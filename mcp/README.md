# Controlled Loxone MCP

Docker service using the official Python MCP SDK (Streamable HTTP, `/mcp`) and
`loxwebsocket==0.6.0`. All ten requested tools are implemented. Production writes
are **disabled by default**. Configuration remains outside Git.

## Scope and limitations

- Rooms and controls come from the authenticated live `LoxAPP3.json`.
- State reads use a fresh authenticated WebSocket initial snapshot, with state UUID
  and timestamp; they do not substitute a historical database value.
- Supported writable runtime parameters: Ventilation `presenceMin`, `presenceMax`,
  `absenceMin`, `absenceMax` (0–100 percent). This includes Leaf Tree controls.
- Each write requires an exact device/parameter allowlist entry, write OAuth scope,
  expected current value, and unique request ID. Min/max consistency, logic-bound
  parameters, restricted controls and the control lock are checked.
- Only mapped `setPresenceMin`, `setPresenceMax`, `setAbsenceMin`, `setAbsenceMax`
  commands exist. There is no arbitrary API path or script execution tool.
- This is runtime structure/state analysis, **not** arbitrary Config programming,
  project upload, firmware updates, or an API exposing every hardware parameter.
  Full automation logic requires a separate authorized project-export integration.
- A failed/uncertain verification is reported and audited. No blind rollback is
  performed: concurrent automation may have changed the value. Read fresh state
  and authorize a conditional restoration when it is demonstrably safe.

## Deployment

The existing VPS uses the Docker default bridge for WireGuard forwarding. Compose
therefore uses `network_mode: bridge`, not a newly created network requiring broad
firewall changes. Backend publication is loopback-only at `127.0.0.1:18089`.

Files:

- `/opt/loxone-mcp`: source and Compose
- `/etc/loxone-mcp/config.json`: Loxone secrets and hashed OAuth credentials
- `/etc/loxone-mcp/policy.json`: runtime write policy
- `/etc/loxone-mcp/owner-credentials.json`: initial OAuth credentials, root-only
- `/var/lib/loxone-mcp/audit.sqlite`: durable audit (WAL, synchronous FULL)
- `/var/lib/loxone-mcp/auth.sqlite`: hashed opaque tokens and short-lived grants

Initial setup on VPS (the bootstrap reads credentials already in the local
`awstates-vps` container, never from MotherDuck):

```sh
python3 bootstrap_vps.py --expected-serial YOUR_SERIAL
 docker compose up -d --build
 docker compose ps
```

The service runs as UID 10001 with a read-only root filesystem, no capabilities,
resource limits, capped logs, healthcheck and `restart: unless-stopped`. Docker
must be enabled at boot. Do not reboot the productive VPS just to test this.

A bootstrap deployment has a deliberately unusable public hostname and no callback
allowlist. It is **not externally connected** until the following steps complete.

## Public HTTPS and ChatGPT OAuth

Choose an owned DNS hostname. Point it at the VPS and arrange narrowly scoped
HTTPS ingress. No provider or host firewall is opened by this deployment.
`config/Caddyfile.example` is a host-proxy template with automatic ACME TLS; replace
its hostname and validate it before enabling. Forward the whole host, including
`/.well-known/*`, `/authorize` and `/token`, not just `/mcp`. Persist the proxy's
certificate storage. Do not proxy the Miniserver directly.

Configure only nonsecret endpoint values:

```sh
python3 /opt/loxone-mcp/configure.py \
  --public-url https://YOUR_HOST \
  --redirect-uri https://chatgpt.com/connector_platform_oauth_redirect
cd /opt/loxone-mcp
 docker compose restart mcp
```

Always use the exact callback shown by ChatGPT. The authorization server advertises
RFC 9207 issuer identification and returns `iss`, supporting the stable callback
above. Static OAuth registration is intentional: no unrestricted DCR endpoint.

In ChatGPT on the web:

1. Open **Plugins → + → Add custom MCP server** (subject to workspace permissions).
2. Name it Loxone; set Server URL to `https://YOUR_HOST/mcp`.
3. Select OAuth with provided/static credentials. Client ID is `loxone-chatgpt`.
   Enter the client secret privately from the root-only initial credential file,
   or use `configure.py --rotate-oauth` to set credentials you manage locally.
4. Review the permissions and create/install the plugin. During authorization,
   use the MCP login password, **not** the Loxone password.
5. Request `loxone_get_system_info`, then a targeted `loxone_get_parameters` call.
6. Only a real successful call from ChatGPT proves integration. An HTTP healthcheck,
   Inspector test or VPS-side MCP call does not prove it.

OAuth uses S256 PKCE, exact callback matching, resource-bound access tokens,
read/write scopes, one-use authorization codes, CSRF protection and 15-minute
access tokens. Rotating refresh tokens expire after 30 days; refresh-token reuse
revokes the token family. There is no password grant. The login password is scrypt
hashed. Tokens and client secrets are SHA-256 hashed at rest. Browser consent alone
cannot bypass the write allowlist.

Official references:
- https://developers.openai.com/api/docs/guides/custom-mcp-server
- https://developers.openai.com/plugins/build/auth

## Secure MCP Tunnel (selected deployment path)

See [TUNNEL.md](TUNNEL.md) for the prepared private sidecar, trust model, secure
runtime-key entry and ChatGPT setup. This path uses OpenAI tunnel ACLs plus a
separate loopback-only read credential instead of the public OAuth login above.
It requires a real account-owned tunnel ID and runtime key before activation.

## Dedicated Loxone user and secrets

The bootstrap reuses an existing server-side account. Its least-privilege status
is not established. Prefer a dedicated account created in Loxone Config with only
needed visible rooms/controls and appropriate runtime permissions. Creating that
account can require a configuration upload and is not performed automatically.

Enter its credentials directly on the VPS, without shell arguments/history:

```sh
python3 /opt/loxone-mcp/configure.py --service-user
python3 /opt/loxone-mcp/configure.py --rotate-oauth
cd /opt/loxone-mcp
 docker compose restart mcp
```

The rotation command prompts without echo and revokes existing OAuth grants.
Initial secrets remain in the root-only owner file until rotation removes it.
Never paste secrets into chat, commits, screenshots, logs, or reports.
The Loxone HTTP hop is within the WireGuard route (VPS to home gateway); the LAN
hop itself uses the existing HTTP endpoint. Use validated Miniserver HTTPS if that
is required by the local threat model. Do not disable certificate validation.

## Controlled pilot

Do not enable writes without the user's explicit pilot approval. After approval,
set the policy to the exact UUID/parameter and one request:

```json
{
  "writes_enabled": true,
  "allowlist": {"CONTROL_UUID": ["presenceMin"]},
  "pilot_grant": {
    "uuid":"CONTROL_UUID", "parameter":"presenceMin", "expected_value":11,
    "value":15, "request_id":"unique-approved-pilot-id"
  }
}
```

Update the existing policy file in place, preserving UID 10001 and mode 0400; policy
is re-read per call. Verify the starting value live, perform one call, verify from
a fresh subscription, inspect audit, then disable writes again. Other fans are
never allowlisted. A consumed/pending request ID cannot be replayed after restart.
For later general use, deliberately expand the allowlist and remove the one-use
pilot restriction only when that scope is authorized.

## Tests and operations

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt pytest pytest-asyncio
.venv/bin/python -m pytest -q tests
curl --fail http://127.0.0.1:18089/healthz
curl -i http://127.0.0.1:18089/mcp  # expected 401 without a bearer token
 docker compose ps
```

Tests use an in-memory MockMiniserver plus HTTP MockTransport, never production
actuators. They cover discovery, authentication, OAuth/PKCE/CSRF, wrong resource,
refresh replay, write scopes, allowlist, input/path validation, conditional writes,
mock verification, upstream errors, audit persistence, concurrency and limits.
Live read checks are separate and must never be confused with mock results.

`/healthz` checks process readiness only, not Miniserver connectivity. Monitor both
Docker health and authenticated `loxone_get_system_info`/targeted state reads. Review
`loxone_get_change_history` for `pending`, `verification_failed`, `outcome_unknown`.
Global limits: 120 API requests/minute, 20 auth requests/minute, 16 KiB bodies.
Read calls have a 35-second budget; upstream HTTP 12 seconds, snapshots 20 seconds.
For slow-body protection the proxy should enforce client body timeouts as well.

Update: review source/dependency changes, run tests, back up SQLite with its backup
API (not by copying only a live WAL database), retain the previous image, then
`docker compose up -d --build`. Verify health, 401 rejection and live reads. Pin
new base-image digests deliberately. Audit history is retained until the operator
archives it; do not delete request IDs while their replay protection is needed.
Backups contain household data and OAuth grants and must remain access restricted.

## Selection rationale

Reviewed on implementation date:

- `reijosirila/loxone-mcp-server`: Streamable HTTP, but documents Ventilation as
  unsupported and limitations for Gen2/TLS. Does not satisfy this Leaf pilot.
- `syntropicsignal-ai/loxone-mcp`: room/control/state tools and generic commands;
  its documented API-key/cloud-connect approach does not provide the required
  ChatGPT OAuth and explicit parameter-policy/audit boundary as-is.
- `Smarteon/lox-mcp`: promising WebSocket tools; retrieval could not be completed,
  so it was not treated as a reviewed deployable dependency.
- Official Miniserver MCP: previous authenticated probe on the current firmware
  returned 404; installing/upgrading a Miniserver plugin is a separate productive
  configuration change, not assumed here.

The adapter reuses the SDK and the same Loxone library already used by the Bronze
collector. It does not execute third-party installation scripts. The existing
`loxone-install` PowerShell installer is unrelated and is not executed or changed.

Loxone command reference:
https://www.loxone.com/wp-content/uploads/datasheets/StructureFile.pdf
(`Ventilation`, `setPresenceMin/{value}` and related runtime commands).


## Expanded owner-authorized allowlist (2026-10-25)

The owner authorized all ventilation controls, lighting and room controllers,
with a fresh human confirmation before each concrete change. Exact runtime
mappings now cover:

- Ventilation: presenceMin/Max and absenceMin/Max (0–100%, min/max consistency).
- LightControllerV2 children: Switch active (0/1), Dimmer position (0–100%),
  ColorPickerV2 brightness (0–100%, preserving current hue/temperature).
- IRoomControllerV2: comfortTemperature and comfortTemperatureCool (10–30 in
  the configured temperature unit; Fahrenheit configurations rejected).

List devices now includes light children with their exact `/AIx` or `/masterValue`
identifiers. Use get_parameters for supported names, commands and limits.
Secured/restricted controls, logic-connected temperatures, parent locks and active
color sequences remain protected. The central lighting aggregate is not exposed
as one blanket write: control its individually allowlisted circuits instead.
This does not authorize editing scenes, color sequences, schedules, safety
settings, arbitrary house switches, or configuration uploads.

`allowlist.py` reads the live structure and emits a proposed explicit policy;
it never operates actuators. Review and save its `policy` object server-side.
New devices are not automatically authorized. Policy changes must preserve an
owner approval record and a backup. Human confirmation remains a ChatGPT-side
requirement; the server cannot prove a human approved a received call.

Mapping source: official Loxone Structure File v17, pages 54–60, 72–78 and 132:
https://www.loxone.com/enen/wp-content/uploads/sites/3/2026/04/1700_Structure-File.pdf
Tests use mocks only for writes. Production acceptance reads the new parameters.


## Blinds and native central groups (owner authorized 2026-10-25)

Individual Jalousie parameters: targetPosition and targetPositionLamelle (0–100%,
0=open/horizontal, 100=closed/vertical), autoAllowed (0/1). Stop is available via
loxone_execute_group even for a single Jalousie. Safety, lock, offline and end-position
adjustment states block commands; no calibration or protection override is exposed.
A verified target is only an acknowledged target, not completed physical movement.
Read position/up/down afterwards to check movement.

loxone_get_group_state returns the members, states, allowed commands and state_token.
Ask the owner to confirm the exact command and all affected members. Then call
loxone_execute_group with this token and a unique request ID. CentralJalousie allows
FullUp, FullDown, stop, auto and NoAuto; CentralLightController allows on and reset
(native light-off). Only the explicit configured IDs go through selectedcontrols.
Group membership is pinned in policy; lighting circuit membership is pinned too.
Membership changes require owner-reviewed policy regeneration. A stale state token
rejects the action. Each operation persists intent and all before/after member states;
partial failure/unknown outcomes are reported, never silently retried or rolled back.

The earlier exclusions of Licht Zentral are superseded by this explicit approval.
The built-in Licht Zentral group has 13 controllers; the Garden controller is not
part of that Loxone group and is not silently added. Group commands follow the
Miniserver's group membership, not a guess based on names.

Official mapping: Structure File v17 pages 43–44 and 87–90. Group tooling increases
MCP discovery from 10 to 12 tools. Refresh the ChatGPT plugin's discovered tools
if an existing chat does not yet see loxone_get_group_state/loxone_execute_group.
