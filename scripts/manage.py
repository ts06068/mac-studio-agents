#!/usr/bin/env python3
"""Provision Linux workspaces and manage a native Apple Silicon LLM server."""
import argparse
from datetime import datetime, timezone
import getpass
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

REPO = Path(__file__).resolve().parents[1]
DEFAULT_STATE = Path.home() / ".local/share/mac-studio-agents/runtime"
DEFAULT_MODEL = "mlx-community/gemma-4-31b-it-8bit"
LABEL_LLM = "org.mac-studio-agents.llm"
LABEL_STACK = "org.mac-studio-agents.stack"


def run(args, **kw):
    return subprocess.run([str(x) for x in args], check=True, **kw)


def write(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value)
    tmp.chmod(mode)
    tmp.replace(path)


def write_json(path, value, mode=0o600):
    write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n", mode)


def docker_bin():
    # Docker's credential helper is a sibling executable and must also be on PATH.
    os.environ["PATH"] = ":".join([
        str(Path.home() / ".docker/bin"),
        "/Applications/Docker.app/Contents/Resources/bin",
        os.environ.get("PATH", "/usr/bin:/bin"),
    ])
    candidates = [shutil.which("docker"), str(Path.home() / ".docker/bin/docker"),
                  "/Applications/Docker.app/Contents/Resources/bin/docker"]
    for p in candidates:
        if p and Path(p).is_file():
            return p
    raise SystemExit("Docker Desktop is required. Run scripts/bootstrap.sh.")


def config(state):
    return json.loads((state / "config.json").read_text())


def compose(state, *args):
    return run([docker_bin(), "compose", "-f", state / "compose.json", *args])


def validate_users(names):
    if not names or len(names) != len(set(names)):
        raise ValueError("Specify distinct user names")
    if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", n) for n in names):
        raise ValueError("User names must be lowercase Linux account names")
    return names


def secret_file(state, name, contents=None):
    path = state / "secrets" / name
    if not path.exists():
        write(path, contents if contents is not None else secrets.token_urlsafe(36))
    return path


