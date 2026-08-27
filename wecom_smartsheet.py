# -*- coding: utf-8 -*-
"""企业微信智能表「函数轮询提取」（免回调域名）。

企业微信「接收消息」回调要求回调域名完成 ICP 备案且主体与企业一致，普通云托管域名无法满足。
本项目因此提供一条**不依赖域名认证**的企业微信输入通道：HR 在企业微信智能表里直接填写/收集招聘数据，
由部署在云函数上的本模块**定时主动拉取**新增行，映射成统一字段后入库并同步到腾讯文档。

依赖：企业微信自建应用的 corpid / corpsecret（读取智能表用，不需要回调域名、不需要 Token/AESKey）。
"""
import os
import json
import requests
import config_store

CHECKPOINT_FILE = "poll_checkpoint.json"
API = "https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet"

# 智能表列标题 -> 本系统字段 key（兼容中文/英文/别名）
COLUMN_ALIASES = {
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
    "time": ["时间", "日期", "面试时间", "time", "date"],
    "remark": ["备注", "说明", "remark", "note", "comment"],
    "gender": ["性别", "gender", "sex"],
    "age": ["年龄", "age"],
    "phone": ["手机号", "手机", "电话", "phone", "mobile", "tel"],
    "email": ["邮箱", "电子邮件", "email", "mail"],
    "expectedCity": ["期望城市", "城市", "工作城市", "expectedcity", "city"],
    "raw_text": ["原始消息", "原始内容", "聊天记录", "消息内容", "raw", "text"],
}


def _coerce(v):
    """智能表读出的单元格值可能是字符串 / {'text':...} / 列表，统一成字符串。"""
    if v is None:
        return ""
    if isinstance(v, dict):
        return str(v.get("text") or v.get("value") or "")
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return str(v).strip()


def get_fields(token, docid, sheet_id):
    """返回 {列标题: field_id}（用标题做映射，避免依赖写入时的字段 ID）。"""
    resp = requests.post(
        f"{API}/get_fields",
        params={"access_token": token},
        json={"docid": docid, "sheet_id": sheet_id, "limit": 200, "offset": 0},
        timeout=15,
    )
    data = resp.json()
    if data.get("errcode", -1) != 0:
        raise RuntimeError("读取智能表字段失败：%s" % data.get("errmsg"))
    out = {}
    for f in data.get("fields", []):
        title = (f.get("field_title") or "").strip()
        if title:
            out[title] = f.get("field_id")
    return out


def get_records(token, docid, sheet_id, limit=100, offset=0):
    resp = requests.post(
        f"{API}/get_records",
        params={"access_token": token},
        json={"docid": docid, "sheet_id": sheet_id, "limit": limit, "offset": offset},
        timeout=15,
    )
    data = resp.json()
    if data.get("errcode", -1) != 0:
        raise RuntimeError("读取智能表记录失败：%s" % data.get("errmsg"))
    return data.get("records", [])


def _load_checkpoint():
    try:
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("wecom_smartsheet", []))
    except Exception:
        return set()


def _save_checkpoint(ids):
    with open(CHECKPOINT_FILE, "w", encoding="utf-8") as f:
        json.dump({"wecom_smartsheet": list(ids)}, f, ensure_ascii=False, indent=2)


def fetch_new_records():
    """拉取企业微信智能表中尚未处理的新增行，映射为本系统记录。
    返回 (records, error)；error 非空表示配置/接口问题，records 为空。"""
    cfg = config_store.load()
    w = cfg["wecom"]
    docid, sheetid = w.get("smartsheet_docid", ""), w.get("smartsheet_sheetid", "")
    if not (docid and sheetid and w["corpid"] and w["corpsecret"]):
        return [], "未配置企业微信智能表（需在「配置」中填写 docid / sheetid，以及 corpid / corpsecret）"
    try:
        token = config_store.get_access_token()
    except Exception as e:
        return [], "获取企业微信 access_token 失败：%s" % e

    try:
        title_to_fid = get_fields(token, docid, sheetid)
    except Exception as e:
        return [], str(e)

    # 反向索引：field_id -> 本系统字段 key
    fid_to_key = {}
    for title, fid in title_to_fid.items():
        tl = title.lower()
        for key, aliases in COLUMN_ALIASES.items():
            if tl in [a.lower() for a in aliases]:
                fid_to_key[fid] = key
                break

    seen = _load_checkpoint()
    records, processed = [], set(seen)
    offset = 0
    while True:
        rows = get_records(token, docid, sheetid, offset=offset)
        if not rows:
            break
        for row in rows:
            rid = row.get("record_id", "")
            if rid in seen:
                continue
            processed.add(rid)
            values = row.get("values", {}) or {}
            rec = {}
            raw_text = ""
            for fid, val in values.items():
                key = fid_to_key.get(fid)
                if key == "raw_text":
                    raw_text = _coerce(val)
                elif key:
                    rec[key] = _coerce(val)
            if not (rec.get("candidate") or rec.get("position")):
                # 结构化列不足，但存在"原始消息"列时，用大模型从原文抽取
                if raw_text and cfg.get("zhipu_api_key"):
                    try:
                        import ai_parse
                        rec = {"_raw": raw_text}
                        rec.update(ai_parse.parse_text(raw_text)[0] if ai_parse.parse_text(raw_text) else {})
                    except Exception:
                        rec = {}
            if rec.get("candidate") or rec.get("position"):
                records.append(rec)
        if len(rows) < 100:
            break
        offset += 100
    _save_checkpoint(processed)
    return records, ""


if __name__ == "__main__":
    recs, err = fetch_new_records()
    if err:
        print("错误：", err)
    else:
        print("新增记录：", len(recs))
        for r in recs:
            print(r)
