import importlib.util
import ipaddress
import json
import sys
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location("manage", Path(__file__).resolve().parents[1] / "scripts/manage.py")
manage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(manage)
sys.modules["manage"] = manage
cf_spec = importlib.util.spec_from_file_location("cloudflare", Path(__file__).resolve().parents[1] / "scripts/provision-cloudflare.py")
cloudflare = importlib.util.module_from_spec(cf_spec)
cf_spec.loader.exec_module(cloudflare)
sys.modules["provision-cloudflare"] = cloudflare
add_spec = importlib.util.spec_from_file_location("add_user", Path(__file__).resolve().parents[1] / "scripts/add_user.py")
add_user = importlib.util.module_from_spec(add_spec)
add_spec.loader.exec_module(add_user)


def test_user_validation_rejects_shell_and_path_injection():
    for names in [["root/../alice"], ["alice;id"], ["alice", "alice"], []]:
        with pytest.raises(ValueError):
            manage.validate_users(names)


def test_workspaces_cannot_see_each_others_keys_or_networks(tmp_path):
    cfg = {"users": [{"name": "alice", "ssh_port": 2221}, {"name": "bob", "ssh_port": 2222}],
           "listen_ip": "100.96.0.10", "api_port": 4000, "dev_memory": "1536m",
           "dev_cpus": 3, "dind": False, "served_model_name": "gemma-4-31b"}
    doc = manage.compose_document(tmp_path, cfg)
    a, b = doc["services"]["dev-alice"], doc["services"]["dev-bob"]
    assert set(a["networks"]).isdisjoint(b["networks"])
    assert {x["source"] for x in a["secrets"]}.isdisjoint(x["source"] for x in b["secrets"])
    assert not any("docker.sock" in str(x) for x in a["volumes"])
    assert not a.get("privileged", False)
    assert {x["host_ip"] for x in a["ports"]} == {"127.0.0.1", "100.96.0.10"}
    assert "gateway_config" not in str(a["secrets"])
    assert set(doc["services"]["gateway"]["networks"]) == {"user_alice", "user_bob"}


def test_cloudflare_allocator_avoids_existing_routes_and_exhaustion():
    routes = [{"network": "10.210.0.2/32"}, {"network": "10.210.0.8/29"}]
    allocations = cloudflare.allocate("10.210.0.0/24", ["alice", "bob"], routes)
    assert allocations["alice"]["subnet"] == "10.210.0.16/29"
    assert allocations["bob"]["subnet"] == "10.210.0.24/29"
    for a in allocations.values():
        subnet = ipaddress.ip_network(a["subnet"])
        assert all(ipaddress.ip_address(a[k]) in subnet for k in ["ssh_ip", "api_ip", "connector_ip"])
        assert len({a[k] for k in ["ssh_ip", "api_ip", "connector_ip"]}) == 3
    with pytest.raises(ValueError):
        cloudflare.allocate("10.210.0.0/29", ["alice"], routes)


def test_tunnel_networks_and_tokens_are_scoped_to_services(tmp_path):
    allocations = cloudflare.allocate("10.210.0.0/24", ["alice", "bob"], [])
    cfg = {"users": [{"name": "alice", "ssh_port": 2221}, {"name": "bob", "ssh_port": 2222}],
           "listen_ip": "127.0.0.1", "api_port": 4000, "dev_memory": "1536m", "dev_cpus": 3,
           "dind": False, "served_model_name": "gemma-4-31b", "access_mode": "tunnel",
           "cloudflare": {"allocations": allocations, "image": "cloudflare/cloudflared:2026.10.3"}}
    doc = manage.compose_document(tmp_path, cfg)
    for name in ["alice", "bob"]:
        dev = doc["services"]["dev-" + name]
        assert list(dev["networks"]) == ["user_" + name]
        assert dev["networks"]["user_" + name]["ipv4_address"] == allocations[name]["ssh_ip"]
        assert "cloudflared_token" not in str(dev["secrets"])
        assert {p["host_ip"] for p in dev["ports"]} == {"127.0.0.1"}
    assert doc["services"]["cloudflared"]["sysctls"]["net.ipv4.ip_forward"] == "0"
    assert doc["services"]["gateway"]["sysctls"]["net.ipv4.ip_forward"] == "0"


