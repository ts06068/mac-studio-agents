# Mac Studio Agents

Apple Silicon Mac 한 대에서 공용 LLM API와 사용자별 SSH 개발 환경을 운영합니다.
LLM은 macOS의 **vLLM Metal**, 개발 환경과 API 게이트웨이는 **Docker Desktop**에서 실행합니다.
원격 접속은 **Cloudflare One Client → Cloudflare Tunnel → 사용자별 사설 IP**를 사용합니다.

기본 모델은 **Gemma 4 31B Instruct 8bit**
(`mlx-community/gemma-4-31b-it-8bit`)입니다. 모델의 일반 BF16 KV 캐시를 사용하며
추가적인 저비트 KV 압축은 켜지 않습니다. 최대 컨텍스트는 `--max-model-len auto`로
현재 메모리 예산에 맞춰 산정하며, 모델 자체의 상한은 262,144 토큰입니다.

## 구성

```text
개발자 PC + Cloudflare One Client
  └─ Cloudflare 사설 네트워크 → Mac의 cloudflared 컨테이너
      ├─ 사용자별 사설 IP:22 → 사용자별 Ubuntu ARM64 SSH 컨테이너
      └─ API 사설 IP:4000/v1 → 사용자별 API 키를 검사하는 게이트웨이
                            └─ macOS vLLM Metal (127.0.0.1:8001)

개발 컨테이너 → http://gateway:4000/v1 → 같은 공용 LLM
```

- 사용자별 RSA PEM 개인 키, SSH 호스트 키, 홈 디렉터리, `/workspace`, 네트워크를 생성합니다.
- 사용자 이름이 Linux 로그인 ID입니다. 지정한 비밀번호는 SSH와 컨테이너 내 `sudo`에 적용됩니다.
- Python, Node.js/npm, Git, 컴파일 도구, tmux, Docker CLI를 제공합니다.
- 사용자별 API 키와 모델 서버 내부 키를 분리합니다. SSE 스트리밍과 오류 응답을 전달합니다.
- 기본 동시 요청 제한은 사용자당 2개, 전체 3개입니다. 초과 요청은 HTTP 429를 반환합니다.
- SSH와 API는 Cloudflare의 사설 /32 경로로 연결합니다. 로컬 검증용 loopback 및
  선택적으로 지정한 LAN IP에도 포트를 게시합니다.
- 기본 구성은 호스트 Docker 소켓을 개발자에게 제공하지 않습니다.
- `--with-dind`로 사용자별 Docker-in-Docker를 추가할 수 있습니다. 기본값은 꺼짐입니다.

