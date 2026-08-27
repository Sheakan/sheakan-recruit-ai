# -*- coding: utf-8 -*-
"""腾讯文档同步适配器（官方 MCP 版）。

改用腾讯文档官方 MCP（Model Context Protocol），个人开发者无需企业资质：
  - 令牌：单个「个人 Token」，从 https://docs.qq.com/open/auth/mcp.html 获取
  - 端点：https://docs.qq.com/openapi/mcp
  - 鉴权：HTTP 头 Authorization: <Token>（注意不是 Access-Token / Client-Id）
  - 协议：JSON-RPC 2.0 over Streamable HTTP（响应可能是 application/json 或 text/event-stream）

写入策略（运行时按「工具实际 schema」动态拼参数，避免把参数名写死）：
  1) 若配置了目标表格 file_id，优先用 batch_update_sheet_range 写入（全量覆盖式）；
  2) 否则兜底用 create_excel_by_markdown 新建一个 Excel 快照。
两种工具都不存在时，返回可用工具清单，便于据此微调参数。
"""
import os
import json
import requests
import config_store
from fields import HEADERS_CN, KEYS

MCP_URL = os.environ.get("TENCENT_DOCS_MCP_URL", "https://docs.qq.com/openapi/mcp")
MCP_TOKEN = os.environ.get("TENCENT_DOCS_MCP_TOKEN", "")
FILE_ID = os.environ.get("TENCENT_DOCS_FILE_ID", "")
SHEET_ID = os.environ.get("TENCENT_DOCS_SHEET_ID", "")


# ---------------- 配置读取 ----------------
def _cfg():
    return config_store.load().get("tencent_docs", {})


def _token():
    # 环境变量优先，其次模块级全局（server.py 注入），其次界面配置
    return (os.environ.get("TENCENT_DOCS_MCP_TOKEN")
            or MCP_TOKEN
            or _cfg().get("mcp_token") or "").strip()


def enabled():
    """是否已具备可写入的令牌。"""
    return bool(_token())


# ---------------- MCP 协议层（JSON-RPC over Streamable HTTP）----------------
def _parse_response(resp):
    """兼容 application/json 与 text/event-stream(SSE) 两种响应。"""
    ctype = resp.headers.get("Content-Type", "")
    text = resp.text or ""
    if "text/event-stream" in ctype:
        last = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    try:
                        last = json.loads(payload)
                    except Exception:
                        pass
        return last
    if not text:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def _rpc(token, method, params=None, req_id=1, session_id=None, is_notification=False, timeout=30):
    """发送一次 JSON-RPC 请求，返回 (result_json, session_id)。"""
    headers = {
        "Authorization": token,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    body = {"jsonrpc": "2.0", "method": method}
    if not is_notification:
        body["id"] = req_id
    if params is not None:
        body["params"] = params
    resp = requests.post(MCP_URL, headers=headers, json=body, timeout=timeout)
    new_session = resp.headers.get("Mcp-Session-Id") or session_id
    return _parse_response(resp), new_session


def _initialize(token):
    """完成 MCP 握手，返回后续调用要用的 session_id。"""
    data, session = _rpc(token, "initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "recruit-ai", "version": "1.0"},
    }, req_id=1)
    if data and data.get("error"):
        raise RuntimeError("MCP initialize 失败：" + json.dumps(data["error"], ensure_ascii=False))
    # 通知服务端初始化完成（无需响应）
    _rpc(token, "notifications/initialized", {}, is_notification=True, session_id=session)
    return session


def list_tools(token, session):
    data, _ = _rpc(token, "tools/list", {}, req_id=2, session_id=session)
    return (data or {}).get("result", {}).get("tools", [])


def call_tool(token, name, arguments, session):
    data, _ = _rpc(token, "tools/call", {"name": name, "arguments": arguments}, req_id=3, session_id=session)
    return (data or {}).get("result")


# ---------------- 参数拼装（按运行时 schema 动态映射）----------------
def _build_rows(records):
    rows = []
    for r in records:
        vals = []
        for k in KEYS:
            v = r.get(k)
            vals.append("" if v is None else str(v))
        rows.append(vals)
    return rows


