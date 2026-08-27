# -*- coding: utf-8 -*-
"""招聘数据自动化服务：看板(GET /)、录入(/api/parse)、企业微信回调(/wecom)、模拟(/simulate)。"""
import os
import re
import json
import time
import csv
import io
import xml.etree.ElementTree as ET
import threading
import requests
from flask import Flask, request, jsonify, send_from_directory, Response, redirect

import ai_parse
import pdf_extract
import smartsheet_store as smartsheet
import tencent_docs_store as tdocs
import config_store
import wecom_smartsheet
import anomaly
import validate
from wecom_crypto import WXBizMsgCrypt

app = Flask(__name__)

STORE = "records.json"
TENCENT_DOCS_REDIRECT_URI = os.environ.get("TENCENT_DOCS_REDIRECT_URI", "")  # 腾讯文档 OAuth 回调地址（留空则自动推断）

# 全局配置（由界面 POST /api/config 热更新；启动时从 config.json 载入）
CFG = config_store.load()
# 同步腾讯文档模块级凭证（兼容其 env 读取方式）
tdocs.CLIENT_ID = os.environ.get("TENCENT_DOCS_CLIENT_ID") or CFG["tencent_docs"]["client_id"]
tdocs.CLIENT_SECRET = os.environ.get("TENCENT_DOCS_CLIENT_SECRET") or CFG["tencent_docs"]["client_secret"]