def compose_document(state, cfg):
    log = {"driver": "local", "options": {"max-size": "10m", "max-file": "3"}}
    doc = {"name": "mac-studio-agents", "services": {}, "volumes": {},
           "networks": {}, "secrets": {}}

    def register_secret(name, path):
        doc["secrets"][name] = {"file": str(path)}

    def volume(name, target):
        doc["volumes"][name] = {}
        return {"type": "volume", "source": name, "target": target}

    def published(port, target):
        public_bind = (cfg.get("lan_ip") if cfg.get("access_mode") == "tunnel" else cfg["listen_ip"])
        ips = list(dict.fromkeys(["127.0.0.1"] + ([public_bind] if public_bind else [])))
        return [{"host_ip": ip, "published": str(port), "target": target, "protocol": "tcp"}
                for ip in ips]

    register_secret("gateway_config", state / "secrets/gateway.json")
    gateway = {
        "build": {"context": str(REPO), "dockerfile": "gateway/Dockerfile"},
        "image": "mac-studio-agents-gateway:local",
        "container_name": "msa-gateway", "restart": "unless-stopped",
        "ports": published(cfg["api_port"], 4000),
        "environment": {"GATEWAY_CONFIG": "/run/secrets/gateway_config"},
        "secrets": ["gateway_config"], "networks": [],
        "read_only": True, "tmpfs": ["/tmp:mode=1777"],
        "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "sysctls": {"net.ipv4.ip_forward": "0"},
        "mem_limit": "512m", "cpus": 2, "pids_limit": 128, "logging": log,
        "healthcheck": {
            "test": ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:4000/healthz')"],
            "interval": "15s", "timeout": "5s", "retries": 4,
        },
    }
    doc["services"]["gateway"] = gateway
    for user in cfg["users"]:
        name, port = user["name"], user["ssh_port"]
        net = "user_" + name
        doc["networks"][net] = {}
        gateway["networks"].append(net)
        env = {"DEV_USER": name, "OPENAI_BASE_URL": "http://gateway:4000/v1",
               "LLM_MODEL": cfg["served_model_name"], "TZ": "Asia/Seoul"}
        mounts = [volume(name + "_home", "/home/" + name),
                  volume(name + "_workspace", "/workspace"),
                  volume(name + "_hostkeys", "/etc/ssh/host-keys")]
        secret_mounts = []
        for suffix, filename, target in [
            ("pubkey", name + ".pem.pub", "authorized_key"),
            ("password", "password", "login_password"),
            ("api", "api_key", "llm_api_key"),
        ]:
            source = name + "_" + suffix
            register_secret(source, state / "secrets/users" / name / filename)
            secret_mounts.append({"source": source, "target": target})
        dev = {
            "build": {"context": str(REPO), "dockerfile": "dev/Dockerfile"},
            "image": "mac-studio-agents-dev:local",
            "container_name": "msa-dev-" + name, "hostname": "dev-" + name,
            "restart": "unless-stopped", "init": True,
            "ports": published(port, 22), "environment": env,
            "volumes": mounts, "secrets": secret_mounts, "networks": [net],
            "working_dir": "/workspace", "mem_limit": cfg["dev_memory"],
            "cpus": cfg["dev_cpus"], "pids_limit": 512, "logging": log,
            "healthcheck": {"test": ["CMD-SHELL", "ss -ltn | grep -q ':22 '"],
                            "interval": "15s", "timeout": "5s", "retries": 6,
                            "start_period": "30s"},
        }
        if cfg["dind"]:
            daemon = "docker-" + name
            doc["services"][daemon] = {
                "image": "docker:29.6.2-dind", "hostname": daemon,
                "restart": "unless-stopped", "privileged": True,
                "environment": {"DOCKER_TLS_CERTDIR": "/certs"},
                "networks": [net], "mem_limit": "1g", "cpus": 2,
                "volumes": [volume(name + "_docker_data", "/var/lib/docker"),
                            volume(name + "_docker_certs", "/certs/client")],
                "logging": log,
                "healthcheck": {"test": ["CMD-SHELL", "docker info >/dev/null 2>&1"],
                                "interval": "10s", "timeout": "5s", "retries": 12},
            }
            env["DOCKER_HOST"] = "tcp://" + daemon + ":2376"
            mounts.append({"type": "volume", "source": name + "_docker_certs",
                           "target": "/certs/client", "read_only": True})
            dev["depends_on"] = {daemon: {"condition": "service_healthy"}}
        doc["services"]["dev-" + name] = dev
    if cfg.get("access_mode") == "tunnel":
        register_secret("cloudflared_token", state / "secrets/cloudflared_token")
        connector_nets, gateway_nets = {}, {}
        for user in cfg["users"]:
            name = user["name"]
            allocation = cfg["cloudflare"]["allocations"][name]
            net = "user_" + name
            doc["networks"][net] = {"ipam": {"config": [{"subnet": allocation["subnet"]}]}}
            doc["services"]["dev-" + name]["networks"] = {net: {"ipv4_address": allocation["ssh_ip"]}}
            gateway_nets[net] = {"ipv4_address": allocation["api_ip"]}
            connector_nets[net] = {"ipv4_address": allocation["connector_ip"]}
        gateway["networks"] = gateway_nets
        doc["services"]["cloudflared"] = {
            "image": cfg["cloudflare"]["image"], "container_name": "msa-cloudflared",
            "restart": "unless-stopped", "init": True,
            "command": ["tunnel", "--no-autoupdate", "--metrics", "0.0.0.0:2000", "run",
                        "--token-file", "/run/secrets/cloudflared_token"],
            "secrets": ["cloudflared_token"], "networks": connector_nets,
            "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "read_only": True, "tmpfs": ["/tmp:mode=1777"],
            "sysctls": {"net.ipv4.ip_forward": "0"}, "mem_limit": "256m", "logging": log,
            "healthcheck": {"test": ["CMD", "cloudflared", "tunnel", "--metrics", "127.0.0.1:2000", "ready"],
                            "interval": "15s", "timeout": "5s", "retries": 8},
        }
    return doc


