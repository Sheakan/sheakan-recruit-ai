# -*- coding: utf-8 -*-
"""腾讯文档 MCP 适配器的本地协议层测试（无需联网/无需真实 Token）。

用假的 requests.post 模拟腾讯文档 MCP 端点，验证：
  1) initialize 握手 -> notifications/initialized 通知；
  2) tools/list 拿到工具清单；
  3) 配置 file_id 时走 batch_update_sheet_range（参数动态映射正确）；
  4) 无 file_id 时兜底走 create_excel_by_markdown；
  5) Authorization 头、SSE 解析、Mcp-Session-Id 透传均正确。
"""
import sys, os, json

# 让 import tencent_docs_store 能拿到本目录
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tencent_docs_store as tdocs

# ---- _mock requests.post ----
CALLS = []          # 记录每次请求: {"url","headers","body"}
SESSION_ID = "sid-abc-123"

def _build_sse(payload):
    """把 JSON-RPC 响应包成 SSE 文本。"""
    return "event: message\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

TOOLS = [
    {
        "name": "batch_update_sheet_range",
        "inputSchema": {
            "properties": {
                "file_id": {"type": "string"},
                "range": {"type": "string"},
                "values": {"type": "array"},
            }
        },
    },
    {
        "name": "create_excel_by_markdown",
        "inputSchema": {
            "properties": {
                "markdown": {"type": "string"},
                "title": {"type": "string"},
            }
        },
    },
]

class _Resp:
    def __init__(self, text, ctype="text/event-stream", session_id=None):
        self.text = text
        self.headers = {"Content-Type": ctype}
        if session_id:
            self.headers["Mcp-Session-Id"] = session_id
    def json(self):
        return json.loads(self.text)

def fake_post(url, headers=None, json=None, timeout=30, **kw):
    body = json
    CALLS.append({"url": url, "headers": headers, "body": body})
    method = body.get("method")
    if method == "initialize":
        return _Resp(_build_sse({
            "jsonrpc": "2.0", "id": body.get("id"),
            "result": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }), session_id=SESSION_ID)
    if method == "notifications/initialized":
        # 通知无响应
        return _Resp("", "application/json", session_id=SESSION_ID)
    if method == "tools/list":
        return _Resp(_build_sse({
            "jsonrpc": "2.0", "id": body.get("id"),
            "result": {"tools": TOOLS},
        }), session_id=SESSION_ID)
    if method == "tools/call":
        name = body["params"]["name"]
        if name == "batch_update_sheet_range":
            return _Resp(_build_sse({
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }), session_id=SESSION_ID)
        if name == "create_excel_by_markdown":
            return _Resp(_build_sse({
                "jsonrpc": "2.0", "id": body.get("id"),
                "result": {"content": [{"type": "text", "text": "created"}]},
            }), session_id=SESSION_ID)
    return _Resp(_build_sse({
        "jsonrpc": "2.0", "id": body.get("id"),
        "error": {"code": -32601, "message": "method not found"},
    }), session_id=SESSION_ID)

# 注入假 requests
tdocs.requests.post = fake_post

RECORDS = [
    {"name": "张伟", "gender": "男", "age": "29", "position": "后端实习", "stage": "offer", "status": "已发"},
]

def _assert(cond, msg):
    if not cond:
        raise AssertionError(msg)
    print("  OK -", msg)

print("== 测试1: 配置 file_id -> batch_update_sheet_range ==")
CALLS.clear()
tdocs.MCP_TOKEN = "test-token"
res = tdocs.push(RECORDS, file_id="FILE123", sheet_id="SHEET1")
print("  push 返回:", res)
_assert(res == (1, []), "push 返回 (1, [])")

# initialize
init = CALLS[0]
_assert(init["body"]["method"] == "initialize", "首个请求是 initialize")
_assert(init["headers"].get("Authorization") == "test-token", "Authorization 头带 token")
_assert(init["headers"].get("Mcp-Session-Id") is None, "initialize 无 session 头")

# notifications/initialized
notif = CALLS[1]
_assert(notif["body"]["method"] == "notifications/initialized", "第二个请求是 notifications/initialized")
_assert(notif["headers"].get("Mcp-Session-Id") == SESSION_ID, "通知携带 session-id")

# tools/list
lst = CALLS[2]
_assert(lst["body"]["method"] == "tools/list", "第三个请求是 tools/list")

# tools/call
call = CALLS[3]
_assert(call["body"]["method"] == "tools/call", "第四个请求是 tools/call")
params = call["body"].get("params")
_assert(params is not None, "tools/call 带 params")
args = params["arguments"]
_assert(params["name"] == "batch_update_sheet_range", "调用 batch_update_sheet_range")
_assert(args["file_id"] == "FILE123", "file_id 映射正确")
_assert(args["range"] == "A1", "range 映射为 A1")
_assert(isinstance(args["values"], list) and len(args["values"]) == 2, "values 是 2 行（表头+数据）")
_assert(args["values"][0][0] == "候选人", "表头首项为中文列名")
_assert(SESSION_ID and call["headers"].get("Mcp-Session-Id") == SESSION_ID, "调用透传 Mcp-Session-Id")

print("== 测试2: 无 file_id -> 兜底 create_excel_by_markdown ==")
CALLS.clear()
res = tdocs.push(RECORDS, file_id=None)
print("  push 返回:", res)
_assert(res == (1, []), "无 file_id 时兜底成功 (1, [])")
call = CALLS[3]
params = call["body"].get("params")
_assert(params["name"] == "create_excel_by_markdown", "兜底调用 create_excel_by_markdown")
_assert("markdown" in params["arguments"], "markdown 参数存在")
_assert(params["arguments"]["markdown"].startswith("| 候选人"), "markdown 以表头行开头")

print("== 测试3: 未配置 Token -> 直接报错不联网 ==")
tdocs.MCP_TOKEN = ""
CALLS.clear()
res = tdocs.push(RECORDS, file_id="FILE123")
_assert(res[0] == 0, "未配置 token 返回 0 条")
_assert(len(res[1]) == 1 and "Token" in res[1][0], "错误信息提示配置 Token")
_assert(len(CALLS) == 0, "未配置 token 时不发起任何请求")

print("\nALL_MCP_MOCK_OK")