def test_add_user_preserves_existing_workspace_and_is_retry_safe(tmp_path, monkeypatch):
    cfg = {"users": [{"name": "alice", "ssh_port": 2221}], "api_port": 2222,
           "backend_port": 8001, "listen_ip": "127.0.0.1", "dev_memory": "1536m",
           "dev_cpus": 3, "dind": False, "served_model_name": "gemma-4-31b", "access_mode": "tunnel",
           "cloudflare": {"allocations": cloudflare.allocate("10.210.0.16/29", ["alice"], []),
                          "tunnel_id": "own-tunnel", "image": "cloudflare/cloudflared:2026.10.3"}}
    cfg["cloudflare"]["api_ip"] = cfg["cloudflare"]["allocations"]["alice"]["api_ip"]
    manage.write_json(tmp_path / "config.json", cfg)
    before = manage.compose_document(tmp_path, cfg)["services"]["dev-alice"]
    manage.write_json(tmp_path / "cloudflare-state.json", {
        "tunnel_id": "own-tunnel", "allocations": cfg["cloudflare"]["allocations"], "route_ids": {}})
    manage.write_json(tmp_path / "secrets/gateway.json", {
        "users": {"alice": "existing-api-key"}, "upstream_key": "internal-key", "global_concurrency": 3})
    manage.write(tmp_path / "secrets/users/alice/password", "existing-password")

    class Client:
        def __init__(self):
            self.saved = [{"network": "10.210.0.0/28", "tunnel_id": "other-tunnel", "id": "occupied"}]
            self.posts = 0

        def routes(self):
            return self.saved.copy()

        def request(self, method, path, data):
            assert (method, path) == ("POST", "/teamnet/routes")
            self.posts += 1
            route = {**data, "id": "new-route"}
            self.saved.append(route)
            return {"result": route}

    def fake_keygen(command):
        assert command[0] == "ssh-keygen"
        key = Path(command[-1])
        key.write_text("new-private-key")
        Path(str(key) + ".pub").write_text("new-public-key")

    monkeypatch.setattr(add_user, "run", fake_keygen)
    client = Client()
    first = add_user.provision_user(tmp_path, "bob", "new-password", client)
    assert first["users"] == [{"name": "alice", "ssh_port": 2221}, {"name": "bob", "ssh_port": 2223}]
    assert first["cloudflare"]["allocations"]["bob"]["subnet"] == "10.210.0.24/29"
    assert first["cloudflare"]["api_ip"] == cfg["cloudflare"]["api_ip"]
    assert manage.compose_document(tmp_path, first)["services"]["dev-alice"] == before
    key = (tmp_path / "secrets/users/bob/api_key").read_text()
    second = add_user.provision_user(tmp_path, "bob", "ignored-on-retry", client)
    assert second == first and client.posts == 1
    assert (tmp_path / "secrets/users/bob/api_key").read_text() == key
    assert (tmp_path / "secrets/users/bob/password").read_text() == "new-password"
    assert (tmp_path / "secrets/users/alice/password").read_text() == "existing-password"
    gateway = json.loads((tmp_path / "secrets/gateway.json").read_text())
    assert gateway["users"] == {"alice": "existing-api-key", "bob": key}
    assert gateway["upstream_key"] == "internal-key" and gateway["global_concurrency"] == 3
    assert json.loads((tmp_path / "backups/add-user-bob/config.json").read_text()) == cfg
    client.saved[-1]["tunnel_id"] = "another-tunnel"
    with pytest.raises(ValueError, match="belongs to another tunnel"):
        add_user.provision_user(tmp_path, "bob", "password", client)

