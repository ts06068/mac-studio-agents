"""Append one workspace without recreating existing developer containers."""
import getpass
from importlib import import_module
import json
import shutil
import subprocess
import time
import urllib.request

from manage import (compose, compose_document, config, docker_bin, run,
                    secret_file, validate_users, write_json)


def next_ssh_port(cfg):
    used = {u["ssh_port"] for u in cfg["users"]} | {cfg["api_port"], cfg["backend_port"]}
    port = max(u["ssh_port"] for u in cfg["users"]) + 1
    while port in used:
        port += 1
    if port > 65535:
        raise ValueError("No free SSH port remains")
    return port


def provision_user(state, name, password, client=None, pool="10.210.0.0/16"):
    validate_users([name])
    if name == "root":
        raise ValueError("root cannot be a developer account")
    if not password or any(c in password for c in "\n\r\0:"):
        raise ValueError("Password must be a nonempty single line without a colon")
    cfg = config(state)
    backup = state / "backups" / ("add-user-" + name)
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for relative in ["config.json", "compose.json", "cloudflare-state.json", "secrets/gateway.json"]:
        source = state / relative
        dest = backup / relative.replace("/", "-")
        if source.exists() and not dest.exists():
            shutil.copy2(source, dest)
            dest.chmod(0o600)
    if cfg.get("access_mode") == "tunnel":
        if client is None:
            raise ValueError("Cloudflare management credentials are required")
        checkpoint = state / "cloudflare-state.json"
        remote = json.loads(checkpoint.read_text())
        if remote["tunnel_id"] != cfg["cloudflare"]["tunnel_id"]:
            raise ValueError("Cloudflare checkpoint does not match the configured tunnel")
        routes = client.routes()
        if name not in remote["allocations"]:
            reserved = routes + [{"network": a["subnet"]} for a in remote["allocations"].values()]
            remote["allocations"].update(import_module("provision-cloudflare").allocate(pool, [name], reserved))
            write_json(checkpoint, remote)
        network = remote["allocations"][name]["ssh_ip"] + "/32"
        matches = [r for r in routes if r["network"] == network]
        if any(r["tunnel_id"] != remote["tunnel_id"] for r in matches):
            raise ValueError("SSH route belongs to another tunnel")
        route = matches[0] if matches else client.request("POST", "/teamnet/routes", {
            "network": network, "tunnel_id": remote["tunnel_id"],
            "comment": "mac-studio-agents ssh-" + name,
        })["result"]
        remote["route_ids"]["ssh-" + name] = route["id"]
        write_json(checkpoint, remote)
        cfg["cloudflare"]["allocations"] = remote["allocations"]
    base = state / "secrets/users" / name
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = base / (name + ".pem")
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-m", "PEM", "-N", "",
             "-C", "mac-studio-agents:" + name, "-f", key])
    key.chmod(0o600)
    if not key.with_suffix(".pem.pub").exists():
        raise ValueError("Existing SSH key is missing its public key")
    secret_file(state, "users/" + name + "/password", password)
    api_key = secret_file(state, "users/" + name + "/api_key").read_text().strip()
    if name not in [u["name"] for u in cfg["users"]]:
        cfg["users"].append({"name": name, "ssh_port": next_ssh_port(cfg)})
    gateway = json.loads((state / "secrets/gateway.json").read_text())
    gateway["users"][name] = api_key
    write_json(state / "secrets/gateway.json", gateway, mode=0o644)
    write_json(state / "config.json", cfg)
    write_json(state / "compose.json", compose_document(state, cfg))
    return cfg


def wait_for_idle(state, cfg, timeout=300):
    deadline = time.monotonic() + timeout
    key = (state / "secrets/upstream_api_key").read_text().strip()
    while True:
        req = urllib.request.Request("http://127.0.0.1:" + str(cfg["backend_port"]) + "/metrics",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=10) as response:
            metrics = response.read().decode()
        active = [float(line.rsplit(" ", 1)[1]) for line in metrics.splitlines()
                  if line.startswith(("vllm:num_requests_running{", "vllm:num_requests_waiting{"))]
        if len(active) >= 2 and sum(active) == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Model is busy. Retry add-user --apply after requests finish.")
        time.sleep(5)


def apply_user(state, name):
    cfg = config(state)
    compose(state, "up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "dev-" + name)
    if cfg.get("access_mode") == "tunnel":
        # Attach the running connector to the added network to preserve SSH sessions.
        network = "mac-studio-agents_user_" + name
        detail = json.loads(subprocess.check_output([docker_bin(), "inspect", "msa-cloudflared"]))[0]
        address = cfg["cloudflare"]["allocations"][name]["connector_ip"]
        attached = detail["NetworkSettings"]["Networks"].get(network)
        if attached and attached["IPAddress"] != address:
            raise ValueError("Connector is attached with an unexpected IP address")
        if not attached:
            run([docker_bin(), "network", "connect", "--ip", address,
                 "--gw-priority", "-1", network, "msa-cloudflared"])
    wait_for_idle(state, cfg)
    # Recreate this small proxy to load the new credential file; keep the model running.
    compose(state, "up", "-d", "--no-build", "--no-deps", "--force-recreate",
            "--wait", "--wait-timeout", "120", "gateway")


def add_user(args, state):
    validate_users([args.name])
    if args.name == "root":
        raise ValueError("root cannot be a developer account")
    cfg = config(state)
    password_path = state / "secrets/users" / args.name / "password"
    password = (password_path.read_text() if password_path.exists() else
                args.password_file.expanduser().read_text().rstrip("\r\n") if args.password_file else
                getpass.getpass("New Linux account password: "))
    client = None
    if cfg.get("access_mode") == "tunnel":
        token_file = args.token_file.expanduser()
        if token_file.stat().st_mode & 0o077:
            raise ValueError("Cloudflare token file must have mode 600 or 400")
        remote = json.loads((state / "cloudflare-state.json").read_text())
        client = import_module("provision-cloudflare").Cloudflare(remote["account_id"], token_file.read_text().strip())
    cfg = provision_user(state, args.name, password, client, args.pool)
    if args.apply:
        apply_user(state, args.name)
    print("User configured: " + args.name + "; existing credentials preserved")
