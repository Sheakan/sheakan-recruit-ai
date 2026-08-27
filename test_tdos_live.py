# -*- coding: utf-8 -*-
"""腾讯文档 MCP 真实联调脚本（需联网 + 真实 Token）。

用法：
  1) 去 https://docs.qq.com/open/auth/mcp.html 复制你的「个人 Token」；
  2) 设环境变量后运行：
       set TENCENT_DOCS_MCP_TOKEN=你的token
       python test_tdos_live.py
  3) （可选）指定已有表格：
       set TENCENT_DOCS_FILE_ID=你的file_id
       set TENCENT_DOCS_SHEET_ID=你的sheet_id

说明：
  - 不带 FILE_ID 时，脚本会调用 create_excel_by_markdown 新建一个「招聘数据同步快照」Excel，
    并打印返回的文档链接，方便你确认写入成功；
  - 带 FILE_ID 时，会调用 batch_update_sheet_range 全量覆盖写入（从 A1 开始）。
  - 写入类工具可能需要腾讯文档 VIP，若返回 400007 等错误码请看报错文本提示。
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tencent_docs_store as tdocs

# 直接用环境变量里的真实 token / file_id（也可在此手填）
tdocs.MCP_TOKEN = os.environ.get("TENCENT_DOCS_MCP_TOKEN", "")
tdocs.FILE_ID = os.environ.get("TENCENT_DOCS_FILE_ID", "")
tdocs.SHEET_ID = os.environ.get("TENCENT_DOCS_SHEET_ID", "")

if not tdocs.enabled():
    print("✗ 未检测到 TENCENT_DOCS_MCP_TOKEN，请先设置环境变量。")
    sys.exit(1)

# 造一条示例数据，确认字段映射无误
sample = [{
    "name": "示例-张伟", "gender": "男", "age": "29", "position": "后端实习",
    "department": "未知", "stage": "offer", "status": "已发", "channel": "内推",
    "phone": "未知", "email": "未知", "city": "未知", "note": "联调用例",
}]

print(f"→ 目标表格 file_id={'（未指定，将新建 Excel 快照）' if not tdocs.FILE_ID else tdocs.FILE_ID}")
pushed, errs = tdocs.push(sample, file_id=tdocs.FILE_ID or None, sheet_id=tdocs.SHEET_ID or None)
print("→ 推送条数:", pushed)
if errs:
    print("→ 错误信息:")
    for e in errs:
        print("   -", e)
else:
    print("✓ 推送成功。若新建了 Excel 快照，返回结果里通常会带文档链接，请到腾讯文档查看。")
