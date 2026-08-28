# 智聘 · 招聘数据智能中枢（RecruitMind）

> © 2026 Sheakan. 保留所有权利。

把 HR 的招聘文本、简历、截图与企业微信数据，用**智谱 GLM** 自动转成结构化招聘数据并实时可视化，替代手工录入。

- 代码仓库：https://github.com/Sheakan/sheakan-recruit-ai
- 在线预览：https://recruit-ai-303150-11-1343245134.sh.run.tcloudbase.com
- （腾讯云 CloudBase 云托管部署）

## 架构

```
浏览器看板(index.html)
      │ 手动录入 / 上传文件 / 智能表轮询拉取
      ▼
统一服务(server.py)
      │ ① /api/parse、/api/parse_pdf、/api/parse_image：调智谱 GLM 解析
      │ ② /wecom：企业微信自建应用回调（接收群消息 → 解析 → 入库 → 被动回复，需备案域名）
      ▼
服务端存储(records.json)  ──实时推送──▶  ECharts 看板
      │ 可选：企业微信智能表格 Webhook（追加写入）
      ▼
(可选) 群机器人 Webhook：把"已录入"确认回发到群里
```

## 本地运行

```bash
pip install -r requirements.txt
python server.py
```

浏览器打开 http://127.0.0.1:5000 ，点击右上角 **「配置我的凭证」** 填入：

- 智谱 GLM API Key（open.bigmodel.cn 创建，glm-4-flash 有免费额度）
- 企业微信智能表 WEBHOOK（可填多个，每行一个）
- 企业微信自建应用 corpid / corpsecret / Token / EncodingAESKey / AgentId
- 腾讯文档 MCP 个人令牌（环境变量 `TENCENT_DOCS_MCP_TOKEN`）

也可以通过环境变量注入（`ZHIPU_API_KEY`、`TENCENT_DOCS_MCP_TOKEN`、`SMARTSHEET_WEBHOOK`、`WECHAT_CORPID` 等），便于云平台部署；界面填写优先于环境变量。

## 使用

1. 左侧粘贴真实招聘信息 → 「手动解析录入」走 `/api/parse`；
2. 「**上传简历 PDF**」走 `/api/parse_pdf`：pdfplumber 提取文本 → 智谱 GLM 抽取 21 字段；
3. 「**上传图片**」亦可：招聘截图 / 简历照片经**视觉模型（glm-4v-plus）识别图中文字**后再结构化；
4. 「**载入演示数据**」一键注入一组覆盖各阶段/渠道的样例（来源标记「示例数据」），便于快速填充看板做演示，可随时「清空」；
5. 右侧看板实时刷新：招聘漏斗、各岗位/渠道/状态分布、明细表、异常预警；
6. 数据可一键同步到已配置的企业微信智能表或腾讯文档在线表格。

## 接入企业微信智能表格（替代手工录入）

本系统的「接收外部数据」落表能力基于**企业微信智能表格**（Webhook 域名 `qyapi.weixin.qq.com），即企业微信生态内的在线文档，与 HR 现有的企业微信文档工作流一致。它免 OAuth、免费、零注册，可直接将解析结果写入 HR 在用的智能表。

1. 在**企业微信**里新建**智能表格**，按以下列名建列（先都设为**文本列**最稳妥，列名需与 `FIELD_ID_MAP` 一致）：
   `候选人、性别、年龄、岗位、部门、阶段、状态、面试官、招聘负责人、渠道、学历、工作年限、当前公司、期望薪资、联系方式、时间、备注、来源、手机号、邮箱、期望城市`
   阶段取值建议固定为：投递 / 简历初筛 / 笔试 / 一面 / 二面 / 三面 / offer / 入职；状态建议：进行中 / 已通过 / 待定 / 已淘汰 / 已发offer / 已接受 / 已拒绝。
   前 16 个字段已在本系统预置好字段 ID（`f0xxx`，已真机验证写入成功）；新增的「性别/年龄/手机号/邮箱/期望城市」5 个字段需在表中加对应列后，把「接收外部数据」示例里的字段 ID 填入 `smartsheet_store.py` 的 `FIELD_ID_MAP` 即可同步（未填则同步时自动跳过，不影响其余字段）。
2. 进入该子表 → **右上角菜单 或 工作表三点菜单** → 点「**接收外部数据**」→「通过 webhook 接收」→ 开始配置 → 复制 Webhook 地址（形如 `https://qyapi.weixin.qq.com/cgi-bin/wedoc/smartsheet/webhook?key=XXX`）；
3. 在界面右上角 **「配置我的凭证」** 中粘贴该 WEBHOOK（可填多个表，每行一个；亦可通过环境变量 `SMARTSHEET_WEBHOOK` 注入）；
4. 此后每次解析录入，记录会**自动写入该智能表**，HR 打开文档即见，彻底替代手工录入；
5. 列名映射在 `smartsheet_store.py` 的 `FIELD_ID_MAP` 中，按你的表头调整（字段 key 为「接收外部数据」示例里的**字段 ID**；本系统已按文本列写入，最稳）。

