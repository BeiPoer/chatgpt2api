import json
import shutil
import unittest
from unittest import mock

from utils import sentinel


class FakeResponse:
    def __init__(self, data: dict):
        self.status_code = 200
        self.text = json.dumps(data)
        self._data = data

    def json(self) -> dict:
        return self._data


class FakeSession:
    def __init__(self, data: dict):
        self.data = data

    def post(self, *_args, **_kwargs) -> FakeResponse:
        return FakeResponse(self.data)


def fake_sdk(turnstile_expression: str) -> str:
    return f"""
var SentinelSDK = {{
  async token(flow) {{
    if (!localStorage["oai-did"].length) throw new Error("missing oai-did");
    if (!history.length) throw new Error("missing history");
    localStorage.setItem("sentinel", "ok");
    const node = document.createElement("div");
    document.body.appendChild(node);
    node.getBoundingClientRect();
    document.body.removeChild(node);
    return JSON.stringify({{p: "proof-token", t: {turnstile_expression}, c: "challenge-token", flow}});
  }},
  async sessionObserverToken(flow) {{
    return JSON.stringify({{so: "session-observer-token", c: "challenge-token", flow}});
  }}
}};
"""


@unittest.skipUnless(shutil.which("node") or shutil.which("nodejs"), "Node.js is required")
class SentinelSDKTests(unittest.TestCase):
    def requirements(self) -> dict:
        return {
            "token": "challenge-token",
            "proofofwork": {"required": True, "seed": "seed", "difficulty": "0"},
            "turnstile": {"required": True, "dx": "dx"},
            "so": {"required": True, "collector_dx": "collector", "snapshot_dx": "snapshot"},
        }

    def build(self, sdk_source: str) -> tuple[str, str, dict[str, str]]:
        details: dict[str, str] = {}
        with mock.patch.object(sentinel, "_load_sentinel_sdk", return_value=("https://sentinel.test/sdk.js", sdk_source)):
            value, cookie = sentinel.build_sentinel_token(
                FakeSession(self.requirements()),
                "device-id",
                "oauth_create_account",
                details=details,
            )
        return value, cookie, details

    def test_rejects_base64_encoded_turnstile_runtime_error(self) -> None:
        sdk_source = fake_sdk('btoa("TypeError: Cannot read properties of undefined (reading \'bind\')")')
        with self.assertRaisesRegex(RuntimeError, "sentinel_required_turnstile_invalid"):
            self.build(sdk_source)

    def test_combined_token_preserves_sdk_artifacts(self) -> None:
        value, cookie, details = self.build(fake_sdk('btoa("turnstile-ok")'))
        payload = json.loads(value)

        self.assertEqual(
            payload,
            {
                "p": "proof-token",
                "t": "dHVybnN0aWxlLW9r",
                "c": "challenge-token",
                "so": "session-observer-token",
                "id": "device-id",
                "flow": "oauth_create_account",
            },
        )
        self.assertEqual(cookie, "0challenge-token")
        self.assertEqual(details["mode"], "node-sdk")
        self.assertFalse(details["token_error"])
        self.assertFalse(details["turnstile_error"])
        self.assertFalse(details["so_error"])


if __name__ == "__main__":
    unittest.main()
