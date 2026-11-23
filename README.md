# 知据 · 企业知识工作台

在同一条问答里查制度原文和业务数据。文件答案可回到页、行、工作表或段落，业务数值可回到当次只读接口请求和返回记录。标准版 **v1.3.0** 包含中文 Web 工作台、FastAPI 服务和 SQLite 存储。新工作区默认空白，导入资料后即可使用本地检索；管理员可随时接入对话模型和向量模型。

按行业交付时可复用同一版本，分别设置数据目录、模型、初始资料、只读业务接口和验收题。[行业模板与部署示例](docs/industry-profiles.md) · [五个高星 RAG 项目调研](docs/rag-projects.md)

## 本机启动

Windows 上在项目目录双击 **start.cmd**。启动器会在本机运行服务，确认健康检查和应用身份后打开默认浏览器。使用期间保持启动窗口开启，按 Ctrl+C 可停止服务。再次双击会打开已运行的工作台。默认端口为 8781，如已被其他程序占用，会在 8782–8785 中选择可用端口，地址以窗口实际显示为准。服务地址只在启动它的电脑上有效。[常用本机地址](http://127.0.0.1:8781/)

```powershell
.\start.ps1
```

上面的 PowerShell 命令也可启动工作台，需要指定其他端口时可使用 `.\start.cmd --port 8782`。启动器会复用依赖版本符合要求的 Python 3.11+ 环境（SQLite 须支持 FTS5）。需要补装依赖时使用项目虚拟环境，先查找随包提供的离线安装文件，再按需连接 Python 软件源。Linux、macOS 或已有虚拟环境也可直接启动。

```sh
python -m pip install -r requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8781 --no-proxy-headers
```

数据目录默认为项目根目录的 `var/`，用 `RAG_DATA_DIR` 可切换。首次启动前设置 `RAG_INDUSTRY_PROFILE=research-finance` 可载入科研财务演示，设置 `manufacturing` 可载入制造设备示例。已初始化的工作区保留原有资料、设置和会话。

## 三个验收问题

在“知识问答”输入问题，或点击输入框下方的示例填入问题。首问发出后，输入框固定在主区底部，可继续提问或停止生成。点击回答中的数字引用或“查看依据”会打开居中的“答案依据”弹窗，关闭后可在原位置继续阅读。选择“打开文档”可阅读引用对应的版本和位置。已完成的多轮问答可从侧栏搜索和回放，生成中断时可保留当页文字并重试。管理员从侧栏底部进入“管理后台”，可查看知识库概览、导入文档和配置数据接口。

在科研财务演示工作区可用下面三个问题验证文档、查数和综合回答。空白工作区随接入来源出现对应类型的示例。

| 问题 | 演示结果 | 复核方法 |
| --- | --- | --- |
| 科研项目经费报销需要哪些材料？ | 报销申请单、有效发票、支付凭证，采购类支出还需合同和验收单 | 在“文档检索”模式提问，打开《科研项目经费报销指引.md》的“报销材料”原文 |
| 2026年材料研发平台的预算执行率是多少？ | 75%，已执行 900 万元，年度预算 1200 万元 | 在接口快照核对项目、年度和金额，计算 900 ÷ 1200 |
| 解释预算执行率的计算公式，并查询2026年智能制造项目的实际执行率 | 制度说明统计口径，接口给出 480 ÷ 800 = 60% | 分别打开制度引文和预算接口快照 |

“查询历史”保存当次答案及证据。文档阅读器提供原件 SHA-256，下载后可用 PowerShell 的 `Get-FileHash -Algorithm SHA256` 核对。接口来源面板显示接口名称、查询时间、已保存的业务筛选条件和记录快照。原始请求地址保存在内部归档。原始响应和提取记录分别保留校验摘要，普通成员下载时会检查所选内容中的受限字段。历史回答使用当时的快照。上游小数或超大整数超出浏览器数字精度时，记录数组以数字文本保存，原始响应保留上游字节。

## 文件与检索

管理员可沿着一条错误回答，依次检查查询执行、导入分块和证据排名。

| 遇到的问题 | 在工作台中核对 |
| --- | --- |
| 问答慢、中断或没有返回数据 | 打开「管理后台 → 知识库概览 → 查询诊断」，展开本次执行。按实际经过的阶段查看毫秒耗时、无结果、失败或中断的位置，可从已归档的执行打开对应回答。旧历史保留原内容，耗时自升级后开始记录 |
| 回答引用了错行、表头或页码 | 在「导入管理」的导入结果或文件行点击「分块对照」。选择一个实际入库分块，在旁边核对对应版本的原件；窄屏可切换两者。PDF 按页定位，图片显示原图，文本按可用行号定位，XLSX 读取工作表中的原始单元格。需要原始版式的 DOCX、PPTX 可按提示下载核对 |
| 提问用词和制度原文不同 | 在「管理后台 → 检索评测」输入问题，指定期望的文档或分块和检查名次。页面显示候选是否命中、重排后排第几，以及实际排在前面的分块。评测使用当前工作区的检索和向量配置，可以从结果打开原文。单题评测独立于问答历史 |

在文档库点文件名或“在线查看”即可阅读已导入的文件。阅读器支持全文搜索、翻页和查看来源位置，现有文档无需重新导入。PDF、PNG 和 JPEG 还可在浏览器内查看原版。Word、Excel、PPT 等格式按导入时提取的正文、单元格或幻灯片内容展示，原件可下载。

| 文件 | 可核验位置 |
| --- | --- |
| TXT、Markdown、HTML/HTM | 行号、标题或正文位置 |
| CSV/TSV、XLSX | 行号或工作表与单元格范围 |
| JSON、JSONL/NDJSON | JSON Pointer（定位对象字段或数组元素的路径）或行号 |
| PDF、DOCX、PPTX | PDF 页码、Word 段落或表格、幻灯片编号 |
| 启用 OCR 后的 PNG、JPG/JPEG、TIF/TIFF、扫描 PDF | 原图或扫描件页码，另附 OCR 识别标记 |

管理员在“管理后台”的“导入管理”上传文件后即可提问。同名文件重复导入会去重，内容更新时新版本参与查询，原版本保留以便核验历史答案。表格以首个非空行为表头。单文件上限 20 MiB、提取文本 400 万字符、2 万个片段，每个请求最多上传 8 个文件，请求总量上限 48 MiB。单个 PDF/PPTX 最多 1200 页/张。Office 压缩包解开后的总量上限是 64 MiB。音频和旧版二进制 Office 文件须先转换。

### 离线识别扫描件和图片

OCR 会把图片中的文字转成可检索文本。默认关闭。需要时，在运行服务的 Python 环境安装 `requirements-ocr.txt`，设置 `RAG_OCR_ENABLED=1` 后重启。若使用 Windows 启动脚本且已经创建虚拟环境，以下命令可直接启用。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt
$env:RAG_OCR_ENABLED = "1"
.\start.ps1
```

组件包括 RapidOCR、ONNX Runtime、PDFium 和 Pillow。服务检查依赖与 RapidOCR 随包的中文模型、字典，条件满足时“导入管理”会列出图片格式，`GET /api/formats` 的 `ocr_enabled` 为真。识别在本机进行。含文字层的 PDF 直接提取原文，仅对没有文字层且有内容的页进行渲染识别，每段引文沿用原页码。若一页已有可提取文字，该页内嵌图片的文字须单独提取。

每文件最多识别 5 页，每张图片最多 1200 万像素、最长边 10000 像素，送入识别前最长边缩至 2000 像素。单文件解析限时 40 秒。已在 Windows 以真实 PNG、JPEG、多页 TIFF、纯扫描及混合 PDF 检查识别和页码。小字号、模糊图片、数字与表格可能被误读，业务数值请按引用位置核对原件。

默认按中文相邻双字和英文词建立全文索引，结合词频、词的稀有程度、标题和问题覆盖率给候选片段排序。自动模式用已配置的接口关键词、描述、参数名和别名判断是文档、数据或混合查询。未命中文档会提示证据不足，必要条件缺失时会要求补充。业务项目、年份和指标口径越明确，越容易选中正确记录。

## 接入自己的业务数据

1. 在服务环境设置 `RAG_ALLOWED_HOSTS`，多个目标以逗号分隔。域名条目只接受协议默认端口，其他端口须填写 `host:port`。公司私网地址还需 `RAG_ALLOW_PRIVATE_NETWORK=1`。
2. 在“数据接口”登记只读 GET 地址、说明、意图关键词和参数。URL 填到路径，查询条件单独声明。带凭据的接口使用 HTTPS，在配置中填写环境变量名，如 `RAG_CONNECTOR_FINANCE`。把真实 Bearer Token 放在服务进程环境。
3. 若上游返回 `{"data":{"rows":[{"project":"材料研发平台","rate":0.75}]}}`，记录路径填 `data.rows`。根节点已经是记录数组时留空。在页面“测试”中传入明确参数，对照结果再用自然语言查询。

可将下列配置通过 `POST /api/connectors` 注册，其中的样例域名需换成部署者有权访问的服务。

```json
{
  "name": "项目预算",
  "description": "按项目及年度查询预算执行金额和执行率",
  "url": "https://finance.example.com/api/budget",
  "keywords": ["预算", "执行率"],
  "parameters": [
    {"name": "project", "label": "项目名称", "type": "string", "required": true, "aliases": ["项目"]},
    {"name": "year", "label": "年度", "type": "integer", "required": true, "aliases": ["年份"]}
  ],
  "records_path": "data.rows",
  "token_env": "RAG_CONNECTOR_FINANCE"
}
```

支持字符串、整数、有限数值和 `YYYY-MM-DD` 日期。服务校验允许的主机、端口及解析地址，拒绝重定向。一次查询最多等待 8 秒，响应上限 1 MiB、500 条记录。需要跨项目汇总、计算同比或完成关联时，应由业务接口按确认的口径计算并返回结果。可下载的原始响应包含记录路径之外的字段，接口应从上游只返回允许该工作区全员读取的数据。

## 配置对话模型与向量模型

打开 **管理后台 → 模型配置**。两种模型分别选择配置来源，可来自不同服务商。选择「使用工作区配置」，填写兼容 OpenAI 协议的 API 基础地址、服务商提供的模型 ID，以及服务需要的 API Key。可以先用当前表单测试连接，通过后保存。下一次提问使用保存的配置；已开始的一轮问答使用开始时的设置。管理员可以设置和测试，读者直接在问答页使用。

切回「使用环境配置」会移除该模型的工作区覆盖配置，按下表读取服务环境。选择「关闭模型」可暂停这一项。保留相同服务地址时，密钥留空沿用原值，管理页可单独替换或移除密钥。更换基础地址后需要为新地址提供其对应凭据。接口只返回密钥是否已设置。

| 环境变量 | 用途和数据范围 |
| --- | --- |
| `RAG_LLM_BASE_URL`、`RAG_LLM_MODEL`、可选 `RAG_LLM_API_KEY` | 兼容 OpenAI 的对话接口。意图解析发送问题、接口说明及参数定义。文档回答发送问题及候选原文，让模型选择引文 |
| `RAG_EMBED_BASE_URL`、`RAG_EMBED_MODEL`、可选 `RAG_EMBED_API_KEY` | 兼容 OpenAI 的向量接口。发送问题、候选文件名和文段，以语义相似度辅助排序 |

基础地址填写到 API 前缀，例如本机模型的 `http://127.0.0.1:11434/v1`。远程模型服务使用 HTTPS。两套配置独立启用。未配置对话模型时，问答页直接推送可核验的原文和接口查询结果。配置对话模型后，文档回答通过 `/chat/completions` 的 `stream: true` 接收文本增量，逐段核对原文和引用。生成中断时页面保留残稿，可重新查询。大文库先按词筛选候选片段，再对候选排序，纯同义改写可能漏查。接入托管模型前应确认允许向该服务发送哪些原文。

## 服务接口

| 操作 | 请求 |
| --- | --- |
| 登录 | `POST /api/login`，具名账号提交 `{"username":"analyst","password":"…"}`，共享角色提交 `{"password":"…"}`，后续请求携带响应设置的会话 Cookie |
| 问答 | `POST /api/query`，正文 `{"question":"科研项目经费报销需要哪些材料？","mode":"auto"}`，返回 `answer`、`intent`、`status` 和 `citations` |
| 流式问答 | `POST /api/query/stream` 接收相同请求，可用 `parent_id` 继续自己的历史会话。同一轮重试时复用 `request_id`（UUID），已归档的回答可直接恢复。SSE 事件包括阶段（`status`）、正文增量（`delta`）、真实依据（`evidence`）、已归档结果（`done`）和异常（`error`）。`GET /api/query/examples` 按已接入来源返回示例 |
| 上传 / 下载原件 | `POST /api/documents`，multipart 表单中以 `files` 重复提交文件，按 `imported` 和 `errors` 检查每份结果。`GET /api/documents/{id}/download` 下载原件 |
| 在线阅读 | `GET /api/documents/{id}/preview`，用 `q` 搜索正文或位置，`limit`、`offset` 分页，`chunk_id` 直达引用所在页。支持原版预览的文件另提供 `GET /api/documents/{id}/original` |
| 管理 / 测试接口 | `POST /api/connectors`、`PUT /api/connectors/{id}`、`POST /api/connectors/{id}/test`，测试正文示例 `{"params":{"year":2026}}` |
| 模型配置（管理员） | `GET /api/admin/models` 查看生效配置；`PUT /api/admin/models/{kind}` 保存；`POST /api/admin/models/{kind}/test` 测试已保存配置或提交的草稿。`kind` 取 `llm` 或 `embedding` |
| 查询诊断、检索评测（管理员） | `GET /api/admin/query-traces` 按批次读取执行；`POST /api/admin/retrieval/evaluate` 提交 `question`、`document_id`、可选 `chunk_id` 和 `top_k`，返回候选、重排名次和实际分块 |
| 导入分块对照（管理员） | `GET /api/documents/{doc_id}/source-window?chunk_id=…` 读取所选版本的文本行或工作表单元格，PDF 与图片复用已鉴权的原版预览 |
| 历史与概览 | `GET /api/history?scope=mine` 可用 `q` 搜索问题，`GET /api/history/{id}` 回放快照。管理员使用 `scope=workspace` 查看工作区查询，在 `GET /api/overview` 查看统计 |
| 下载接口证据 | `GET /api/history/{id}/sources/{source_id}/raw` 下载当次原始响应，`GET /api/history/{id}/sources/{source_id}/rows` 下载提取出的记录数组，`source_id` 取本次答案内的引文编号，如 `S1` |
| 运行检查 / 接口定义 | `GET /healthz`、`GET /api/openapi.json` |

已知业务参数时，可在问答请求中指定 `connector_id` 和 `params`，例如 `{"question":"查询预算执行率","mode":"data","connector_id":1,"params":{"project":"材料研发平台","year":2026}}`。接口 ID 可由 `GET /api/connectors` 查询。

## 部署与运行边界

空白账号配置下，只有服务端与访问端均为回环地址的本机直连可免密体验。具名用户可由管理员在相同 `RAG_DATA_DIR` 下创建，密码通过交互输入。创建首个账号后，本机也需要登录。个人查询记录可跨次登录回看。

```sh
python -m ragcore.accounts add analyst --role reader
python -m ragcore.accounts add operator --role admin
python -m ragcore.accounts passwd analyst
python -m ragcore.accounts disable analyst
```

还可通过环境变量配置共享管理员密码 `RAG_ADMIN_PASSWORD` 和独立的共享只读密码 `RAG_READER_PASSWORD`。远程访问需要登录。在反向代理上启用 HTTPS，将请求转发到本机监听地址并保留原始 Host，然后设置 `RAG_COOKIE_SECURE=1`。服务以实际连接方识别本机访问，启动参数保留 `--no-proxy-headers`。

使用 Docker Compose 2.24+ 时，复制 `.env.example` 为 `.env`，填写独立的管理员密码，再启动服务。

```sh
docker compose up --build -d
```

容器以非 root 用户运行，发布到宿主机 `127.0.0.1:8781`，数据库位于命名卷 `workspace`。可运行 `docker compose exec rag python -m ragcore.accounts add analyst --role reader` 创建具名用户。容器中的 `127.0.0.1` 指向容器自身。宿主机模式通过 PowerShell 的 `$env:变量名` 或系统环境配置，Compose 从项目的 `.env` 传入配置。

默认容器只安装基础依赖。容器需要 OCR 时，在 `.env` 中设置 `RAG_OCR_ENABLED=1` 后重新执行 `docker compose up --build -d`。Compose 将同时启用构建参数 `WITH_OCR=1` 和运行开关，构建期间会检查依赖和模型文件。两种配置均通过 Compose 配置校验，镜像构建及容器内 OCR 仍待在有 Docker 引擎的目标环境验证。

本实现适合单工作区、单实例的企业内部试点。管理员和读者均可查询工作区的数据。管理员可查看全工作区历史，具名读者只能查看本人的历史，共享只读密码只显示当前会话历史。跨部门上线需要补齐单点登录、文档/数据行授权、备份恢复演练、容量与检索质量评测。高并发和高可用部署还需独立任务队列和数据库。文件原件、接口快照和会话记录均位于数据库，备份应使用 SQLite 在线备份，或停止服务后复制整个数据目录。

工作区内录入的模型密钥加密保存。Windows 使用运行服务账号的 DPAPI，迁移到另一台机器或另一个账号时应重新录入密钥。Linux/macOS 使用数据目录下权限为 `0600` 的 `.model-credentials.key`，恢复配置需要同时恢复数据库和此文件。数据库中的业务资料和历史需要由部署方单独保护。

## 离线验收

行业题集可在每道题的顶层 `evidence` 数组标注期望的文档证据。每项需填写 `filename`，可进一步写明 `sha256`、`locator` 或该测试库的 `chunk_id`。跨部署复用时优先用版本摘要和原文位置。[题集中的报账与报销案例](eval/seed_cases.jsonl)

```sh
python -m ragcore.evaluate --top-k 3 --report retrieval-report.json
```

JSON 报告逐项给出候选命中情况和最佳重排名次。汇总以有有效标注的预期证据总数为分母，分别计算候选召回率和前 K 名召回率。纯查数题、无正例题以及未标注题单列，失效的版本或位置会提示修正。小文库使用全量候选，测试大库的召回情况需使用超过 180 个有效分块的语料。

运行 `python -m ragcore.evaluate` 会在临时数据库导入演示文件和预算接口，逐题校验意图、结果状态、参数、引文编号、原文件摘要及原始接口快照。任一用例失败会返回非零退出码。复制 `eval/seed_cases.jsonl`，每行填写 `id`、`question`、`expect` 和可选的 `mode`、`params`，再用 `python -m ragcore.evaluate --cases 路径` 运行替换后的验收题。该结果只描述给定用例的通过情况，真实数据的查询准确率需要用独立业务样本评估。

安装项目依赖后，可运行自动化用例。

```sh
python -m unittest discover -s tests -v
```

安装 OCR 组件后，设置 `RAG_TEST_OCR=1` 再运行 `python -m unittest discover -s tests -p test_ocr.py -v`，会实际识别测试图片并通过问答、原件下载复核识别结果。
