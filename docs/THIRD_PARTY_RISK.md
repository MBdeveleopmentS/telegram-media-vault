# Third-party risk

| Dependency | Role | Risk / mitigation |
|---|---|---|
| Telegram (MTProto + Bot API) | platform | Stores all delivered media; ToS changes; FloodWaits. Uses only official APIs |
| Telethon | MTProto client | Full account access in-process; pinned by hash, monitored by Dependabot and pip-audit |
| aiogram | Bot API client | Handles payments and updates; pinned |
| cryptography | AES-GCM | Pinned; mainstream |
| PostgreSQL, Redis images | storage | Digest-pinned official images; internal network only |
| Python base image | runtime | Digest-pinned; scanned by Trivy in CI |

Licenses: all runtime dependencies are under permissive licenses (MIT, BSD, Apache-2.0, or PSF). Re-check with `pip-licenses` before each release.