把「阶段 / 状态」列设为单选可获得更好可视化；若如此，写入时字段格式需对应（本系统统一按文本写入，最稳）。想读取智能表反向驱动看板，可用企业微信 API 模式机器人的 `smartsheet_get_records`，本系统以本地 `records.json` 作看板数据源。

## 从企业微信智能表「函数轮询提取」（免域名输入）

企业微信「接收消息」回调需要 ICP 备案域名，云托管平台默认域名不满足。本通道**完全不需要回调域名**：HR 在企业微信智能表里直接收集/填写招聘数据，云函数**主动拉取**新增行并 AI 提取，是"函数提取消息"的合规落地方式。

1. 准备企业微信自建应用的 `corpid` / `corpsecret`（用于读取智能表，无需 Token / AESKey）；
2. 在企业微信智能表里建列（同上述 21 列，或用"候选人/岗位"等核心列即可），复制表格链接中的 `docid`（`/sheet/` 之后一段）；
3. 在「配置我的凭证」填写 `corpid / corpsecret / 智能表 docid / sheetid`（环境变量 `WECHAT_CORPID` `WECHAT_CORPSECRET` `WECHAT_SMARTSHEET_DOCID` `WECHAT_SMARTSHEET_SHEETID` 亦可）；
4. 点录入区「**从企业微信智能表拉取**」即可手动触发；若部署在 SCF，建一个**定时触发器**调用 `/api/poll_smartsheet` 即可自动执行；
5. 模块 `wecom_smartsheet.py` 会按列标题自动映射字段、对"原始消息"类列再用大模型抽取，并把已处理行记入 `poll_checkpoint.json` 避免重复。

未配置或未授权时，本功能静默不可用，不影响「手动录入 / 上传 / 腾讯文档同步」等其它能力。

## 接入腾讯文档在线表格（官方 MCP 个人令牌）

个人开发者用 **单个 MCP 个人令牌** 即可直接读写你的文档，**无需 OAuth 回调域名、无需 Client Secret**：

