# Private OpenAI Secure MCP Tunnel

The VPS stays private. No inbound firewall change, DNS record, ACME certificate,
or public OAuth login is required for this transport mode.

## Trust and authorization

OpenAI tunnel organization/workspace permissions authenticate and authorize remote
callers. Restrict the tunnel to the intended personal organization/workspace; do
not associate a shared workspace unless everyone with tunnel-use permission should
have the configured Loxone read access. The tunnel client authenticates to OpenAI
with a dedicated runtime key (Tunnels Read + Use, not an admin key).

The sidecar injects a separate random credential **only on the loopback hop** to
the MCP server. The server checks its hash, expiry, enabled flag and actual local
peer address. By default this credential grants **read scope only**. The owner can enable
write scope with `--allow-writes`; exact device/parameter allowlists still apply. The key never goes to ChatGPT;
configure **No authentication** for the app-level OAuth choice, since the OpenAI
tunnel ACL supplies remote authorization and the sidecar supplies local backend
authentication. This does not make the MCP endpoint anonymous or public.

All users authorized by the tunnel ACL share the server-side service identity.
This is not a per-user OAuth delegation design. The existing OAuth flow remains
available separately. Restrict tunnel membership to the intended owner.

## Prepared artifacts

- Official client `ghcr.io/openai/tunnel-client:v0.0.18`, pinned to digest
  `sha256:486321616c197fed2d3fbe7d44f22433ce9a1e6c1ee434c0d6652868a5fda5c1`.
- `compose.tunnel.yaml`: optional `tunnel` profile; non-root UID 10002, read-only
  filesystem, no capabilities, restart policy, resource/log limits.
- It shares the MCP network namespace and calls `127.0.0.1:8080/mcp`.
- Admin/readiness endpoint is private at `127.0.0.1:18090` inside that namespace.
- `configure_tunnel.py`: interactive VPS setup; never prints secrets.

## Account step

1. Open https://platform.openai.com/settings/organization/tunnels .
2. Create a tunnel named `Loxone VPS`, or select an existing dedicated one. Associate
   the intended ChatGPT workspace and personal Platform organization. Copy the
   exact returned tunnel ID (not a fabricated/example ID).
3. Create a dedicated runtime API key with the tunnel-use permissions at
   https://platform.openai.com/settings/organization/api-keys . Do not use an
   organization admin key. Keep the key out of chat and command arguments.
4. On the VPS run the following interactively and enter the key at its hidden
   prompt. The script also generates the local backend key.

```sh
ssh -t -o HostKeyAlias=217.160.143.8 root@192.168.178.204 \
  python3 /opt/loxone-mcp/configure_tunnel.py
```

This writes only `/etc/loxone-mcp/tunnel/*` and the new hashed tunnel credential in
`/etc/loxone-mcp/config.json`. Runtime key files are mode 0400, UID 10002. The local
backend key expires in 30 days; repeat this setup to rotate it before expiry.

## Validate and start

```sh
cd /opt/loxone-mcp
docker compose restart mcp
docker compose -f compose.yaml -f compose.tunnel.yaml --profile tunnel \
  run --rm tunnel doctor --config /run/tunnel/config.json --explain
docker compose -f compose.yaml -f compose.tunnel.yaml --profile tunnel up -d tunnel
```

Check actual readiness, not just a running process:

```sh
docker exec loxone-mcp-mcp-1 python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:18090/readyz', timeout=5).status)"
```

The tunnel's status UI and logs are not publicly exposed. If the MCP container is
recreated on update, recreate the tunnel too because it shares its network namespace:

```sh
docker compose -f compose.yaml -f compose.tunnel.yaml --profile tunnel up -d --force-recreate
```

Do not start with fabricated IDs/keys. Until the account step completes, the image
and configuration are only staged, and no active tunnel connection is claimed.

## ChatGPT verification

In ChatGPT Plugins, add a custom MCP server, choose **Tunnel**, select this tunnel,
and choose **No authentication** for app-level OAuth as explained above. Install
it into the intended account/workspace and ask for `loxone_get_system_info`, then
a targeted `loxone_get_parameters` read. Record the successful ChatGPT tool result.
Neither the local MCP smoke test nor `/readyz=200` alone proves ChatGPT integration.

Official guide: https://developers.openai.com/api/docs/guides/secure-mcp-tunnels
Client: https://github.com/openai/tunnel-client
Release source inspected; no third-party install script executed.

## Optional writes: always confirm each concrete change

The owner subsequently authorized write capability, conditional on a fresh user
confirmation for every concrete change. `configure_tunnel.py --allow-writes`
now enables the tunnel's write scope, but does not change the device allowlist.
Without this flag the transport stays read-only.

Both write tools are marked `readOnlyHint=false`, `destructiveHint=true` and
explicitly instruct the assistant to ask before each exact device/parameter/value
change. ChatGPT asks for confirmation by default for write tools. **Never select
"remember approval" / auto-approve** for these tools. Verify the actual ChatGPT
confirmation behavior before opening the production allowlist.

This is a client-side human confirmation: the MCP server cannot independently
prove that a human approved a received tool call. Its own checks enforce scopes,
allowlist, expected values, replay prevention and audit. General write capability
is not permission for the assistant to perform any specific change.

Source: https://developers.openai.com/api/docs/guides/custom-mcp-server

The owner authorized the ventilation, lighting and room-controller allowlist on
2026-10-25. See README for the exact supported parameter scope. No blanket access
to all commands, scenes, schedules or safety settings is granted.
