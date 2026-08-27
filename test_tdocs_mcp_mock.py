# -*- coding: utf-8 -*-
"""腾讯文档 MCP 适配器的本地协议层测试（无需联网/无需真实 Token）。

用假的 requests.post 模拟腾讯文档 MCP 端点，验证：
  1) initialize 握手 -> notifications/initialized 通知；
  2) tools/list 拿到工具清单；
  3) 配置 file_id 时走 sheet.set_range_value（单元格列表格式，表头+数据）；
  4) 未配置 file_id 时直接报错（不再有 create 兜底）；
  5) 未配置 Token 时不发起任何请求、直接报错；
  6) Authorization 头、SSE 解析、Mcp-Session-Id 透传均正确。
"""
import sys, os, json as _json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tencent_docs_store as tdocs
import config_store as _cs
# 隔离外部配置：重置为默认（不依赖仓库/本地的 config.json 污染）
try:
    _cs.save({})
except Exception:
    pass

# ---- _mock requests.post ----
CALLS = []          # 记录每次请求: {"url","headers","body"}
SESSION_ID = "sid-abc-123"

def _build_sse(payload):
    return "event: message\ndata: " + _json.dumps(payload, ensure_ascii=False) + "\n\n"

TOOLS = [
    {"name": "sheet.get_sheet_info", "inputSchema": {"properties": {"file_id": {"type": "string"}}}},
    {"name": "sheet.get_cell_data", "inputSchema": {"properties": {}}},
    {"name": "sheet.set_range_value", "inputSchema": {
        "properties": {"file_id": {"type": "string"}, "sheet_id": {"type": "string"}, "values": {"type": "array"}}}},
    {"name": "smartsheet.add_records", "inputSchema": {"properties": {}}},
    {"name": "manage.query_file_info", "inputSchema": {"properties": {"file_id": {"type": "string"}}}},
]

class _Resp:
    def __init__(self, text, ctype="text/event-stream", session_id=None):
        self.text = text
        self.headers = {"Content-Type": ctype}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
    def json(self):
        return json.loads(self.text)

def _ok(content):
    return {"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": content}]}}

def fake_post(url, headers=None, json=None, timeout=30, **kw):
    body = json
    CALLS.append({"url": url, "headers": headers, "body": body})
    method = body.get("method")
    if method == "initialize":
        return _Resp(_build_sse({"jsonrpc": "2.0", "id": body.get("id"),
                                 "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}), session_id=SESSION_ID)
    if method == "notifications/initialized":
        return _Resp("", "application/json", session_id=SESSION_ID)
    if method == "tools/list":
        return _Resp(_build_sse({"jsonrpc": "2.0", "id": body.get("id"),
                                 "result": {"tools": TOOLS}}), session_id=SESSION_ID)
    if method == "tools/call":
        name = body["params"]["name"]
        if name == "sheet.get_sheet_info":
            return _Resp(_build_sse(_ok(_json.dumps({"sheets": [{"sheet_id": "BB08J2"}]}))), session_id=SESSION_ID)
        if name == "sheet.get_cell_data":
            return _Resp(_build_sse(_ok(_json.dumps({"csv_data": ""}))), session_id=SESSION_ID)
        if name == "sheet.set_range_value":
            return _Resp(_build_sse(_ok("ok")), session_id=SESSION_ID)
        if name == "manage.query_file_info":
            return _Resp(_build_sse(_ok(_json.dumps({"url": "https://docs.qq.com/sheet/DU2FKUXNEakJveE9B"}))), session_id=SESSION_ID)
        if name == "smartsheet.add_records":
            return _Resp(_build_sse(_ok("ok")), session_id=SESSION_ID)
    return _Resp(_build_sse({"jsonrpc": "2.0", "id": body.get("id"),
                             "error": {"code": -32601, "message": "method not found"}}), session_id=SESSION_ID)

tdocs.requests.post = fake_post

RECORDS = [
    {"candidate": "张伟", "gender": "男", "age": "29", "position": "后端实习", "stage": "offer", "status": "已发"},
]

def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  OK -", msg)

def find_call(method):
    for c in CALLS:
        if c["body"].get("method") == method:
            return c
        if c["body"].get("params", {}).get("name") == method:
            return c
    return None

print("== 测试1: 配置 file_id -> sheet.set_range_value ==")
CALLS.clear()
tdocs.MCP_TOKEN = "test-token"
tdocs.FILE_ID = ""
res = tdocs.push(RECORDS, file_id="FILE123", sheet_id=None)
print("  push 返回:", res)
_assert(isinstance(res, tuple) and len(res) == 3, "push 返回 3 元组 (pushed, errs, url)")
_assert(res[0] == 1 and res[1] == [], "push 返回 (1, [], url)")
_assert("docs.qq.com" in (res[2] or ""), "返回可打开的腾讯文档链接")

init = find_call("initialize")
_assert(init is not None, "存在 initialize 请求")
_assert(init["headers"].get("Authorization") == "test-token", "Authorization 头带 token")
_assert(init["headers"].get("Mcp-Session-Id") is None, "initialize 无 session 头")

notif = find_call("notifications/initialized")
_assert(notif is not None, "存在 notifications/initialized 通知")
_assert(notif["headers"].get("Mcp-Session-Id") == SESSION_ID, "通知携带 session-id")

lst = find_call("tools/list")
_assert(lst is not None, "存在 tools/list 请求")

info = find_call("sheet.get_sheet_info")
_assert(info is not None, "调用了 sheet.get_sheet_info 取子表 ID")

call = find_call("sheet.set_range_value")
_assert(call is not None, "调用了 sheet.set_range_value")
params = call["body"].get("params")
args = params["arguments"]
_assert(params["name"] == "sheet.set_range_value", "调用名正确")
_assert(args["file_id"] == "FILE123", "file_id 映射正确")
_assert(args["sheet_id"] == "BB08J2", "sheet_id 取自 get_sheet_info")
_assert(SESSION_ID and call["headers"].get("Mcp-Session-Id") == SESSION_ID, "调用透传 Mcp-Session-Id")
vals = args["values"]
_exp = len(__import__("fields").get_keys()) * (1 + len(RECORDS))  # 表头行 + 每条记录一行
_assert(isinstance(vals, list) and len(vals) == _exp, f"values 共 {_exp} 个单元格(表头+每记录一行)")
_assert(vals[0] == {"row": 1, "col": 0, "value_type": "STRING", "string_value": "候选人"}, "表头首项为中文列名 候选人")
_assert(any(v.get("string_value") == "张伟" for v in vals), "数据单元格含 张伟")
_assert(any(v.get("string_value") == "offer" for v in vals), "数据单元格含 offer(阶段)")

print("== 测试2: 未指定 file_id -> 直接报错，不联网 ==")
CALLS.clear()
tdocs.FILE_ID = ""
res = tdocs.push(RECORDS, file_id=None)
_assert(res[0] == 0, "未指定 file_id 返回 0 条")
_assert(len(res[1]) == 1 and "file_id" in res[1][0], "错误信息提示需指定 file_id")
_assert(len(CALLS) == 0, "未指定 file_id 时不发起任何请求(无 create 兜底)")

print("== 测试3: 未配置 Token -> 直接报错不联网 ==")
tdocs.MCP_TOKEN = ""
CALLS.clear()
res = tdocs.push(RECORDS, file_id="FILE123")
_assert(res[0] == 0, "未配置 token 返回 0 条")
_assert(len(res[1]) == 1 and "Token" in res[1][0], "错误信息提示配置 Token")
_assert(len(CALLS) == 0, "未配置 token 时不发起任何请求")

print("\nALL_MCP_MOCK_OK")
