#!/usr/bin/env python3
"""Verify the deployed API and SSH routes without printing credentials.

Run after `scripts/msa bundle`. The calling Mac must be connected to the
organization's Cloudflare One Client. Uses only Python's standard library.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

from manage import DEFAULT_STATE, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    args = parser.parse_args()
    state = args.state.expanduser().resolve()
    cfg = json.loads((state / "config.json").read_text())
    result = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "access_mode": cfg.get("access_mode", "direct"), "users": {}}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for user in cfg["users"]:
        name = user["name"]
        folder = state / "connection-info" / name
        info = json.loads((folder / "connection.json").read_text())
        req = urllib.request.Request(info["llm_api_base_url"] + "/models",
                                     headers={"Authorization": "Bearer " + info["llm_api_key"]})
        with opener.open(req, timeout=20) as response:
            models = json.load(response)["data"]
        assert any(m["id"] == info["model"] for m in models)
        ssh = ["ssh", "-i", str(folder / (name + ".pem")), "-p", str(info["port"]),
               "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "IdentitiesOnly=yes",
               "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + str(folder / "known_hosts"),
               name + "@" + info["ip"]]
        identity = subprocess.check_output(ssh + ["id -un"], text=True, timeout=30).strip()
        assert identity == name
        command = ('. "$HOME/.config/agent/env.sh"; '
                   'curl --fail --silent --show-error --max-time 20 '
                   '-H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"')
        inside = json.loads(subprocess.check_output(ssh + [command], text=True, timeout=40))
        assert any(m["id"] == info["model"] for m in inside["data"])
        result["users"][name] = {"ssh_key": True, "api_key": True, "api_from_workspace": True,
                                 "ip": info["ip"], "port": info["port"]}
    model = cfg["served_model_name"]
    base, key = info["llm_api_base_url"], info["llm_api_key"]

    def post(payload):
        req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        return opener.open(req, timeout=600)

    common = {"model": model, "temperature": 0, "chat_template_kwargs": {"enable_thinking": False}}
    with post({**common, "messages": [{"role": "user", "content": "Reply with the single word READY."}],
               "max_tokens": 32, "stream": True}) as response:
        stream = response.read().decode()
    assert "data: [DONE]" in stream and '"content"' in stream
    result["sse_streaming"] = True
    with post({**common, "messages": [{"role": "user", "content": "Use get_weather to check the weather in Seoul."}],
               "max_tokens": 256, "tool_choice": "auto", "tools": [{"type": "function", "function": {
                   "name": "get_weather", "description": "Return current weather for a city",
                   "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                                  "required": ["city"]}}}]}) as response:
        answer = json.load(response)
    calls = answer["choices"][0]["message"].get("tool_calls") or []
    assert calls and calls[0]["function"]["name"] == "get_weather", answer
    assert "seoul" in json.loads(calls[0]["function"]["arguments"])["city"].lower()
    result["automatic_tool_calling"] = True
    result["reported_max_model_len"] = models[0].get("max_model_len")
    write_json(state / "network-agent-validation.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
