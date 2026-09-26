# 实现安置入住容量预演与分阶段切换基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行、移交情景，以及入住容量预演与分阶段切换；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
```

三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。

## 入住容量预演与分阶段切换

专班（`taskforce` 角色）先冻结住房及公共服务容量版本，再提交不可变的家庭画像、目标曲线与回退方案：

```bash
# 1. 冻结容量版本（住房、学位、基层医疗、接驳四类来源）
curl -X POST http://127.0.0.1:8080/resource-versions \
  -H 'X-Actor-Id: tf' -H 'Content-Type: application/json' -d '{...}'

# 2. 确认计划（与容量预留同一事务；返回 prepare/pilot/expand/converge 四阶段）
curl -X POST http://127.0.0.1:8080/onboarding/plans \
  -H 'X-Actor-Id: tf' -H 'Content-Type: application/json' -d '{...}'

# 3. 阶段切换：登记回执 -> 上报四项指标 -> 门禁通过后推进；异常按方案回退
curl -X POST http://127.0.0.1:8080/onboarding/checkins   -H 'X-Actor-Id: tf' -d '{...}'
curl -X POST http://127.0.0.1:8080/onboarding/metrics    -H 'X-Actor-Id: tf' -d '{...}'
curl -X POST http://127.0.0.1:8080/onboarding/plans/<id>/advance
curl -X POST http://127.0.0.1:8080/onboarding/plans/<id>/rollback -d '{"to_stage": "..."}'

# 4. 查询每阶段容量来源、当前阻断原因与可回退范围（审计员可读）
curl http://127.0.0.1:8080/onboarding/plans/<id> -H 'X-Actor-Id: audit'
```

关键规则：

- **不可变输入**：家庭画像与规则在确认时以 SHA-256 哈希存证，确认后只能按冻结版本执行；
- **事务一致性**：计划确认与四类资源预留在同一个 `BEGIN IMMEDIATE` 事务内完成，容量不足整体回滚；
- **版本失效**：同一容量来源出现新的 `source_revision` 时，旧版本标记为 `superseded`，引用它的已确认计划立即 `invalidated`；
- **幂等回执**：回执与确认请求均以 `idempotency_key` 去重，重复回执返回首次结果、不重复扣减容量，同一家庭只能有一条有效回执；
- **门禁推进**：入住到位、四项指标（入住率、学位安置率、基层医疗服务率、接驳准点率）达标且下一阶段需求保留了要求的周转余量，才能开放下一批；阻断原因会写审计事件并可在计划状态中查询；
- **异常回退**：只回退阶段状态并标记 `rolled_back`，入住回执等证据原样保留；可回退范围由专班提交的 `rollback_policy` 限定；
- **写事务复核**：确认、推进、收口、回退、回执均在拿写锁后复核状态与门禁，避免并发下的超卖与越级推进。
