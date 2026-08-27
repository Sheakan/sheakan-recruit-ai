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


def _build_sheet_cells(records, header_row, data_start):
    """构造 sheet.set_range_value 的 values：表头行 + 数据行。
    每个单元格形如 {row,col,value_type,string_value|number_value}。"""
    cells = []
    for c, h in enumerate(HEADERS_CN):
        cells.append({"row": header_row, "col": c, "value_type": "STRING", "string_value": h})
    for i, r in enumerate(records):
        rr = data_start + i
        for c, k in enumerate(KEYS):
            v = r.get(k)
            if v is None or v == "":
                cells.append({"row": rr, "col": c, "value_type": "STRING", "string_value": ""})
                continue
            s = str(v)
            # 数值（年龄等）自动按 NUMBER 写，便于筛选/排序
            try:
                num = float(s) if "." in s else int(s)
                if str(num) == s.strip():
                    cells.append({"row": rr, "col": c, "value_type": "NUMBER", "number_value": num})
                    continue
            except Exception:
                pass
            cells.append({"row": rr, "col": c, "value_type": "STRING", "string_value": s})
    return cells


def _extract_sheet_id(info):
    """从 sheet.get_sheet_info 的返回里取第一个子表 ID。"""
    if not info:
        return ""
    try:
        txt = ""
        if isinstance(info, dict):
            for it in (info.get("content") or []):
                if isinstance(it, dict) and it.get("type") == "text":
                    txt += it.get("text", "")
        data = json.loads(txt) if txt else info
        sheets = (data.get("sheets") or (data.get("result") or {}).get("sheets") or [])
        if sheets:
            return sheets[0].get("sheet_id", "")
    except Exception:
        return ""
    return ""


def _detect_layout(token, session, file_id, sheet_id):
    """探测表头所在行与数据起始行：兼容 A1 为标题、第2行为表头的情况（如『招聘候选人信息表』标题+A1表头）。"""
    head_n = min(len(HEADERS_CN), 12)

    def match(row):
        parts = [x.strip() for x in row.split(",")]
        if len(parts) < head_n:
            return False
        return all(parts[i] == HEADERS_CN[i] for i in range(head_n))

    try:
        r = call_tool(token, "sheet.get_cell_data",
                      {"file_id": file_id, "sheet_id": sheet_id,
                       "start_row": 0, "start_col": 0,
                       "end_row": 3, "end_col": len(HEADERS_CN),
                       "return_csv": True}, session)
        txt = ""
        if isinstance(r, dict):
            for it in (r.get("content") or []):
                if isinstance(it, dict) and it.get("type") == "text":
                    txt += it.get("text", "")
        data = json.loads(txt) if txt else {}
        rows = data.get("csv_data", "").splitlines()
        if len(rows) > 1 and match(rows[1]):
            return 1, 2
        if rows and match(rows[0]):
            return 0, 1
    except Exception:
        pass
    return 1, 2  # 默认：A1 为标题、第2行为表头


def _real_doc_url(token, session, file_id):
    """尽量取真实可打开链接（file_id slug 与实际 URL slug 通常不同）。"""
    try:
        r = call_tool(token, "manage.query_file_info", {"file_id": file_id}, session)
        return _extract_url(_text_of(r))
    except Exception:
        return ""


# ---------------- 写入入口 ----------------
import re as _re
_URL_RE = _re.compile(r'https?://docs\.qq\.com[^\s"\'<>\)]*')

def _extract_url(text):
    if not text:
        return ""
    m = _URL_RE.search(text)
    return m.group(0) if m else ""

def _doc_url(res, file_id):
    """尽量拿到可打开的腾讯文档链接：优先从返回文本里找 docs.qq.com 链接，
    其次用 file_id 兜底拼一个（腾讯文档表格 URL 形如 https://docs.qq.com/sheet/<id>）。"""
    if isinstance(res, dict):
        url = _extract_url(_text_of(res))
        if url:
            return url
        for k in ("url", "web_url", "link", "file_url"):
            v = res.get(k)
            if v and "docs.qq.com" in str(v):
                return str(v)
    if file_id:
        return "https://docs.qq.com/sheet/" + file_id
    return ""

def push(records, file_id=None, sheet_id=None):
    """把记录同步到腾讯文档。返回 (记录条数, 错误信息列表, 文档链接)。失败不影响主流程。

    写入路径（按 MCP 实际暴露的工具动态选择）：
      1) 在线表格 sheet：用 sheet.set_range_value（需先取子表 sheet_id，单元格列表格式）。
         覆盖式同步——重写表头 + 全量数据（从探测到的数据起始行起）。
      2) 智能表格 smartsheet：用 smartsheet.add_records（按字段标题写，追加到末尾）。
    """
    token = _token()
    if not token:
        return 0, ["未配置腾讯文档 Token：请在「配置我的凭证」填入 MCP Token（https://docs.qq.com/open/auth/mcp.html 获取）"], ""
    file_id = file_id or FILE_ID
    if not file_id:
        return 0, ["未指定目标表格 file_id：请在配置或主页面粘贴腾讯文档表格链接（从链接里取 ID）"], ""
    if not records:
        return 0, [], ""

    pushed = len(records)
    errs = []
    try:
        session = _initialize(token)
        tools = list_tools(token, session)
    except Exception as e:
        return 0, [f"连接腾讯文档 MCP 失败：{e}"], ""
    names = {t.get("name") for t in tools}

    # 路径1：普通在线表格 sheet.set_range_value（推荐）
    if "sheet.set_range_value" in names:
        if not sheet_id:
            sheet_id = _extract_sheet_id(call_tool(token, "sheet.get_sheet_info", {"file_id": file_id}, session))
        if not sheet_id:
            return pushed, ["无法获取子表 ID（请确认 file_id 正确，且该文件是在线表格 sheet 而非智能表格）"], ""
        header_row, data_start = _detect_layout(token, session, file_id, sheet_id)
        cells = _build_sheet_cells(records, header_row, data_start)
        try:
            res = call_tool(token, "sheet.set_range_value",
                           {"file_id": file_id, "sheet_id": sheet_id, "values": cells}, session)
            if res is None or (isinstance(res, dict) and res.get("isError")):
                return pushed, ["写入失败：" + _text_of(res)], ""
            url = _real_doc_url(token, session, file_id) or ("https://docs.qq.com/sheet/" + file_id)
            return pushed, errs, url
        except Exception as e:
            return pushed, [f"写入调用失败：{e}"], ""

    # 路径2：智能表格 smartsheet.add_records（按字段标题写，追加到末尾）
    if "smartsheet.add_records" in names:
        recs = []
        for r in records:
            fv = [{"title": HEADERS_CN[i], "value": ("" if r.get(KEYS[i]) is None else str(r.get(KEYS[i])))}
                  for i in range(len(KEYS))]
            recs.append({"field_values": fv})
        try:
            res = call_tool(token, "smartsheet.add_records", {"file_id": file_id, "records": recs}, session)
            if res is None or (isinstance(res, dict) and res.get("isError")):
                return pushed, ["智能表格写入失败：" + _text_of(res)], ""
            url = _real_doc_url(token, session, file_id) or ("https://docs.qq.com/smartsheet/" + file_id)
            return pushed, errs, url
        except Exception as e:
            return pushed, [f"智能表格写入调用失败：{e}"], ""

    return pushed, ["未找到可用写入工具；可用工具：" + ", ".join(sorted(names))], ""


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