1. 打开 [腾讯文档 MCP 授权页](https://docs.qq.com/open/auth/mcp.html)，登录后复制你的 **MCP 个人令牌**（32 位十六进制，形如 `9f1f...`）；
2. 新建一个**在线表格**，按 `fields.py` 当前字段建表头（首行）；打开该表，从地址栏复制：
   - `fileId`：URL 中 `/sheet/` 之后的一段（如 `.../sheet/SaJQsDjBoxOA` → `SaJQsDjBoxOA`）；
   - `sheetId`：URL 中 `?tab=` 之后的一段（如 `?tab=BB08J2` → `BB08J2`）；
3. 在「配置我的凭证」填写 **MCP 令牌 / 目标表格链接**（或部署时在平台设置环境变量 `TENCENT_DOCS_MCP_TOKEN`；fileId/sheetId 页面输入可免设）；
4. 左侧点「**连接腾讯文档**」校验令牌 → 点「**同步到腾讯文档**」把看板数据全量写入该在线表格（后端以官方 MCP 协议调用 `sheet.set_range_value`，全量覆盖写入，天然幂等）。

字段定义统一在 `fields.py`，企业微信智能表与腾讯文档共用，改一处即两处生效；表头与字段顺序均由 `fields.py` 驱动，无需手动对齐列。

## 接入真实企业微信（落地用）

企业微信「群机器人(Webhook)」只能发消息、收不到消息。要"自动收消息"必须用**自建应用 + 接收消息回调**。

1. 企业微信管理后台 → 应用管理 → 创建自建应用，拿到 `AgentId / Secret / CorpID`；
2. 应用 → 接收消息 → 设置回调 URL（需公网 HTTPS，开发期用内网穿透如 cpolar/natapp 把本地 5000 暴露）为 `https://<你的域名>/wecom`；
3. 在界面右上角 **「配置我的凭证」** 中填写：`corpid / corpsecret / Token / EncodingAESKey / AgentId`（亦可通过环境变量 `WECHAT_*` 注入）；
4. 把自建应用 Bot 拉进招聘群，HR 在群里 **@Bot** 或私聊发送**文本 / 图片 / 简历 PDF**，服务端自动解析入库并被动回复"已录入"（图片经视觉模型识别、PDF 经 pdfplumber 抽取）；
5. 企业微信被动回复有 5 秒超时，重解析在后台线程执行，并由去重保证重试不产生重复记录。

企业微信要求回调 URL 的域名必须 **ICP 备案主体与当前企业微信企业主体相同或有关联关系**。使用云平台默认域名（如 `*.tcloudbase.com`、第三方内网穿透域名）都会报「该域名主体为第三方服务商，请使用企业主体域名」而**无法保存**。因此：接口 `/wecom`（GET/POST）已在代码中完整保留，配置好「企业备案域名 + corpid/secret/Token/AESKey」即可一键启用；在缺少企业备案域名的演示/个人场景下，可用「Web 录入 / 上传文件 / 智能表轮询拉取」完整演示同一套解析入库逻辑，不影响系统核心价值；若确有需要，使用一个**备案主体与企微主体一致的自有域名**解析到本服务，即可打通真实群消息实时接收。

## 图片文字提取能力

除文本与 PDF 外，本系统支持**直接上传图片**（招聘截图、简历照片、群聊长图等）：

- 后端用智谱 **视觉模型 `glm-4v-plus`** 识别图中招聘相关文字（候选人、性别、年龄、岗位、阶段、面试官、手机号、邮箱、期望城市、时间等），再将识别结果交给文本模型结构化抽取为 21 字段；
- 前端「上传图片」按钮走 `/api/parse_image`，与「上传简历 PDF」「手动录入」并列，组成文本/PDF/图片三类采集入口；
- 该能力让"截图即录入"成为可能，进一步降低 HR 手工转录成本。

想"被动监听群内所有消息"需开通**会话内容存档**（付费+企业认证），超出本方案免费范围。

## 说明

- API Key 仅在服务端使用，前端无密钥、无 CORS 风险；
- 演示存储用 `records.json`，正式落地替换为**企业微信智能表格**即可无缝衔接 HR 现有工作流；
- 模型默认 `glm-4-flash`（免费额度），可在环境变量切换。
- 看板支持**单条编辑 / 删除**（`/api/record/<id>` PUT/DELETE）与 **CSV 导出**（`/api/export`），可用作日常招聘跟踪工具。

## 在线预览 / 公开部署

本系统是一个 Python(Flask) 服务，需要一个能跑后端的环境（纯静态托管不行）。推荐几种免费/低成本的部署方式，**部署时务必在平台设置环境变量** `ZHIPU_API_KEY`（解析必需）、`TENCENT_DOCS_MCP_TOKEN`（同步腾讯文档时需要）。

### 方式一：Render

1. 把本仓库推到 GitHub；
2. 打开 https://render.com → New → Web Service → 连 GitHub 仓库；
3. Build Command：`pip install -r requirements.txt`；Start Command：`python server.py`；
4. 在 Environment 里加 `ZHIPU_API_KEY`（其余按需）；
5. 部署完成即获得 `https://xxx.onrender.com` 公开预览链接。

### 方式二：Railway / Fly.io

- 已附 `Procfile`（`web: python server.py`），Railway 连仓库后自动识别；
- 在平台 Variables 设 `ZHIPU_API_KEY`，部署即得公开地址。

### 方式三：CloudBase

- 仓库已含 `Dockerfile` 与 `cloudbaserc.json`（云托管容器模式）；
- 在 CloudBase 控制台创建环境 → 云托管 → 新建服务，来源选「代码仓库 / Dockerfile」；
- 在环境变量中填 `ZHIPU_API_KEY`、`TENCENT_DOCS_MCP_TOKEN`（同步腾讯文档时需要）等；
- 部署后获得公网访问地址，可直接在方案里展示"腾讯生态一键部署"。
- 本项目已实际部署于 CloudBase 云托管（环境 `control103`），预览地址：**https://recruit-ai-303150-11-1343245134.sh.run.tcloudbase.com**
