# -*- coding: utf-8 -*-
"""解析层：把自然语言招聘文本/图片转成结构化记录。

支持两个模型供应商，按 model 名称自动切换（无需改调用点）：
  - 智谱 GLM：glm-4-flash / glm-4v-plus 等，走 ZHIPU_URL + 智谱 Key（默认文本模型）
  - DeepSeek：deepseek-chat 等，走 DEEPSEEK_URL + DeepSeek Key（备选：在模型名填 deepseek-chat 可切换）
"""
import os
import re
import io
import json
import base64
import time
import requests

import config_store
import fields

ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "")
ZHIPU_URL = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"



def log_cost(model: str, usage: dict):
    """成本跟踪已禁用（按需求移除 API 成本统计），保留签名避免调用点改动。"""
    pass


def _text_api_key():
    return ZHIPU_API_KEY or config_store.load().get("zhipu_api_key", "")


def _model():
    return config_store.load().get("zhipu_model", "") or "glm-4-flash"


def _vision_model():
    return config_store.load().get("zhipu_vision_model", "glm-4v-plus")


def _is_deepseek(model):
    return bool(model) and str(model).lower().startswith("deepseek")


def _provider(model):
    """按 model 名称返回 (base_url, api_key, 供应商名)。"""
    if _is_deepseek(model):
        return DEEPSEEK_URL, (DEEPSEEK_API_KEY or config_store.load().get("deepseek_api_key", "")), "DeepSeek"
    return ZHIPU_URL, (ZHIPU_API_KEY or config_store.load().get("zhipu_api_key", "")), "智谱"


def _chat(payload, api_key=None, timeout=60, retries=1):
    """统一的模型调用：按 model 自动选择供应商与 Key，负责友好错误转换与 429 限流重试。

    所有对外的报错都转换为「人话」，绝不直接把后端 HTTP 异常透传到前端。
    """
    model = payload.get("model", "")
    url, key, prov = _provider(model)
    api_key = api_key or key
    if not api_key:
        raise RuntimeError(f"未配置 {prov} API Key（请在界面「配置我的凭证」中填写对应 Key）")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429:
                if attempt < retries:
                    time.sleep(3 * (attempt + 1)); continue
                raise RuntimeError(
                    f"{prov} 模型调用频率超限（429）。免费额度有限，请稍候 1~2 分钟再试；"
                    "或改用其它模型。若用量大建议升级付费额度。"
                )
            if resp.status_code == 401:
                raise RuntimeError(f"{prov} API Key 无效或已失效，请在「配置我的凭证」中检查 Key。")
            if resp.status_code >= 500:
                if attempt < retries:
                    time.sleep(2); continue
                raise RuntimeError(f"{prov} 服务暂时不可用（5xx），请稍后重试。")
            if resp.status_code >= 400:
                raise RuntimeError(f"请求被拒绝（{resp.status_code}）：{resp.text[:200]}")
            data = resp.json()
            return data
        except requests.exceptions.ConnectionError as e:
            last_err = f"网络异常，无法连接 {prov} 服务：{e}"
            if attempt < retries:
                time.sleep(2); continue
        except requests.exceptions.Timeout as e:
            last_err = f"请求超时（{e}），请稍后重试。"
            if attempt < retries:
                time.sleep(2); continue
        except RuntimeError:
            raise
    raise RuntimeError(last_err or f"调用 {prov} 服务失败")


def build_system_prompt():
    """按当前字段配置动态生成大模型抽取提示词，使新增/重命名的字段也能被正确抽取。"""
    fields_list = fields.field_descriptions()
    schema_lines = []
    for key, name, desc in fields_list:
        schema_lines.append(f'      "{key}": "{name}：{desc}"')
    schema = ",\n".join(schema_lines)
    return f"""你是一个资深招聘数据录入助手。
用户会给你一段自然语言（可能是企业微信群消息、HR 随手记、语音转写文本），其中包含招聘进展。
请从中抽取所有招聘记录，只输出严格 JSON，不要输出任何多余文字，也不要使用 markdown 代码块。

输出格式：
{{
  "records": [
    {{
{schema},
      "confidence": "0到1之间的小数(字符串)，表示本条抽取的可信度"
    }}
  ]
}}
规则：
- **逐条列出每一个独立的候选人**：即便同一段话里出现多个姓名/岗位（如「李娜…；王强…；张伟…」），也必须分别为每个人输出一条记录，严禁合并或遗漏；
- 没有任何招聘信息时，返回 {{"records": []}}；
- 不要编造字段，缺失就填空字符串；字段集合以「输出格式」为准，严格按给定字段名输出 JSON key（不要自创 key）；
- 阶段必须严格从给定枚举中选，不要自创（例如「初试」应归为「一面」）；
- 状态要符合当前阶段语境（如 offer 阶段常用「已发offer/已接受/已拒绝」）。"""


def extract_json(content: str):
    """兼容模型偶尔用 ```json 包裹的情况"""
    content = content.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", content)
    if m:
        content = m.group(1).strip()
    return json.loads(content)