def _build_markdown(records):
    rows = _build_rows(records)
    cols = HEADERS_CN
    lines = ["| " + " | ".join(cols) + " |",
             "| " + " | ".join(["---"] * len(cols)) + " |"]
    for vals in rows:
        lines.append("| " + " | ".join(vals) + " |")
    return "\n".join(lines)


def _pick(props, *candidates):
    for c in candidates:
        if c in props:
            return c
    return None


def _map_range_args(props, file_id, sheet_id, records):
    """把我们的数据映射到 batch_update_sheet_range 的参数（按属性名实际存在与否）。"""
    rows = _build_rows(records)
    values = [list(HEADERS_CN)] + rows
    args = {}
    k = _pick(props, "file_id", "doc_id", "fileId", "docId")
    if k:
        args[k] = file_id
    k = _pick(props, "sheet_id", "sheetId")
    if k:
        args[k] = sheet_id or ""
    k = _pick(props, "range", "rangeA1", "a1Range")
    if k:
        args[k] = "A1"
    k = _pick(props, "values", "data", "cells")
    if k:
        args[k] = values
    k = _pick(props, "valueInputOption", "value_input_option")
    if k:
        args[k] = "USER_ENTERED"
    k = _pick(props, "markdown", "content", "markdownContent")
    if k and not any(x in args for x in ("values", "data", "cells")):
        args[k] = _build_markdown(records)
    return args or None


def _map_create_args(props, records):
    md = _build_markdown(records)
    args = {}
    k = _pick(props, "markdown", "content", "markdownContent")
    if k:
        args[k] = md
    k = _pick(props, "title", "name", "fileName")
    if k:
        args[k] = "招聘数据同步快照"
    k = _pick(props, "parent_id", "parentId", "folder_id")
    if k:
        args[k] = ""
    return args or None


# ---------------- 写入入口 ----------------
def push(records, file_id=None, sheet_id=None):
    """把记录同步到腾讯文档。返回 (记录条数, 错误信息列表)。失败不影响主流程。"""
    token = _token()
    if not token:
        return 0, ["未配置腾讯文档 Token：请在「配置我的凭证」填入 MCP Token（https://docs.qq.com/open/auth/mcp.html 获取）"]
    file_id = file_id or FILE_ID
    if not records:
        return 0, []

    pushed = len(records)
    errs = []
    try:
        session = _initialize(token)
        tools = list_tools(token, session)
    except Exception as e:
        return 0, [f"连接腾讯文档 MCP 失败：{e}"]

    names = {t.get("name") for t in tools}

    # 1) 已有目标表格 → 更新
    if file_id and "batch_update_sheet_range" in names:
        t = next(t for t in tools if t.get("name") == "batch_update_sheet_range")
        props = (t.get("inputSchema") or {}).get("properties", {})
        args = _map_range_args(props, file_id, sheet_id, records)
        if args:
            try:
                res = call_tool(token, "batch_update_sheet_range", args, session)
                if isinstance(res, dict) and res.get("isError"):
                    errs.append("batch_update_sheet_range 报错：" + _text_of(res))
                else:
                    return pushed, errs
            except Exception as e:
                errs.append(f"batch_update_sheet_range 调用失败：{e}")
        else:
            errs.append("batch_update_sheet_range 参数无法映射，schema=" + json.dumps(props, ensure_ascii=False))

    # 2) 兜底：新建 Excel 快照
    if "create_excel_by_markdown" in names:
        t = next(t for t in tools if t.get("name") == "create_excel_by_markdown")
        props = (t.get("inputSchema") or {}).get("properties", {})
        args = _map_create_args(props, records)
        if args:
            try:
                res = call_tool(token, "create_excel_by_markdown", args, session)
                if isinstance(res, dict) and res.get("isError"):
                    errs.append("create_excel_by_markdown 报错：" + _text_of(res))
                else:
                    return pushed, errs
            except Exception as e:
                errs.append(f"create_excel_by_markdown 调用失败：{e}")
        else:
            errs.append("create_excel_by_markdown 参数无法映射，schema=" + json.dumps(props, ensure_ascii=False))
    else:
        errs.append("未找到可用写入工具；可用工具：" + ", ".join(sorted(names)))

    return pushed, errs


def _text_of(result):
    """从 MCP tools/call 的 result 里抽取文本，便于报错展示。"""
    try:
        parts = []
        for item in (result.get("content") or []):
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return " ".join(parts) or json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)
