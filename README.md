# 电梯井道占用账与事件响应

只使用 Python 标准库和 SQLite 的模块化原型，默认端口 `8336`。在原有设备、检验、维保、困人报警、救援任务、整改证据和恢复许可之上，新增**井道（shaft）/ 进场许可（work_permit）/ 井道维保任务**与一份**占用账**，解决两支维保班组在同一井道交叉作业时"报警来了不知道该不该停手"的问题。

## 核心规则

- **同一井道同时只放一张进场许可**：`work_permit` 发放（`issue`/`activate`）时互斥，并有部分唯一索引 `status IN ('issued','active')` 在数据库层兜底。
- **进场必须持有效许可**：维保任务 `start` 要求存在同井道、同班组且处于 `issued/active` 的许可；井道有进行中的报警/救援时禁止进场。
- **状态一变，旧许可失效**：井道一旦出现未关闭的困人报警或新派救援，占用中的许可自动转为 `rescue_stop`（状态改变即作废旧许可，不能继续使用）。
- **没确认撤离的任务转待复核**：报警/救援触发时仍在井道内（`in_progress`）的维保任务自动转 `review_pending`；复核确认撤离（`review_evacuated`）后才能 `complete`；复核要求恢复（`review_resume`）时必须仍有有效许可且井道无救援。
- **占用账**：许可、任务、报警、救援的每次状态变化都追加到 `occupancy_log`，`GET /api/shafts/<id>/account` 返回当前占用状态（`available / permit_issued / occupied / rescue`）、关联对象和完整事件时间线。

## 断网回网：按班次和序号合并

`POST /api/offline-records` 接收班组终端的断网记录，每条必须带 `source_id`（班组/设备）、`shift_id`（班次）、`seq`（班内序号，从 1 开始）：

- **漏传**：可用 `shift_end_seq` 声明本班次末序号，服务端算出缺失序号返回 `gap`；补齐后再次回传自动重放。
- **重复**：同 `(source_id, shift_id, seq)` 重传且内容一致自动去重；内容不一致计入 `content_conflicts`，班次状态为 `conflict`，需人工核对。
- **按序重放**：记录中的 `request`（`create` / `transition` / `log`）按序号排成持久化操作步骤；前面序号缺失时后续步骤保持 `blocked`，补齐后继续，不会越序执行。

## 许可/任务写入失败：保留已确认步骤，断点续跑

- 所有重放/批量写都进持久化操作队列（`operations` + `operation_steps`），每步独立提交并记录 `confirmed/failed/blocked`。
- 某步失败：前面已确认的步骤保留，可修正后用同一操作 ID 重试（`POST /api/operations/<id>/retry`），已确认步骤不会重复执行。
- **重启接着处理**：服务启动时自动 `retry_pending()`，把 `pending/blocked`（缺口已补齐）的操作继续跑完。
- 直接业务接口 `POST /api/<kind>` 与 `POST /api/entities/<id>/actions` 仍保持单请求事务语义；操作队列面向断网回网与多步骤批量写入。

## 模块结构

- `app.py`：参数解析、依赖组装、启动（含未完成操作恢复）和信号处理。
- `src/domain.py`：角色、领域异常、实体数据结构。
- `src/rules.py`：状态机、角色权限、井道互斥与进场/复核校验。
- `src/repository.py`：SQLite 建表（实体、审计、占用账、操作队列步骤、离线记录/班次）、事务、乐观锁、许可互斥部分唯一索引。
- `src/service.py`：用例编排、井道联动冻结、占用账事件、操作队列断点续跑、离线班次序号合并。
- `src/http_api.py`：HTTP 路由、JSON 解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `tests/`：完整流程、规则、占用账联动、离线合并与失败恢复测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8336
curl http://127.0.0.1:8336/health
```

身份通过 `X-User-Id` / `X-Role` 请求头传入（角色：`admin`、`inspector`、`dispatcher`、`maintenance`、`viewer`）。

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| GET/POST | `/api/shafts` | 井道台账 |
| GET/POST | `/api/work_permits` | 进场许可（申请：`requested`） |
| POST | `/api/entities/<id>/actions` | 状态动作（`issue/activate/confirm_evacuation/close/void` 等） |
| GET | `/api/shafts/<id>/account` | 井道占用账：当前状态 + 关联许可/任务/报警 + 事件流 |
| POST | `/api/offline-records` | 离线记录按班组+班次+序号合并 |
| GET | `/api/offline-shifts` | 各班次漏传/重复/冲突核对结果 |
| POST | `/api/operations` | 提交持久化操作（`create` / `transition` / `batch`），返回 200 完成 / 202 未完成 |
| GET | `/api/operations`、`/api/operations/<id>` | 操作及步骤状态 |
| POST | `/api/operations/<id>/retry` | 保留已确认步骤，重试未完成项 |
| GET | `/api/audit` | 审计记录 |

许可状态机：`requested → issued → active → evacuated → closed`；报警/救援介入时 `issued/active → rescue_stop`，或申请被记为 `blocked_by_rescue`，均可 `close`；`void` 为人工作废。

维保任务状态机：`planned → in_progress → evacuated → completed`；救援介入时 `in_progress → review_pending`，之后 `review_evacuated → evacuated` 或 `review_resume → in_progress`（需有效许可且无救援）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

请求头模拟身份、SQLite 单机持久化和简化状态机，仅用于原型演示与流程验证，不替代行业正式系统、设备控制系统或现场安全规程。对已落盘的旧数据库，修改部分唯一索引定义需删除旧索引后重启（新库自动生效）。
