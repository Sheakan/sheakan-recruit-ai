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
    "zhipu_model": "glm-4-flash",     # 文本模型（默认 GLM-4-Flash，免费额度、响应快）；也可填 deepseek-chat 用 DeepSeek
    "zhipu_vision_model": "glm-4v-plus",
    "deepseek_api_key": "",
    "deepseek_model": "deepseek-chat",
    "image_mode": "vision",          # 图片解析方式：vision(云端视觉模型读图,免本地安装,需智谱Key) / ocr_local(仅本地tesseract OCR,需安装)
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
        "mcp_token": "",        # 腾讯文档官方 MCP 个人 Token（敏感，仅运行时填写，不入库明文/不前端回填/GitHub 不含）
        "file_id": "SaJQsDjBoxOA",   # demo 默认目标表（公开分享链接 slug 等效，仅文档标识，非敏感）
        "sheet_id": "BB08J2",        # demo 默认工作表（链接 ?tab= 之后）
    },
    "fields": None,             # 字段自定义配置（列表，见 fields.py）；None 表示沿用内置默认
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
        cfg["zhipu_model"] = os.environ.get("ZHIPU_MODEL", "") or "glm-4-flash"
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
    if not cfg["tencent_docs"]["mcp_token"]:
        cfg["tencent_docs"]["mcp_token"] = os.environ.get("TENCENT_DOCS_MCP_TOKEN", "")
    if not cfg["tencent_docs"]["file_id"]:
        cfg["tencent_docs"]["file_id"] = os.environ.get("TENCENT_DOCS_FILE_ID", "SaJQsDjBoxOA")
    if not cfg["tencent_docs"]["sheet_id"]:
        cfg["tencent_docs"]["sheet_id"] = os.environ.get("TENCENT_DOCS_SHEET_ID", "BB08J2")
    return cfg


def save(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


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
        "tdocs": bool(tdoc.get("mcp_token")),
        "tdocs_configured": bool(tdoc.get("mcp_token")),
        "tdocs_configured_target": bool(tdoc.get("file_id")),
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
            "tdocs_mcp_token": _m(tdoc.get("mcp_token")),          # 仅脱敏展示，前端不回填
            "tdocs_file_id": tdoc.get("file_id", ""),
            "tdocs_sheet_id": tdoc.get("sheet_id", ""),
            "tdocs_file_id_raw": tdoc.get("file_id", ""),
            "tdocs_sheet_id_raw": tdoc.get("sheet_id", ""),
            "wecom_smartsheet_docid": w.get("smartsheet_docid", ""),
            "wecom_smartsheet_sheetid": w.get("smartsheet_sheetid", ""),
            "fields": cfg.get("fields"),   # 字段自定义（None 表示沿用内置默认）
        },
    }


def get_tdocs_token():
    """读取腾讯文档令牌（兼容旧调用）；现统一用 mcp_token，故返回 None 表示未配置。"""
    return None


def set_tdocs_token(tok):
    """兼容旧调用：MCP 模式下令牌由 mcp_token 字段承载，此处不再单独持久化。"""
    return None


def get_tdocs_token():
    """读取腾讯文档令牌（兼容旧调用）；现统一用 mcp_token，故返回 None 表示未配置。"""
    return None


def set_tdocs_token(tok):
    """兼容旧调用：MCP 模式下令牌由 mcp_token 字段承载，此处不再单独持久化。"""
    return None


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
