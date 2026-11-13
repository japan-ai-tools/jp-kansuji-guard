"""MCP server (stdio, JSON-RPC 2.0). Python standard library only, no network.

Start: python -m kansujiguard.server
"""
import json
import sys

from . import __version__
from .guard import convert, parse_number

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
RO = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}

TOOLS = [
    {
        "name": "jp_kansuji_convert",
        "title": "Convert kanji numerals in Japanese text (safely)",
        "annotations": RO,
        "description": ("Convert kanji numerals in Japanese text to digits only where they are clearly numbers "
                        "(三億五千万円 -> 350000000円, 第三条 -> 第3条, 令和六年三月十五日 -> 令和6年3月15日, 金壱萬円 -> 金10000円). "
                        "Names, places, companies, words and idioms are kept (一宮市, 五十嵐, 三菱, 統一, 一石二鳥). "
                        "Spans whose meaning depends on the reading (十分, 一時, 一番, 二三人) or that are approximate (数万円) "
                        "are kept and returned in flags. Returns text, changes (start/end/before/after/value/reason), kept, flags. "
                        "Use before searching, indexing or extracting numbers from Japanese text. 漢数字を安全に数字へ。"),
        "inputSchema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Japanese text"}}, "required": ["text"]},
    },
    {
        "name": "jp_kansuji_parse",
        "title": "Parse one Japanese numeral",
        "annotations": RO,
        "description": ("Parse one numeral string to an integer: kanji (三億五千万), formal daiji (壱萬, 参拾), flat digits "
                        "(二〇二四) or mixed (1億2,300万). Returns {value} or {value: null, error} when it is malformed."),
        "inputSchema": {"type": "object", "properties": {
            "numeral": {"type": "string"}}, "required": ["numeral"]},
    },
]


def _parse(a):
    s = _str_arg(a, "numeral")
    v = parse_number(s)
    return {"input": s, "value": v} if v is not None else {"input": s, "value": None, "error": "malformed numeral"}


def _str_arg(args, key):
    v = args.get(key)
    if not isinstance(v, str):
        raise ValueError(f"'{key}' must be a string")
    return v


HANDLERS = {
    "jp_kansuji_convert": lambda a: convert(_str_arg(a, "text")),
    "jp_kansuji_parse": _parse,
}


def _handle_legacy(msg):
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    try:
        if method == "initialize":
            want = params.get("protocolVersion")
            result = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "jp-kansuji-guard", "version": __version__},
                      "instructions": "Before turning Japanese kanji numerals into digits, call jp_kansuji_convert. It never guesses: spans it cannot decide come back in flags, unchanged."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            name = params.get("name")
            if name not in HANDLERS:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            try:
                out = HANDLERS[name](params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                          "structuredContent": out, "isError": False}
            except ValueError as e:
                result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    except Exception as e:  # keep the server alive
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": f"Internal error: {e}"}}


# --- MCP 2026-07-28 (stateless) support. The legacy initialize flow above stays as it was. ---
# Spec: https://modelcontextprotocol.io/specification/2026-07-28/changelog
MODERN_PROTOCOLS = ["2026-07-28"]
_K = "io.modelcontextprotocol/"
_TOOLS_TTL_MS = 300000


def _legacy_info():
    r = _handle_legacy({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})["result"]
    return r["serverInfo"], r.get("instructions"), {k: {} for k in r.get("capabilities", {})}


def _modern_error(mid, code, message, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": err}


def _handle_modern(msg):
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = meta.get(_K + "protocolVersion")
    if not isinstance(version, str):
        return _modern_error(mid, -32602, "Missing _meta io.modelcontextprotocol/protocolVersion")
    if version not in MODERN_PROTOCOLS:
        return _modern_error(mid, -32022, "Unsupported protocol version",
                             {"supported": MODERN_PROTOCOLS + PROTOCOLS, "requested": version})
    if not isinstance(meta.get(_K + "clientCapabilities"), dict):
        return _modern_error(mid, -32602, "Missing _meta io.modelcontextprotocol/clientCapabilities")
    server_info, instructions, caps = _legacy_info()
    if method == "server/discover":
        result = {"resultType": "complete", "supportedVersions": list(MODERN_PROTOCOLS), "capabilities": caps,
                  "_meta": {_K + "serverInfo": server_info}, "ttlMs": 3600000, "cacheScope": "public"}
        if instructions:
            result["instructions"] = instructions
        return {"jsonrpc": "2.0", "id": mid, "result": result}
    if method not in ("tools/list", "tools/call"):
        return _modern_error(mid, -32601, f"Method not found: {method}")
    resp = _handle_legacy(msg)
    if "result" in resp:
        result = dict(resp["result"])
        result["resultType"] = "complete"
        result.setdefault("_meta", {})[_K + "serverInfo"] = server_info
        if method == "tools/list":
            result["ttlMs"] = _TOOLS_TTL_MS
            result["cacheScope"] = "public"
        resp = {"jsonrpc": "2.0", "id": mid, "result": result}
    return resp


def handle(msg):
    """Dual-era: server/discover or a request with modern _meta is stateless 2026-07-28; initialize keeps the old flow."""
    params = msg.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    method = msg.get("method")
    if method == "server/discover" or (method != "initialize" and isinstance(meta, dict)
                                       and _K + "protocolVersion" in meta):
        return _handle_modern(msg)
    return _handle_legacy(msg)


def main():
    for s in (sys.stdin, sys.stdout):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            sys.stdout.flush()
            continue
        resp = handle(msg) if isinstance(msg, dict) else None
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
