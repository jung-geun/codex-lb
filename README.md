# Codex LB · subscription gateway fork

[Soju06/codex-lb](https://github.com/Soju06/codex-lb)를 확장한 자체 호스팅 게이트웨이입니다. Codex 계정과 모델 소스를 관리하고, 클라이언트에는 **게이트웨이 API 키 하나**로 모델 검색·추론·사용량 조회를 제공합니다.

- 기준 소스: **1.25.0-beta.9**, [`69f128afcbc616d9f8e924ca6583f7031d75cf82`](https://github.com/Soju06/codex-lb/commit/69f128afcbc616d9f8e924ca6583f7031d75cf82) ([기록](gateway/UPSTREAM_REVISION)). 이후 upstream `main` 변경을 모두 포함한 버전은 아닙니다.
- 공개 저장소: <https://github.com/jung-geun/codex-lb>. 기본 브랜치는 **`feature/subscription-gateway`**이며, `main`은 upstream 이력을 보존합니다. 이 포크의 설치·업데이트에는 기능 브랜치를 사용하세요.
- 원 저작자 Soju06와 기여자의 저작권 및 **MIT 라이선스**를 유지합니다. [라이선스](gateway/LICENSE), [서드파티 고지](gateway/THIRD_PARTY_LICENSES.md)를 확인하세요. 공식 upstream 배포판은 아닙니다.

## 구성

```text
gateway/           FastAPI 앱, React UI, Rust native helper, migration, upstream 문서·테스트
Dockerfile         gateway/ 소스의 다단계 빌드
compose.yaml       로컬 바인딩과 영구 데이터 volume
scripts/           실행·백업·진단 및 기존 클라이언트 연결 도구
tests/             wrapper의 연결 수명·인증·중복 실행 방지·OMP 회귀 테스트
integrations/omp/  OMP 플러그인 소스
```

upstream의 일반 설명은 [gateway/README.md](gateway/README.md)와 [gateway/docs/](gateway/docs/)를 참고하세요. upstream 이미지 설치 명령은 이 포크를 설치하지 않습니다. 이 저장소에서는 아래 **루트 Compose 절차**를 사용합니다.

## 설치

필수 환경은 Git, Bash, **curl**, Docker Engine 또는 Docker Desktop, 그리고 `docker compose up --wait`를 지원하는 Docker Compose v2입니다. `up.sh`의 마지막 검증 단계가 호스트의 curl을 사용합니다. 최초 빌드는 이미지·패키지 다운로드를 위한 인터넷 연결과 빌드 공간이 필요합니다. 호스트에 Rust/Bun/Python을 따로 설치할 필요는 없습니다. 루트 `Dockerfile`이 digest로 고정한 **Rust 1.96.0, Bun 1.4.2, Python 3.14, uv 0.12.12**를 사용해 native helper·UI·앱을 빌드합니다.

```bash
git clone --branch feature/subscription-gateway https://github.com/jung-geun/codex-lb.git
cd codex-lb
./scripts/up.sh
```

대시보드: <http://127.0.0.1:2455>

`up.sh`는 `.env`가 없으면 `.env.example`에서 만들고, `codex-lb-data` volume 및 Compose용 `codex-lb-net`·`backend` network를 생성한 뒤 빌드·기동·healthcheck·기본 경로 검사를 수행합니다. 기본 호스트 바인딩은 **127.0.0.1**이며, 2455는 대시보드/API, 1455는 Codex OAuth callback용입니다. 별도 reverse proxy가 자동 설치되지는 않습니다.

### 최초 관리자와 보안 설정

1. 대시보드에서 강한 비밀번호를 설정합니다. 최초 설정은 로컬 `admin` 계정을 만듭니다. 원격으로 인식되는 연결에는 최초 기동 로그의 **일회용 bootstrap token**도 필요합니다. Docker 네트워크를 경유하면 같은 컴퓨터에서도 원격으로 판정될 수 있습니다.

   ```bash
   docker compose logs codex-lb
   ```

   토큰·로그를 공개하지 마세요. 서버가 연결을 로컬로 판정한 경우에만 bootstrap token 입력을 생략할 수 있습니다.
2. **Settings → Access**에서 TOTP를 등록하고 복구용 관리자 접근을 보관합니다. 필요하면 별도의 일상용 관리자를 초대하세요. 유일한 복구 계정을 삭제하거나 비밀번호·2단계 인증 없이 외부 인증만 강제하지 마세요.
3. **Settings → API Key Auth**를 활성화하고, **API Keys → Create**에서 클라이언트용 게이트웨이 키를 만듭니다. 전체 키는 생성 시 한 번만 표시됩니다. 필요한 모델·계정·model source·과금 정책으로 권한을 제한하세요. Codex 계정 할당과 model source 할당은 별개입니다.
4. 외부 접속은 신뢰할 수 있는 인증서의 HTTPS reverse proxy 뒤에서 구성하세요. HTTP/WebSocket 전달과 신뢰할 proxy 범위를 명시하고, 2455·1455를 인터넷 전체에 직접 공개하지 마세요. 초기 설정을 마치기 전에 외부 접근을 열지 않는 것을 권장합니다.

자세한 내용: [최초 설정](gateway/docs/getting-started.md#remote-setup-bootstrap-token), [대시보드 인증](gateway/docs/authentication.md), [복구 계정](gateway/docs/sso.md), [API 키](gateway/docs/api-keys.md).

## 계정·모델 소스

**Accounts → Add account**에서 다음 경로를 선택합니다.

| 종류 | 연결 방식 |
| --- | --- |
| Codex 구독 | Codex OAuth 또는 지원되는 인증 파일 가져오기 |
| OpenAI / ChatGPT API | OpenAI API 키와 모델 설정 |
| Claude 구독 | Claude OAuth 또는 지원되는 자격 증명 가져오기 |
| Claude API | Anthropic API 키와 모델 설정 |

OAuth 인증·상위 공급자 API 키는 **서버에만** 등록합니다. 클라이언트에는 게이트웨이 키를 전달하며, upstream 토큰이나 API 키를 클라이언트 설정에 복사하지 않습니다. 동일 OAuth 인증을 복제한 서버를 동시에 실행하면 refresh token 경합이 생길 수 있습니다.

공식 API 소스의 모델별 한도·기능·가격을 확인하세요. 확인되지 않은 context/output 한도는 명시적으로 설정해야 검색에 포함될 수 있고, 가격이 비어 있으면 비용도 미확인으로 남습니다. 다른 공급자 preset/custom endpoint가 UI에 있더라도 **GLM·DeepSeek 등 추가 공급자의 실제 추론 성공을 보장하거나 검증했다고 주장하지 않습니다.** 모델 목록에 보이는 것과 해당 계정으로 추론에 성공하는 것은 다릅니다.

### 구독 우선과 유료 API fallback

API 소스의 **구독 사용 후 API 사용**(`api_fallback_only`, 기본 활성화)은 같은 공급자의 **정확히 같은 upstream 모델**을 지원하는 사용 가능한 구독을 먼저 사용하고, 처리 가능한 구독이 없을 때 API를 최후 수단으로 사용하도록 합니다. OpenAI와 Anthropic 사이의 교차 공급자 대체가 아닙니다.

유료 API 사용에는 키의 과금 정책 `any`, 해당 소스/모델 권한 및 사용 가능한 API 소스가 필요합니다. **`subscription_only`는 API 소스를 사용하지 않습니다.** 옵션을 끄면 API도 라우팅 정책에 참여하며, native Codex에서는 같은 Burn first / Normal / Preserve 계층일 때 구독이 우선하고 API가 더 낮은 계층의 구독보다 먼저 선택될 수 있습니다. 계정·모델 할당, quota, continuation 소유권 및 이미 수락된 작업의 안전 장벽을 우회하지 않습니다. API fallback에는 공급자의 실제 요금이 발생할 수 있습니다.

### 사용량의 의미

- 공급자 quota는 **관측 시점의 값**입니다. 게이트웨이의 기록 토큰·추정 용량·API 환산 비용은 공식 token cap이나 결제 잔액이 아닙니다.
- API rate-limit 잔여량은 짧은 시간창의 관측값이며 구독 총량이나 선불 잔액이 아닙니다. 가격 없는 사용량과 누락·오래된 관측은 **unknown**으로 남습니다. **미확인은 0이 아닙니다.**
- quota 소진, 인증 문제, 모델·소스 권한 제한은 목록 검색이나 재시작으로 우회할 수 없습니다. 실제 유료 fallback 성공도 소스 등록이나 모델 노출만으로 입증되지 않습니다.

## 클라이언트 연결

| 용도 | 로컬 경로 |
| --- | --- |
| Codex native Responses root | `http://127.0.0.1:2455/backend-api/codex` |
| 일반 OpenAI 호환 root | `http://127.0.0.1:2455/v1` |
| Claude native Messages | `POST http://127.0.0.1:2455/v1/messages` |
| Claude token count | `POST http://127.0.0.1:2455/v1/messages/count_tokens` |
| OMP 모델 manifest | `GET http://127.0.0.1:2455/v1/omp/models` |

Claude native 클라이언트의 base URL은 **suffix 없는 origin** `http://127.0.0.1:2455`입니다. Messages는 게이트웨이 키를 `x-api-key` 또는 `Authorization: Bearer`로 받습니다. 다른 클라이언트는 [연결 문서](gateway/docs/client-setup.md)를 참고하고, 공개 연결에는 위 HTTP 예시 대신 HTTPS origin을 사용하세요.

### OMP 권장: 별도 공개 플러그인

[omp-codex-lb](https://github.com/jung-geun/omp-codex-lb)의 설치·환경 설정을 권장합니다. 해당 문서의 지원 호스트는 **OMP 18.12.3**입니다. 게이트웨이는 별도로 설치해야 합니다.

```bash
omp plugin install https://github.com/jung-geun/omp-codex-lb.git
```

플러그인은 **HTTPS만 허용**합니다. 위의 로컬 HTTP 서버에 바로 연결하지 말고, 신뢰할 인증서를 사용하는 TLS reverse proxy를 먼저 구성하세요. 다음은 `https://localhost`에서 그 proxy를 운영하는 경우의 **Bash** 예시입니다. 실제 인증서가 유효한 게이트웨이 origin으로 바꾸되 `/v1`·`/backend-api/codex` suffix를 붙이지 않습니다.

```bash
export CODEX_LB_BASE_URL='https://localhost'
read -r -s -p 'Gateway API key: ' CODEX_LB_API_KEY; printf '\n'
export CODEX_LB_API_KEY
omp models refresh
omp models codex-lb --json
omp
```

인증서 검증을 끄지 마세요. 키는 secret manager나 숨김 입력으로 전달하고 채팅·명령 인자·저장소에 넣지 않습니다. 다른 키 환경 변수 이름을 사용한다면 `CODEX_LB_KEY_ENV`에 **값이 아닌 변수 이름**을 지정합니다. 환경 변경 후 OMP를 재시작하세요.

| OMP 명령 | 동작 |
| --- | --- |
| `/lb-connect` | origin·키 변수 이름·키 존재 여부와 연결 도움말 표시. 키 저장이나 OAuth 연결은 하지 않음 |
| `/lb-models` | OMP에 이미 등록된 `codex-lb` 모델 표시. 네트워크 refresh는 아님 |
| `/lb-quota` | 선택한 모델의 quota 조회. 추론 요청은 보내지 않음 |
| `/lb-update` | 공개 플러그인 릴리스 확인 및 수동 업데이트 안내. 자동 설치는 하지 않음 |
| `/model` | `codex-lb` 아래 실제 검색된 모델 선택 |

플러그인은 하나의 `codex-lb` provider 아래 모델별 native 프로토콜을 사용합니다. 모델/권한 변경 후 터미널에서 `omp models refresh`를 실행하고 OMP를 재시작하세요. 기존 `scripts/connect-omp.py` 설치 사용자는 플러그인 README의 이관 절차를 따라 **복사된 extension과 플러그인을 동시에 로드하지 않도록** 하고, 기존 `models.yml`·역할·다른 provider·키 설정은 보존합니다.

```bash
omp plugin upgrade omp-codex-lb
# 업그레이드 후 OMP 재시작
```

플러그인 업데이트는 서버를 배포하지 않으며, 서버 업데이트도 이미 설치된 플러그인을 자동 갱신하지 않습니다.

## 상태 확인·업데이트·백업

```bash
./scripts/verify.sh
./scripts/diagnose.sh
curl --fail http://127.0.0.1:2455/health/ready
```

정상 기동 후 업데이트하기 **전**, 실행 중인 컨테이너에서 DB와 암호화 키를 함께 백업합니다. 백업 스크립트는 호스트의 `python3`와 `shasum`도 필요합니다.

```bash
./scripts/backup.sh
git switch feature/subscription-gateway
git pull --ff-only origin feature/subscription-gateway
./scripts/up.sh
```

`backup.sh`는 SQLite online backup과 무결성 검사를 수행하고 `backups/codex-lb-<UTC timestamp>/`에 `store.db`, `encryption.key`, `SHA256SUMS`를 보관합니다. **DB만 복원하면 토큰을 복호화할 수 없습니다.** 같은 백업의 DB·암호화 키를 함께 보관하고, 별도 보호된 위치에도 복사하세요. 백업·`.env`·인증 파일·실제 세션·로그를 커밋하지 않습니다.

업데이트는 migration을 실행할 수 있으므로 변경 내용을 먼저 검토하고, 필요한 경우 운영 데이터와 격리된 환경에서 검증하세요. 진행 중인 요청을 안전하게 마칠 수 있는 점검 시간에 재기동합니다. **`codex-lb-data`를 삭제하거나 `docker compose down -v`로 초기화하지 마세요.** GitHub에 push하는 것만으로 실행 중인 서버가 자동 배포되지는 않습니다.

## 개발

호스트 개발에는 Python **3.13 이상**, uv, Bun(컨테이너와 맞추려면 1.4.2), native helper 작업에는 Rust(컨테이너 빌드는 1.96.0)가 필요합니다. root 배포와 달리 앱의 작업 디렉터리는 `gateway/`입니다.

```bash
cd gateway
uv sync --frozen
cd frontend
bun install --frozen-lockfile
bun run build
cd ../..
```

UI 산출물은 `gateway/app/static/`에 생성됩니다. 운영 OAuth·DB를 테스트에 사용하지 마세요. 루트에서 안전성 회귀를 실행하는 명령은 다음과 같습니다.

```bash
./scripts/test-lifetime.sh
```

이 스크립트는 `gateway/.venv/bin/python`, `PYTHONPATH=gateway`, gateway pytest 설정과 `tests.conftest`를 사용해 루트의 다섯 안전성 테스트 파일을 실행합니다. OMP installer 회귀는 추가로 `ruamel.yaml`을 필요로 하므로 별도로 실행합니다.

```bash
PYTHONPATH=gateway uv run --project gateway --with ruamel-yaml python -m pytest \
  -c gateway/pyproject.toml tests/test_omp_integration.py -q
```

앱·프런트엔드 전체 개발/검증 절차는 [gateway/README.md](gateway/README.md), [프런트엔드 가이드](gateway/frontend/README.md)를 참고하세요. 위 명령의 제공은 모든 환경에서 전체 upstream suite가 통과한다는 주장이 아닙니다.

## 알려진 한계와 안전한 복구

- upstream 연결 수명·capacity·문맥 제한·compaction timeout 및 WebSocket steering 제약을 없애는 게이트웨이는 아닙니다. 유휴 연결 교체는 단절 가능성을 줄이는 조치이며 진행 중 응답의 완주를 보장하지 않습니다.
- `stream_incomplete` 또는 `upstream_operation_status_unknown`은 앞선 작업이 수락됐지만 완료를 확인할 수 없는 상태일 수 있습니다. **불확정 작업을 자동 재전송하지 마세요.** 대화와 도구 실행 결과를 보존하고 실제 변경·완료 증거를 확인한 뒤 명시적으로 복구합니다. 재로그인·재시작·operation 기록 삭제는 안전한 재실행의 근거가 아닙니다.
- 모델 검색·quota 화면·합성 테스트는 실제 공급자 추론 성공이나 잔여 결제 잔액의 증명이 아닙니다. 공급자 약관·계정 권한·과금 및 사용 한도는 별도로 확인해야 합니다.
