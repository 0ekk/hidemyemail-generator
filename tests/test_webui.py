import base64
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import AioHTTPTestCase

from hidemyemail_generator import webui
from hidemyemail_generator.inbox import connect_db, list_addresses, upsert_address
from hidemyemail_generator.main import RichHideMyEmail


LIST_RESPONSE = {
    "success": True,
    "result": {
        "hmeEmails": [
            {
                "anonymousId": "abc-123",
                "hme": "example@icloud.com",
                "label": "Example",
                "note": "Original note",
                "isActive": True,
                "createTimestamp": 1753531200000,
            }
        ]
    },
}
GENERATE_RESPONSE = {"success": True, "result": {"hme": "fresh@icloud.com"}}


class FakeTransport:
    """Replays canned iCloud responses and records what was sent."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    async def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json")))
        endpoint = url.rsplit("/", 1)[-1]
        return self.responses.get(endpoint, {"success": True, "result": {}})


class WebUITestCase(AioHTTPTestCase):
    token = ""

    def setUp(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        self.tmp = Path(tmpdir.name)

        self.cookie_file = self.tmp / "cookies.txt"
        self.cookie_file.write_text(
            'X-APPLE-WEBAUTH-USER="v=1:s=0:d=1"', encoding="utf-8"
        )
        self.db_file = str(self.tmp / "hidemyemail.db")
        self.output_file = str(self.tmp / "emails.txt")

        self.transport = FakeTransport(
            {"list": LIST_RESPONSE, "generate": GENERATE_RESPONSE}
        )
        patcher = patch.object(RichHideMyEmail, "_request_json", self.transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    async def get_application(self):
        return webui.create_app(
            webui.Settings(
                cookie_file=str(self.cookie_file),
                output_file=self.output_file,
                db_file=self.db_file,
                config_file=str(self.tmp / "inbox_config.json"),
                export_dir=str(self.tmp / "exports"),
                region="global",
                token=self.token,
            )
        )

    def store(self, email: str, **kwargs):
        conn = connect_db(self.db_file)
        try:
            upsert_address(conn, email, **kwargs)
        finally:
            conn.close()

    def stored(self, email: str):
        conn = connect_db(self.db_file)
        try:
            rows = [row for row in list_addresses(conn) if row["email"] == email]
            return rows[0] if rows else None
        finally:
            conn.close()

    async def get_json(self, path: str):
        response = await self.client.get(path)
        return response.status, await response.json()

    async def post_json(self, path: str, body: dict, **kwargs):
        response = await self.client.post(path, json=body, **kwargs)
        return response.status, await response.json()


class ConfigAndPageTests(WebUITestCase):
    async def test_index_is_served(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    async def test_config_reports_server_defaults(self):
        status, payload = await self.get_json("/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["db_file"], self.db_file)
        self.assertEqual(payload["config"]["region"], "global")
        self.assertIn("china", payload["config"]["regions"])
        self.assertEqual(
            payload["config"]["address_states"], ["unused", "used", "trash"]
        )


class GenerateTests(WebUITestCase):
    async def test_generate_reserves_and_stores_addresses(self):
        status, payload = await self.post_json(
            "/api/generate", {"label": "web", "count": 1}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["emails"], ["fresh@icloud.com"])

        _, url, body = self.transport.calls[-1]
        self.assertTrue(url.endswith("/v1/hme/reserve"))
        self.assertEqual(body["label"], "web")
        self.assertEqual(self.stored("fresh@icloud.com")["state"], "unused")
        self.assertIn("fresh@icloud.com", Path(self.output_file).read_text())

    async def test_generate_can_skip_the_output_file(self):
        await self.post_json(
            "/api/generate", {"label": "web", "count": 1, "save_file": False}
        )
        self.assertFalse(Path(self.output_file).exists())

    async def test_generate_requires_a_label(self):
        status, payload = await self.post_json("/api/generate", {"count": 1})
        self.assertEqual(status, 400)
        self.assertIn("label", payload["error"]["message"])

    async def test_generate_rejects_an_unknown_region(self):
        status, payload = await self.post_json(
            "/api/generate", {"label": "web", "region": "mars"}
        )
        self.assertEqual(status, 400)
        self.assertIn("mars", payload["error"]["message"])

    async def test_failed_generation_surfaces_the_icloud_error(self):
        self.transport.responses["generate"] = {
            "success": False,
            "error": {"errorCode": "-41015", "errorMessage": "Too many requests"},
        }
        status, payload = await self.post_json("/api/generate", {"label": "web"})
        self.assertEqual(status, 200)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["emails"], [])
        self.assertEqual(payload["error"]["code"], "-41015")


class AddressTests(WebUITestCase):
    async def test_icloud_list_is_proxied(self):
        status, payload = await self.get_json("/api/icloud/addresses?active=true")
        self.assertEqual(status, 200)
        self.assertEqual(payload["addresses"][0]["email"], "example@icloud.com")

    async def test_deactivate_writes_through_to_the_local_database(self):
        self.store("example@icloud.com", label="Example")
        status, payload = await self.post_json(
            "/api/icloud/forwarding", {"email": "example@icloud.com", "active": False}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        _, url, body = self.transport.calls[-1]
        self.assertTrue(url.endswith("/v1/hme/deactivate"))
        self.assertEqual(body, {"anonymousId": "abc-123"})
        self.assertEqual(self.stored("example@icloud.com")["is_active"], 0)

    async def test_forwarding_needs_an_explicit_boolean(self):
        status, payload = await self.post_json(
            "/api/icloud/forwarding", {"email": "example@icloud.com"}
        )
        self.assertEqual(status, 400)
        self.assertIn("active", payload["error"]["message"])

    async def test_metadata_edit_writes_through(self):
        self.store("example@icloud.com", label="Example", note="Original note")
        status, payload = await self.post_json(
            "/api/icloud/metadata", {"email": "example@icloud.com", "label": "Renamed"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["label"], "Renamed")
        _, _, body = self.transport.calls[-1]
        self.assertEqual(body["note"], "Original note")
        self.assertEqual(self.stored("example@icloud.com")["label"], "Renamed")

    async def test_metadata_can_clear_a_note(self):
        self.store("example@icloud.com", label="Example", note="Original note")
        await self.post_json(
            "/api/icloud/metadata", {"email": "example@icloud.com", "note": ""}
        )
        self.assertEqual(self.stored("example@icloud.com")["note"], "")

    async def test_unknown_address_fails_without_mutating(self):
        status, payload = await self.post_json(
            "/api/icloud/forwarding", {"email": "nope@icloud.com", "active": False}
        )
        self.assertEqual(status, 400)
        self.assertIn("nope@icloud.com", payload["error"]["message"])
        self.assertEqual(len(self.transport.calls), 1)

    async def test_local_addresses_can_be_filtered(self):
        self.store("kept@icloud.com", label="Keep", state="used")
        self.store("other@icloud.com", label="Other")
        status, payload = await self.get_json("/api/addresses?state=used")
        self.assertEqual(status, 200)
        self.assertEqual(
            [row["email"] for row in payload["addresses"]], ["kept@icloud.com"]
        )

    async def test_unsupported_state_filter_is_rejected(self):
        status, _ = await self.get_json("/api/addresses?state=archived")
        self.assertEqual(status, 400)

    async def test_marking_an_address_updates_its_state(self):
        self.store("kept@icloud.com")
        status, payload = await self.post_json(
            "/api/addresses/state", {"email": "kept@icloud.com", "state": "trash"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["state"], "trash")
        self.assertEqual(self.stored("kept@icloud.com")["state"], "trash")

    async def test_sync_from_icloud_fills_the_local_database(self):
        status, payload = await self.post_json("/api/icloud/sync", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(self.stored("example@icloud.com")["label"], "Example")


VALID_ACCOUNT = {
    "dsInfo": {
        "appleId": "person@example.com",
        "fullName": "A Person",
        "dsid": 12345,
        "isHideMyEmailFeatureAvailable": True,
    },
    "userPartition": 68,
    "detectedMaildomainHost": "p68-maildomainws.icloud.com",
}
PASTED_COOKIE = (
    'curl "https://p68-maildomainws.icloud.com/v1/hme/list" '
    '-b "X-APPLE-WEBAUTH-USER=pasted-session; X-APPLE-WEBAUTH-TOKEN=abc"'
)


class CookieUpdateTests(WebUITestCase):
    """Pasting a session has to replace the live one only once iCloud accepts
    it, and must never hand the cookie back to the browser."""

    def validating(self, account):
        return patch(
            "hidemyemail_generator.main.fetch_account_info_from_cookie",
            new=AsyncMock(return_value=account),
        )

    async def test_a_validated_paste_replaces_the_session(self):
        self.cookie_file.write_text("old-cookie", encoding="utf-8")
        with self.validating(dict(VALID_ACCOUNT)) as validate:
            status, payload = await self.post_json(
                "/api/cookie", {"text": PASTED_COOKIE}
            )

        self.assertEqual(status, 200)
        self.assertEqual(payload["account"]["apple_id"], "person@example.com")
        # The cookie iCloud was asked about is the one that was pasted.
        self.assertIn("pasted-session", validate.await_args.args[0])

        stored = self.cookie_file.read_text()
        self.assertIn("HIDEMYEMAIL_COOKIE_BASE64=", stored)
        self.assertIn(
            "pasted-session",
            base64.b64decode(
                stored.split("HIDEMYEMAIL_COOKIE_BASE64=")[1].strip()
            ).decode(),
        )
        # The partition iCloud reported has to survive into the saved file.
        self.assertIn("p68-maildomainws.icloud.com", stored)

    async def test_the_response_never_echoes_the_cookie(self):
        with self.validating(dict(VALID_ACCOUNT)):
            _, payload = await self.post_json("/api/cookie", {"text": PASTED_COOKIE})
        self.assertNotIn("pasted-session", str(payload))

    async def test_the_new_session_is_used_by_the_next_request(self):
        with self.validating(dict(VALID_ACCOUNT)):
            await self.post_json("/api/cookie", {"text": PASTED_COOKIE})
        # No restart: the next iCloud call builds a client from the saved file.
        await self.get_json("/api/icloud/addresses")
        self.assertTrue(self.transport.calls)

    async def test_a_rejected_paste_leaves_the_old_session_in_place(self):
        self.cookie_file.write_text("old-cookie", encoding="utf-8")
        with self.validating({"error": "Failed to validate cookie: expired"}):
            status, payload = await self.post_json(
                "/api/cookie", {"text": PASTED_COOKIE}
            )

        self.assertEqual(status, 400)
        self.assertIn("expired", payload["error"]["message"])
        self.assertEqual(self.cookie_file.read_text(), "old-cookie")
        self.assertFalse(self.cookie_file.with_name("cookies.txt.new").exists())

    async def test_text_without_a_cookie_is_rejected_before_icloud(self):
        # "//" is the comment marker the cookie file understands, so text made
        # only of comments parses to an empty cookie.
        self.cookie_file.write_text("old-cookie", encoding="utf-8")
        with self.validating(dict(VALID_ACCOUNT)) as validate:
            status, payload = await self.post_json(
                "/api/cookie", {"text": "// pasted the wrong thing"}
            )
        self.assertEqual(status, 400)
        self.assertIn("No iCloud cookie found", payload["error"]["message"])
        validate.assert_not_awaited()
        self.assertEqual(self.cookie_file.read_text(), "old-cookie")

    async def test_empty_text_is_rejected(self):
        status, payload = await self.post_json("/api/cookie", {"text": "   "})
        self.assertEqual(status, 400)
        self.assertIn("text", payload["error"]["message"])

    async def test_an_unwritable_cookie_file_reports_why(self):
        # The branch a read-only Kubernetes Secret mount takes. Permission bits
        # cannot express that here because the suite may run as root, so this
        # uses a directory that does not exist: both raise OSError on write.
        unwritable = self.tmp / "missing" / "cookies.txt"
        self.app[webui.SETTINGS].cookie_file = str(unwritable)

        status, payload = await self.post_json("/api/cookie", {"text": PASTED_COOKIE})
        self.assertEqual(status, 409)
        self.assertIn("not writable", payload["error"]["message"])
        self.assertIn(str(unwritable), payload["error"]["message"])
        self.assertFalse(unwritable.exists())

    async def test_config_reports_whether_the_cookie_can_be_replaced(self):
        _, payload = await self.get_json("/api/config")
        self.assertTrue(payload["config"]["cookie_writable"])

        self.app[webui.SETTINGS].cookie_file = str(self.tmp / "missing" / "cookies.txt")
        _, payload = await self.get_json("/api/config")
        self.assertFalse(payload["config"]["cookie_writable"])


class InboxAndBatchTests(WebUITestCase):
    async def test_inbox_reports_missing_configuration(self):
        status, payload = await self.get_json("/api/inbox")
        self.assertEqual(status, 200)
        self.assertFalse(payload["configured"])
        self.assertEqual(payload["counts"]["addresses"], 0)

    async def test_inbox_configuration_round_trips_without_the_password(self):
        status, payload = await self.post_json(
            "/api/inbox/config",
            {
                "host": "imap.example.com",
                "username": "person@example.com",
                "password": "secret",
                "folder": "INBOX",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["configured"])
        self.assertNotIn("secret", str(payload))
        self.assertEqual(payload["config"]["username"], "pe***n@example.com")

    async def test_quota_counts_recent_addresses(self):
        self.store("fresh@icloud.com")
        status, payload = await self.get_json("/api/quota")
        self.assertEqual(status, 200)
        self.assertEqual(payload["used"], 1)
        self.assertEqual(payload["remaining"], payload["limit"] - 1)

    async def test_batches_can_be_created_and_transitioned(self):
        status, payload = await self.post_json(
            "/api/batches", {"label": "bulk", "target": 3, "interval_minutes": 30}
        )
        self.assertEqual(status, 200)
        batch_id = payload["batch"]["id"]
        self.assertEqual(payload["batch"]["interval_seconds"], 1800)

        status, payload = await self.post_json(
            f"/api/batches/{batch_id}/state", {"state": "paused"}
        )
        self.assertEqual(payload["batch"]["state"], "paused")

        status, payload = await self.get_json(f"/api/batches/{batch_id}")
        self.assertEqual(payload["batch"]["target"], 3)
        self.assertEqual(payload["addresses"], [])

    async def test_unknown_batch_is_a_404(self):
        status, _ = await self.get_json("/api/batches/nope")
        self.assertEqual(status, 404)

    async def test_generated_addresses_join_their_batch(self):
        _, payload = await self.post_json(
            "/api/batches", {"label": "bulk", "target": 2}
        )
        batch_id = payload["batch"]["id"]
        await self.post_json(
            "/api/generate", {"label": "bulk", "count": 1, "batch_id": batch_id}
        )
        status, payload = await self.get_json(f"/api/batches/{batch_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["batch"]["reserved"], 1)
        self.assertEqual(payload["addresses"][0]["email"], "fresh@icloud.com")

    async def test_export_writes_csv_files(self):
        self.store("kept@icloud.com")
        status, payload = await self.post_json("/api/export", {})
        self.assertEqual(status, 200)
        for path in payload["outputs"].values():
            self.assertTrue(Path(path).exists())


class AccessControlTests(WebUITestCase):
    async def test_cross_origin_writes_are_refused(self):
        status, payload = await self.post_json(
            "/api/addresses/state",
            {"email": "kept@icloud.com", "state": "used"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Cross-origin", payload["error"]["message"])

    async def test_same_origin_writes_are_allowed(self):
        self.store("kept@icloud.com")
        origin = f"http://{self.server.host}:{self.server.port}"
        status, _ = await self.post_json(
            "/api/addresses/state",
            {"email": "kept@icloud.com", "state": "used"},
            headers={"Origin": origin},
        )
        self.assertEqual(status, 200)


class TokenTests(WebUITestCase):
    token = "s3cret-token"

    async def test_requests_without_the_token_are_rejected(self):
        status, payload = await self.get_json("/api/config")
        self.assertEqual(status, 401)
        self.assertIn("token", payload["error"]["message"].lower())

    async def test_the_page_is_served_so_it_can_ask_for_the_token(self):
        # Guarding the page too would make the in-browser token prompt
        # impossible: the user would only ever see a JSON 401.
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])
        # The page is public; the data behind it is not.
        status, _ = await self.get_json("/api/account")
        self.assertEqual(status, 401)

    async def test_health_check_stays_open_for_container_probes(self):
        status, payload = await self.get_json("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        # A probe endpoint must not become a way to read anything else.
        self.assertNotIn("cookie", str(payload).lower())

    async def test_the_token_can_travel_in_a_header_or_the_query(self):
        response = await self.client.get(
            "/api/config", headers={"X-Auth-Token": self.token}
        )
        self.assertEqual(response.status, 200)
        response = await self.client.get(f"/api/config?token={self.token}")
        self.assertEqual(response.status, 200)


class HelperTests(AioHTTPTestCase):
    async def get_application(self):
        return webui.create_app(
            webui.Settings(cookie_file="cookies.txt", output_file="emails.txt")
        )

    async def test_loopback_detection(self):
        self.assertTrue(webui.is_loopback("127.0.0.1"))
        self.assertTrue(webui.is_loopback("localhost"))
        self.assertTrue(webui.is_loopback("::1"))
        self.assertFalse(webui.is_loopback("0.0.0.0"))
        self.assertFalse(webui.is_loopback("192.168.1.10"))

    async def test_server_url_points_at_a_reachable_host(self):
        self.assertEqual(webui.server_url("0.0.0.0", 8765), "http://127.0.0.1:8765/")
        self.assertEqual(
            webui.server_url("127.0.0.1", 80, "abc"), "http://127.0.0.1:80/?token=abc"
        )

    async def test_the_front_end_ships_with_the_package(self):
        self.assertTrue((webui.static_dir() / "index.html").is_file())
