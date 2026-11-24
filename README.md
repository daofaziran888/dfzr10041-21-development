# 拾音工坊 · 日志采集与事件流处理平台

面向多租户应用日志的结构化采集、事件流解析、格式标准化、敏感脱敏、采样归并、异常告警与归档回溯的日志采集与事件流处理平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 日志源 | `log_source` | 日志来源 | 源编号、源名称、应用标识 |
| 解析规则 | `parser_rule` | 解析方案 | 方案编号、日志来源、正则表达式 |
| 脱敏规则 | `mask_rule` | 脱敏配置 | 规则编号、目标字段、脱敏方式 |
| 事件分类 | `event_type` | 事件类型 | 类型编号、类型名称、分类标签 |
| 采集作业 | `ingest_job` | 采集任务 | 作业编号、日志来源、采集模式 |
| 批次检查点 | `batch_checkpoint` | 检查点记录 | 记录编号、采集作业、文件路径 |
| 事件存储 | `event_store` | 事件记录 | 记录编号、事件类型、原始内容 |
| 异常告警 | `anomaly_rule` | 告警规则 | 规则编号、事件类型、检测条件 |
| 采样归并 | `sample_job` | 采样计划 | 计划编号、事件类型、采样比例 |
| 字段增强 | `field_enrich` | 增强规则 | 规则编号、目标字段、增强来源 |
| 归档策略 | `archive_policy` | 归档方案 | 方案编号、事件类型、保留天数 |
| 转发路由 | `forward_rule` | 转发规则 | 规则编号、事件类型、目标地址 |
| Schema版本 | `schema_version` | 版本记录 | 版本编号、日志来源、字段变更 |
| 去重过滤 | `dedup_filter` | 去重规则 | 规则编号、事件类型、去重字段 |
| 采集指标 | `ingest_metric` | 指标记录 | 记录编号、日志来源、采集速率 |
| 事件重放 | `replay_record` | 重放任务 | 任务编号、来源事件、重放范围 |
| 质量报告 | `quality_report` | 质量评估 | 报告编号、日志来源、完整率 |
| 标签分类 | `tag_taxonomy` | 标签定义 | 标签编号、标签名称、父级标签 |
| 打包导出 | `export_bundle` | 导出包 | 包编号、事件类型、时间范围 |
| 访问审计 | `audit_event` | 审计记录 | 记录编号、操作模块、操作人 |
| 租户隔离 | `tenant_isolation` | 租户配置 | 租户编号、租户名称、日志来源 |
| 格式适配 | `format_adapt` | 适配器配置 | 适配器编号、输入格式、输出格式 |

## 样本覆盖对照与补抽

`sample_job` 与 `dedup_filter` 页面顶部提供同一个可展开的“样本覆盖对照明细”：

- 按来源显示原始分母、`原始 → 去重 → 已归并` 的实际数目；
- 展开来源可查看每条原始行、去重重复组/代表、归并组/代表和待补原行；
- 明确列出去重挤掉的原行、归并合并的原行、额度未覆盖的组；
- 补抽按实际采用明细选择未覆盖组数最多的来源，并在组内补最早待补原行；
- 采样交付台账与覆盖回执独立确认。已派出的原行不会重复发送；重启后先核对交付，再决定剩余缺口；
- 同一轮并发确认由锁和幂等键去重，只保留一个补抽结果。

运行题设剧本（初始 A/B/C 数据、C1 重启核对、B2 分组更正、两轮并发确认）：

```bash
PYTHONPATH=backend backend/.venv/bin/python backend/scripts/run_coverage_scenario.py
```

接口：

```text
GET  /api/sample-coverage
POST /api/sample-coverage/deduplicate
POST /api/sample-coverage/sample
POST /api/sample-coverage/claims
POST /api/sample-coverage/claims/{id}/sample
POST /api/sample-coverage/claims/{id}/reconcile
POST /api/sample-coverage/claims/{id}/confirm
POST /api/sample-coverage/restart
POST /api/sample-coverage/correction
POST /api/sample-coverage/scenarios/run
POST /api/sample-coverage/reset
```

补抽状态会原子写入 `SAMPLE_COVERAGE_STATE_FILE`（默认 `/tmp/shiyin-sample-coverage-state.json`），因此杀掉并重启 FastAPI 进程后，已派出的 C1 和未确认领用单仍可通过交付台账核清。

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据采集、爬虫与RPA
