# Codex LB

**Codex·Claude 구독과 API 계정을 하나의 웹 대시보드에서 관리하는 자체 호스팅 LLM 게이트웨이입니다.** OMP에서는 게이트웨이 키 하나로 연결하고, `codex-lb` 아래에서 GPT·Claude 모델을 선택할 수 있습니다.

[Soju06/codex-lb](https://github.com/Soju06/codex-lb)의 확장 포크입니다. 이 저장소의 설치·업데이트 브랜치는 **`feature/subscription-gateway`**입니다.

## OmniRoute 기반 새 게이트웨이

`omniroute/`의 별도 인스턴스를 **https://llm.pieroot.xyz**에서 운영합니다. 운영자 결정에 따라 기존 `codex.pieroot.xyz` 서비스는 안전하게 중지하며, 기존 DB·키·설정·이미지와 `codex-lb-data` 볼륨은 보존합니다. 기존 주소를 새 게이트웨이로 자동 전환하지 않습니다. 아래 Codex LB 설치 안내는 별도 설치 또는 명시적인 복구가 필요할 때만 사용하세요.

### 다른 사용자에게 배포하기

서버 변경은 [omniroute-subscriptions](https://github.com/jung-geun/omniroute-subscriptions)의 **빌드 시 적용하는 확장 패키지**로 분리합니다. 원본 OmniRoute **3.8.52 / `edab7c3a59bf7d6f040d581dbda3a46adbee0d57`** 소스에만 적용하며 변경 전후 체크섬을 확인합니다. 기존 훅 플러그인으로는 Next API·UI·native 완료 보호를 모두 등록할 수 없으므로 대시보드에서 바로 설치하는 런타임 플러그인으로 안내하지 않습니다. 설치·제거는 소스에만 적용되고 DB·키·OAuth·실행 중 서비스는 변경하지 않으며, 적용 후 빌드와 재시작이 필요합니다. 자세한 명령은 [서버 확장 안내](integrations/omniroute-extension/README.md)를 확인하세요.

OMP는 별도 [공개 클라이언트 플러그인](https://github.com/jung-geun/omp-codex-lb)을 설치하고 OMP 안에서 `/omniroute-connect https://your-gateway.example`로 자신의 서버 주소를 저장합니다. 키는 따로 환경/시크릿 매니저로 전달하며 채팅이나 명령 인자로 넣지 않습니다. 기존·재개 세션의 `codex-lb` 선택은 자동 이관되지 않습니다. 새 셸에서 키를 전달하고 시작한 뒤 `/model`에서 `omniroute/<모델>`을 명시적으로 고르세요.

### 간단 설치

Git·Docker Compose v2·OpenSSL이 필요합니다. 전체 웹 UI 소스 빌드는 큰 메모리를 사용하므로 빌더에 최소 24GB의 여유 메모리를 준비하세요. 빌드 heap 기본값은 16GB이며 `LLM_BUILD_MEMORY_MB`로 조정합니다.

```bash
git clone --branch feature/subscription-gateway https://github.com/jung-geun/codex-lb.git
cd codex-lb
./scripts/up-llm.sh
```

접속 주소는 `http://127.0.0.1:20148`입니다. 최초 관리자 비밀번호는 자동 생성되어 설치 폴더의 `.env.llm`에 저장됩니다. 이 파일과 `llm-pieroot-data` 볼륨을 비공개로 보관하세요. 원격 설치는 `ssh -L 20148:127.0.0.1:20148 user@server`로 접속합니다. `compose.llm.proxy.yaml`은 이미 `backend` Docker network가 있는 서버에만 쓰는 선택적 overlay입니다.

공개 도메인으로 운영할 때는 `LLM_PUBLIC_BASE_URL=https://your-gateway.example`을 설정하고 proxy overlay를 함께 적용하세요. overlay는 `OMNIROUTE_PUBLIC_BASE_URL`을 고정해 대시보드 origin 검사가 내부 컨테이너 주소와 충돌하지 않게 합니다. CSRF 보호나 origin 검사를 끄지 마세요.

1. **Providers**에서 Codex 또는 Claude OAuth를 연결합니다. OpenAI·Anthropic API 키는 해당 API 공급자로 별도 등록합니다. 기존 Codex LB의 암호화 DB·refresh token을 새 인스턴스에 그대로 복사하지 마세요. 동시 refresh는 기존 연결을 무효화할 수 있습니다.
2. **API Keys**에서 클라이언트용 게이트웨이 키를 발급하고 모델·연결 접근 범위를 설정합니다. OMP에 공급자 잔량을 표시하려면 **Shared Account Quota**를 명시적으로 켜세요. 이 권한을 끄면 quota는 숨겨집니다. 관리자 권한은 일반 추론 키에 부여하지 마세요.
3. **Settings**의 native subscription routing에서 구독 우선·정확히 같은 공급자/모델 API fallback을 명시적으로 켭니다. Subscription-only 키는 유료 API를 사용하지 않습니다. 완료 불명·계정 소유권이 있는 후속 요청은 자동 재전송하지 않습니다.
4. **API Keys**에서 키별 요청·입출력·캐시 토큰·공급자/모델 분포와 API 환산 비용을 확인합니다. **Costs / Logs / Provider Quota**는 기존 OmniRoute 화면을 사용합니다. 가격·사용량 누락은 `unknown`이며 구독 잔량·API rate limit·결제 잔액은 다른 값입니다.
5. quota 토큰 추정은 동일 계정·초기화 구간의 실측 변화가 충분할 때만 표시됩니다. Warm-up은 기본적으로 꺼져 있으며 실제 quota를 소비합니다. 공급자 사용량 새로고침과 구분하세요.

각 Codex·Claude 계정의 **구독 소진 후 크레딧 자동 사용**은 계정별로 명시적으로 허용합니다. 기존 허용/차단 설정은 유지하며, 새 계정은 기본적으로 차단합니다. 사용 가능한 구독 한도를 먼저 사용하고, 실제 소진 및 크레딧 사용 가능 여부가 확인된 경우에만 허용한 계정의 크레딧을 이어서 사용합니다. OpenAI의 크레딧 수량은 API의 달러 잔액이 아니고, Claude의 추가 사용 한도 잔여액은 충전 지갑 잔액이 아닙니다. 자동 구매·자동 충전 설정은 변경하지 않으며, 구독 전용 API 키의 추가 과금 제한도 유지합니다.

기본 설정에서는 크레딧을 허용한 계정도 남은 구독 한도를 먼저 사용합니다. 관리자가 별도로 지정한 예비 할당량·차단 기준에 도달하면 구독 잔량이 남은 상태로 대기할 수 있으며, 이를 “구독 소진”으로 간주해 크레딧을 조기 사용하지 않습니다. 크레딧 사용 허용은 이 게이트웨이의 새 요청에만 적용하고, 다른 클라이언트나 이미 진행 중인 요청을 중단하지 않습니다.

Codex는 **구독 풀 전체를 먼저** 씁니다. 어느 Codex 계정이든 주간 한도가 남아 있으면 그 계정을 먼저 쓰고, 모든 Codex 계정의 주간 한도가 소진된 뒤에야 크레딧을 씁니다. 5시간 한도만 찬 계정은 크레딧으로 넘어가지 않고 5시간 창이 다시 열릴 때까지 기다리며, 그동안 유료 API 대체도 하지 않습니다. 이전 대화 내용을 다시 보내는 요청은 주간 한도가 남은 계정으로 옮겨 처리하고, `previous_response_id`·Codex 턴 바인딩·암호화된 reasoning처럼 특정 계정에 묶인 요청은 다른 계정에 주간 한도가 남아 있으면 크레딧을 쓰지 않고 거절합니다.

Codex 계정에는 **크레딧 사용량 일·주 한도**를 따로 지정할 수 있습니다(계정 설정의 크레딧 자동 사용 아래). Codex가 보고하는 잔액 감소량을 최근 24시간·최근 7일 기준으로 합산하며, 다른 클라이언트가 쓴 양도 포함되고 충전은 사용량으로 세지 않습니다. 한도에 도달하면 이 게이트웨이는 그 계정의 크레딧 사용을 멈추고, 한도를 지정했는데 사용량을 아직 관측하지 못했으면 크레딧을 쓰지 않습니다. 요청 직전에 확인하므로 동시 요청이 있으면 한도를 조금 넘을 수 있으며, 공급자 측 결제 상한은 아닙니다.

### OMP 연결

활성 게이트웨이는 `omniroute` provider로 연결합니다. 기존 `codex-lb` provider·키·클라이언트 설정은 보존하지만 중지된 구 주소의 모델은 호출할 수 없습니다. 기존 키를 새 게이트웨이에 재사용하거나 `CODEX_LB_*` 설정을 새 주소로 조용히 바꾸지 마세요. 플러그인 패키지를 설치하거나 기존 설치를 업그레이드합니다.

```bash
omp plugin install https://github.com/jung-geun/omp-codex-lb.git
# 이미 설치했다면:
omp plugin upgrade omp-codex-lb
```

아래 키는 반드시 **새 OmniRoute 게이트웨이에서 발급**받으세요. 기존 게이트웨이의 같은 이름 키와도 별개의 자격 증명입니다. Codex/Claude 구독 토큰이나 공급자 API 키를 넣지 마세요. macOS zsh와 bash에서 모두 쓸 수 있는 숨김 입력 예시입니다.

```bash
export OMNIROUTE_BASE_URL='https://llm.pieroot.xyz'
printf 'OmniRoute gateway API key: '
IFS= read -r -s OMNIROUTE_API_KEY
printf '\n'
export OMNIROUTE_API_KEY
omp models refresh
omp models omniroute --json
omp --model omniroute/gpt-6.1-sol
```

Claude는 검색 결과에 해당 모델이 있으면 `omp --model omniroute/claude-opus-5-5`로 선택합니다. OMP의 `/model`에서 `omniroute` 모델을 고르고, `/omniroute-connect`로 연결 상태, `/omniroute-models`로 등록된 모델, `/omniroute-quota`로 선택 모델 quota를 확인합니다. 환경 변경이나 권한·계정·모델 접근 변경 뒤에는 `omp models refresh`를 실행하고 OMP를 재시작하세요. 키의 scopes와 entitlements는 새 게이트웨이에서 설정합니다. **Shared Account Quota**는 계정 풀 quota 표시를 원할 때만 선택적으로 활성화하며 모델 역할은 변경하지 않습니다. 모델 검색 결과는 추론 성공을 보장하지 않습니다.

연결된 Claude 계정이 제공하는 Haiku 5.5는 `omp --model omniroute/claude-haiku-5-5`로 선택합니다. 계정 모델 목록과 검증된 모델 사양을 사용하므로 정적 공급자 레지스트리에 아직 없는 모델도 검색할 수 있습니다. 알 수 없는 한도는 추측하지 않으며 기존 모델·연결 접근 제한은 그대로 적용합니다.

**Providers → Antigravity**의 활성 OAuth 연결에서 동기화된 채팅 모델을 표시합니다. Gemini·Gemini 4뿐 아니라 Antigravity에서 제공하는 Claude Opus·Sonnet과 GPT-OSS도 포함하며, 별도 Claude 구독 경로와 섞이지 않도록 `omniroute/antigravity/claude-opus-5-5-medium`처럼 공급자 경로를 유지합니다. Gemini 4가 IDE 프로필에서 검색되지 않으면 연결의 공식 **CLI** 프로필을 사용하고 모델 목록을 다시 동기화하세요. 검색과 호출은 같은 프로필을 사용합니다. 키의 연결·모델 권한과 숨김 설정을 반영하며, 검증된 컨텍스트·출력 한도가 없는 모델과 이미지 생성 전용 모델은 제외합니다. Antigravity quota는 아직 OMP에 제공하지 않습니다. 모델 목록 갱신은 추론을 실행하지 않습니다. OMP의 24시간 캐시와 명시적 `models.yml` 정적 항목은 서버의 현재 권한과 다를 수 있으므로 연결 변경 후 강제 갱신하고, 서버 인증을 최종 기준으로 삼으세요.

```bash
omp models refresh
omp --model omniroute/antigravity/gemini-3.8-flash-low
```

일반 플러그인 설치는 자격 증명을 저장하지 않습니다. 선택적으로 OMP 프로필의 `extensions/omniroute-v1.json`을 직접 만들 수 있습니다. 이 파일에는 키가 없고, 환경의 `OMNIROUTE_BASE_URL`이 설정되어 있으면 그 값이 우선합니다.

```json
{"schemaVersion":1,"baseOrigin":"https://llm.pieroot.xyz","keyEnv":"OMNIROUTE_API_KEY"}
```

업데이트는 `git pull --ff-only` 후 `./scripts/up-llm.sh`로 다시 빌드합니다. 먼저 볼륨·`.env.llm`을 백업하고 진행 중인 요청이 끝난 뒤 실행하세요. `docker compose down -v`는 데이터를 삭제하므로 사용하지 마세요.

---

## 기존 Codex LB 설치와 사용

## 제공 기능

| 기능 | 설명 |
| --- | --- |
| 계정 통합 관리 | Accounts → Add account에서 Codex 구독, Claude 구독, OpenAI API, Anthropic API 등록. 공급자별 분류와 이메일 중심 계정 표시 |
| 사용량 대시보드 | 계정별 quota·초기화 시각·이력·토큰 사용량·비용 리포트. 지원되는 구독 quota의 토큰 할당량 추정 |
| 라우팅 제어 | 계정 우선순위, 라우팅 일시정지, 정보 새로고침과 구독 warm-up |
| 구독 우선 사용 | 같은 공급자·같은 upstream 모델의 구독을 우선 사용하고, 사용할 수 없을 때 유료 API를 최후 수단으로 선택 |
| API 제공 | Codex native Responses, OpenAI 호환 API, Claude native Messages·token count. 게이트웨이 키별 모델·계정·소스 접근 제한 |
| OMP 연동 | 별도 플러그인으로 모델 검색·선택, 선택한 모델에 맞는 Codex/Claude quota 표시와 플러그인 업데이트 알림 |

quota는 공급자가 반환한 관측값이며, 추정 토큰은 공식 한도나 결제 잔액이 아닙니다. 알 수 없는 사용량·가격은 `unknown`으로 남습니다. GLM·DeepSeek 등 추가 공급자의 실제 추론 호환성은 별도 확인이 필요합니다.

## 1. 서버 설치

**준비:** Git, Bash, curl, 실행 중인 Docker와 Docker Compose v2(`up --wait` 지원). macOS는 Docker Desktop을 사용할 수 있습니다. 호스트에 Python·Bun·Rust를 따로 설치할 필요는 없습니다.

```bash
git clone --branch feature/subscription-gateway https://github.com/jung-geun/codex-lb.git
cd codex-lb
./scripts/up.sh
```

최초 실행은 소스 이미지 빌드로 시간이 걸릴 수 있습니다. 스크립트가 `.env`, 데이터 volume과 Docker network를 준비하고, 빌드·기동·상태 확인까지 수행합니다.

완료되면 **<http://127.0.0.1:2455>**에 접속합니다. 서버에 설치했다면 로컬 컴퓨터에서 SSH 터널을 열어 접속할 수 있습니다. `user@server`를 실제 SSH 주소로 바꾸세요. 1455는 Codex OAuth callback용입니다.

```bash
ssh -L 2455:127.0.0.1:2455 -L 1455:127.0.0.1:1455 user@server
```

### 최초 설정과 계정 등록

1. 대시보드에서 관리자 비밀번호를 설정합니다. bootstrap token을 요구하면 **서버의 저장소 폴더**에서 `docker compose logs codex-lb`로 최초 기동 토큰을 확인합니다. Docker 경유 접속은 같은 컴퓨터에서도 원격으로 인식될 수 있습니다.
2. **Settings → Access**에서 TOTP를 설정하고 복구용 관리자 접근을 보관합니다.
3. **Accounts → Add account**에서 구독 OAuth 연결·인증 파일 가져오기 또는 공급자 API 키 등록을 진행합니다. API 소스는 사용할 모델과 필요한 한도·가격 정보도 설정합니다.
4. **Settings → API Key Auth**를 활성화하고 **API Keys → Create**에서 **게이트웨이 API 키**를 생성합니다. 사용할 Codex 계정과 Claude/model source 및 모델 접근을 허용하세요. 계정 할당과 model source 할당은 별개입니다.

**OMP에 넣는 것은 4번의 게이트웨이 키입니다.** OpenAI·Anthropic API 키나 구독 OAuth 토큰을 클라이언트에 넣지 마세요. 유료 API fallback을 허용하려면 해당 키의 과금 정책이 `any`여야 합니다. `subscription_only`는 API를 사용하지 않습니다.

## 2. OMP 연결용 HTTPS 설정

**OMP 플러그인은 HTTPS만 허용합니다.** 이미 인증서가 유효한 HTTPS 주소가 있다면 이 단계는 건너뛰세요. 로컬 HTTP 주소를 그대로 OMP에 넣거나 인증서 검증을 끄면 안 됩니다.

<details>
<summary>HTTPS 주소가 없다면: Caddy로 구성하기</summary>

다음은 **공인 서버 + 도메인 + Caddy** 구성 예시입니다. `lb.example.com`을 실제 도메인으로 바꾸고, DNS의 A/AAAA 레코드가 서버를 가리키게 하세요. 서버의 TCP 80·443 포트가 외부에서 접근 가능하고 다른 서비스가 사용하지 않아야 합니다. 기존 reverse proxy가 있으면 새 Caddy 대신 그 proxy에서 `codex-lb:2455`로 전달하세요.

서버에서 최초 관리자 설정을 마친 뒤, **서버의 저장소 폴더에서** 실행합니다.

```bash
mkdir -p "$HOME/.config/codex-lb"
cat > "$HOME/.config/codex-lb/Caddyfile" <<'EOF'
lb.example.com {
    reverse_proxy codex-lb:2455
}
EOF

docker run -d --name codex-lb-https --restart unless-stopped \
  --network backend -p 80:80 -p 443:443 \
  -v "$HOME/.config/codex-lb/Caddyfile:/etc/caddy/Caddyfile:ro" \
  -v codex-lb-caddy-data:/data -v codex-lb-caddy-config:/config \
  caddy:2
```

게이트웨이가 **이 Caddy의 IP에서만** 전달 헤더를 신뢰하도록 설정합니다. 새 설치 기준이며, 이미 `compose.override.yaml`이 있다면 덮어쓰지 말고 아래 `environment` 항목을 기존 파일에 합치세요.

```bash
proxy_ip="$(docker inspect -f '{{(index .NetworkSettings.Networks "backend").IPAddress}}' codex-lb-https)"
test -n "$proxy_ip" || exit 1
cat > compose.override.yaml <<EOF
services:
  codex-lb:
    environment:
      FORWARDED_ALLOW_IPS: "$proxy_ip"
      CODEX_LB_FIREWALL_TRUST_PROXY_HEADERS: "true"
      CODEX_LB_FIREWALL_TRUSTED_PROXY_CIDRS: "$proxy_ip/32"
EOF
docker compose up -d --no-build --wait --wait-timeout 90
```

Caddy 컨테이너를 재생성해 IP가 바뀌면 이 설정도 갱신하세요. 신뢰 범위를 `*`로 넓히지 마세요.

Caddy가 HTTPS 인증서를 발급하고 HTTP/WebSocket을 게이트웨이에 전달합니다. 발급에는 위 DNS·접근 조건이 필요합니다. 브라우저에서 `https://lb.example.com`을 열거나 아래 명령으로 확인하세요. 앱의 2455·1455 포트는 기본 loopback 바인딩을 유지합니다.

```bash
curl --fail https://lb.example.com/health/ready
```

</details>

## 3. OMP 플러그인 설치·설정

**OMP가 설치된 클라이언트 컴퓨터에서** 실행합니다. 플러그인의 검증된 지원 호스트는 **OMP 18.12.3**입니다. [플러그인 저장소·상세 설명](https://github.com/jung-geun/omp-codex-lb).

```bash
omp plugin install https://github.com/jung-geun/omp-codex-lb.git
```

다음은 **Bash** 예시입니다. 주소를 실제 HTTPS origin으로 바꾸고, 숨김 입력에 앞에서 만든 **게이트웨이 API 키**를 입력합니다. 주소에 `/v1`이나 `/backend-api/codex`를 붙이지 않습니다.

```bash
export CODEX_LB_BASE_URL='https://lb.example.com'
read -r -s -p 'Gateway API key: ' CODEX_LB_API_KEY; printf '\n'
export CODEX_LB_API_KEY

omp models refresh
omp models codex-lb --json
omp
```

이 환경 변수는 현재 셸에만 적용됩니다. 다음 실행에도 같은 주소와 키가 전달되도록 shell/secret manager에서 설정하세요. 키를 명령 인자·채팅·저장소에 저장하지 마세요. 환경을 변경했으면 실행 중인 OMP를 재시작합니다.

OMP 안에서 **`/model` → `codex-lb` → 사용할 모델**을 선택합니다. 키에 허용된 모델만 검색되며, 목록에 보여도 quota 소진이나 공급자 오류로 추론이 거절될 수 있습니다.

| 명령 | 용도 |
| --- | --- |
| `/lb-connect` | 연결 주소·키 존재 여부와 설정 도움말 확인. 키 값은 표시하지 않음 |
| `/lb-models` | OMP에 등록된 모델 확인 |
| `/lb-quota` | 선택 모델에 해당하는 공급자의 quota 새로고침 |
| `/lb-update` | 플러그인 새 릴리스 확인과 수동 업데이트 안내 |

모델·권한을 바꾼 뒤에는 터미널에서 `omp models refresh`를 실행하고 OMP를 다시 시작하세요. quota를 컨텍스트 길이 옆에 배치하는 방법은 [상태 표시줄 설정](integrations/omp/README.md#how-to-place-quota-beside-context-length)을 참고하세요.

기존 `connect-omp.py` 사용자라면 [이관 절차](integrations/omp/README.md#how-to-preserve-an-existing-helper-installation)를 먼저 확인하세요. **기존 복사 extension과 플러그인을 동시에 로드하지 말고**, 모델 설정·역할·다른 provider·키는 보존합니다.

## 업데이트·상태 확인

서버 저장소 폴더에서 실행합니다. 백업에는 호스트의 `python3`와 `shasum`도 필요합니다. 진행 중인 요청을 마친 뒤 서버를 업데이트하세요.

```bash
./scripts/backup.sh
git pull --ff-only origin feature/subscription-gateway
./scripts/up.sh
./scripts/verify.sh
```

백업의 **DB와 `encryption.key`를 함께 보관**하세요. `codex-lb-data`를 삭제하거나 `docker compose down -v`로 초기화하지 마세요. 문제 진단은 `./scripts/diagnose.sh`와 `docker compose logs codex-lb`를 사용합니다.

OMP 플러그인은 클라이언트에서 별도로 업데이트하고 OMP를 재시작합니다.

```bash
omp plugin upgrade omp-codex-lb
```

플러그인은 기본적으로 하루 한 번 새 안정 릴리스를 확인해 알리고, 자동 설치하지 않습니다. GitHub **Watch → Custom → Releases**로 릴리스 알림을 구독할 수도 있습니다. 서버 push는 운영 서버 배포나 플러그인 업데이트를 자동 실행하지 않습니다.

## 참고

- [개발·기여](.github/CONTRIBUTING.md) · [앱 문서](gateway/docs/) · [다른 클라이언트 연결](gateway/docs/client-setup.md)
- upstream 기준은 [1.25.0-beta.9 / `69f128af`](gateway/UPSTREAM_REVISION)입니다. 이후 upstream `main` 변경을 모두 포함하지 않으며, 공식 upstream 배포판은 아닙니다. [MIT 라이선스](LICENSE)와 [서드파티 고지](gateway/THIRD_PARTY_LICENSES.md)를 유지합니다.
- `stream_incomplete`·`upstream_operation_status_unknown`이 발생하면 앞선 작업의 완료를 확인하기 전 자동 재전송하지 마세요. 대화·작업 결과를 보존하고 실제 실행 상태를 확인해야 합니다.
