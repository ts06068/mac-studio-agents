#!/usr/bin/env python3
"""Create one dedicated Cloudflare Tunnel and private /32 routes for this stack.

Only this stack's resources are modified. Existing account/device policies are
left intact. The chosen private pool must already route through clients' WARP.
"""
import argparse
import base64
import ipaddress
import json
import re
import secrets
import urllib.error
import urllib.request
from pathlib import Path

from manage import DEFAULT_STATE, compose, compose_document, config, write, write_json


class Cloudflare:
    def __init__(self, account, token):
        self.base = "https://api.cloudflare.com/client/v4/accounts/" + account
        self.token = token

    def request(self, method, path, data=None):
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as e:
            try:
                result = json.load(e)
            except Exception:
                raise RuntimeError("Cloudflare HTTP " + str(e.code)) from None
            raise RuntimeError("Cloudflare HTTP " + str(e.code) + ": " + str(result.get("errors", []))) from None
        if not result.get("success"):
            raise RuntimeError("Cloudflare API failed: " + str(result.get("errors", [])))
        return result

    def routes(self):
        result, page = [], 1
        while True:
            response = self.request("GET", "/teamnet/routes?per_page=100&page=" + str(page))
            result.extend(x for x in response["result"] if not x.get("deleted_at"))
            info = response.get("result_info", {})
            if page >= info.get("total_pages", 1):
                return result
            page += 1


def allocate(pool, names, routes):
    network = ipaddress.ip_network(pool, strict=True)
    if network.version != 4 or not network.is_private or network.prefixlen > 29:
        raise ValueError("Use a private IPv4 pool containing at least one /29")
    used = [ipaddress.ip_network(x["network"]) for x in routes]
    result = {}
    candidates = network.subnets(new_prefix=29)
    for name in names:
        candidate = next((s for s in candidates if not any(s.overlaps(u) for u in used if u.version == 4)), None)
        if candidate is None:
            raise ValueError("No unused /29 remains in the private pool")
        addresses = list(candidate.hosts())
        result[name] = {"subnet": str(candidate), "ssh_ip": str(addresses[1]),
                        "api_ip": str(addresses[2]), "connector_ip": str(addresses[3])}
        used.append(candidate)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--pool", default="10.210.0.0/16")
    parser.add_argument("--tunnel-name", default="mac-studio-agents")
    parser.add_argument("--image", default="cloudflare/cloudflared:2026.10.3")
    parser.add_argument("--apply", action="store_true", help="Recreate only this stack's Docker networks, preserving data volumes")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{32}", args.account_id):
        raise SystemExit("Expected a 32-character Cloudflare Account ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", args.tunnel_name):
        raise SystemExit("Tunnel name must contain only letters, digits, underscores, and hyphens")
    state = args.state.expanduser().resolve()
    from manage import REPO
    if state == REPO or REPO in state.parents:
        raise SystemExit("Runtime state and credentials must be outside the public repository")
    cfg = config(state)
    token_path = args.token_file.expanduser()
    if token_path.stat().st_mode & 0o077:
        raise SystemExit("API token file must have mode 600 or 400")
    client = Cloudflare(args.account_id, token_path.read_text().strip())
    checkpoint = state / "cloudflare-state.json"
    remote = json.loads(checkpoint.read_text()) if checkpoint.exists() else {}
    routes = client.routes()
    if not remote:
        remote = {"account_id": args.account_id, "name": args.tunnel_name,
                  "allocations": allocate(args.pool, [u["name"] for u in cfg["users"]], routes),
                  "route_ids": {}}
        write_json(checkpoint, remote)
    if remote["account_id"] != args.account_id:
        raise SystemExit("State belongs to a different Cloudflare account")
    if remote["name"] != args.tunnel_name:
        raise SystemExit("State belongs to a different tunnel name")
    if "tunnel_id" not in remote:
        # Refuse to create a duplicate after an ambiguous POST/connection failure.
        existing = client.request("GET", "/cfd_tunnel?is_deleted=false&name=" + args.tunnel_name)["result"]
        if any(x.get("name") == args.tunnel_name for x in existing):
            raise SystemExit("A tunnel with this name already exists. Reconcile its ID before retrying.")
        result = client.request("POST", "/cfd_tunnel", {
            "name": args.tunnel_name, "config_src": "cloudflare",
            "tunnel_secret": base64.b64encode(secrets.token_bytes(32)).decode(),
        })["result"]
        remote["tunnel_id"] = result["id"]
        write_json(checkpoint, remote)
    tid = remote["tunnel_id"]
    tunnel_token = client.request("GET", "/cfd_tunnel/" + tid + "/token")["result"]
    write(state / "secrets/cloudflared_token", tunnel_token, mode=0o644)
    client.request("PUT", "/cfd_tunnel/" + tid + "/configurations", {"config": {
        "warp-routing": {"enabled": True}, "ingress": [{"service": "http_status:404"}],
    }})
    api_ip = remote["allocations"][cfg["users"][0]["name"]]["api_ip"]
    required = {"ssh-" + name: a["ssh_ip"] + "/32" for name, a in remote["allocations"].items()}
    required["llm-api"] = api_ip + "/32"
    for label, network in required.items():
        matches = [r for r in routes if r["network"] == network]
        if any(r["tunnel_id"] != tid for r in matches):
            raise RuntimeError("Route now belongs to another tunnel: " + network)
        if matches:
            route_id = matches[0]["id"]
        else:
            result = client.request("POST", "/teamnet/routes", {
                "network": network, "tunnel_id": tid, "comment": "mac-studio-agents " + label,
            })["result"]
            route_id = result["id"]
        remote["route_ids"][label] = route_id
        write_json(checkpoint, remote)
    cfg["access_mode"] = "tunnel"
    cfg["cloudflare"] = {"tunnel_id": tid, "allocations": remote["allocations"],
                         "api_ip": api_ip, "image": args.image}
    write_json(state / "config.json", cfg)
    write_json(state / "compose.json", compose_document(state, cfg))
    if args.apply:
        compose(state, "down")
        compose(state, "up", "-d", "--build", "--wait", "--wait-timeout", "300")
    print(json.dumps({"tunnel_id": tid, "ssh": {n: a["ssh_ip"] for n, a in remote["allocations"].items()},
                      "api": "http://" + api_ip + ":4000/v1"}, indent=2))


if __name__ == "__main__":
    main()