def initialize(args, state):
    names = validate_users(args.users.split(","))
    ipaddress.ip_address(args.listen_ip)
    if not 1024 <= args.ssh_port_start <= 65535 - len(names):
        raise ValueError("SSH ports must be unprivileged and in range")
    ports = set(range(args.ssh_port_start, args.ssh_port_start + len(names)))
    if (args.api_port in ports or args.backend_port in ports or args.api_port == args.backend_port
            or not all(1024 <= p <= 65535 for p in [args.api_port, args.backend_port])):
        raise ValueError("API and SSH ports must be distinct")
    password = (Path(args.password_file).read_text().rstrip("\r\n")
                if args.password_file else getpass.getpass("Linux account password: "))
    if not password or any(c in password for c in ("\n", "\r", "\0", ":")):
        raise ValueError("Password must be a nonempty single line without a colon")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    old = config(state) if (state / "config.json").exists() else None
    if old and [u["name"] for u in old["users"]] != names:
        raise ValueError("Existing user list differs; use a separate state directory to avoid replacing users")
    cfg = {
        "users": [{"name": name, "ssh_port": args.ssh_port_start + i}
                  for i, name in enumerate(names)],
        "listen_ip": args.listen_ip, "lan_ip": args.lan_ip,
        "hostname": args.hostname, "cloudflare_team": args.cloudflare_team,
        "api_port": args.api_port, "backend_port": args.backend_port,
        "model": args.model, "served_model_name": args.served_model_name,
        "max_model_len": args.max_model_len, "max_num_seqs": 3,
        "gpu_memory_utilization": args.gpu_memory_utilization,
        "dev_memory": args.dev_memory, "dev_cpus": 3, "dind": args.with_dind,
        "llm_venv": str(Path(args.llm_venv).expanduser()),
        "created_at": old["created_at"] if old else datetime.now(timezone.utc).isoformat(),
    }
    if old and old.get("access_mode") == "tunnel":
        cfg["access_mode"] = "tunnel"
        cfg["cloudflare"] = old["cloudflare"]
    upstream_key = secret_file(state, "upstream_api_key").read_text().strip()
    user_keys = {}
    for name in names:
        base = state / "secrets/users" / name
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = base / (name + ".pem")
        if not key.exists():
            run(["ssh-keygen", "-q", "-t", "rsa", "-b", "3072", "-m", "PEM",
                 "-N", "", "-C", "mac-studio-agents:" + name, "-f", key])
        key.chmod(0o600)
        secret_file(state, "users/" + name + "/password", password)
        user_keys[name] = secret_file(state, "users/" + name + "/api_key").read_text().strip()
    # The private parent directory is 0700; 0644 lets the unprivileged gateway
    # read this one bind-mounted secret inside its own container.
    write_json(state / "secrets/gateway.json", {
        "upstream": "http://host.docker.internal:" + str(cfg["backend_port"]),
        "upstream_key": upstream_key, "users": user_keys,
        "per_user_concurrency": 2, "global_concurrency": 3,
    }, mode=0o644)
    write_json(state / "config.json", cfg)
    write_json(state / "compose.json", compose_document(state, cfg))
    print("Initialized " + str(state) + "; existing keys and passwords preserved")


def serve(state):
    cfg = config(state)
    binary = Path(cfg["llm_venv"]) / "bin/vllm"
    if not binary.is_file():
        raise SystemExit("Install vLLM Metal first (scripts/bootstrap.sh).")
    env = os.environ.copy()
    env.update({
        "VLLM_API_KEY": (state / "secrets/upstream_api_key").read_text().strip(),
        "VLLM_METAL_MULTIMODAL_MODE": "text-only",
        "HF_HUB_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false",
    })
    command = [str(binary), "serve", cfg["model"],
               "--host", "127.0.0.1", "--port", str(cfg["backend_port"]),
               "--served-model-name", cfg["served_model_name"],
               "--max-model-len", str(cfg["max_model_len"]),
               "--max-num-seqs", str(cfg["max_num_seqs"]),
               "--max-num-batched-tokens", "2048",
               "--gpu-memory-utilization", str(cfg["gpu_memory_utilization"]),
               "--enable-auto-tool-choice", "--tool-call-parser", "gemma4",
               "--reasoning-parser", "gemma4"]
    if sys.platform == "darwin":
        command = ["/usr/bin/caffeinate", "-is", *command]
    os.execvpe(command[0], command, env)