# ---------------- 存储 ----------------
def load_store():
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_store(records):
    with open(STORE, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


# 去重依据：同一组关键字段视为同一条记录，重复导入时跳过
DEDUP_FIELDS = ["candidate", "position", "stage", "status", "interviewer", "recruiter", "time"]


def _norm(v):
    return (v or "").strip().lower()


def _signature(r):
    """完全重复检测的 7 字段签名。
    防止一个 AI 抽取失败产出的全空记录被当作「完全重复」全部 skipped，
    这里对所有字段都空的记录，额外挂一个随机前缀，保证它们不会被误跳过。"""
    parts = [_norm(r.get(f, "")) for f in DEDUP_FIELDS]
    if not any(parts):
        # 7 字段全空：大概率是模型没抽到真实信息；
        # 加一个小随机串让它能被写入（随后会被 anomaly 规则标红提示）
        parts.append("__empty_" + os.urandom(4).hex())
    return tuple(parts)


def _merge_key(r):
    """身份键：有手机号则按手机号；否则按 候选人+岗位。用于把同一人的多次进展合并到一条。
    **关键保护**：候选人或岗位至少有一个非空才能建立 merge key；
    如果两个全空就返回 None——避免把「姓名空+岗位空」的多条失败记录全合并成一条。"""
    phone = _norm(r.get("phone"))
    if phone:
        return ("phone", phone)
    cand = _norm(r.get("candidate"))
    pos = _norm(r.get("position"))
    if cand or pos:
        return ("cp", cand + "|" + pos)
    return None


def _merge_into(old, new):
    """把 new 的非空字段覆盖进 old，并刷新 ts；保留 old 的 id。"""
    for k, v in new.items():
        if v not in (None, "", []):
            old[k] = v
    old["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")


def add_records(records, source):
    """统一入库：补字段 + 去重/合并 + 写本地文件 + 同步到智能表（若已配置）。

    两条规则：
    1) 完全相同的记录（7 字段签名一致）→ 跳过（skipped）。
    2) 同一人（手机号，或 候选人+岗位）但进展不同 → 原地合并更新（merged），不再新增一行，
       避免「同一人从投递到 offer 被算成多条」导致漏斗虚高。
    返回 (added, skipped, merged)。
    """
    if not records:
        return [], 0, 0
    store = load_store()
    sigs = {_signature(r) for r in store}
    keys = {}
    for r in store:
        mk = _merge_key(r)
        if mk and mk not in keys:
            keys[mk] = r
    added, skipped, merged = [], 0, 0
    for r in records:
        r["id"] = f"{int(time.time()*1000)}-{len(store)+1}"
        r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        r["source"] = source
        # 相对时间归一化 + 字段校验（写入 r["issues"]），保证入库数据规范、看板准确
        validate.clean_record(r)
        sig = _signature(r)
        if sig in sigs:
            skipped += 1
            continue
        mk = _merge_key(r)
        if mk and mk in keys:
            _merge_into(keys[mk], r)
            merged += 1
            continue
        sigs.add(sig)
        if mk:
            keys[mk] = r
        store.append(r)
        added.append(r)
    if added or merged:
        save_store(store)
        # 真实落点：自动写入 HR 在用的智能表（可配置多个，全部同步），替代手工录入
        webhooks = CFG.get("smartsheet_webhooks") or []
        if webhooks and added:
            smartsheet.push(added, webhooks)
        # 腾讯文档（全量覆盖模型）：把完整 store 写入在线表格
        if tdocs.enabled():
            tdocs.push(store)
    return added, skipped, merged


def build_reply(added, skipped=0, merged=0):
    if not added and not merged:
        return "未识别到新的招聘信息（内容已存在，已自动去重）"
    parts = []
    if added:
        parts.append("已自动录入 %d 条：" % len(added))
        for r in added:
            parts.append(f"· {r.get('candidate','?')} / {r.get('position','?')} / {r.get('stage','?')} / {r.get('status','')}".rstrip(" /"))
    if merged:
        parts.append("（已合并更新 %d 条已有候选人进展）" % merged)
    if skipped:
        parts.append("（已自动跳过 %d 条完全重复记录）" % skipped)
    return "\n".join(parts)


def push_to_group(text):
    """可选：把确认消息回发到企业微信群机器人（需在环境变量 GROUP_ROBOT_WEBHOOK 配置）。"""
    wh = os.environ.get("GROUP_ROBOT_WEBHOOK", "")
    if not wh:
        return
    try:
        requests.post(wh, json={"msgtype": "text", "text": {"content": text}}, timeout=5)
    except Exception:
        pass


# ---------------- 路由 ----------------
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/api/records")
def api_records():
    return jsonify(load_store())


@app.route("/api/clear", methods=["POST"])
def api_clear():
    save_store([])
    return jsonify({"ok": True})


@app.route("/api/config")
def api_config():
    m = config_store.mask(CFG)
    try:
        m["values"]["effective_field_map"] = {
            k: fid for k, (fid, _) in smartsheet.get_effective_map().items()
        }
    except Exception:
        m["values"]["effective_field_map"] = {}
    return jsonify(m)


@app.route("/api/config", methods=["POST"])
def api_config_save():
    """界面保存配置：写入 config.json 并热加载（无需重启）。

    安全约定（防止「脱敏串被当作真值覆盖回写」）：
    - 对任何凭证字段，**仅当传入非空字符串时才更新**；前端对敏感字段留空表示「保留原值」，不发送。
    - 腾讯文档 access_token 同理：空值不更新令牌，仅当用户输入了新令牌时才覆盖。
    """
    data = request.get_json(silent=True) or {}
    cfg = config_store.load()

    def _set(key, val):
        val = (val or "").strip()
        if val:                      # 非空才写入，空值视为「保留现有」
            cfg[key] = val

    if "zhipu_api_key" in data:
        _set("zhipu_api_key", data["zhipu_api_key"])
    if "zhipu_model" in data:
        _set("zhipu_model", data["zhipu_model"] or "deepseek-chat")
    if "zhipu_vision_model" in data:
        _set("zhipu_vision_model", data["zhipu_vision_model"] or "glm-4v-plus")
    if "deepseek_api_key" in data:
        _set("deepseek_api_key", data["deepseek_api_key"])
    if "deepseek_model" in data:
        _set("deepseek_model", data["deepseek_model"] or "deepseek-chat")
    if "image_mode" in data and data["image_mode"] in ("ocr_first", "ocr_only", "vision_only"):
        cfg["image_mode"] = data["image_mode"]
    if "smartsheet_field_ids" in data and isinstance(data["smartsheet_field_ids"], dict):
        cfg["smartsheet_field_ids"] = {k: (v or "").strip() for k, v in data["smartsheet_field_ids"].items() if (v or "").strip()}
    if "smartsheet_webhooks" in data and isinstance(data["smartsheet_webhooks"], list):
        cfg["smartsheet_webhooks"] = [w.strip() for w in data["smartsheet_webhooks"] if (w or "").strip()]
    if isinstance(data.get("wecom"), dict):
        for k in ("corpid", "corpsecret", "token", "aes_key", "agentid", "smartsheet_docid", "smartsheet_sheetid"):
            if k in data["wecom"]:
                _set_wecom(cfg, k, data["wecom"][k])
    if isinstance(data.get("tencent_docs"), dict):
        td = data["tencent_docs"]
        for k in ("client_id", "client_secret", "file_id", "sheet_id"):
            if k in td:
                _set_tdoc(cfg, k, td[k])
        # 个人直连令牌模式：用户直接填入 access_token + open_id，无需 OAuth 回调
        if "access_token" in td and (td["access_token"] or "").strip():
            tok = {
                "access_token": td["access_token"].strip(),
                "open_id": (td.get("open_id") or "").strip(),
                "refresh_token": "",
                "expires_at": int(time.time()) + 365 * 86400,  # 个人令牌长期有效，置远过期
            }
            config_store.set_tdocs_token(tok)
        elif "open_id" in td:
            # 仅更新 open_id，保留现有令牌（用户没重新填 token 时）
            cur = config_store.get_tdocs_token() or {"access_token": "", "open_id": "", "refresh_token": "", "expires_at": 0}
            cur["open_id"] = (td.get("open_id") or "").strip()
            config_store.set_tdocs_token(cur)

    config_store.save(cfg)
    global CFG
    CFG = cfg
    tdocs.CLIENT_ID = cfg["tencent_docs"]["client_id"]
    tdocs.CLIENT_SECRET = cfg["tencent_docs"]["client_secret"]
    tdocs.FILE_ID = cfg["tencent_docs"]["file_id"]
    tdocs.SHEET_ID = cfg["tencent_docs"]["sheet_id"]
    return jsonify(config_store.mask(CFG))


def _set_wecom(cfg, k, val):
    v = (val or "").strip()
    if v:
        cfg["wecom"][k] = v


def _set_tdoc(cfg, k, val):
    v = (val or "").strip()
    if v:
        cfg["tencent_docs"][k] = v



@app.route("/api/sync", methods=["POST"])
def api_sync():
    """把本地已有记录全量同步到已配置的企业微信智能表（可多个）"""
    webhooks = CFG.get("smartsheet_webhooks") or []
    if not webhooks:
        return jsonify({"error": "未配置智能表 WEBHOOK（请在界面「配置」中填写）"}), 400
    ok, errs = smartsheet.push(load_store(), webhooks)
    return jsonify({"pushed": ok, "errors": errs})


# ---------------- 企业微信智能表「函数轮询提取」（免回调域名） ----------------
@app.route("/api/poll_smartsheet", methods=["GET", "POST"])
def api_poll_smartsheet():
    """云函数定时触发器调用的入口：主动从企业微信智能表拉取新增行 → AI 提取 → 入库同步。
    不依赖回调域名，解决了「企业微信实时收消息需 ICP 备案」的限制。"""
    try:
        recs, err = wecom_smartsheet.fetch_new_records()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if err:
        return jsonify({"error": err}), 400
    added, skipped, merged = add_records(recs, "企业微信智能表")
    return jsonify({"fetched": len(recs), "added": len(added), "skipped": skipped, "merged": merged})


# ---------------- 腾讯文档 OAuth 与同步 ----------------
def _tdocs_redirect_uri():
    """构造腾讯文档 OAuth 回调地址。

    关键坑：CloudBase/SCF 网关在前面做 TLS 终止，后端 Flask 实际收到的是
    http:// 请求，request.url_root 因此生成 http://... 的回调地址；但腾讯文档
    OAuth 强制要求 redirect_uri 必须是 https，用 http 会被直接拒绝（400）。
    所以这里把 http:// 统一升级为 https://。
    """
    if TENCENT_DOCS_REDIRECT_URI:
        return TENCENT_DOCS_REDIRECT_URI
    root = request.url_root
    if root.startswith("http://"):
        root = "https://" + root[len("http://"):]
    return root.rstrip("/") + "/tdocs/callback"


@app.route("/tdocs/auth")
def tdocs_auth():
    """跳转到腾讯文档授权页，引导用户完成 OAuth 授权。

    注意：部署在云函数/API 网关后，服务端直接 redirect() 外部绝对 URL 时，
    网关会对 Location 头二次编码（路径 / 变成 %2F），导致腾讯文档收到畸形 URL（400）。
    因此改为返回一段 HTML，由浏览器端 JS 执行跳转，绕过网关对 Location 的改写。
    """
    if not (tdocs.CLIENT_ID and tdocs.CLIENT_SECRET):
        return "未配置腾讯文档应用凭证：请在「配置我的凭证」中填写 Client ID / Client Secret 后重试", 501
    url = tdocs.authorize_url(_tdocs_redirect_uri())
    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>跳转中…</title></head>
<body style="font-family:sans-serif;padding:40px;color:#475569">正在跳转至腾讯文档授权页…（若未自动跳转，<a id="a" href="{url}">请点击这里</a>）
<script>window.location.href={json.dumps(url)};</script>
</body></html>"""


@app.route("/tdocs/callback")
def tdocs_callback():
    """授权回调：用 code 换取 token 并持久化，随后回到主页。"""
    code = request.args.get("code", "")
    if not code:
        return "授权失败：回调缺少 code 参数", 400
    try:
        tdocs.exchange_code(code, _tdocs_redirect_uri())
    except Exception as e:
        return f"授权失败：{e}", 500
    return redirect("/?tdocs=connected")


@app.route("/api/tdocs/status")
def api_tdocs_status():
    return jsonify({
        "configured": bool(tdocs.CLIENT_ID),
        "authorized": tdocs.enabled(),
        "fileId": bool(tdocs.FILE_ID),
    })


@app.route("/api/tdocs/sync", methods=["POST"])
def api_tdocs_sync():
    """把本地已有记录全量同步到腾讯文档在线表格。fileId/sheetId 可由请求体覆盖。"""
    if not tdocs.CLIENT_ID:
        return jsonify({"error": "未配置腾讯文档 Client ID：请在「配置我的凭证」中填写"}), 400
    if not tdocs.get_token():
        return jsonify({"error": "未配置有效令牌：请在「配置我的凭证」填入 Access Token + Open ID 后保存，再点「连接腾讯文档」"}), 400
    data = request.get_json(silent=True) or {}
    pushed, errs = tdocs.push(load_store(), data.get("fileId") or None, data.get("sheetId") or None)
    return jsonify({"pushed": pushed, "errors": errs})


# 可编辑字段（置信度等内部字段不允许通过接口修改）
EDITABLE = {"candidate","position","department","stage","status","interviewer",
            "recruiter","channel","education","experience","currentCompany",
            "expectedSalary","contact","time","remark","gender","age","phone","email","expectedCity"}


@app.route("/api/record/<rid>", methods=["PUT"])
def api_update(rid):
    data = request.get_json(silent=True) or {}
    store = load_store()
    for r in store:
        if r.get("id") == rid:
            for k, v in data.items():
                if k in EDITABLE:
                    r[k] = v
            r["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
            save_store(store)
            webhooks = CFG.get("smartsheet_webhooks") or []
            if webhooks:
                smartsheet.push([r], webhooks)
            if tdocs.enabled():
                tdocs.push(store)
            return jsonify({"ok": True, "record": r})
    return jsonify({"error": "记录不存在"}), 404


@app.route("/api/record/<rid>", methods=["DELETE"])
def api_delete(rid):
    store = load_store()
    new = [r for r in store if r.get("id") != rid]
    if len(new) == len(store):
        return jsonify({"error": "记录不存在"}), 404
    save_store(new)
    if tdocs.enabled():
        tdocs.push(new)
    return jsonify({"ok": True, "removed": len(store) - len(new)})


@app.route("/api/export")
def api_export():
    """导出全部记录为 CSV（带 BOM，Excel 直接可打开）"""
    store = load_store()
    cols = ["candidate","position","department","stage","status","interviewer",
            "recruiter","channel","education","experience","currentCompany",
            "expectedSalary","contact","time","remark","source",
            "gender","age","phone","email","expectedCity"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    for r in store:
        w.writerow([r.get(c, "") for c in cols])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=recruit_records.csv"})


@app.route("/api/parse_pdf", methods=["POST"])
def api_parse_pdf():
    """上传简历 PDF → 提取文本 → 智谱 GLM 抽取 16 字段 → 入库并同步智能表。"""
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段（请使用 multipart/form-data 上传 PDF）"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "未选择文件"}), 400
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "仅支持 PDF 文件"}), 400
    try:
        raw = f.read()
        text = pdf_extract.extract_text_from_pdf(raw)
    except Exception as e:
        return jsonify({"error": f"PDF 解析失败：{e}"}), 500
    if not text.strip():
        return jsonify({"error": "未能从 PDF 提取到文本（可能是扫描件/图片型 PDF，需 OCR）"}), 422
    try:
        recs = ai_parse.parse_text(text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added, skipped, merged = add_records(recs, "简历PDF")
    return jsonify({"added": len(added), "skipped": skipped, "merged": merged, "records": recs, "extractedLen": len(text)})


# 演示用样例数据：覆盖各阶段/渠道，含扩充的个人信息字段，便于快速填充看板做演示
DEMO_RECORDS = [
    {"candidate":"张伟","gender":"男","age":"26","phone":"13945128821","email":"zhangwei@163.com","expectedCity":"北京","position":"后端开发工程师","department":"技术部","stage":"投递","status":"进行中","interviewer":"","recruiter":"王敏","channel":"内推","education":"本科","experience":"3年","currentCompany":"美团","expectedSalary":"25k","contact":"13945128821","time":"2026-08-10 14:00","remark":"有微服务经验，内推人是我们前同事"},
    {"candidate":"李娜","gender":"女","age":"24","phone":"18721003567","email":"lina_li@qq.com","expectedCity":"上海","position":"产品经理","department":"产品部","stage":"简历初筛","status":"已通过","interviewer":"赵磊","recruiter":"王敏","channel":"校招","education":"硕士","experience":"0年","currentCompany":"应届生（复旦）","expectedSalary":"20k","contact":"18721003567","time":"2026-08-11 10:30","remark":"两段大厂实习，项目经历丰富"},
    {"candidate":"王强","gender":"男","age":"29","phone":"13501227890","email":"wangqiang@foxmail.com","expectedCity":"深圳","position":"算法工程师","department":"AI Lab","stage":"笔试","status":"进行中","interviewer":"","recruiter":"陈静","channel":"猎头","education":"博士","experience":"4年","currentCompany":"某研究院","expectedSalary":"40k","contact":"13501227890","time":"2026-08-12 19:00","remark":"NeurIPS 一篇一作，薪资期望偏高需谈"},
    {"candidate":"刘洋","gender":"男","age":"27","phone":"13866771234","email":"liuyang.dev@126.com","expectedCity":"杭州","position":"前端开发工程师","department":"技术部","stage":"一面","status":"已通过","interviewer":"孙浩","recruiter":"王敏","channel":"官网","education":"本科","experience":"3年","currentCompany":"网易","expectedSalary":"23k","contact":"13866771234","time":"2026-08-13 15:00","remark":"React 很熟，有开源贡献"},
    {"candidate":"陈静","gender":"女","age":"28","phone":"13688990011","email":"chenjing.design@gmail.com","expectedCity":"北京","position":"UI设计师","department":"设计部","stage":"二面","status":"待定","interviewer":"周婷","recruiter":"陈静","channel":"招聘平台","education":"本科","experience":"5年","currentCompany":"字节跳动","expectedSalary":"22k","contact":"13688990011","time":"2026-08-14 11:00","remark":"作品集优秀，但薪资要 22k 偏高"},
    {"candidate":"赵磊","gender":"男","age":"31","phone":"15012345678","email":"zhaolei@sina.com","expectedCity":"广州","position":"数据分析师","department":"数据部","stage":"三面","status":"已通过","interviewer":"吴鹏","recruiter":"陈静","channel":"内推","education":"硕士","experience":"6年","currentCompany":"招商银行","expectedSalary":"30k","contact":"15012345678","time":"2026-08-15 16:30","remark":"SQL 与建模都强，候选意愿高"},
    {"candidate":"孙悦","gender":"女","age":"25","phone":"18976543210","email":"sunyue@outlook.com","expectedCity":"成都","position":"测试工程师","department":"质量部","stage":"offer","status":"已发offer","interviewer":"郑凯","recruiter":"王敏","channel":"社招","education":"本科","experience":"2年","currentCompany":"完美世界","expectedSalary":"18k","contact":"18976543210","time":"2026-08-16 09:00","remark":"自动化测试经验足，本周内给答复"},
    {"candidate":"周强","gender":"男","age":"33","phone":"13911112222","email":"zhouqiang@tencent.com","expectedCity":"北京","position":"技术总监","department":"技术部","stage":"offer","status":"已接受","interviewer":"","recruiter":"陈静","channel":"猎头","education":"硕士","experience":"10年","currentCompany":"阿里巴巴","expectedSalary":"60k","contact":"13911112222","time":"2026-08-17 20:00","remark":"带过 30 人团队，下周入职"},
    {"candidate":"吴敏","gender":"女","age":"23","phone":"17722334455","email":"wumin@stu.edu.cn","expectedCity":"武汉","position":"运营专员","department":"运营部","stage":"入职","status":"已入职","interviewer":"","recruiter":"王敏","channel":"校招","education":"本科","experience":"0年","currentCompany":"应届生（武大）","expectedSalary":"12k","contact":"17722334455","time":"2026-08-18 09:30","remark":"执行力强，已办入职手续"},
    {"candidate":"郑凯","gender":"男","age":"30","phone":"18655443322","email":"zhengkai@qq.com","expectedCity":"南京","position":"后端开发工程师","department":"技术部","stage":"简历初筛","status":"已淘汰","interviewer":"孙浩","recruiter":"陈静","channel":"招聘平台","education":"本科","experience":"4年","currentCompany":"某外包公司","expectedSalary":"21k","contact":"18655443322","time":"2026-08-19 14:00","remark":"基础偏弱，与岗位匹配度不够"},
    {"candidate":"黄丽","gender":"女","age":"27","phone":"15987654321","email":"huangli@163.com","expectedCity":"深圳","position":"数据分析师","department":"数据部","stage":"一面","status":"已淘汰","interviewer":"吴鹏","recruiter":"王敏","channel":"官网","education":"硕士","experience":"3年","currentCompany":"OPPO","expectedSalary":"26k","contact":"15987654321","time":"2026-08-20 13:30","remark":"业务理解一般，暂不推进"},
    {"candidate":"徐峰","gender":"男","age":"32","phone":"13322331100","email":"xufeng@foxmail.com","expectedCity":"上海","position":"产品经理","department":"产品部","stage":"三面","status":"待定","interviewer":"赵磊","recruiter":"陈静","channel":"猎头","education":"硕士","experience":"8年","currentCompany":"拼多多","expectedSalary":"38k","contact":"13322331100","time":"2026-08-21 17:00","remark":"资历深，但薪资预期与公司预算有差距"},
]


@app.route("/api/demo", methods=["POST"])
def api_demo():
    """载入演示数据：便于快速填充看板做演示（来源标记为「示例数据」，可一键清空）。"""
    added, skipped, merged = add_records([dict(r) for r in DEMO_RECORDS], "示例数据")
    return jsonify({"added": len(added), "skipped": skipped, "merged": merged})


@app.route("/api/parse", methods=["POST"])
def api_parse():
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    try:
        recs = ai_parse.parse_text(text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added, skipped, merged = add_records(recs, "手动录入")
    return jsonify({"added": len(added), "skipped": skipped, "merged": merged, "records": recs})


@app.route("/api/extract", methods=["POST"])
def api_extract():
    """「函数提取消息」核心接口：接收一段消息/聊天文本，调用大模型抽取结构化招聘记录。
    与 /api/parse 的区别：默认只返回抽取结果、不入库（store=true 才写入并同步），
    便于被云函数定时触发器、企业微信智能表轮询、群机器人等任意上游调用。"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    try:
        recs = ai_parse.parse_text(text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if data.get("store"):
        added, skipped, merged = add_records(recs, "函数提取")
        return jsonify({"added": len(added), "skipped": skipped, "merged": merged, "records": recs})
    return jsonify({"count": len(recs), "records": recs})


@app.route("/api/parse_image", methods=["POST"])
def api_parse_image():
    """上传图片（招聘截图/简历照片/群聊长图）→ 视觉模型识别文字 → 智谱 GLM 抽取 21 字段 → 入库并同步。"""
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段（请使用 multipart/form-data 上传图片）"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "未选择文件"}), 400
    if not f.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
        return jsonify({"error": "仅支持图片文件（png/jpg/jpeg/gif/bmp/webp）"}), 400
    try:
        raw = f.read()
        recs = ai_parse.parse_image(raw, f.filename, mode=CFG.get("image_mode", "ocr_first"))
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added, skipped, merged = add_records(recs, "图片")
    return jsonify({"added": len(added), "skipped": skipped, "merged": merged, "records": recs})


# 字段名 -> 候选别名（中英文），用于识别结构化模板表头
FIELD_ALIASES = {
    "candidate": ["候选人", "姓名", "名字", "candidate", "name"],
    "position": ["岗位", "职位", "应聘岗位", "position", "job", "title"],
    "department": ["部门", "department", "team"],
    "stage": ["阶段", "招聘阶段", "stage", "phase"],
    "status": ["状态", "status"],
    "interviewer": ["面试官", "interviewer"],
    "recruiter": ["招聘负责人", "招聘", "hr", "recruiter", "负责人"],
    "channel": ["渠道", "channel", "source"],
    "education": ["学历", "教育", "education"],
    "experience": ["工作年限", "经验", "年限", "experience", "years"],
    "currentCompany": ["当前公司", "公司", "currentcompany", "company"],
    "expectedSalary": ["期望薪资", "薪资", "工资", "salary", "expectedsalary", "期望工资"],
    "contact": ["联系方式", "联系电话", "联系", "contact", "phone"],
    "time": ["时间", "日期", "time", "date"],
    "remark": ["备注", "说明", "remark", "note", "comment"],
    "gender": ["性别", "gender", "sex"],
    "age": ["年龄", "age"],
    "phone": ["手机号", "手机", "电话", "phone", "mobile", "tel"],
    "email": ["邮箱", "电子邮件", "email", "mail"],
    "expectedCity": ["期望城市", "城市", "工作城市", "expectedcity", "city"],
}


def _detect_columns(headers):
    """返回 {列索引: field_key}；命中已知字段名才映射，空 dict 表示非结构化（聊天记录）。"""
    lower_aliases = {k: [a.lower() for a in v] for k, v in FIELD_ALIASES.items()}
    mapping = {}
    for i, h in enumerate(headers):
        hl = (h or "").strip().lower()
        for key, aliases in lower_aliases.items():
            if hl in aliases:
                mapping[i] = key
                break
    return mapping


@app.route("/api/import_csv", methods=["POST"])
def api_import_csv():
    """导入招聘记录 CSV/Excel（接微信聊天记录导出 / 结构化招聘表）。
    - 表头命中招聘字段名 → 直接按列映射入库（不需 AI，可零配置演示）；
    - 否则（聊天记录/自由文本）→ 每行当消息文本调 AI 解析（需智谱 Key）。"""
    if "file" not in request.files:
        return jsonify({"error": "缺少 file 字段"}), 400
    f = request.files["file"]
    if not f or f.filename == "":
        return jsonify({"error": "未选择文件"}), 400
    name = (f.filename or "").lower()
    try:
        if name.endswith(".csv"):
            raw = f.read().decode("utf-8-sig", "ignore")
            rows = list(csv.reader(io.StringIO(raw)))
        elif name.endswith((".xlsx", ".xls")):
            try:
                import openpyxl
            except ImportError:
                return jsonify({"error": "服务端未安装 openpyxl，无法解析 xlsx；请导出为 CSV 后导入"}), 500
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), read_only=True, data_only=True)
            ws = wb.active
            rows = [[c.value for c in row] for row in ws.iter_rows()]
        else:
            return jsonify({"error": "仅支持 CSV / XLSX 文件"}), 400
    except Exception as e:
        return jsonify({"error": f"文件解析失败：{e}"}), 400

    rows = [[(c or "") for c in r] for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        return jsonify({"error": "文件无有效内容"}), 400
    headers = [str(h).strip() for h in rows[0]]
    mapping = _detect_columns(headers)
    # 是否结构化模板：必须命中核心列（候选人/岗位），否则按聊天记录逐行 AI 解析。
    # 仅命中「时间」等弱信号（如微信聊天记录导出的 时间/发送人/消息内容）不算模板。
    is_template = "candidate" in mapping.values() or "position" in mapping.values()

    if is_template:
        out = []
        for row in rows[1:]:
            rec = {}
            for i, key in mapping.items():
                if i < len(row):
                    rec[key] = str(row[i]).strip()
            if rec.get("candidate") or rec.get("position"):
                out.append(rec)
        if not out:
            return jsonify({"error": "未识别到有效记录（需至少含「候选人」或「岗位」列）"}), 422
        added, skipped, merged = add_records(out, "CSV导入")
        return jsonify({"mode": "template", "added": len(added), "skipped": skipped, "merged": merged, "records": out})

    # 非结构化：当聊天记录逐行解析
    if not config_store.load().get("zhipu_api_key"):
        return jsonify({"error": "未识别到招聘字段表头，已把每行当作聊天消息解析，但需要「智谱 GLM API Key」（在「配置我的凭证」中填写）"}), 400
    texts = []
    for row in rows:
        line = " ".join(str(c).strip() for c in row).strip()
        if line:
            texts.append(line)
    if not texts:
        return jsonify({"error": "文件无有效内容"}), 422
    all_recs, errs = [], []
    for t in texts:
        try:
            all_recs.extend(ai_parse.parse_text(t))
        except Exception as e:
            errs.append(str(e))
    if not all_recs:
        return jsonify({"error": "AI 解析未产出记录（可能内容非招聘信息或 Key 无效）", "detail": errs}), 422
    added, skipped, merged = add_records(all_recs, "聊天记录导入")
    return jsonify({"mode": "chatlog", "added": len(added), "skipped": skipped, "merged": merged, "errors": errs})


@app.route("/api/template")
def api_template():
    """下载 CSV 导入模板（含 21 列表头与示例行）。"""
    labels = ["候选人", "岗位", "部门", "阶段", "状态", "面试官", "招聘负责人", "渠道", "学历",
              "工作年限", "当前公司", "期望薪资", "联系方式", "时间", "备注", "性别", "年龄",
              "手机号", "邮箱", "期望城市", "来源"]
    sample = ["张三", "后端开发工程师", "技术部", "一面", "已通过", "李四", "王敏", "内推", "本科",
              "3年", "美团", "25k", "13800001111", "2026-08-25 14:00", "有微服务经验", "男", "26",
              "13800001111", "zhang@163.com", "北京", "示例"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(labels)
    w.writerow(sample)
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=招聘记录导入模板.csv"})


# ---------------- 大模型「理解与生成」能力（问答 / 周报） ----------------

@app.route("/api/ask", methods=["POST"])
def api_ask():
    """自然语言问答：基于当前招聘数据回答 HR 的中文问题。"""
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()
    if not q:
        return jsonify({"error": "问题不能为空"}), 400
    try:
        ans = ai_parse.ask_question(q, load_store())
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"answer": ans})


