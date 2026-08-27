# -*- coding: utf-8 -*-
"""服务端配置持久化：凭证改为界面可配置并持久化到 config.json（已 gitignore，不进仓库）。"""
import json
import os
import time
import requests
from wecom_crypto import WXBizMsgCrypt

CONFIG_FILE = "config.json"
_token_cache = {"token": None, "expire": 0}

DEFAULT = {
    "zhipu_api_key": "",
    "zhipu_model": "deepseek-chat",   # 文本模型（默认 DeepSeek，响应更快）；可填 glm-4-flash 切回智谱
    "zhipu_vision_model": "glm-4v-plus",
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "image_mode": "ocr_first",       # 图片解析方式：ocr_first(本地OCR+文本模型,低成本) / ocr_only(仅OCR,无OCR则报错) / vision_only(仅视觉模型)
    "smartsheet_webhooks": [],            # 多个智能表 WEBHOOK，录入后全部同步
    "smartsheet_field_ids": {},           # 字段 key -> 智能表字段ID（界面可配，覆盖内置映射）
    "wecom": {
        "corpid": "",
        "corpsecret": "",
        "token": "",
        "aes_key": "",
        "agentid": "",
        "smartsheet_docid": "",     # 企业微信智能表 docid（用于「函数轮询提取」，免回调域名）
        "smartsheet_sheetid": "",   # 企业微信智能表 sheetid
    },
    "tencent_docs": {
        "client_id": "",
        "client_secret": "",
        "file_id": "",          # 目标表格 fileId（粘贴文档链接即可，保存时解析）
        "sheet_id": "",         # 目标工作表 sheetId（链接 ?tab= 之后，可空由链接识别）
        "access_token": "",     # 个人直连令牌（存于 config.json，与配置同生命周期）
        "open_id": "",
        "refresh_token": "",
        "expires_at": 0,
    },
}


def load():
    """读取配置并合并默认值，保证字段齐全。
    UI 配置（config.json）优先；缺省时回退到环境变量（便于云平台部署，重部署不丢）。"""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        data = {}
    cfg = dict(DEFAULT)
    for k in DEFAULT:
        if isinstance(DEFAULT[k], dict):
            d = dict(DEFAULT[k])
            d.update((data.get(k) or {}))
            cfg[k] = d
        else:
            cfg[k] = data.get(k, DEFAULT[k])

    # 环境变量回退（仅当界面未填写时）
    if not cfg["zhipu_api_key"]:
        cfg["zhipu_api_key"] = os.environ.get("ZHIPU_API_KEY", "")
    if not cfg["deepseek_api_key"]:
        cfg["deepseek_api_key"] = os.environ.get("DEEPSEEK_API_KEY", "")
    if not cfg["zhipu_model"]:
        cfg["zhipu_model"] = os.environ.get("ZHIPU_MODEL", "") or "deepseek-chat"
    if not cfg["deepseek_model"]:
        cfg["deepseek_model"] = os.environ.get("DEEPSEEK_MODEL", "") or "deepseek-chat"
    w = cfg["wecom"]
    env_map = {
        "corpid": "WECHAT_CORPID",
        "corpsecret": "WECHAT_CORPSECRET",
        "token": "WECHAT_TOKEN",
        "aes_key": "WECHAT_AES_KEY",
        "agentid": "WECHAT_AGENTID",
        "smartsheet_docid": "WECHAT_SMARTSHEET_DOCID",
        "smartsheet_sheetid": "WECHAT_SMARTSHEET_SHEETID",
    }
    for k, envk in env_map.items():
        if not w[k]:
            w[k] = os.environ.get(envk, "")
    if not cfg["tencent_docs"]["client_id"]:
        cfg["tencent_docs"]["client_id"] = os.environ.get("TENCENT_DOCS_CLIENT_ID", "")
    if not cfg["tencent_docs"]["client_secret"]:
        cfg["tencent_docs"]["client_secret"] = os.environ.get("TENCENT_DOCS_CLIENT_SECRET", "")
    if not cfg["tencent_docs"]["file_id"]:
        cfg["tencent_docs"]["file_id"] = os.environ.get("TENCENT_DOCS_FILE_ID", "")
    if not cfg["tencent_docs"]["sheet_id"]:
        cfg["tencent_docs"]["sheet_id"] = os.environ.get("TENCENT_DOCS_SHEET_ID", "")
    return cfg


