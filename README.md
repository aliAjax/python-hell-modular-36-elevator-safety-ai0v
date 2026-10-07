# 电梯与自动扶梯巡检和事件响应

这是一个只使用Python标准库和SQLite的模块化原型项目，默认端口为`8336`。领域对象包括设备、检验、维保、困人报警、救援任务、整改证据和恢复许可。`app.py`只负责参数解析、依赖组装和服务生命周期，业务状态机与约束集中在`src/rules.py`。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理；启动时自动恢复未完成的写入步骤。
- `src/domain.py`：角色、领域异常、身份解析和实体数据结构。
- `src/rules.py`：状态机、角色权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务、乐观锁、审计、幂等键、发件箱（outbox）和断网记录。
- `src/service.py`：用例编排、占用账协调、断网记录合并、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、JSON解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则、失败场景和占用账测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8336
```

服务启动时自动建表并恢复发件箱中未完成的操作。`--host`可修改监听地址，`--db`可指定其他SQLite文件。初始化服务不需要单独命令，首次启动即可访问：

```bash
curl http://127.0.0.1:8336/health
```

## 井道占用账（交叉作业协调）

两支维保班组在同一井道交叉作业时，一支遇急停或困人报警，另一支常不知道该停手还是继续。占用账把作业许可、维保任务、困人报警和救援任务接成一份账：

- 同一井道同时只放一张进场许可；发放新许可时旧许可自动失效，井道状态随之翻转。
- 急停或困人报警触发后：井道进入`alarm`，有效进场许可立即失效，井道内未确认撤离的在途维保任务转`pending_review`待复核。
- 维保任务通过`confirm_evacuation`确认撤离；报警未关闭、救援未完成或有未撤离任务时，井道不可释放。
- 全部清空后`release`释放井道，有效许可置为`evacuated`。

### 占用账接口

- `POST /api/hoistways`：创建井道（`name`唯一，可带`equipment_id`）。
- `POST /api/hoistways/<id>/issue-permit`：发放进场许可（`team`、`purpose`、`shift`、`seq`）。
- `POST /api/hoistways/<id>/report-alarm`：急停/困人报警（`code`、`occurred_at`、`team`）。
- `POST /api/hoistways/<id>/release`：清空后释放井道。
- `GET /api/hoistways/<id>/occupancy`：读取占用账（有效许可、在途/待复核任务、报警、救援、可否释放）。
- `POST /api/entities/<id>/actions`：维保任务`confirm_evacuation`确认撤离。

## 断网记录合并（按班次和序号）

井下或断网设备按`shift`（班次）和`seq`（序号）本地记录，回网后合并：

- 按班次分组、序号顺序应用；同一`(shift, seq)`重复上报判为重复，跳过不重复入账。
- 同键但内容不一致判为冲突，标记核对。
- 某班次序号不连续（缺号）判为漏传，列入`gaps`待补传。

接口：`POST /api/offline-records`，请求体`{"records":[{"shift":"s1","seq":1,"kind":"maintenance","payload":{...}}]}`；返回`applied`、`duplicates`、`conflicts`、`errors`、`gaps`。`GET /api/offline/gaps`可单独读取漏传缺口。

## 写入失败恢复（发件箱）

许可或任务等多步写入通过发件箱持久化每一步：已确认步骤落库保留，未完成步骤在原操作上重试，进程重启后由`recover()`接着处理，已完成步骤不重复执行。

- `POST /api/recover`：手动触发恢复，返回`recovered`/`failed`明细。
- 服务启动时自动调用一次`recover()`。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

身份通过`X-User-Id`和`X-Role`请求头传入，角色和动作权限由规则引擎校验。

## 核心流程

创建设备后安排检验、维保和困人报警；报警派发救援任务，完成后才能解决。整改证据通过复核后关闭，恢复运行许可必须基于有效的检验和已关闭整改。进场许可、维保任务、困人报警与救援任务通过井道占用账联动。

## 规则重点

- 同一设备编号不能重复创建；同一设备和故障代码不能同时存在多个未关闭报警。
- 组件更换维保必须填写`part_serial`。
- 恢复许可受设备状态、通过检验和未关闭整改共同限制。
- 同一井道同时只存在一张有效进场许可；报警触发后旧许可失效，未撤离维保任务转待复核。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

项目使用请求头模拟身份、SQLite单机持久化和简化状态机，适合原型演示和流程验证，不替代行业正式系统、设备控制系统或现场安全规程。
