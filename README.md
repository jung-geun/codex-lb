# Codex LB

**Codex·Claude 구독과 API 계정을 하나의 웹 대시보드에서 관리하는 자체 호스팅 LLM 게이트웨이입니다.** OMP에서는 게이트웨이 키 하나로 연결하고, `codex-lb` 아래에서 GPT·Claude 모델을 선택할 수 있습니다.

[Soju06/codex-lb](https://github.com/Soju06/codex-lb)의 확장 포크입니다. 이 저장소의 설치·업데이트 브랜치는 **`feature/subscription-gateway`**입니다.

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
