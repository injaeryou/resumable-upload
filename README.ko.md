# Resumable Upload

[![Python Version](https://img.shields.io/pypi/pyversions/resumable-upload.svg)](https://pypi.org/project/resumable-upload/)
[![PyPI Version](https://img.shields.io/pypi/v/resumable-upload.svg)](https://pypi.org/project/resumable-upload/)
[![PyPI Downloads](https://img.shields.io/pepy/dt/resumable-upload)](https://pepy.tech/projects/resumable-upload)
[![License](https://img.shields.io/pypi/l/resumable-upload.svg)](https://github.com/injaeryou/resumable-upload/blob/main/LICENSE)

[English](README.md) | **한국어**

서버와 클라이언트를 위한 [TUS 재개 가능 업로드 프로토콜](https://tus.io/) v1.0.0의 Python 구현체로, 런타임 의존성이 없습니다.

## ✨ 특징

- 🚀 **의존성 없음**: Python 표준 라이브러리만 사용 (코어 기능에 외부 의존성 없음)
- 📦 **서버 & 클라이언트**: 양쪽 모두 완전한 구현
- 🔄 **재개 기능**: 중단된 업로드 자동 재개
- ✅ **데이터 무결성**: 청크별 체크섬 (`sha1`/`sha256`/`sha512`/`md5`), 헤더 또는 HTTP 트레일러로 전송
- 🔁 **재시도 로직**: 지수 백오프를 사용한 내장 자동 재시도
- 📊 **진행률 추적**: `UploadStats` 콜백으로 상세한 업로드 진행률 제공
- 🌐 **웹 프레임워크 지원**: Flask, FastAPI, Django 통합
- 🐍 **Python 3.9+**: Python 3.9부터 3.14까지 지원
- 🏪 **스토리지 백엔드**: SQLite 기반 스토리지 (커스텀 백엔드로 확장 가능)
- 🔐 **TLS 지원**: 인증서 검증 제어 및 mTLS 인증
- 📝 **URL 스토리지**: 세션 간 업로드 URL 유지
- ⬇️ **다운로드 엔드포인트**: 완료된 업로드를 GET으로 서빙 (tusd 스타일, opt-in, 안전한 헤더 기본값)
- 🎯 **TUS 프로토콜 준수**: TUS v1.0.0 코어 + 모든 확장 — creation, creation-with-upload, creation-defer-length, termination, checksum, expiration, concatenation, concatenation-unfinished

## 📦 설치

### uv 사용 (권장)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv pip install resumable-upload
```

### pip 사용

```bash
pip install resumable-upload
```

### 선택 extras

코어 설치는 런타임 의존성이 없습니다. 필요한 기능만 골라 설치하세요:

```bash
pip install "resumable-upload[async]"         # AsyncTusClient (httpx)
pip install "resumable-upload[s3]"            # AWS S3 스토리지 백엔드
pip install "resumable-upload[gcs]"           # Google Cloud Storage 백엔드
pip install "resumable-upload[azure]"         # Azure Blob Storage 백엔드
pip install "resumable-upload[redis]"         # RedisLockBackend
pip install "resumable-upload[all-storage]"   # 클라우드 백엔드 전체
pip install "resumable-upload[all]"           # 선택 기능 전체
```

## 🚀 빠른 시작

### 기본 서버

```python
from http.server import HTTPServer
from resumable_upload import TusServer, TusHTTPRequestHandler, SQLiteStorage

storage = SQLiteStorage(db_path="uploads.db", upload_dir="uploads")
tus_server = TusServer(storage=storage, base_path="/files")


class Handler(TusHTTPRequestHandler):
    pass


Handler.tus_server = tus_server
server = HTTPServer(("0.0.0.0", 8080), Handler)
print("서버가 http://localhost:8080 에서 실행 중입니다")
server.serve_forever()
```

### 기본 클라이언트

```python
from resumable_upload import TusClient, UploadStats


def progress(stats: UploadStats):
    print(
        f"진행률: {stats.progress_percent:.1f}% | "
        f"{stats.uploaded_bytes}/{stats.total_bytes} 바이트 | "
        f"속도: {stats.upload_speed_mbps:.2f} MB/s"
    )


client = TusClient("http://localhost:8080/files")
upload_url = client.upload_file(
    "large_file.bin", metadata={"filename": "large_file.bin"}, progress_callback=progress
)
print(f"업로드 완료: {upload_url}")
```

### 비동기 클라이언트

```python
# Async client — pip install "resumable-upload[async]"
import asyncio
from resumable_upload import AsyncTusClient


async def main():
    async with AsyncTusClient("http://localhost:8080/files") as client:
        url = await client.upload_file("large_file.bin")


asyncio.run(main())
```

동기 `TusClient`의 모든 메서드(업로드, 재개, 삭제, concatenation, `parallel_uploads=N`, 프로토콜 조회)는 awaitable 등가 메서드를 갖습니다.

### 체크섬 알고리즘

`sha1`, `sha256`, `sha512`, `md5` 중 원하는 조합을 advertise하고 검증합니다:

```python
TusServer(storage=..., checksum_algorithms=("sha1", "sha256"))
```

클라이언트도 알고리즘 선택:

```python
TusClient("...", checksum="sha256")
```

### 클라이언트 훅 + URL 스토리지

관측/재시도 세밀 제어:

```python
def before(method, url, headers):
    print(f"-> {method} {url}")


def after(method, url, status):
    print(f"<- {method} {status}")


def should_retry(err, attempt):
    return not isinstance(err, PermissionError)


client = TusClient(
    "...",
    before_request=before,
    after_response=after,
    on_should_retry=should_retry,
)
```

URL 스토리지 3종 (모두 `URLStorage` ABC 구현):

- `FileURLStorage` — JSON 파일, flock으로 멀티프로세스 안전
- `SQLiteURLStorage` — DB 기반, 멀티프로세스 클라이언트 권장 기본값
- `InMemoryURLStorage` — 빠르지만 영속성 없음 (테스트/단기 세션용)

파일로 이전 업로드 찾기:

```python
previous = client.find_previous_uploads("big.bin")
if previous:
    client.resume_upload("big.bin", previous[0]["upload_url"])
```

### ASGI (FastAPI, Starlette, Quart 등)

ASGI 애플리케이션으로 TUS 서버 마운트:

```python
from fastapi import FastAPI
from resumable_upload import SQLiteStorage, TusServer
from resumable_upload.asgi import TusASGIApp

app = FastAPI()
tus = TusServer(storage=SQLiteStorage(), base_path="/files")
app.mount("/files", TusASGIApp(tus))
```

어댑터는 `TusServer.handle_request_async`를 직접 await합니다. 기본 `*_async` 구현을 유지하는 스토리지 백엔드는 `asyncio.to_thread` 기반 래퍼를 그대로 사용하므로 별도 수정 없이 이벤트 루프가 블로킹되지 않습니다. `*_async`를 네이티브 비동기 I/O로 오버라이드한 백엔드는 논블로킹으로 end-to-end 실행됩니다.

### 커맨드라인 서버 & 클라이언트

Python 코드를 작성하지 않고 쉘에서 바로 TUS 서버를 실행하거나, 서버를 상대로 업로드·다운로드·조회를 할 수 있습니다:

```bash
# 서버
resumable-upload serve --host 0.0.0.0 --port 8080 --upload-dir ./uploads

# 클라이언트: 업로드(재개 가능, 진행률 표시), 조회, 다운로드
resumable-upload upload big.bin --url http://host/files --parallel 4
resumable-upload info http://host/files/<id>
resumable-upload download http://host/files/<id> -o out.bin
```

서버 플래그: `--host`, `--port`, `--base-path`, `--upload-dir`, `--db-path`, `--max-size`, `--max-chunk-size`, `--request-timeout`, `--upload-expiry`, `--cleanup-interval`, `--cors-origin`, `--cors-credentials`, `--cors-max-age`, `--checksum-algorithms`, `--enable-downloads`, `--behind-proxy`, `--location-base-url`, `--disable-termination`, `--disable-concatenation`, `--metrics-path`, `--lock-backend`, `--redis-url`, `--lock-ttl`, `--lock-wait`, `--log-level`. 자세한 내용은 `resumable-upload serve --help`로 확인하세요.

`serve`는 연결마다 스레드를 하나씩 사용하므로 동시 `PATCH`, `--parallel` 업로드, 락 경합이 실제 운영 환경과 동일하게 동작합니다. `SIGINT`/`SIGTERM`을 받으면 새 연결을 받지 않고 처리 중인 요청이 끝나기를 기다린 뒤 종료하며, 대기 시간은 `--request-timeout`(기본 30초)으로 제한됩니다 — 멈춘 클라이언트 때문에 종료가 `SIGKILL`까지 밀리지 않도록 오케스트레이터의 grace period보다 작게 두세요. 연결당 스레드 하나를 상한 없이 사용하므로, 번들 서버는 직접 노출하지 말고 리버스 프록시 뒤에 두는 것을 권장합니다.

### 병렬 청크 업로드

고대역폭 환경에서 큰 파일을 N개의 partial 업로드로 분할해 동시에 전송하고, 서버에서 [concatenation 확장](https://tus.io/protocols/resumable-upload.html#concatenation)을 통해 병합합니다:

```python
client = TusClient("http://localhost:8080/files", chunk_size=1024 * 1024)
url = client.upload_file("large.bin", parallel_uploads=4)
```

TUS `concatenation` 확장을 지원하는 서버가 필요합니다(본 라이브러리는 지원). [`tus-js-client`](https://github.com/tus/tus-js-client)의 `parallelUploads` 옵션과 호환됩니다.

### 수동 partial / final 제어

여러 기기 또는 세션에 걸쳐 업로드를 재개해야 하는 고급 워크플로우에서는 partial / final 프리미티브를 직접 사용할 수 있습니다:

```python
url1 = client.create_partial_upload("part1.bin")
url2 = client.create_partial_upload("part2.bin")
final_url = client.create_final_upload(
    partial_urls=[url1, url2],
    metadata={"filename": "merged.bin"},
)
```

## 🔧 고급 사용법

자세한 가이드는 **[문서 사이트의 Advanced Usage 섹션](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)**을 참조하세요:

- 지수 백오프 자동 재시도와 `on_should_retry` 제어 — [Retry & Error Handling](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)
- 중단된 업로드 재개 (세션 내 및 세션 간) — [Resume & Partial Uploads](https://injaeryou.github.io/resumable-upload/advanced-usage/resume/)
- Concatenation 확장 + `parallel_uploads=N` — [Concatenation & Parallel Uploads](https://injaeryou.github.io/resumable-upload/advanced-usage/concatenation/)
- 트레이싱·재시도 훅 — [Observability & Retry Gating](https://injaeryou.github.io/resumable-upload/advanced-usage/observability/)
- `Uploader`를 통한 청크 단위 제어 + `stop_event` 취소 — [Low-Level Uploader](https://injaeryou.github.io/resumable-upload/advanced-usage/uploader/)
- 웹 프레임워크 통합: [Flask](https://injaeryou.github.io/resumable-upload/web-frameworks/flask/), [FastAPI](https://injaeryou.github.io/resumable-upload/web-frameworks/fastapi/), [Django](https://injaeryou.github.io/resumable-upload/web-frameworks/django/), [범용 ASGI](https://injaeryou.github.io/resumable-upload/web-frameworks/asgi/)
- 운영: [CLI](https://injaeryou.github.io/resumable-upload/operations/cli/), [Metrics](https://injaeryou.github.io/resumable-upload/operations/metrics/), [Distributed Locks](https://injaeryou.github.io/resumable-upload/operations/locks/)

## 📚 API 참조

전체 API 문서는 문서 사이트 참조: [Client](https://injaeryou.github.io/resumable-upload/api-reference/client/), [Server](https://injaeryou.github.io/resumable-upload/api-reference/server/), [Storage](https://injaeryou.github.io/resumable-upload/api-reference/storage/), [Exceptions & Utilities](https://injaeryou.github.io/resumable-upload/api-reference/exceptions/).

### 빠른 참조

| 클래스 | 임포트 | 용도 |
|--------|--------|------|
| `TusClient` | `from resumable_upload import TusClient` | TUS 프로토콜로 파일 업로드 |
| `TusServer` | `from resumable_upload import TusServer` | TUS 업로드 서버 (프레임워크 무관) |
| `TusHTTPRequestHandler` | `from resumable_upload import TusHTTPRequestHandler` | Python 내장 `HTTPServer`용 핸들러 |
| `SQLiteStorage` | `from resumable_upload import SQLiteStorage` | SQLite + 파일시스템 스토리지 백엔드 |
| `FileURLStorage` | `from resumable_upload import FileURLStorage` | JSON 파일 기반 URL 영속성 |
| `Uploader` | `from resumable_upload.client.uploader import Uploader` | 저수준 청크 단위 제어 |

### 주요 파라미터

**`TusClient`**: `url`, `chunk_size` (기본값 1 MB), `checksum` (SHA1, 기본값 `True`), `max_retries` (기본값 3), `retry_delay` (기본값 1.0s, 지수 백오프 최대 60s), `timeout` (기본값 30s), `store_url` / `url_storage` (세션 간 재개), `verify_tls_cert`, `headers` — 요청 훅, PATCH-over-POST 터널링, 요청 ID 주입 등 전체 목록: **[클라이언트 API 레퍼런스](https://injaeryou.github.io/resumable-upload/api-reference/client/)**

**`TusServer`**: `storage`, `base_path` (기본값 `/files`), `max_size`, `upload_expiry`, `cors_allow_origins`, `request_timeout` (기본값 30s — Slowloris 공격 방어) — 프록시/`Location` 설정, CORS 자격 증명, 다운로드, 체크섬 트레일러, 기능 토글 등 전체 목록: **[서버 API 레퍼런스](https://injaeryou.github.io/resumable-upload/api-reference/server/)**

**`lock_backend`**: `TusServer`는 기본으로 in-process `InMemoryLockBackend`를 설치하므로 한 업로드에 대한 동시 `PATCH`/`DELETE`가 프로세스 안에서 직렬화됩니다 — `lock_wait_seconds`(5초)를 넘기면 진 쪽은 **`423 Locked`**를 받습니다. 멀티프로세스·멀티노드 배포는 `RedisLockBackend`를 넘겨야 합니다. 끄려면 `lock_backend=None`을 넘기거나, 락이 없는 `TusServerCore`를 쓰세요: **[락 백엔드](https://injaeryou.github.io/resumable-upload/operations/locks/)**

**`SQLiteStorage`**: `db_path` (기본값 `uploads.db`), `upload_dir` (기본값 `uploads`) — 업로드별 락으로 스레드 안전; `fcntl.flock`으로 프로세스 안전

**`FileURLStorage`**: `storage_path` (기본값 `.tus_urls.json`) — `threading.Lock`으로 스레드 안전; `fcntl.flock`으로 프로세스 안전

## 🔍 TUS 프로토콜 준수

[TUS 프로토콜 v1.0.0](https://tus.io/protocols/resumable-upload.html) 구현. 전체 준수 현황: **[TUS Compliance](https://injaeryou.github.io/resumable-upload/compliance/)**.

### 확장 기능

| 확장 | 상태 |
|------|------|
| **core** | ✅ 구현됨 |
| **creation** | ✅ 구현됨 |
| **creation-with-upload** | ✅ 구현됨 |
| **termination** | ✅ 구현됨 |
| **creation-defer-length** | ✅ 구현됨 |
| **checksum** | ✅ 구현됨 (`sha1`/`sha256`/`sha512`/`md5`, 헤더 또는 트레일러) |
| **expiration** | ✅ 구현됨 |
| **concatenation** | ✅ 구현됨 (SQLite / S3 / GCS / Azure) |
| **concatenation-unfinished** | ✅ 구현됨 (SQLite) |

> **참고:** 세션 간 재개를 위한 내부 파일 지문(fingerprint)은 **SHA-256**을 사용하며, TUS 프로토콜과는 무관합니다. 헤더별 상세 내역: **[준수 매트릭스](https://injaeryou.github.io/resumable-upload/compliance/)**.

### 표준 외 지원

| 기능 | 상태 |
|------|------|
| `X-HTTP-Method-Override` | ✅ 구현됨 — PATCH/DELETE를 차단하는 환경을 위해 POST를 PATCH/DELETE/HEAD로 재작성 |

## 🧪 테스트

```bash
# uv가 없으면 설치
curl -LsSf https://astral.sh/uv/install.sh | sh

uv venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
make install

make test-minimal      # 최소 테스트 (웹 프레임워크 제외)
make test              # 전체 테스트
make lint              # 린팅
make format            # 코드 포맷팅
make test-all-versions # 모든 Python 버전 테스트 (3.9-3.14, tox 필요)
make ci                # 전체 CI 검사 (린팅 + 포맷팅 + 테스트)
make interop           # 구현체 간 호환성 테스트 (아래 참고)
```

### 상호운용성 테스트 (로컬 전용, 선택)

`make interop`은 실제 와이어 호환성을 TUS 생태계 대비 검증합니다
(`tests/test_interop.py`). **로컬에서만** 실행되며 — `make ci`에 포함되지 않음 —
전제 조건이 없는 조합은 자동으로 스킵됩니다:

| 조합 | 커버 | 전제 조건 |
|------|------|-----------|
| 우리 서버 ↔ 우리 클라이언트 | 업로드, 재개, concatenation, 다운로드 | 없음 (항상 실행) |
| 우리 클라이언트 ↔ **tusd** 서버 | 업로드, concatenation, termination | `tusd`가 `PATH`에 (또는 `TUSD_BIN=<경로>`) |
| 우리 서버 ↔ **tus-js-client** (Node) | 업로드, HEAD, 다운로드, termination | `node` + `tests/interop/`에서 `npm install` |
| 우리 서버 ↔ **tus-py-client** (`tuspy`) | 업로드, 재개, checksum | `uv pip install tuspy` |

tusd는 클라이언트 바이너리를 제공하지 않으므로(서버 전용), 우리 서버를 때리는
두 번째 레퍼런스 클라이언트는 tus 프로젝트의 공식 Python 클라이언트입니다. 각 조합은
상대 구현이 지원하는 기능만 검증합니다 — 예를 들어 tusd는 checksum·expiration을
광고하지 않고, tus-py-client는 termination 호출을 제공하지 않으므로 해당 조합은 의도적으로 스킵됩니다.

## 📖 문서

- **문서 사이트**: [injaeryou.github.io/resumable-upload](https://injaeryou.github.io/resumable-upload/)
- **English README**: [README.md](https://github.com/injaeryou/resumable-upload/blob/main/README.md)
- **한국어 README**: [README.ko.md](https://github.com/injaeryou/resumable-upload/blob/main/README.ko.md)
- **고급 사용법**: [advanced-usage/retry](https://injaeryou.github.io/resumable-upload/advanced-usage/retry/)
- **전체 API 참조**: [api-reference/client](https://injaeryou.github.io/resumable-upload/api-reference/client/)
- **TUS 프로토콜 준수**: [compliance](https://injaeryou.github.io/resumable-upload/compliance/)

## 🤝 기여하기

기여를 환영합니다! 가이드라인은 [Contributing Guide](.github/CONTRIBUTING.md)를 확인해주세요.

## 📄 라이선스

MIT License - 자세한 내용은 [LICENSE](LICENSE) 파일을 참조하세요.

## 🙏 감사의 말

이 라이브러리는 공식 [TUS Python client](https://github.com/tus/tus-py-client)에서 영감을 받았으며 [TUS 재개 가능 업로드 프로토콜](https://tus.io/)을 구현합니다.

## 📞 지원

- 📫 Issues: [GitHub Issues](https://github.com/injaeryou/resumable-upload/issues)
- 📖 Documentation: [injaeryou.github.io/resumable-upload](https://injaeryou.github.io/resumable-upload/)
- 🌟 GitHub에서 스타를 눌러주세요!
