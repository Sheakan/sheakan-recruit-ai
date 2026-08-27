# -*- coding: utf-8 -*-
"""腾讯文档在线表格写入适配器：OAuth2 授权后调用 batchUpdate 批量写入（全量覆盖，天然幂等）。"""
import os
import time
import urllib.parse
import requests
import config_store
from fields import HEADERS_CN, KEYS

CLIENT_ID = os.environ.get("TENCENT_DOCS_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("TENCENT_DOCS_CLIENT_SECRET", "")
FILE_ID = os.environ.get("TENCENT_DOCS_FILE_ID", "")
SHEET_ID = os.environ.get("TENCENT_DOCS_SHEET_ID", "")
REDIRECT_URI = os.environ.get("TENCENT_DOCS_REDIRECT_URI", "")

AUTH_URL = "https://docs.qq.com/oauth/v2/authorize"
TOKEN_URL = "https://docs.qq.com/oauth/v2/token"
API_BASE = "https://docs.qq.com/openapi/spreadsheet/v3/files"


# ---------------- Token 管理 ----------------
def _load_token():
    return config_store.get_tdocs_token()


def _save_token(tok):
    config_store.set_tdocs_token(tok)


def enabled():
    """是否已具备有效令牌（个人直连令牌 或 OAuth 授权均可）。

    个人令牌模式：用户直接在界面填入 access_token + open_id，无需 client_secret、无需 OAuth 回调。
    OAuth 模式：通过 /tdocs/auth 换取 token 后写入同一份存储。
    """
    tok = _load_token()
    return bool(tok and tok.get("access_token") and CLIENT_ID)


def authorize_url(redirect_uri, state="recruitmind"):
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "all",
        "state": state,
    })


def exchange_code(code, redirect_uri):
    """用授权码换取 token，持久化到 config.json（与界面配置同生命周期）。"""
    r = requests.get(TOKEN_URL, params={
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code": code,
    }, timeout=15)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError(d.get("error_description") or d.get("msg") or str(d))
    tok = {
        "access_token": d["access_token"],
        "open_id": d.get("user_id") or d.get("open_id", ""),
        "refresh_token": d.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(d.get("expires_in", 2592000)),
    }
    _save_token(tok)
    return tok


def refresh_token(tok):
    """用 refresh_token 换新 token；失败返回 None。"""
    if not (CLIENT_ID and CLIENT_SECRET and tok.get("refresh_token")):
        return None
    try:
        r = requests.get(TOKEN_URL, params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        }, timeout=15)
        d = r.json()
        if "access_token" not in d:
            return None
        new = {
            "access_token": d["access_token"],
            "open_id": d.get("user_id") or d.get("open_id") or tok.get("open_id", ""),
            "refresh_token": d.get("refresh_token") or tok.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(d.get("expires_in", 2592000)),
        }
        _save_token(new)
        return new
    except Exception:
        return None


def get_token():
    """返回有效 token，临近过期则自动刷新；无则返回 None。"""
    tok = _load_token()
    if not tok:
        return None
    if tok.get("expires_at", 0) - 120 < time.time():
        tok = refresh_token(tok)
    return tok


# ---------------- 写入 ----------------
def _headers(tok):
    return {
        "Access-Token": tok["access_token"],
        "Client-Id": CLIENT_ID,
        "Open-Id": tok["open_id"],
        "Content-Type": "application/json",
    }


def _cell(v):
    return {"cellValue": {"text": "" if v is None else str(v)}}


def _build_operations(records, sheet_id):
    """构造 batchUpdate 的 requests：表头(1 操作) + 数据(每 1000 行 1 操作)。"""
    header_op = {
        "updateRangeRequest": {
            "sheetId": sheet_id,
            "gridData": {
                "startRow": 0,
                "startColumn": 0,
                "rows": [{"values": [_cell(c) for c in HEADERS_CN]}],
            },
        }
    }
    data_ops = []
    chunk_size = 1000
    for i in range(0, len(records), chunk_size):
        chunk = records[i:i + chunk_size]
        rows = [{"values": [_cell(r.get(k)) for k in KEYS]} for r in chunk]
        data_ops.append({
            "updateRangeRequest": {
                "sheetId": sheet_id,
                "gridData": {
                    "startRow": 1 + i,
                    "startColumn": 0,
                    "rows": rows,
                },
            }
        })
    return [header_op] + data_ops


def push(records, file_id=None, sheet_id=None):
    """
    把记录全量写入腾讯文档在线表格（表头 + 数据，从首行覆盖写入）。
    返回 (记录条数, 错误信息列表)。失败不影响主流程（仅记录）。
    """
    file_id = file_id or FILE_ID
    sheet_id = sheet_id or SHEET_ID
    if not CLIENT_ID:
        return 0, ["未配置腾讯文档 Client ID：请在「配置我的凭证」中填写"]
    if not file_id:
        return 0, ["未指定腾讯文档 fileId（环境变量或请求参数）"]
    if not sheet_id:
        return 0, ["未指定 sheetId（表格 URL 中 ?tab= 后的字符串）"]

    tok = get_token()
    if not tok:
        return 0, ["未授权：请先访问 /tdocs/auth 完成腾讯文档授权"]

    operations = _build_operations(records, sheet_id)
    errs = []
    pushed = len(records)
    # 每批最多 5 个操作
    for i in range(0, len(operations), 5):
        batch = operations[i:i + 5]
        try:
            resp = requests.post(
                f"{API_BASE}/{file_id}/batchUpdate",
                headers=_headers(tok),
                json={"requests": batch},
                timeout=20,
            )
            data = resp.json()
            code = data.get("code")
            if code is not None and code != 0:
                errs.append(f"code={code} msg={data.get('message')}")
        except Exception as e:
            errs.append(str(e))
    return pushed, errs