@app.route("/api/insight", methods=["POST"])
def api_insight():
    """AI 周报：基于当前招聘数据生成结构化进展/风险/建议。"""
    try:
        text = ai_parse.gen_insight(load_store())
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"insight": text})


@app.route("/api/anomalies", methods=["GET"])
def api_anomalies():
    """规则异常检测：缺联系方式/缺阶段/阶段停滞/薪资异常/低置信度待复核。"""
    return jsonify({"anomalies": anomaly.detect(load_store())})


@app.route("/api/cost", methods=["GET"])
def api_cost():
    """演示用成本统计：汇总 cost_log.json 的 token 用量与估算金额。"""
    try:
        with open(ai_parse.COST_LOG, "r", encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return jsonify({"calls": 0, "total_tokens": 0, "cost": 0.0, "by_model": {}})
    total_tokens = sum(x.get("total_tokens", 0) for x in log)
    total_cost = round(sum(x.get("cost", 0) for x in log), 4)
    by_model = {}
    for x in log:
        m = x.get("model", "unknown")
        d = by_model.setdefault(m, {"calls": 0, "tokens": 0, "cost": 0.0})
        d["calls"] += 1
        d["tokens"] += x.get("total_tokens", 0)
        d["cost"] = round(d["cost"] + x.get("cost", 0), 6)
    return jsonify({"calls": len(log), "total_tokens": total_tokens, "cost": total_cost, "by_model": by_model})


@app.route("/simulate", methods=["POST"])
def simulate():
    """演示用：模拟一条企业微信消息走完整解析+入库+回发流程（无需真实企业微信）"""
    data = request.get_json(silent=True) or {}
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text 不能为空"}), 400
    try:
        recs = ai_parse.parse_text(text)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    added, skipped, merged = add_records(recs, "企业微信群(模拟)")
    reply = build_reply(added, skipped, merged)
    push_to_group(reply)
    return jsonify({"reply": reply, "added": len(added), "skipped": skipped, "merged": merged, "records": recs})


# ---------------- 企业微信回调 ----------------
def parse_wecom_xml(xml_text):
    root = ET.fromstring(xml_text)
    out = {}
    for child in root:
        out[child.tag] = child.text or ""
    return out


def download_media(access_token, media_id):
    """用 access_token 下载企业微信临时媒体文件（图片/文件）。"""
    r = requests.get(
        "https://qyapi.weixin.qq.com/cgi-bin/media/get",
        params={"access_token": access_token, "media_id": media_id},
        timeout=20,
    )
    r.raise_for_status()
    return r.content


def process_wecom_message(msg):
    """
    后台线程处理：按消息类型抽取招聘信息并入库。
    文本 → 直接解析；图片 → 视觉模型识别；文件 → 下载后 PDF 解析 / 文本解析。
    依赖去重，企业微信重试也不会产生重复记录。
    """
    try:
        mtype = msg.get("MsgType", "")
        recs = []
        if mtype == "text":
            content = msg.get("Content", "").strip()
            if content:
                recs = ai_parse.parse_text(content)
        elif mtype == "image":
            token = config_store.get_access_token()
            if not token:
                return
            data = download_media(token, msg.get("MediaId", ""))
            recs = ai_parse.parse_image(data, "wecom_image.png")
        elif mtype == "file":
            token = config_store.get_access_token()
            if not token:
                return
            data = download_media(token, msg.get("MediaId", ""))
            fn = (msg.get("FileName", "") or "").lower()
            if fn.endswith(".pdf"):
                text = pdf_extract.extract_text_from_pdf(data)
                recs = ai_parse.parse_text(text)
            else:
                try:
                    recs = ai_parse.parse_text(data.decode("utf-8", "ignore"))
                except Exception:
                    recs = []
        else:
            return  # 仅处理文本/图片/文件
        if recs:
            added, skipped, merged = add_records(recs, "企业微信会话")
            if added:
                push_to_group(build_reply(added, skipped, merged))
    except Exception as e:
        print("[wecom] 处理失败:", e)


@app.route("/wecom", methods=["GET"])
def wecom_verify():
    """首次配置回调 URL 时的校验"""
    WX = config_store.get_wxcrypt()
    if not WX:
        return "企业微信未配置（请在界面「配置」中填写 corpid/token/aes_key）", 501
    msg_sig = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")
    echostr = request.args.get("echostr", "")
    plain = WX.verify_url(msg_sig, timestamp, nonce, echostr)
    if plain is None:
        return "verify failed", 403
    return plain


@app.route("/wecom", methods=["POST"])
def wecom_receive():
    WX = config_store.get_wxcrypt()
    if not WX:
        return "企业微信未配置（请在界面「配置」中填写 corpid/token/aes_key）", 501
    msg_sig = request.args.get("msg_signature", "")
    timestamp = request.args.get("timestamp", "")
    nonce = request.args.get("nonce", "")

    body = request.get_data(as_text=True)
    enc = parse_wecom_xml(body).get("Encrypt", "")
    plain_xml = WX.decrypt_msg(msg_sig, timestamp, nonce, enc)
    if plain_xml is None:
        return "decrypt failed", 403

    msg = parse_wecom_xml(plain_xml)
    user = msg.get("FromUserName", "")

    # 企业微信被动回复有 5s 超时，模型调用可能更久；
    # 因此立刻回执「收到」，真正的解析入库放到后台线程（去重保证重试安全）。
    threading.Thread(target=process_wecom_message, args=(msg,), daemon=True).start()

    ack = (
        "<xml>"
        f"<ToUserName><![CDATA[{user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{WX.corp_id}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        "<Content><![CDATA[收到，正在自动录入…]]></Content>"
        "</xml>"
    )
    return Response(WX.encrypt_reply(ack, nonce, timestamp), mimetype="application/xml")


if __name__ == "__main__":
    # 云平台（Render / Railway / CloudBase 等）通常注入 PORT 环境变量
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
