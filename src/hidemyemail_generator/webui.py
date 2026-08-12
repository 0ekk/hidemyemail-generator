"""Local web UI on top of the existing CLI capabilities.

Every handler delegates to the same functions `main.py` wires its commands to,
so the browser and the terminal cannot drift apart in behaviour. The server is
meant to run on loopback next to the cookie file and the local database; it is
not a multi-user service.
"""

import asyncio
import json
import secrets
import sys
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from aiohttp import web

from hidemyemail_generator.hidemyemail import HideMyEmail
from hidemyemail_generator.inbox import (
    ADDRESS_STATES,
    BATCH_STATES,
    DEFAULT_DB_FILE,
    DEFAULT_EXPORT_DIR,
    DEFAULT_FOLDER,
    DEFAULT_INBOX_CONFIG_FILE,
    InboxConfig,
    connect_db,
    create_batch,
    export_csv_files,
    get_batch,
    list_addresses,
    list_batches,
    list_messages,
    load_config,
    mark_address,
    mark_messages_read,
    mask_account,
    save_config,
    set_batch_state,
    sync_inbox,
)


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_LIMIT = 1000
# Container probes have no token, so this one path stays open. It reports that
# the process is serving, nothing about the account or the database.
HEALTH_PATH = "/healthz"


class RequestError(Exception):
    """A bad request from the browser, reported as HTTP 400."""


@dataclass
class Settings:
    cookie_file: str
    output_file: str
    db_file: str = DEFAULT_DB_FILE
    config_file: str = DEFAULT_INBOX_CONFIG_FILE
    export_dir: str = DEFAULT_EXPORT_DIR
    region: str = "global"
    token: str = ""


SETTINGS: "web.AppKey[Settings]" = web.AppKey("settings", Settings)


def static_dir() -> Path:
    """Where the bundled front end lives, in a source tree or a frozen binary."""
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        return Path(bundle) / "hidemyemail_generator" / "static"
    return Path(__file__).resolve().parent / "static"