def _parse_once(text: str, model: str):
    """调用一次模型，返回 records 列表（不负责分段/合并）。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": build_system_prompt()},
            {"role": "user", "content": text},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    # 长文本（如大段简历/职位 JD）生成较慢，给足超时；失败自动重试 1 次
    resp = _chat(payload, timeout=120, retries=1)
    content = resp["choices"][0]["message"]["content"]
    data = extract_json(content)
    return data.get("records", [])


def _norm(v):
    return (v or "").strip().lower()


def _dedupe(records):
    """按 (候选人, 岗位, 阶段) 去重，保留首次出现的记录，消除模型偶发的重复输出
    （如同一人被模型输出了两遍、却漏了另一人）。"""
    seen = set()
    out = []
    for r in records:
        key = (_norm(r.get("candidate")), _norm(r.get("position")), _norm(r.get("stage")))
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# 文档自带表头里常出现的列名；若模型把这些表头当成数据返回，应直接丢弃
HEADER_WORDS = {
    "候选人", "姓名", "岗位", "部门", "阶段", "状态", "面试官", "招聘负责人", "hr",
    "渠道", "学历", "工作年限", "当前公司", "期望薪资", "薪资", "联系方式", "手机",
    "手机号", "邮箱", "期望城市", "时间", "备注", "来源", "性别", "年龄", "电话", "微信",
}


def _clean_records(records):
    """清洗明显无用的脏记录：
    - 把「文档表头行」误当成数据（candidate/position 等于列名）；
    - 既没有姓名、也没有岗位、手机、邮箱的空记录。
    """
    out = []
    for r in records:
        if not isinstance(r, dict):
            continue
        cand = (r.get("candidate") or "").strip()
        pos = (r.get("position") or "").strip()
        if cand in HEADER_WORDS or pos in HEADER_WORDS:
            continue
        if (not cand and not pos
                and not (r.get("phone") or "").strip()
                and not (r.get("email") or "").strip()):
            continue
        out.append(r)
    return out


def parse_text(text: str, api_key=None, model=None):
    """
    调用大模型解析招聘文本，返回 records 列表。
    未配置任何 Key 时抛出 RuntimeError。
    """
    model = model or _model()
    if not (_text_api_key() or DEEPSEEK_API_KEY or config_store.load().get("deepseek_api_key")):
        raise RuntimeError("未配置任何模型 API Key（请在界面「配置我的凭证」中填写 智谱 或 DeepSeek Key）")
    text = (text or "").strip()
    if not text:
        return []

    raw = _parse_once(text, model)
    # 按强分隔符（；; 或换行）切分段落，用于补偿模型偶发的「漏抽候选人 / 重复候选人」
    segs = [s.strip() for s in re.split(r"[；;\n]+", text) if len(s.strip()) >= 3]
    recs = _dedupe(raw)
    # 触发分段补偿的两种情形：
    #  1) 抽出的条数明显少于段落数（漏抽，如一段话 3 个候选人只出 1~2 条）；
    #  2) 整段抽取结果里出现了重复候选（模型把同一人输出了两遍，却漏了另一人）。
    # 仅对段落数较少（<=8）的输入触发，避免长文本产生过多调用。
    if segs and len(segs) <= 8 and (len(recs) < len(segs) or len(recs) < len(raw)):
        for part in segs:
            recs += _parse_once(part, model)
        recs = _dedupe(recs)
    return _clean_records(recs)


def _ocr_text(image_bytes):
    """低成本优先：用本地 OCR（tesseract）识别图片文字；不可用则返回 None。

    环境需安装 `pytesseract` 与 tesseract 二进制（如 `apt-get install tesseract-ocr`），
    以及中文包 `tesseract-ocr-chi-sim`。任一缺失都会安全回退到视觉模型，不影响主流程。
    """
    try:
        from PIL import Image
        import pytesseract
    except Exception:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        return pytesseract.image_to_string(img, lang="chi_sim+eng") or None
    except Exception:
        return None


def parse_image(image_bytes: bytes, filename: str = "image.png", api_key=None, model=None, mode=None):
    """
    图片解析，支持两种模式（前端可配）：
      - vision（默认）：云端视觉模型直接读图取原文 → 文本大模型结构化抽取。
                       免本地安装（不再依赖 tesseract），复用已有智谱 Key；
                       成本高于纯文本，视觉模型有频率限制（429）。
      - ocr_local：仅本地 tesseract OCR 识别文字 → 文本大模型抽取；
                   需部署机安装 tesseract-ocr 及中文包 chi_sim，未安装则明确报错。
    主路径改为云端视觉，解决「本地 tesseract 在多数云端环境缺失导致 OCR 静默失效」的问题。
    返回 records 列表。
    """
    if not (_text_api_key() or DEEPSEEK_API_KEY or config_store.load().get("deepseek_api_key")):
        raise RuntimeError("未配置 模型 API Key（请在界面「配置」中填写）")
    mode = mode or config_store.load().get("image_mode", "vision")

    # 云端视觉读图（默认）：不依赖任何本地二进制
    if mode in ("vision", "vision_only"):
        if not _text_api_key():
            raise RuntimeError(
                "云端视觉读图需要「智谱 Key」（视觉模型 glm-4v-plus）。"
                "请在「配置我的凭证」中填写智谱 Key；或改用「仅本地 OCR」模式并在部署机安装 tesseract。"
            )
        return _parse_image_vision(image_bytes, filename)

    # 仅本地 OCR：需安装 tesseract + 中文包
    if mode in ("ocr_local", "ocr_only"):
        ocr = _ocr_text(image_bytes)
        if ocr and len(ocr.strip()) >= 8:
            return parse_text(ocr)
        raise RuntimeError(
            "本地 OCR 未识别出足够文字。原因：当前环境未安装 tesseract 或中文包 chi_sim；"
            "请在部署机执行 `apt-get install tesseract-ocr tesseract-ocr-chi-sim`（或对应平台命令），"
            "或改用「云端视觉读图」模式。"
        )

    # 未知模式兜底：走云端视觉
    return _parse_image_vision(image_bytes, filename)


def _parse_image_vision(image_bytes: bytes, filename: str):
    """视觉模型读取图片（成本较高、易触发 429 限流），识别结果再交给文本模型结构化抽取。"""
    model = _vision_model()
    mime = "image/png"
    if filename.lower().endswith((".jpg", ".jpeg")):
        mime = "image/jpeg"
    b64 = base64.b64encode(image_bytes).decode()
    data_uri = f"data:{mime};base64,{b64}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是招聘信息提取助手。请仔细识别图片中的招聘相关文字（" +
             "、".join(n for _, n, _ in fields.field_descriptions()) +
             "），逐字保留原文。"},
            {"role": "user", "content": [
                {"type": "text", "text": "请提取图片里的招聘信息文字内容，保留关键原文："},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]},
        ],
        "temperature": 0.2,
    }
    data = _chat(payload, timeout=60)
    desc = data["choices"][0]["message"]["content"]
    if not desc.strip():
        return []
    # 复用文本模型做结构化抽取（统一用便宜文本模型填写字段）
    return parse_text(desc)


# ---------------- 大模型“理解与生成”能力（超越单纯抽取） ----------------

def _records_to_text(records: list) -> str:
    """把记录列表压成一段易读文本，作为问答/周报的上下文。"""
    lines = []
    for i, r in enumerate(records, 1):
        parts = [f"{i}. {r.get('candidate') or '?'} | 岗位:{r.get('position') or '-'} | 阶段:{r.get('stage') or '-'} | 状态:{r.get('status') or '-'}"]
        for k in ("channel", "interviewer", "recruiter", "time", "expectedSalary", "expectedCity", "education", "experience"):
            if r.get(k):
                parts.append(f"{k}:{r[k]}")
        if r.get("remark"):
            parts.append(f"备注:{r['remark']}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def ask_question(question: str, records: list, api_key=None, model=None):
    """自然语言问答：基于已有招聘数据回答 HR 的中文问题。"""
    if not (_text_api_key() or DEEPSEEK_API_KEY or config_store.load().get("deepseek_api_key")):
        raise RuntimeError("未配置 模型 API Key（请在界面「配置」中填写）")
    ctx = _records_to_text(records)
    sys_p = ("你是招聘数据分析助手。下面是一份招聘数据，请仅基于这些数据用中文简洁回答用户的问题。"
             "若数据不足以回答，请明确说明“数据中未包含相关信息”，不要编造任何数据。回答尽量分点、控制在 200 字内。")
    user_p = f"招聘数据：\n{ctx}\n\n用户问题：{question}\n请回答。"
    payload = {
        "model": model or _model(),
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": user_p},
        ],
        "temperature": 0.3,
    }
    resp = _chat(payload, timeout=60)
    return resp["choices"][0]["message"]["content"].strip()


def gen_insight(records: list, api_key=None, model=None):
    """AI 周报：基于招聘数据生成结构化进展、风险与建议。"""
    if not records:
        return "暂无数据，请先录入招聘信息后再生成周报。"
    if not (_text_api_key() or DEEPSEEK_API_KEY or config_store.load().get("deepseek_api_key")):
        raise RuntimeError("未配置 模型 API Key（请在界面「配置」中填写）")
    ctx = _records_to_text(records)
    sys_p = ("你是资深招聘 HRBP。请基于给定的招聘数据，生成一份结构化的招聘周报，包含："
             "1）总体进展；2）各阶段分布与转化情况；3）风险与异常（如长时间停滞、关键信息缺失）；"
             "4）下一步行动建议。用中文、分点、简洁专业，不超过 400 字。")
    user_p = f"招聘数据：\n{ctx}\n\n请生成招聘周报。"
    payload = {
        "model": model or _model(),
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": user_p},
        ],
        "temperature": 0.4,
    }
    resp = _chat(payload, timeout=60)
    return resp["choices"][0]["message"]["content"].strip()
