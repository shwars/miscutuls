#!/usr/bin/env python3
"""mcprobe - probe an MCP (Model Context Protocol) server.

Tries to connect to the server at the given URL using all supported
transports (raw JSON-RPC POST, Streamable HTTP, legacy HTTP+SSE) and,
on success, lists everything the server exposes: tools, prompts,
resources and resource templates, with descriptions and parameter
schemas.

Requires: Python 3.10+, libraries listed in mcprobe.requirements.txt.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from typing import Any

try:
    import httpx
except ImportError:
    print("error: the 'httpx' library is required (see mcprobe.requirements.txt)", file=sys.stderr)
    raise SystemExit(2)

try:
    import mcp
    from mcp import ClientSession, types as mcp_types
    from mcp.client.session import ClientSession as _ClientSession  # noqa: F401
except ImportError:
    print("error: the 'mcp' library is required (see mcprobe.requirements.txt)", file=sys.stderr)
    raise SystemExit(2)


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

VERBOSE = False


def log(msg: str) -> None:
    print(msg)


def vlog(msg: str) -> None:
    if VERBOSE:
        print(f"  [mcprobe] {msg}")


class TransportError(Exception):
    """Raised when a transport attempt fails."""


# --------------------------------------------------------------------------
# Raw JSON-RPC POST probe (no SSE framing)
# --------------------------------------------------------------------------

JSONRPC_HEADERS = {"Content-Type": "application/json", "Accept": "application/json"}

INITIALIZE_REQUEST = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "mcprobe", "version": "1.0.0"},
    },
}


def _looks_like_jsonrpc_error(response_json: Any) -> bool:
    return (
        isinstance(response_json, dict)
        and response_json.get("jsonrpc") == "2.0"
        and (
            "error" in response_json
            or ("result" not in response_json and "id" in response_json)
        )
    )


def _parse_sse_data(body: str) -> dict[str, Any] | None:
    """Extract the first JSON-RPC message from an SSE text body."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[5:].strip()
            if not data:
                continue
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                continue
    return None