이 프로젝트는 [ubuntu-docker](https://github.com/dr-you-group/ubuntu-docker)의
사용자별 개발 환경 및 원격 접속 운영 방식을 참고하되 Mac의 GPU 실행 구조에 맞춰 작성했습니다.

## 요구사항

- Apple Silicon, macOS 15 이상. 기본 31B 8bit 구성에는 통합 메모리 64GB 이상 권장.
- 약 35GB 이상의 모델 저장 공간 및 Docker 이미지/개발 작업용 추가 공간.
- Docker Desktop을 실행할 수 있는 macOS 사용자 세션.
- 원격 접속용 Cloudflare Zero Trust 조직과 각 PC의 Cloudflare One Client 등록.

64GB Mac에서 31B 16bit 모델은 가중치만 약 62GB가 필요하므로 개발 환경 및 KV 캐시와
함께 사용하기 어렵습니다. 8bit를 기본으로 사용하고 개발 환경의 메모리를 별도로 제한합니다.
개발 컨테이너 기본 한도는 사용자당 1536MiB/CPU 3개입니다. Docker VM의 한도와 별개입니다.
Docker VM의 기본 메모리가 부족하면 Docker Desktop 설정에서 조절하되 LLM 예산도 함께 조절하세요.

## 설치

```bash
git clone https://github.com/ts06068/mac-studio-agents.git
cd mac-studio-agents
bash scripts/bootstrap.sh
bash scripts/download-model.sh
```

Git이 아직 없는 새 Mac은 GitHub의 **Code → Download ZIP**으로 내려받아 압축을 푼 뒤
`bash scripts/bootstrap.sh`를 실행할 수 있습니다. Bootstrap은 사용자 경로에 `uv`, Python,
필요 시 Git을 설치합니다. Docker가 없으면 공식 설치 파일로 설치하고 표준 이용약관을 적용해
실행합니다. 회사/기관의 Docker Desktop 라이선스 조건은 해당 조직의 계약을 따릅니다.

모델 서버의 Python 패키지는 `requirements-metal.lock`에 고정되어 있습니다.
Python 3.12, vLLM 0.32.0, vLLM Metal 0.32.0, MLX 0.32.2를 기준으로 합니다.
가중치 라이선스는 코드의 MIT 라이선스와 별개로 해당 모델 저장소를 따릅니다.

## 계정과 서비스 생성

아래 조직명과 계정 이름은 예시입니다. 초기 상태는 loopback으로만 연결합니다.

```bash
scripts/msa init \
  --users alice,bob,carol \
  --listen-ip 127.0.0.1 \
  --cloudflare-team YOUR_TEAM \
  --hostname your-mac.local

scripts/msa up
scripts/msa serve
```

`init`은 비밀번호를 터미널에서 입력받습니다. 자동화에서는 `--password-file`로 외부의
권한 600 파일을 전달할 수 있습니다. 비밀번호를 명령 인수나 Git 추적 파일에 넣지 마세요.
동일한 사용자 목록으로 재실행하면 기존 개인 키, API 키, 비밀번호를 보존합니다.
Mac의 로컬 SSH 포트는 입력한 사용자 순서대로 2221부터 배정됩니다.
Cloudflare Tunnel을 구성하면 원격에서는 각 사용자의 별도 IP와 22번 포트를 사용합니다.

상태와 비밀정보의 기본 위치는 `~/.local/share/mac-studio-agents/runtime`이며, 공개
저장소 내부를 상태 경로로 지정할 수 없습니다. 비밀번호와 API 키는 이미지 빌드에 포함하지 않습니다.

서빙이 정상 동작한 뒤 포그라운드 서버를 `Ctrl-C`로 종료하고 자동 시작 서비스를 등록합니다.

```bash
scripts/msa install-services
scripts/msa status
```

등록된 두 LaunchAgent가 **해당 macOS 사용자가 로그인할 때** Docker 서비스와 LLM을 시작합니다.
FileVault 잠금 해제 전이나 로그인 전의 자동 시작을 제공하는 시스템 LaunchDaemon은 아닙니다.
LLM 실행 중에는 `caffeinate -is`가 시스템 유휴 절전을 방지합니다.

## Cloudflare 접속

Mac의 One Client 등록과 서버용 Tunnel 생성은 별도 작업입니다. 개발자 PC는 동일 조직의
One Client에 로그인하고 **Traffic and DNS** 모드를 사용해야 합니다.

Cloudflare Dashboard의 **Manage Account → Account API Tokens**에서 대상 계정에 한정한
토큰을 만듭니다. 필요한 권한은 `Cloudflare One Connector: cloudflared — Write`와
`Cloudflare One Networks — Write`입니다. 개인 API 토큰 화면에서는 Write 대신 Edit로 표시될 수 있습니다.
토큰을 공개 저장소 밖의 권한 600 파일에 저장한 뒤 실행합니다.

```bash
~/.local/share/mac-studio-agents/ops-venv/bin/python scripts/provision-cloudflare.py \
  --account-id YOUR_ACCOUNT_ID \
  --token-file ~/.local/share/mac-studio-agents/cloudflare-api-token \
  --pool 10.210.0.0/16 \
  --apply
scripts/msa bundle
```

스크립트는 기존 Cloudflare 경로와 겹치지 않는 /29 네트워크를 사용자마다 선택하고,
새 전용 Tunnel에 SSH 및 API용 /32 경로만 추가합니다. `--apply`는 이 프로젝트의 컨테이너와
네트워크를 다시 만들며 사용자 데이터 볼륨을 보존합니다. 진행 상태를 로컬에 저장하므로 재실행할 수 있습니다.

선택한 사설 IP 범위가 클라이언트의 **Split Tunnels 제외 목록에 없어야** 합니다.
Gateway TCP 프록시와 Network 정책도 접속을 허용해야 합니다. 기존 조직 설정은 자동 변경하지 않습니다.
기본 `10.210.0.0/16`이 조직에서 라우팅되지 않으면 적절한 `--pool`을 선택하거나 조직 설정을 조절하세요.

생성된 `connection.json`의 주소로 개발자 PC에서 검사합니다.

```bash
bash scripts/check-connection.sh API_PRIVATE_IP USER_SSH_IP
```

HTTPS 공개 도메인은 생성하지 않습니다. API의 HTTP 연결은 One Client와 Tunnel의 암호화된
사설 전송 경로를 사용합니다. API 키도 필요합니다.
[Cloudflare 사설 네트워크 문서](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/private-net/)

기기 간 Mesh 연결이 이미 구성된 조직에서는 Tunnel 없이 `init --listen-ip MESH_IP`를 사용할 수도 있습니다.
이 경우 Mac의 Mesh IP와 로컬 포트(2221부터)를 사용하며 기기 간 수신 연결 정책을 별도로 확인해야 합니다.

## 개발자 접속과 API

```bash
scripts/msa bundle
```

`runtime/connection-info/<사용자>`에 다음 파일이 생성됩니다.

| 파일 | 용도 |
|---|---|
| `<사용자>.pem` | 해당 사용자의 SSH 개인 키 |
| `known_hosts` | 실행 중인 컨테이너에서 확인한 SSH 호스트 키 |
| `ssh_config` | VS Code Remote SSH 및 OpenSSH 접속 설정 |
| `connection.json` | IP, hostname, 포트, ID, 비밀번호, LLM API 정보 |
| `llm.env` | 개발자 PC에서 사용하는 API 환경 변수 |
| `CONNECT.txt` | 설치 및 접속 안내 |
| `LLM_API.txt` | SSH 없이 외부 개발자 PC에서 API를 호출하는 안내 |
| `llm-api-connection.json` | API endpoint, 모델명, 인증 방식 및 키 파일 위치 |
| `curl-llm.sh` | 모델 조회, 스트리밍 및 일반 응답 curl 예제 |

부모 폴더에는 서버 설정과 전체 비공개 ZIP도 생성됩니다. 이 디렉터리는 비공개 저장소 또는
접근 권한이 제한된 Drive 폴더에만 보관하고 개발자에게는 본인 파일만 전달하세요.
`ssh_config`의 hostname은 클라이언트 별칭이며 자동으로 생성되는 공용 DNS 이름은 아닙니다.

개발 컨테이너의 로그인 셸에는 `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL`이 설정됩니다.

외부 개발자 PC에서는 One Client에 연결한 뒤 본인의 `llm.env`와 `curl-llm.sh`를 같은
폴더에 다운로드하고 아래 명령을 실행합니다. SSH 로그인은 필요하지 않습니다.

```bash
chmod 600 llm.env
bash curl-llm.sh --models
bash curl-llm.sh "한국어로 짧게 인사해줘."
MAX_TOKENS=2048 bash curl-llm.sh "Python으로 웹 크롤러를 작성해줘."
bash curl-llm.sh --no-stream "1+1은?"
```

`curl`과 `jq`가 필요합니다. 스크립트는 같은 폴더의 `llm.env`를 자동으로 읽으며,
파일이 없으면 현재 셸의 환경 변수를 사용합니다. 기본 출력 상한은 512토큰이고
스트리밍으로 표시합니다. `--no-stream`은 생성이 끝난 뒤 전체 답변을 표시합니다.

```python
import os
from openai import OpenAI

client = OpenAI()
response = client.chat.completions.create(
    model=os.environ["LLM_MODEL"],
    messages=[{"role": "user", "content": "간단한 Python 예제를 작성해줘."}],
    max_tokens=512,
)
print(response.choices[0].message.content)
```

Python 프로젝트별 가상환경을 만든 뒤 필요한 SDK를 설치하세요.
서버는 텍스트 Agent 개발용으로 설정되어 있습니다. 최대 컨텍스트 확보를 위해 비전 경로는 비활성화합니다.
게이트웨이의 `/v1/embeddings` 등의 경로 허용은 현재 생성 모델의 임베딩 기능 지원을 의미하지 않습니다.
긴 입력의 prefill을 위해 게이트웨이 응답 대기 시간을 1시간으로 설정했습니다.
클라이언트 SDK의 timeout도 작업 크기에 맞게 조절하세요.

## 컨텍스트와 메모리

`runtime/config.json`의 `max_model_len`, `gpu_memory_utilization`, `max_num_seqs`를 조절할 수 있습니다.

- `max_model_len: "auto"`: 메모리에 들어가는 최대 길이를 계산합니다. 모델 상한을 넘지 않습니다.
- `gpu_memory_utilization: 0.9`: vLLM Metal의 메모리 예산 비율입니다. 서버 로그의 실제 예산을 확인하세요.
- `max_num_seqs: 3`: 스케줄러가 동시에 처리하는 시퀀스 수입니다.
- BF16 KV 캐시를 유지합니다. 컨텍스트 확장을 위한 추가 저비트 압축은 기본으로 사용하지 않습니다.
- 최대 길이는 입력과 출력을 합한 값입니다. 전체 캐시를 세 요청이 공유하므로 최대 길이의 요청
  세 개가 동시에 들어간다고 보장하지 않습니다. 서버는 요청을 대기시키거나 선점할 수 있습니다.

M5 Max / 통합 메모리 64GB에서 이 구성으로 측정한 최대 길이는 **141,824 토큰**입니다.
141,760 입력 토큰과 4 출력 토큰의 요청을 약 644초에 완료했습니다. 반복 토큰으로
캐시 용량과 실행을 검증한 결과이며 장문 추론 품질 평가는 아닙니다. 시작 시 다른 프로그램의
메모리 사용량에 따라 자동 산정값이 달라질 수 있으며 `/v1/models`의 `max_model_len`에서 확인할 수 있습니다.

설정을 변경한 뒤 다음 명령으로 모델 서버를 재시작합니다.

```bash
launchctl kickstart -k "gui/$(id -u)/org.mac-studio-agents.llm"
```

## 운영

기존 사용자와 설정을 유지하며 계정을 추가하려면:

```bash
scripts/msa add-user dave --apply
scripts/msa bundle
```

새 계정의 비밀번호를 입력하면 독립된 SSH 키·API 키·작업 볼륨을 생성합니다.
자동화에서는 `--password-file`을 사용할 수 있습니다. Cloudflare 관리 토큰은 기본적으로
`~/.local/share/mac-studio-agents/cloudflare-api-token`에서 읽으며 `--token-file`로 변경할 수 있습니다.
기존 IP 및 키는 유지하고 새 사용자의 사설 경로를 추가합니다. 기존 개발 컨테이너와
실행 중인 Tunnel에는 새 네트워크만 연결합니다. 모델이 유휴 상태가 된 뒤 API 게이트웨이를
짧게 재시작하여 새 키를 반영합니다. LLM 서버는 재시작하지 않습니다.
같은 사용자 이름으로 재실행하면 발급된 키와 비밀번호를 보존합니다.
추가 전 설정은 로컬 `runtime/backups/add-user-<이름>`에 보관합니다.
사용자가 늘어도 모델의 동시 요청 한도와 GPU 메모리 예산은 유지됩니다.
접속 파일을 Drive에 저장할 때는 대상 폴더의 공유 권한을 확인하세요.

```bash
scripts/msa status       # 컨테이너 상태
scripts/msa down         # 컨테이너 정지; 볼륨 보존
scripts/msa up           # 빌드 및 시작
scripts/msa bundle       # 현재 호스트 키와 접속 정보 다시 내보내기
~/.local/share/mac-studio-agents/ops-venv/bin/python scripts/verify.py
```

LLM 로그: `runtime/logs/serve.log`, `runtime/logs/serve.error.log`.
게이트웨이는 요청 본문과 API 키를 접근 로그에 기록하지 않습니다.
Docker 로그는 크기를 제한합니다. 장기 운영 시 launchd 로그를 별도로 순환/보관하세요.

`down`은 macOS의 LLM LaunchAgent를 정지하지 않습니다. 중지하려면:

```bash
launchctl bootout "gui/$(id -u)/org.mac-studio-agents.llm"
launchctl bootout "gui/$(id -u)/org.mac-studio-agents.stack"
```

Docker Desktop을 초기화하거나 볼륨을 삭제하면 사용자 작업이 사라질 수 있으므로
중요 프로젝트는 Git 원격 저장소와 별도 백업을 함께 사용하세요.
선택적인 DinD는 privileged 컨테이너이며 VM 수준의 사용자 격리를 보장하지 않습니다.

## 개발 검증

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r gateway/requirements.txt pytest
.venv/bin/python -m pytest -q
bash -n scripts/*.sh scripts/msa dev/entrypoint.sh
```

인증 누락/오류, 내부 API 키 분리, SSE 전달, 동시 요청 제한, 실패 시 슬롯 반환,
과대 입력 거부, 사용자별 네트워크와 비밀정보 분리, 기존 Cloudflare 경로와 충돌하지 않는
주소 할당을 테스트합니다. `scripts/verify.py`는 실제 SSH 키 로그인, 컨테이너 안팎의 API,
SSE 스트리밍, Gemma 4 자동 도구 호출을 확인하고 비밀정보 없이 결과를 로컬에 기록합니다.