def _json(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(
        payload,
        status=status,
        dumps=lambda value: json.dumps(value, ensure_ascii=False),
    )


def _failure(
    message: str, status: int = 400, code: Optional[str] = None
) -> web.Response:
    return _json(
        {"ok": False, "error": {"code": code, "message": message, "retry_after": None}},
        status=status,
    )


def _result(payload: dict) -> web.Response:
    """Passes a CLI-shaped result through, mapping a failure onto HTTP 400."""
    return _json(payload, status=200 if payload.get("ok") else 400)


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception as e:
        raise RequestError(f"Invalid JSON body: {e}") from e
    if not isinstance(data, dict):
        raise RequestError("Request body must be a JSON object")
    return data


def _settings(request: web.Request) -> Settings:
    return request.app[SETTINGS]


def _region(request: web.Request, data: Optional[dict] = None) -> str:
    value = (data or {}).get("region") or request.query.get("region")
    region = str(value or _settings(request).region).lower()
    if region not in HideMyEmail.REGION_CONFIG:
        raise RequestError(f'Unsupported iCloud region "{region}"')
    return region


def _text(data: dict, key: str, required: bool = False) -> Optional[str]:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RequestError(f'"{key}" is required')
        return None
    return str(value).strip()


def _int(
    value, name: str, default: int, minimum: int = 1, maximum: int = MAX_LIMIT
) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as e:
        raise RequestError(f'"{name}" must be a number') from e
    if number < minimum or number > maximum:
        raise RequestError(f'"{name}" must be between {minimum} and {maximum}')
    return number


def _flag(value) -> Optional[bool]:
    """Reads a tri-state filter: true, false, or "no opinion"."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).lower()
    if lowered in ("true", "1", "yes", "on", "active"):
        return True
    if lowered in ("false", "0", "no", "off", "inactive"):
        return False
    return None


def _state(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value not in ADDRESS_STATES:
        raise RequestError(f'Unsupported address state "{value}"')
    return value


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except RequestError as e:
        return _failure(str(e), status=400)
    except web.HTTPException:
        raise
    except Exception as e:  # keeps a bad response from killing the session
        return _failure(f"{type(e).__name__}: {e}", status=500)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Token gate plus a same-origin check.

    The token only matters when the server is reachable beyond loopback. The
    origin check always applies: without it any page the user happens to have
    open could POST to this server and mutate their real iCloud addresses.
    """
    token = _settings(request).token
    if token and request.path != HEALTH_PATH:
        sent = request.headers.get("X-Auth-Token") or request.query.get("token") or ""
        if not secrets.compare_digest(sent, token):
            return _failure("Invalid or missing token", status=401)

    origin = request.headers.get("Origin")
    if origin and request.method != "GET":
        if urlsplit(origin).netloc != request.headers.get("Host"):
            return _failure("Cross-origin requests are not allowed", status=403)

    return await handler(request)


routes = web.RouteTableDef()


@routes.get("/")
async def index(request: web.Request) -> web.Response:
    page = static_dir() / "index.html"
    if not page.exists():
        return web.Response(text="Web UI assets are missing", status=500)
    return web.Response(
        body=page.read_bytes(), content_type="text/html", charset="utf-8"
    )


@routes.get(HEALTH_PATH)
async def healthz(request: web.Request) -> web.Response:
    return _json({"ok": True, "status": "serving", "error": None})


@routes.get("/api/config")
async def get_config(request: web.Request) -> web.Response:
    settings = _settings(request)
    return _json(
        {
            "ok": True,
            "config": {
                "region": settings.region,
                "regions": sorted(HideMyEmail.REGION_CONFIG),
                "cookie_file": settings.cookie_file,
                "output_file": settings.output_file,
                "db_file": settings.db_file,
                "inbox_config_file": settings.config_file,
                "export_dir": settings.export_dir,
                "address_states": list(ADDRESS_STATES),
                "batch_states": list(BATCH_STATES),
            },
            "error": None,
        }
    )


@routes.get("/api/account")
async def get_account(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _whoami

    settings = _settings(request)
    return _result(await _whoami(settings.cookie_file, _region(request)))


@routes.get("/api/quota")
async def get_quota(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import quota_snapshot

    return _result(await asyncio.to_thread(quota_snapshot, _settings(request).db_file))


@routes.post("/api/generate")
async def post_generate(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _generate

    settings = _settings(request)
    data = await _body(request)
    label = _text(data, "label", required=True)
    count = _int(data.get("count"), "count", default=1, maximum=100)
    save_file = data.get("save_file", True)

    result = await _generate(
        label,
        count,
        settings.cookie_file,
        settings.output_file,
        no_output_file=not save_file,
        region=_region(request, data),
        db_file=settings.db_file,
        no_db=False,
        batch_id=_text(data, "batch_id"),
    )
    # A partial run still reserved real addresses, so hand them back either way.
    return _json(result, status=200)


@routes.get("/api/icloud/addresses")
async def get_icloud_addresses(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _list

    settings = _settings(request)
    active = _flag(request.query.get("active"))
    return _result(
        await _list(
            request.query.get("label_query") or None,
            True if active is None else active,
            settings.cookie_file,
            _region(request),
        )
    )


@routes.post("/api/icloud/forwarding")
async def post_forwarding(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _set_active, _write_through

    settings = _settings(request)
    data = await _body(request)
    active = _flag(data.get("active"))
    if active is None:
        raise RequestError('"active" must be true or false')

    result = await _set_active(
        _text(data, "email", required=True),
        active,
        settings.cookie_file,
        _region(request, data),
    )
    if result["ok"]:
        await asyncio.to_thread(_write_through, settings.db_file, result)
    return _result(result)


@routes.post("/api/icloud/metadata")
async def post_metadata(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _update_metadata, _write_through

    settings = _settings(request)
    data = await _body(request)
    if "label" not in data and "note" not in data:
        raise RequestError('Provide "label" and/or "note"')

    # Empty strings are meaningful here: they clear the field.
    label = data.get("label")
    note = data.get("note")
    result = await _update_metadata(
        _text(data, "email", required=True),
        None if label is None else str(label),
        None if note is None else str(note),
        settings.cookie_file,
        _region(request, data),
    )
    if result["ok"]:
        await asyncio.to_thread(_write_through, settings.db_file, result)
    return _result(result)


@routes.post("/api/icloud/sync")
async def post_sync_hme(request: web.Request) -> web.Response:
    from hidemyemail_generator.main import _sync_hme_to_db

    settings = _settings(request)
    data = await _body(request)
    count = await _sync_hme_to_db(
        settings.cookie_file, _region(request, data), settings.db_file
    )
    return _json({"ok": True, "count": count, "error": None})


def _read_addresses(db_file: str, **filters) -> list[dict]:
    from hidemyemail_generator.main import _address_payload

    conn = connect_db(db_file)
    try:
        return [_address_payload(row) for row in list_addresses(conn, **filters)]
    finally:
        conn.close()


@routes.get("/api/addresses")
async def get_addresses(request: web.Request) -> web.Response:
    addresses = await asyncio.to_thread(
        _read_addresses,
        _settings(request).db_file,
        state=_state(request.query.get("state")),
        limit=_int(request.query.get("limit"), "limit", default=100),
        active=_flag(request.query.get("active")),
        query=request.query.get("query") or None,
        batch_id=request.query.get("batch_id") or None,
    )
    return _json({"ok": True, "addresses": addresses, "error": None})


def _mark(db_file: str, email: str, state: str) -> None:
    conn = connect_db(db_file)
    try:
        mark_address(conn, email, state)
    finally:
        conn.close()


@routes.post("/api/addresses/state")
async def post_address_state(request: web.Request) -> web.Response:
    data = await _body(request)
    email = _text(data, "email", required=True)
    state = _state(_text(data, "state", required=True))
    await asyncio.to_thread(_mark, _settings(request).db_file, email, state)
    return _json({"ok": True, "email": email, "state": state, "error": None})


def _inbox_status(config_file: str, db_file: str) -> dict:
    from hidemyemail_generator.main import inbox_counts

    conn = connect_db(db_file)
    try:
        counts = inbox_counts(conn)
    finally:
        conn.close()

    try:
        config = load_config(config_file)
    except Exception:
        return {
            "ok": True,
            "configured": False,
            "config": None,
            "counts": counts,
            "error": None,
        }

    return {
        "ok": True,
        "configured": True,
        "config": {
            "host": config.host,
            "port": config.port,
            "username": mask_account(config.username),
            "folder": config.folder,
            "use_ssl": config.use_ssl,
        },
        "counts": counts,
        "error": None,
    }


@routes.get("/api/inbox")
async def get_inbox(request: web.Request) -> web.Response:
    settings = _settings(request)
    return _json(
        await asyncio.to_thread(_inbox_status, settings.config_file, settings.db_file)
    )


@routes.post("/api/inbox/config")
async def post_inbox_config(request: web.Request) -> web.Response:
    settings = _settings(request)
    data = await _body(request)
    config = InboxConfig(
        host=_text(data, "host", required=True),
        port=_int(data.get("port"), "port", default=993, maximum=65535),
        username=_text(data, "username", required=True),
        password=_text(data, "password", required=True),
        folder=_text(data, "folder") or DEFAULT_FOLDER,
        use_ssl=bool(data.get("use_ssl", True)),
    )

    def store() -> None:
        save_config(config, settings.config_file)
        connect_db(settings.db_file).close()

    await asyncio.to_thread(store)
    return _json(
        await asyncio.to_thread(_inbox_status, settings.config_file, settings.db_file)
    )


def _sync_inbox(config_file: str, db_file: str, limit: int) -> list[dict]:
    from hidemyemail_generator.main import _message_payload

    config = load_config(config_file)
    return [
        _message_payload(row)
        for row in sync_inbox(config, db_file=db_file, limit=limit)
    ]


@routes.post("/api/inbox/sync")
async def post_inbox_sync(request: web.Request) -> web.Response:
    settings = _settings(request)
    data = await _body(request)
    limit = _int(data.get("limit"), "limit", default=50)
    messages = await asyncio.to_thread(
        _sync_inbox, settings.config_file, settings.db_file, limit
    )
    return _json(
        {"ok": True, "count": len(messages), "messages": messages, "error": None}
    )


def _read_messages(
    db_file: str, only_codes: bool, limit: int, only_unread: bool
) -> dict:
    from hidemyemail_generator.main import _message_payload
    from hidemyemail_generator.inbox import count_unread

    conn = connect_db(db_file)
    try:
        rows = list_messages(
            conn, only_codes=only_codes, limit=limit, only_unread=only_unread
        )
        return {
            "messages": [_message_payload(row) for row in rows],
            "unread": count_unread(conn),
        }
    finally:
        conn.close()


@routes.get("/api/messages")
async def get_messages(request: web.Request) -> web.Response:
    payload = await asyncio.to_thread(
        _read_messages,
        _settings(request).db_file,
        bool(_flag(request.query.get("only_codes"))),
        _int(request.query.get("limit"), "limit", default=50),
        bool(_flag(request.query.get("unread"))),
    )
    return _json({"ok": True, **payload, "error": None})


def _mark_read(db_file: str, ids: list[int]) -> dict:
    from hidemyemail_generator.inbox import count_unread

    conn = connect_db(db_file)
    try:
        return {"count": mark_messages_read(conn, ids), "unread": count_unread(conn)}
    finally:
        conn.close()


@routes.post("/api/messages/read")
async def post_messages_read(request: web.Request) -> web.Response:
    data = await _body(request)
    raw_ids = data.get("ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise RequestError('"ids" must be a non-empty list of message ids')
    try:
        ids = [int(value) for value in raw_ids]
    except (TypeError, ValueError) as e:
        raise RequestError('"ids" must contain numbers') from e

    payload = await asyncio.to_thread(_mark_read, _settings(request).db_file, ids)
    return _json({"ok": True, **payload, "error": None})


def _read_batches(db_file: str, limit: int) -> list[dict]:
    from hidemyemail_generator.main import _batch_payload

    conn = connect_db(db_file)
    try:
        return [_batch_payload(row) for row in list_batches(conn, limit=limit)]
    finally:
        conn.close()


@routes.get("/api/batches")
async def get_batches(request: web.Request) -> web.Response:
    batches = await asyncio.to_thread(
        _read_batches,
        _settings(request).db_file,
        _int(request.query.get("limit"), "limit", default=50),
    )
    return _json({"ok": True, "batches": batches, "error": None})


def _create_batch(
    db_file: str, label: str, target: int, interval_seconds: Optional[int]
) -> dict:
    from hidemyemail_generator.main import _batch_payload

    conn = connect_db(db_file)
    try:
        return _batch_payload(create_batch(conn, label, target, interval_seconds))
    finally:
        conn.close()


@routes.post("/api/batches")
async def post_batches(request: web.Request) -> web.Response:
    data = await _body(request)
    interval_minutes = data.get("interval_minutes")
    batch = await asyncio.to_thread(
        _create_batch,
        _settings(request).db_file,
        _text(data, "label", required=True),
        _int(data.get("target"), "target", default=1, maximum=10_000),
        _int(interval_minutes, "interval_minutes", default=0, minimum=0, maximum=1440)
        * 60
        or None,
    )
    return _json({"ok": True, "batch": batch, "error": None})


def _read_batch(db_file: str, batch_id: str) -> Optional[dict]:
    from hidemyemail_generator.main import _address_payload, _batch_payload

    conn = connect_db(db_file)
    try:
        batch = get_batch(conn, batch_id)
        if batch is None:
            return None
        return {
            "batch": _batch_payload(batch),
            "addresses": [
                _address_payload(row)
                for row in list_addresses(conn, batch_id=batch_id, limit=100_000)
            ],
        }
    finally:
        conn.close()


@routes.get("/api/batches/{batch_id}")
async def get_batch_detail(request: web.Request) -> web.Response:
    batch_id = request.match_info["batch_id"]
    payload = await asyncio.to_thread(_read_batch, _settings(request).db_file, batch_id)
    if payload is None:
        return _failure(f'No batch matching "{batch_id}"', status=404)
    return _json({"ok": True, **payload, "error": None})


def _set_batch_state(db_file: str, batch_id: str, state: str) -> Optional[dict]:
    from hidemyemail_generator.main import _batch_payload

    conn = connect_db(db_file)
    try:
        batch = set_batch_state(conn, batch_id, state)
        return None if batch is None else _batch_payload(batch)
    finally:
        conn.close()


@routes.post("/api/batches/{batch_id}/state")
async def post_batch_state(request: web.Request) -> web.Response:
    batch_id = request.match_info["batch_id"]
    data = await _body(request)
    state = _text(data, "state", required=True)
    if state not in BATCH_STATES:
        raise RequestError(f'Unsupported batch state "{state}"')

    batch = await asyncio.to_thread(
        _set_batch_state, _settings(request).db_file, batch_id, state
    )
    if batch is None:
        return _failure(f'No batch matching "{batch_id}"', status=404)
    return _json({"ok": True, "batch": batch, "error": None})


@routes.post("/api/export")
async def post_export(request: web.Request) -> web.Response:
    settings = _settings(request)
    data = await _body(request)
    outputs = await asyncio.to_thread(
        export_csv_files,
        settings.db_file,
        settings.export_dir,
        _text(data, "batch_id"),
    )
    return _json(
        {
            "ok": True,
            "outputs": {name: str(path) for name, path in outputs.items()},
            "error": None,
        }
    )


def create_app(settings: Settings) -> web.Application:
    app = web.Application(middlewares=[error_middleware, auth_middleware])
    app[SETTINGS] = settings
    app.add_routes(routes)
    return app


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def server_url(host: str, port: int, token: str = "") -> str:
    display_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    url = f"http://{display_host}:{port}/"
    return f"{url}?token={token}" if token else url
