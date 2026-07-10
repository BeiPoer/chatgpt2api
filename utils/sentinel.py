"""OpenAI Sentinel token generation for login and registration flows."""
from __future__ import annotations

import base64
import json
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from curl_cffi.requests import Session


SENTINEL_FRAME_URL = "https://sentinel.openai.com/backend-api/sentinel/frame.html"
DEFAULT_SENTINEL_SDK_URL = "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js"
SENTINEL_RUNNER = Path(__file__).with_name("sentinel_sdk_runner.js")
DEFAULT_SENTINEL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)
DEFAULT_SENTINEL_SEC_CH_UA = '"Chromium";v="149", "Not_A Brand";v="99", "Google Chrome";v="149"'

_sdk_cache_lock = threading.Lock()
_sdk_cache: tuple[str, str] | None = None
_RUNTIME_ERROR_RE = re.compile(
    r"(?:TypeError|ReferenceError|SyntaxError|RangeError|EvalError|URIError):"
    r"|Cannot (?:read|set) properties|is not a function|(?:turnstile|session_observer)_vm_timeout",
    re.I,
)


class SentinelTokenGenerator:
    MAX_ATTEMPTS = 500_000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: str, ua: str, sdk_url: str = DEFAULT_SENTINEL_SDK_URL):
        self.device_id = device_id
        self.user_agent = ua
        self.sdk_url = sdk_url
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        value = 2166136261
        for char in text:
            value ^= ord(char)
            value = (value * 16777619) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 2246822507) & 0xFFFFFFFF
        value ^= value >> 13
        value = (value * 3266489909) & 0xFFFFFFFF
        value ^= value >> 16
        return format(value & 0xFFFFFFFF, "08x")

    def _get_config(self) -> list:
        perf_now = random.uniform(1000, 50000)
        return [
            random.choice([3000, 2340, 4000, 6000]),
            time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)", time.gmtime()),
            4294705152,
            random.random(),
            self.user_agent,
            self.sdk_url,
            "",
            "en-US",
            "en-US,en",
            random.random(),
            random.choice(["vendor\u2212Google Inc.", "webdriver\u2212false", "hardwareConcurrency\u221216"]),
            random.choice(["location", "documentElement", "currentScript"]),
            random.choice(["window", "document", "navigator", "performance"]),
            perf_now,
            self.sid,
            "",
            random.choice([8, 12, 16, 24, 32]),
            time.time() * 1000 - perf_now,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
        ]

    @staticmethod
    def _b64(data: object) -> str:
        raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def generate_requirements_token(self) -> str:
        data = self._get_config()
        data[3] = 1
        data[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(data)

    def generate_token(self, seed: str, difficulty: str) -> str:
        started_at = time.time()
        data = self._get_config()
        difficulty = str(difficulty or "0")
        for index in range(self.MAX_ATTEMPTS):
            data[3] = index
            data[9] = round((time.time() - started_at) * 1000)
            payload = self._b64(data)
            if self._fnv1a_32(seed + payload)[: len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + self.ERROR_PREFIX + self._b64(str(None))


def _sentinel_headers(user_agent: str, sec_ch_ua: str, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": SENTINEL_FRAME_URL,
        "Origin": "https://sentinel.openai.com",
        "User-Agent": user_agent,
        "sec-ch-ua": sec_ch_ua,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


def _load_sentinel_sdk(session: "Session", user_agent: str, sec_ch_ua: str) -> tuple[str, str]:
    global _sdk_cache
    with _sdk_cache_lock:
        if _sdk_cache is not None:
            return _sdk_cache
        headers = _sentinel_headers(user_agent, sec_ch_ua, "text/html,application/xhtml+xml")
        frame = session.get(SENTINEL_FRAME_URL, headers=headers, timeout=20, verify=True)
        if frame.status_code != 200:
            raise RuntimeError(f"sentinel_frame_failed_{frame.status_code}")
        match = re.search(r"<script[^>]+src=['\"]([^'\"]+/sentinel/[^'\"]+/sdk\.js)['\"]", str(frame.text or ""), re.I)
        if not match:
            raise RuntimeError("sentinel_sdk_url_missing")
        sdk_url = urljoin(SENTINEL_FRAME_URL, match.group(1))
        parsed_sdk_url = urlparse(sdk_url)
        if parsed_sdk_url.scheme != "https" or parsed_sdk_url.hostname != "sentinel.openai.com":
            raise RuntimeError("sentinel_sdk_url_invalid")
        sdk = session.get(sdk_url, headers=_sentinel_headers(user_agent, sec_ch_ua, "*/*"), timeout=20, verify=True)
        if sdk.status_code != 200 or "SentinelSDK" not in str(sdk.text or ""):
            raise RuntimeError(f"sentinel_sdk_failed_{sdk.status_code}")
        _sdk_cache = (sdk_url, str(sdk.text))
        return _sdk_cache


def _decode_runtime_error(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    candidates = [text]
    try:
        padded = text + "=" * ((4 - len(text) % 4) % 4)
        candidates.append(base64.b64decode(padded, validate=True).decode("utf-8", errors="replace"))
    except Exception:
        pass
    return next((item for item in candidates if _RUNTIME_ERROR_RE.search(item)), "")


def _run_sentinel_sdk(
    sdk_source: str,
    sdk_url: str,
    requirements: dict,
    requirements_token: str,
    device_id: str,
    flow: str,
    user_agent: str,
) -> dict[str, str]:
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        raise RuntimeError("sentinel_node_runtime_missing")
    payload = json.dumps(
        {
            "sdk_source": sdk_source,
            "sdk_url": sdk_url,
            "requirements": requirements,
            "requirements_token": requirements_token,
            "device_id": device_id,
            "flow": flow,
            "user_agent": user_agent,
        },
        ensure_ascii=False,
    )
    try:
        completed = subprocess.run(
            [node, str(SENTINEL_RUNNER)],
            input=payload,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=75,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("sentinel_sdk_timeout") from exc
    try:
        result = json.loads(str(completed.stdout or "").strip())
    except Exception as exc:
        detail = str(completed.stderr or completed.stdout or "")[-500:]
        raise RuntimeError(f"sentinel_sdk_invalid_output: {detail}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("sentinel_sdk_invalid_output: expected object")
    if completed.returncode != 0 or result.get("error"):
        detail = str(result.get("error") or completed.stderr or "unknown error")[-800:]
        raise RuntimeError(f"sentinel_sdk_failed: {detail}")
    return {str(key): str(value or "") for key, value in result.items()}


def _is_required(data: dict, key: str) -> bool:
    value = data.get(key)
    return isinstance(value, dict) and bool(value.get("required"))


def build_sentinel_token(
    session: "Session",
    device_id: str,
    flow: str,
    *,
    user_agent: str = "",
    sec_ch_ua: str = "",
    details: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Return the Sentinel header value and matching ``oai-sc`` cookie."""
    ua = user_agent or DEFAULT_SENTINEL_USER_AGENT
    ch_ua = sec_ch_ua or DEFAULT_SENTINEL_SEC_CH_UA
    sdk_url, sdk_source = _load_sentinel_sdk(session, ua, ch_ua)
    requirements_token = SentinelTokenGenerator(device_id, ua, sdk_url).generate_requirements_token()
    resp = session.post(
        "https://sentinel.openai.com/backend-api/sentinel/req",
        data=json.dumps({"p": requirements_token, "id": device_id, "flow": flow}),
        headers={**_sentinel_headers(ua, ch_ua, "application/json"), "Content-Type": "text/plain;charset=UTF-8"},
        timeout=20,
        verify=False,
    )
    try:
        data = resp.json() if resp.text else {}
    except Exception as exc:
        raise RuntimeError(f"sentinel_req_invalid_response_{resp.status_code}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"sentinel_req_invalid_response_{resp.status_code}")
    challenge_token = str(data.get("token") or "").strip()
    if resp.status_code != 200 or not challenge_token:
        raise RuntimeError(f"sentinel_req_failed_{resp.status_code}")

    result = _run_sentinel_sdk(sdk_source, sdk_url, data, requirements_token, device_id, flow, ua)
    proof_token = str(result.get("proof_token") or "").strip()
    turnstile_token = str(result.get("turnstile_token") or "").strip()
    runner_challenge = str(result.get("challenge_token") or "").strip()
    so_token = str(result.get("so_token") or "").strip()
    token_error = str(result.get("token_error") or "").strip()
    turnstile_error = str(result.get("turnstile_error") or _decode_runtime_error(turnstile_token)).strip()
    so_error = str(result.get("so_error") or _decode_runtime_error(so_token)).strip()
    proof_error = _decode_runtime_error(proof_token)

    if turnstile_error:
        turnstile_token = ""
    if so_error:
        so_token = ""
    if details is not None:
        details.update(
            {
                "mode": str(result.get("mode") or "node-sdk"),
                "proof": proof_token,
                "turnstile": turnstile_token,
                "challenge": challenge_token,
                "so": so_token,
                "token_error": token_error,
                "proof_error": proof_error,
                "turnstile_error": turnstile_error,
                "so_error": so_error,
            }
        )

    if token_error:
        raise RuntimeError(f"sentinel_token_invalid: {token_error}")
    if proof_error or SentinelTokenGenerator.ERROR_PREFIX in proof_token:
        raise RuntimeError(f"sentinel_proof_invalid: {proof_error or 'proof generation failed'}")
    if runner_challenge and runner_challenge != challenge_token:
        raise RuntimeError("sentinel_challenge_mismatch")
    if _is_required(data, "proofofwork") and not proof_token:
        raise RuntimeError("sentinel_required_proof_missing")
    if _is_required(data, "turnstile") and not turnstile_token:
        raise RuntimeError(f"sentinel_required_turnstile_invalid: {turnstile_error or 'missing token'}")
    if _is_required(data, "so") and not so_token:
        raise RuntimeError(f"sentinel_required_so_invalid: {so_error or 'missing token'}")

    payload = {
        "p": proof_token or requirements_token,
        "t": turnstile_token,
        "c": challenge_token,
        "so": so_token,
        "id": device_id,
        "flow": flow,
    }
    sentinel_value = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return sentinel_value, "0" + challenge_token