def save(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------- 腾讯文档 OAuth token ----------------
def get_tdocs_token():
    """读取腾讯文档令牌；未授权返回 None。

    存于 config.json 的 tencent_docs 段，与界面配置同生命周期，
    避免云函数/云托管容器临时磁盘重启后 token 文件丢失导致「已配置却未授权」。"""
    cfg = load()
    t = cfg["tencent_docs"]
    if not t.get("access_token"):
        return None
    return {
        "access_token": t["access_token"],
        "open_id": t.get("open_id", ""),
        "refresh_token": t.get("refresh_token", ""),
        "expires_at": t.get("expires_at", 0),
    }


def set_tdocs_token(tok):
    """持久化腾讯文档令牌到 config.json。
    tok 可为完整令牌对象；调用方需保证 access_token 为非空真实值（空则不更新，保留现有）。"""
    cfg = load()
    if tok and tok.get("access_token"):
        cfg["tencent_docs"]["access_token"] = tok.get("access_token", "")
        cfg["tencent_docs"]["open_id"] = tok.get("open_id", "")
        cfg["tencent_docs"]["refresh_token"] = tok.get("refresh_token", "")
        cfg["tencent_docs"]["expires_at"] = tok.get("expires_at", 0)
        save(cfg)


def mask(cfg=None):
    """返回给前端的「状态 + 脱敏值」，绝不回传完整密钥用于可编辑回填。

    规则：
    - 敏感密钥（智谱 Key / 企微 corpid·secret·token·aes / 腾讯文档 access_token）只回传脱敏串，
      且前端**不回填**到可编辑框（留空 + placeholder 提示「已配置」），保存时空值表示「保留原值」，
      避免脱敏串被当作真值覆盖写回。
    - 非敏感标识（client_id / open_id / file_id / sheet_id / agentid / model）回传原值，可直接回填编辑。
    """
    cfg = cfg or load()
    w = cfg["wecom"]

    def _m(v):
        v = v or ""
        if not v:
            return ""
        if len(v) <= 4:
            return "****"
        return v[:2] + "****" + v[-2:]

    tdoc = cfg.get("tencent_docs", {})
    return {
        "zhipu": bool(cfg.get("zhipu_api_key")),
        "deepseek": bool(cfg.get("deepseek_api_key")),
        "wecom": bool(w.get("corpid") and w.get("corpsecret") and w.get("token") and w.get("aes_key")),
        "smartsheet": len(cfg.get("smartsheet_webhooks") or []) > 0,
        "smartsheet_count": len(cfg.get("smartsheet_webhooks") or []),
        "tdocs": bool(tdoc.get("access_token")),
        "tdocs_configured": bool(tdoc.get("client_id")),
        "tdocs_configured_target": bool(tdoc.get("file_id") and tdoc.get("sheet_id")),
        "values": {
            "zhipu_api_key": _m(cfg.get("zhipu_api_key")),
            "zhipu_model": cfg.get("zhipu_model", ""),
            "zhipu_vision_model": cfg.get("zhipu_vision_model", ""),
            "deepseek_api_key": _m(cfg.get("deepseek_api_key")),
            "deepseek_model": cfg.get("deepseek_model", ""),
            "image_mode": cfg.get("image_mode", ""),
            "smartsheet_webhooks": cfg.get("smartsheet_webhooks", []),
            "smartsheet_field_ids": cfg.get("smartsheet_field_ids", {}),
            "wecom_corpid": _m(w.get("corpid")),
            "wecom_corpsecret": _m(w.get("corpsecret")),
            "wecom_token": _m(w.get("token")),
            "wecom_aes_key": _m(w.get("aes_key")),
            "wecom_agentid": w.get("agentid", ""),
            "tdocs_client_id": tdoc.get("client_id", ""),          # 非敏感，原值回填
            "tdocs_client_secret": _m(tdoc.get("client_secret")),
            "tdocs_access_token": _m(tdoc.get("access_token")),  # 仅脱敏展示，前端不回填
            "tdocs_open_id": tdoc.get("open_id", ""),              # 非敏感，原值回填
            "tdocs_file_id": tdoc.get("file_id", ""),
            "tdocs_sheet_id": tdoc.get("sheet_id", ""),
            "tdocs_file_id_raw": tdoc.get("file_id", ""),
            "tdocs_sheet_id_raw": tdoc.get("sheet_id", ""),
            "wecom_smartsheet_docid": w.get("smartsheet_docid", ""),
            "wecom_smartsheet_sheetid": w.get("smartsheet_sheetid", ""),
        },
    }


def get_wxcrypt(cfg=None):
    """根据当前配置构造回调加解密实例；未配置则返回 None。"""
    cfg = cfg or load()
    w = cfg["wecom"]
    if w["token"] and w["aes_key"] and w["corpid"]:
        return WXBizMsgCrypt(w["token"], w["aes_key"], w["corpid"])
    return None


def get_access_token(cfg=None):
    """获取企业微信 access_token（带内存缓存）。"""
    cfg = cfg or load()
    w = cfg["wecom"]
    if not (w["corpid"] and w["corpsecret"]):
        return None
    now = time.time()
    if _token_cache["token"] and _token_cache["expire"] > now + 60:
        return _token_cache["token"]
    r = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
        params={"corpid": w["corpid"], "corpsecret": w["corpsecret"]},
        timeout=10,
    )
    d = r.json()
    if d.get("errcode", 0) != 0:
        raise RuntimeError("获取 access_token 失败：%s" % d.get("errmsg"))
    _token_cache["token"] = d["access_token"]
    _token_cache["expire"] = now + d.get("expires_in", 7200)
    return _token_cache["token"]