def probe_raw_post(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """Try bare JSON-RPC over HTTP POST (no SSE). Returns initialize result."""
    try:
        response = httpx.post(
            url,
            json=INITIALIZE_REQUEST,
            headers={**JSONRPC_HEADERS, **headers},
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise TransportError(f"connection failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise TransportError(
            f"HTTP {response.status_code} - authentication required "
            f"(try --header 'Authorization: Bearer <token>')"
        )
    if response.status_code == 404:
        raise TransportError("HTTP 404 - endpoint not found (wrong URL?)")
    if response.status_code == 405:
        raise TransportError("HTTP 405 - POST not allowed on this URL")
    if response.status_code >= 400:
        raise TransportError(f"HTTP {response.status_code}: {response.text[:200]}")

    content_type = response.headers.get("content-type", "")
    body = response.text

    if "text/event-stream" in content_type:
        # Server wants SSE framing even for POST responses (Streamable HTTP).
        parsed = _parse_sse_data(body)
        if parsed is None:
            raise TransportError("SSE response but no 'data:' line with JSON")
    else:
        try:
            parsed = response.json()
        except json.JSONDecodeError:
            raise TransportError(
                f"unexpected content-type '{content_type.split(';')[0]}' "
                f"and body is not JSON: {body[:200]}"
            ) from None

    if _looks_like_jsonrpc_error(parsed):
        err = parsed.get("error", {})
        raise TransportError(f"JSON-RPC error: {err}")

    result = parsed.get("result")
    if not isinstance(result, dict) or "serverInfo" not in result:
        raise TransportError("response is not a valid MCP initialize result")
    return result


# --------------------------------------------------------------------------
# SDK-based transports
# --------------------------------------------------------------------------

async def _run_session(
    transport_name: str,
    url: str,
    headers: dict[str, str],
    timeout: float,
    transport_factory: Any,
) -> dict[str, Any]:
    """Open a transport, initialize a session, and collect server info."""
    import datetime as _dt

    read_timeout = _dt.timedelta(seconds=max(timeout * 3, 15))
    vlog(f"{transport_name}: connecting to {url}")

    async with transport_factory(url, headers=headers or None, timeout=timeout) as streams:
        read_stream, write_stream = streams[0], streams[1]
        async with ClientSession(
            read_stream, write_stream, read_timeout_seconds=read_timeout
        ) as session:
            init_start = time.monotonic()
            try:
                init_result = await session.initialize()
            except Exception as exc:
                raise TransportError(f"initialize failed: {exc}") from exc
            init_elapsed = time.monotonic() - init_start

            info: dict[str, Any] = {
                "transport": transport_name,
                "url": url,
                "handshake_seconds": round(init_elapsed, 3),
                "serverInfo": {
                    "name": init_result.serverInfo.name,
                    "version": init_result.serverInfo.version,
                },
                "protocolVersion": init_result.protocolVersion,
                "capabilities": _capabilities_to_dict(init_result.capabilities),
                "instructions": init_result.instructions,
            }

            # Ping round-trip
            try:
                ping_start = time.monotonic()
                await session.send_ping()
                info["ping_seconds"] = round(time.monotonic() - ping_start, 3)
            except Exception as exc:
                vlog(f"ping failed: {exc}")
                info["ping_seconds"] = None

            # Enumerate capabilities
            caps = info["capabilities"]
            if caps.get("tools"):
                info["tools"] = await _safe_list(session.list_tools, "tools")
            if caps.get("prompts"):
                info["prompts"] = await _safe_list(session.list_prompts, "prompts")
            if caps.get("resources"):
                info["resources"] = await _safe_list(session.list_resources, "resources")
                info["resource_templates"] = await _safe_list(
                    session.list_resource_templates, "resource templates"
                )
            return info


async def _safe_list(coro_fn, what: str) -> list[dict[str, Any]] | None:
    try:
        result = await coro_fn()
    except Exception as exc:
        vlog(f"listing {what} failed: {exc}")
        return None
    # list_* calls return an object with a `.tools` / `.prompts` /
    # `.resources` / `.resourceTemplates` collection attribute.
    items = None
    for attr in ("tools", "prompts", "resources", "resourceTemplates"):
        items = getattr(result, attr, None)
        if items is not None:
            break
    if items is None:
        items = result
    out = []
    for item in items:
        out.append(item.model_dump(mode="json", exclude_none=True))
    return out


def _capabilities_to_dict(caps: Any) -> dict[str, Any]:
    if caps is None:
        return {}
    try:
        return caps.model_dump(mode="json", exclude_none=True)
    except Exception:
        return {"raw": str(caps)}


async def probe_streamable_http(
    url: str, headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    from mcp.client.streamable_http import streamablehttp_client

    def factory(u, headers=None, timeout=30, **kwargs):
        return streamablehttp_client(u, headers=headers, timeout=timeout, **kwargs)

    return await _run_session(
        "streamable-http", url, headers, timeout, _wrap_ctx(factory)
    )


async def probe_sse(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    from mcp.client.sse import sse_client

    def factory(u, headers=None, timeout=30, **kwargs):
        return sse_client(u, headers=headers, timeout=timeout, **kwargs)

    return await _run_session("sse", url, headers, timeout, _wrap_ctx(factory))


def _wrap_ctx(factory):
    """Adapt a transport factory so `_run_session` can open it uniformly as
    `factory(url, headers=..., timeout=...)` and use the result as an async
    context manager.  Works both with factories that return an async CM
    directly (sse_client) and coroutine factories (async def ...)."""

    class _Adapter:
        def __init__(self, u, headers, timeout):
            self._factory = factory
            self._args = (u, headers, timeout)
            self._cm = None

        async def __aenter__(self):
            u, hdrs, timeout = self._args
            obj = self._factory(u, headers=hdrs, timeout=timeout)
            # If factory is `async def`, awaiting yields the async CM;
            # otherwise the call already returned the async CM itself.
            if hasattr(obj, "__aenter__"):
                self._cm = obj
            else:
                self._cm = await obj
            return await self._cm.__aenter__()

        async def __aexit__(self, *args):
            return await self._cm.__aexit__(*args)

    return _Adapter


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def _format_schema(schema: dict[str, Any], indent: int = 6) -> list[str]:
    """Render a JSON schema (tool inputSchema) into readable lines."""
    lines: list[str] = []
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not props:
        if isinstance(schema, dict) and schema.get("type"):
            lines.append(" " * indent + f"(no parameters, type: {schema['type']})")
        else:
            lines.append(" " * indent + "(no parameters)")
        return lines

    required = set(schema.get("required", []))

    def render_prop(name: str, spec: Any, req: bool, indent: int) -> None:
        pad = " " * indent
        if not isinstance(spec, dict):
            lines.append(f"{pad}{name}: {spec!r}")
            return
        ptype = spec.get("type", "?")
        if "anyOf" in spec:
            ptype = " | ".join(
                s.get("type", "?") if isinstance(s, dict) else "?" for s in spec["anyOf"]
            )
        elif "enum" in spec:
            ptype = f"enum{spec['enum']}"
        desc = spec.get("description", "")
        default = spec.get("default")
        star = "" if req else " (optional)"
        head = f"{pad}{name}: {ptype}{star}"
        if default is not None:
            head += f"  [default: {json.dumps(default)}]"
        lines.append(head)
        if desc:
            for dline in desc.splitlines():
                lines.append(f"{pad}    {dline}")
        if ptype == "array" and isinstance(spec.get("items"), dict):
            item_props = spec["items"].get("properties")
            if item_props:
                lines.append(f"{pad}    items:")
                sub_required = set(spec["items"].get("required", []))
                for sub_name, sub_spec in item_props.items():
                    render_prop(sub_name, sub_spec, sub_name in sub_required, indent + 8)
        if ptype == "object" and isinstance(spec.get("properties"), dict):
            lines.append(f"{pad}    properties:")
            sub_required = set(spec.get("required", []))
            for sub_name, sub_spec in spec["properties"].items():
                render_prop(sub_name, sub_spec, sub_name in sub_required, indent + 8)

    for name, spec in props.items():
        render_prop(name, spec, name in required, indent)
    return lines


def format_report(info: dict[str, Any]) -> str:
    out: list[str] = []
    srv = info.get("serverInfo", {})
    out.append("=" * 70)
    out.append(f"Server : {srv.get('name', '?')} v{srv.get('version', '?')}")
    out.append(f"URL    : {info['url']}")
    out.append(f"Transport: {info['transport']}   (handshake {info['handshake_seconds']}s)")
    pv = info.get("protocolVersion")
    out.append(f"Protocol version: {pv}")
    ping = info.get("ping_seconds")
    out.append(f"Ping   : {ping}s" if ping is not None else "Ping   : n/a")
    if info.get("instructions"):
        out.append("Instructions:")
        for line in info["instructions"].splitlines():
            out.append(f"    {line}")

    caps = info.get("capabilities", {})
    cap_bits = []
    for k in ("tools", "prompts", "resources", "resource_templates"):
        if info.get(k) is not None:
            cap_bits.append(k)
    for k, v in caps.items():
        if k not in ("tools", "prompts", "resources") and v:
            cap_bits.append(k)
    out.append(f"Capabilities: {', '.join(cap_bits) if cap_bits else '(none reported)'}")

    tools = info.get("tools")
    prompts = info.get("prompts")
    resources = info.get("resources")
    templates = info.get("resource_templates")

    if tools:
        out.append("")
        out.append(f"TOOLS ({len(tools)})")
        out.append("-" * 70)
        for tool in tools:
            out.append(f"* {tool.get('name', '?')}")
            desc = tool.get("description")
            if desc:
                for dline in desc.splitlines():
                    out.append(f"    {dline}")
            schema = tool.get("inputSchema") or {}
            for line in _format_schema(schema, indent=4):
                out.append(line)
            if tool.get("outputSchema"):
                out.append("    output schema:")
                for line in _format_schema(tool["outputSchema"], indent=6):
                    out.append(line)
            if tool.get("annotations"):
                out.append(f"    annotations: {json.dumps(tool['annotations'])}")

    if prompts:
        out.append("")
        out.append(f"PROMPTS ({len(prompts)})")
        out.append("-" * 70)
        for prompt in prompts:
            out.append(f"* {prompt.get('name', '?')}")
            desc = prompt.get("description")
            if desc:
                for dline in desc.splitlines():
                    out.append(f"    {dline}")
            args = prompt.get("arguments") or []
            for arg in args:
                req = "required" if arg.get("required") else "optional"
                out.append(f"    - {arg.get('name')}: {req}")
                if arg.get("description"):
                    out.append(f"        {arg['description']}")

    if resources or templates:
        out.append("")
        out.append(
            f"RESOURCES ({len(resources or [])})  TEMPLATES ({len(templates or [])})"
        )
        out.append("-" * 70)
        for res in resources or []:
            out.append(f"* {res.get('uri', '?')}  [{res.get('mimeType') or '-'}]")
            if res.get("name"):
                out.append(f"    name: {res['name']}")
            if res.get("description"):
                for dline in res["description"].splitlines():
                    out.append(f"    {dline}")
        for tpl in templates or []:
            out.append(f"* {tpl.get('uriTemplate', '?')}  (template)")
            if tpl.get("name"):
                out.append(f"    name: {tpl['name']}")
            if tpl.get("description"):
                for dline in tpl["description"].splitlines():
                    out.append(f"    {dline}")

    out.append("=" * 70)
    return "\n".join(out)


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------

def parse_header_args(header_args: list[str] | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    for raw in header_args or []:
        if ":" not in raw:
            raise SystemExit(f"error: bad --header value '{raw}', expected 'Name: value'")
        name, _, value = raw.partition(":")
        headers[name.strip()] = value.strip()
    return headers


async def probe_all(
    url: str,
    headers: dict[str, str],
    timeout: float,
    transport: str,
) -> dict[str, Any]:
    attempts: list[str] = []
    order = {
        "auto": ["raw", "streamable-http", "sse"],
        "raw": ["raw"],
        "http": ["streamable-http"],
        "sse": ["sse"],
    }.get(transport)
    if order is None:
        raise SystemExit(f"error: unknown transport '{transport}'")

    if "raw" in order:
        vlog("trying raw JSON-RPC POST...")
        try:
            result = probe_raw_post(url, headers, timeout)
            # Got initialize result but we still need full session for listing.
            vlog("raw POST answered initialize; trying full streamable-http session")
        except TransportError as exc:
            attempts.append(f"raw-post: {exc}")
            vlog(f"raw POST failed: {exc}")
        else:
            # Fall through to full session transports for enumeration.
            pass

    for name in ("streamable-http", "sse"):
        if name not in order:
            continue
        vlog(f"trying {name} transport...")
        try:
            if name == "streamable-http":
                return await probe_streamable_http(url, headers, timeout)
            else:
                return await probe_sse(url, headers, timeout)
        except TransportError as exc:
            attempts.append(f"{name}: {exc}")
            vlog(f"{name} failed: {exc}")
        except BaseException as exc:
            # SDK transports raise ExceptionGroup with the underlying cause.
            reasons = [f"{type(sub).__name__}: {sub}" for sub in getattr(exc, "exceptions", [exc])]
            attempts.append(f"{name}: {'; '.join(reasons)}")
            vlog(f"{name} failed: {'; '.join(reasons)}")

    details = "\n".join(f"  - {a}" for a in attempts) or "  (no details)"
    raise SystemExit(
        f"error: could not connect to {url} with any transport:\n{details}"
    )


def summarize_raw(info: dict[str, Any]) -> None:
    """If only the raw POST succeeded, at least show the server identity."""
    srv = info.get("serverInfo", {})
    log("=" * 70)
    log(f"Server : {srv.get('name', '?')} v{srv.get('version', '?')}")
    log(f"URL    : raw JSON-RPC POST responded, but session enumeration")
    log(f"         requires a full transport (streamable-http or sse).")
    log("=" * 70)


def main() -> None:
    global VERBOSE
    parser = argparse.ArgumentParser(
        prog="mcprobe",
        description="Probe an MCP server: try all transports, list tools/prompts/resources.",
        epilog="example: mcprobe.py https://mcp.example.com/mcp --header 'Authorization: Bearer TOKEN'",
    )
    parser.add_argument("url", help="MCP server URL (http://, https://)")
    parser.add_argument(
        "--header",
        action="append",
        metavar="NAME: VALUE",
        help="extra HTTP header, repeatable (e.g. authorization)",
    )
    parser.add_argument("--timeout", type=float, default=10, help="per-request timeout, seconds (default 10)")
    parser.add_argument(
        "--transport",
        choices=["auto", "raw", "http", "sse"],
        default="auto",
        help="transport to use: auto = try all (default)",
    )
    parser.add_argument("--json", action="store_true", help="output raw JSON instead of a readable report")
    parser.add_argument("-v", "--verbose", action="store_true", help="show per-transport progress")
    args = parser.parse_args()

    VERBOSE = args.verbose
    url = args.url
    if not re.match(r"^https?://", url):
        raise SystemExit(f"error: URL must start with http:// or https:// (got '{url}')")

    headers = parse_header_args(args.header)
    info = asyncio.run(probe_all(url, headers, args.timeout, args.transport))

    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False))
    else:
        print(format_report(info))


if __name__ == "__main__":
    main()