def autostart_stack(state):
    if sys.platform != "darwin":
        raise SystemExit("Autostart requires macOS")
    run(["open", "-a", "Docker"])
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        r = subprocess.run([docker_bin(), "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode == 0:
            compose(state, "up", "-d", "--no-build")
            return
        time.sleep(5)
    raise SystemExit("Docker did not become available within five minutes")


def install_services(state):
    if sys.platform != "darwin":
        raise SystemExit("launchd is only available on macOS")
    base = Path.home() / "Library/LaunchAgents"
    base.mkdir(parents=True, exist_ok=True)
    logs = state / "logs"
    logs.mkdir(exist_ok=True)
    for label, command, keepalive in [(LABEL_LLM, "serve", True), (LABEL_STACK, "autostart", False)]:
        spec = {
            "Label": label, "ProgramArguments": [sys.executable, str(REPO / "scripts/manage.py"),
                                                 "--state", str(state), command],
            "RunAtLoad": True, "KeepAlive": keepalive, "ThrottleInterval": 30,
            "WorkingDirectory": str(REPO),
            "StandardOutPath": str(logs / (command + ".log")),
            "StandardErrorPath": str(logs / (command + ".error.log")),
            "EnvironmentVariables": {"PATH": str(Path.home() / ".local/bin") + ":/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
        }
        path = base / (label + ".plist")
        path.write_bytes(plistlib.dumps(spec))
        path.chmod(0o600)
        target = "gui/" + str(os.getuid())
        subprocess.run(["launchctl", "bootout", target + "/" + label],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(["launchctl", "bootstrap", target, path])
    print("Installed login services. They start when this macOS account logs in.")


def bundle(state):
    cfg = config(state)
    out = state / "connection-info"
    out.mkdir(exist_ok=True, mode=0o700)
    summary = {k: v for k, v in cfg.items() if k != "llm_venv"}
    tunnel = cfg.get("access_mode") == "tunnel"
    api_ip = cfg["cloudflare"]["api_ip"] if tunnel else cfg["listen_ip"]
    api_port = 4000 if tunnel else cfg["api_port"]
    summary["api_base_url"] = "http://" + api_ip + ":" + str(api_port) + "/v1"
    summary["repository"] = "https://github.com/ts06068/mac-studio-agents"
    summary["network_validation"] = "Check validation.json; host-local checks do not prove remote reachability."
    write_json(out / "server.json", summary)
    api_help = f"""LLM API: 외부 개발자 PC에서 사용하기

Base URL: {summary['api_base_url']}
Chat Completions: {summary['api_base_url']}/chat/completions
Models: {summary['api_base_url']}/models
모델명: {cfg['served_model_name']}
인증: Authorization: Bearer <사용자별 API 키>

1. 개발자 PC의 Cloudflare One Client를 {cfg['cloudflare_team']} 조직에 등록하고
   Traffic and DNS 모드로 연결하세요. 이 API는 Cloudflare 사설 주소입니다.
2. 본인의 사용자 폴더에서 llm.env와 curl-llm.sh를 같은 로컬 폴더로 다운로드하세요.
   macOS/Linux/Windows WSL 또는 Git Bash에서 curl 및 jq가 필요합니다.
3. 터미널에서 해당 폴더로 이동한 뒤 실행하세요. SSH 로그인은 필요하지 않습니다.

   chmod 600 llm.env
   bash curl-llm.sh --models
   bash curl-llm.sh "한국어로 짧게 인사해줘."
   MAX_TOKENS=2048 bash curl-llm.sh "Python으로 간단한 웹 크롤러를 작성해줘."
   bash curl-llm.sh --no-stream "1+1은?"

기본값은 스트리밍이며 최대 출력은 512토큰입니다. MAX_TOKENS로 출력 상한을 조절합니다.
긴 입력은 첫 출력까지 시간이 걸릴 수 있습니다. 기본 요청 timeout은 3600초입니다.
최대 컨텍스트는 입력과 출력 합계이며 --models 결과의 max_model_len에서 현재 값을 확인합니다.
여러 사용자가 같은 모델과 캐시 용량을 공유합니다.

직접 curl로 호출하려면 같은 폴더에서:

   source ./llm.env
   curl -sS "$OPENAI_BASE_URL/chat/completions" \\
     -H "Authorization: Bearer $OPENAI_API_KEY" \\
     -H "Content-Type: application/json" \\
     -d '{{"model":"{cfg['served_model_name']}","messages":[{{"role":"user","content":"안녕하세요"}}],"max_tokens":128}}' \\
     | jq -r '.choices[0].message.content'

연결 시간 초과: One Client 연결, 조직, Split Tunnels 및 접근 정책을 확인하세요.
HTTP 401: 본인의 llm.env 및 API 키를 확인하세요.
HTTP 429: 동시 요청 한도이므로 잠시 후 재시도하세요.

개발 컨테이너 내부에서는 http://gateway:4000/v1 주소를 사용합니다.
같은 curl-llm.sh를 복사하고 로그인 셸의 환경 변수를 사용해도 됩니다.
llm.env에는 개인 API 키가 있으므로 본인 계정으로 보관하세요.
"""
    write(out / "LLM_API.txt", api_help)
    for user in cfg["users"]:
        name, port = user["name"], user["ssh_port"]
        remote_ip = cfg["cloudflare"]["allocations"][name]["ssh_ip"] if tunnel else cfg["listen_ip"]
        remote_port = 22 if tunnel else port
        source = state / "secrets/users" / name
        dest = out / name
        dest.mkdir(exist_ok=True, mode=0o700)
        for filename in [name + ".pem", name + ".pem.pub"]:
            shutil.copy2(source / filename, dest / filename)
        hostkey = subprocess.check_output([
            docker_bin(), "exec", "msa-dev-" + name,
            "cat", "/etc/ssh/host-keys/ssh_host_ed25519_key.pub"], text=True).strip()
        host_pattern = remote_ip if remote_port == 22 else "[" + remote_ip + "]:" + str(remote_port)
        known = host_pattern + " " + hostkey + "\n"
        known += "[127.0.0.1]:" + str(port) + " " + hostkey + "\n"
        write(dest / "known_hosts", known)
        alias = "mac-studio-" + name
        write(dest / "ssh_config", "\n".join([
            "Host " + alias, "    HostName " + remote_ip,
            "    Port " + str(remote_port), "    User " + name,
            "    IdentityFile ~/.ssh/mac-studio/" + name + ".pem",
            "    IdentitiesOnly yes", "    StrictHostKeyChecking yes",
            "    UserKnownHostsFile ~/.ssh/mac-studio/known_hosts", "",
        ]))
        info = {"username": name, "password": (source / "password").read_text(),
                "hostname": alias, "container_hostname": "dev-" + name,
                "mac_hostname": cfg["hostname"], "ip": remote_ip, "port": remote_port,
                "lan_ip": cfg["lan_ip"], "local_ssh_port": port,
                "cloudflare_team": cfg["cloudflare_team"],
                "llm_api_base_url": summary["api_base_url"],
                "llm_api_key": (source / "api_key").read_text(),
                "model": cfg["served_model_name"], "model_source": cfg["model"]}
        write_json(dest / "connection.json", info)
        write(dest / "llm.env", "\n".join([
            "OPENAI_BASE_URL=" + shlex.quote(info["llm_api_base_url"]),
            "OPENAI_API_KEY=" + shlex.quote(info["llm_api_key"]),
            "LLM_MODEL=" + shlex.quote(info["model"]), "",
        ]))
        shutil.copy2(REPO / "scripts/curl-llm.sh", dest / "curl-llm.sh")
        write(dest / "LLM_API.txt", api_help)
        write_json(dest / "llm-api-connection.json", {
            "username": name, "base_url": summary["api_base_url"],
            "chat_completions_url": summary["api_base_url"] + "/chat/completions",
            "models_url": summary["api_base_url"] + "/models",
            "model": cfg["served_model_name"], "cloudflare_team": cfg["cloudflare_team"],
            "authentication": "Bearer", "api_key_file": "llm.env",
            "api_key_environment_variable": "OPENAI_API_KEY",
            "client_requirement": "Cloudflare One Client, Traffic and DNS",
            "example_script": "curl-llm.sh",
        })
        write(dest / "CONNECT.txt", f"""Mac Studio development environment — {name}

1. Enroll Cloudflare One Client in organization {cfg['cloudflare_team']}.
   The account administrator must allow your identity and device to connect.
   Use Traffic and DNS mode and route the server's private address through WARP.
2. Copy {name}.pem, known_hosts, and ssh_config to ~/.ssh/mac-studio/.
   chmod 700 ~/.ssh/mac-studio
   chmod 600 ~/.ssh/mac-studio/{name}.pem
3. Connect: ssh -F ~/.ssh/mac-studio/ssh_config {alias}
   Or: ssh -i {name}.pem -o UserKnownHostsFile=known_hosts -o StrictHostKeyChecking=yes -p {remote_port} {name}@{remote_ip}
4. VS Code Remote - SSH: select ~/.ssh/mac-studio/ssh_config as your SSH configuration file.

User: {name}
Password (SSH and sudo): {info['password']}
Cloudflare private IP: {remote_ip}
SSH port: {remote_port}
Local Mac LAN IP / forwarded SSH port: {cfg['lan_ip']} / {port}
SSH alias: {alias} (defined by ssh_config, not a public DNS record)
Container hostname: dev-{name}
Workspace: /workspace (persistent Docker volume)
Home: /home/{name} (persistent Docker volume)

LLM API from your own computer: {info['llm_api_base_url']}
LLM API inside your development container: http://gateway:4000/v1
Model name: {cfg['served_model_name']}
API key: stored in connection.json and llm.env in this directory.
Container shells automatically load their own API configuration.
For non-interactive SSH commands, source ~/.config/agent/env.sh explicitly.

The model uses 8-bit weights and the model's normal KV cache precision.
Maximum context is shared capacity, not a guarantee that all users can fill it simultaneously.
See server.json and validation.json for the measured server configuration.
Do not put these files or API keys in a public repository.
""")
    shutil.copy2(state / "config.json", out / "admin-config.json")
    if (state / "validation.json").exists():
        shutil.copy2(state / "validation.json", out / "validation.json")
    archive = state / "mac-studio-agents-connection-info.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, str(p.relative_to(out)))
    archive.chmod(0o600)
    print("Connection information: " + str(out))
    print("Private archive: " + str(archive))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    sub = p.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--users", required=True, help="Comma-separated Linux account names")
    init.add_argument("--listen-ip", required=True, help="Mac's Cloudflare Mesh IP or an explicit private interface")
    init.add_argument("--lan-ip", default="")
    init.add_argument("--hostname", default="")
    init.add_argument("--cloudflare-team", default="")
    init.add_argument("--password-file")
    init.add_argument("--ssh-port-start", type=int, default=2221)
    init.add_argument("--api-port", type=int, default=4000)
    init.add_argument("--backend-port", type=int, default=8001)
    init.add_argument("--model", default=DEFAULT_MODEL)
    init.add_argument("--served-model-name", default="gemma-4-31b")
    init.add_argument("--max-model-len", default="auto")
    init.add_argument("--gpu-memory-utilization", type=float, default=0.9)
    init.add_argument("--dev-memory", default="1536m")
    init.add_argument("--llm-venv", default="~/.venv-vllm-metal")
    init.add_argument("--with-dind", action="store_true")
    add = sub.add_parser("add-user", help="Append a workspace while preserving existing users")
    add.add_argument("name")
    add.add_argument("--password-file", type=Path)
    add.add_argument("--token-file", type=Path, default=DEFAULT_STATE.parent / "cloudflare-api-token")
    add.add_argument("--pool", default="10.210.0.0/16")
    add.add_argument("--apply", action="store_true")
    for cmd in ["up", "status", "down", "serve", "install-services", "autostart", "bundle"]:
        sub.add_parser(cmd)
    args = p.parse_args()
    state = args.state.expanduser().resolve()
    if state == REPO or REPO in state.parents:
        raise SystemExit("Runtime state and credentials must be outside the public repository")
    if args.command == "init":
        initialize(args, state)
    elif args.command == "add-user":
        from add_user import add_user
        add_user(args, state)
    elif args.command == "up":
        write_json(state / "compose.json", compose_document(state, config(state)))
        compose(state, "up", "-d", "--build", "--wait", "--wait-timeout", "300")
    elif args.command == "status":
        compose(state, "ps")
    elif args.command == "down":
        compose(state, "down")
    elif args.command == "serve":
        serve(state)
    elif args.command == "install-services":
        install_services(state)
    elif args.command == "autostart":
        autostart_stack(state)
    elif args.command == "bundle":
        bundle(state)


if __name__ == "__main__":
    main()
