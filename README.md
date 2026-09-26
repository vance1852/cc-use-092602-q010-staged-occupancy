# 实现安置入住容量预演与分阶段切换基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行、移交情景，以及城镇新区入住容量预演与分阶段切换；
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

城镇新区接收多个村的家庭时，住房、学位、基层医疗和交通接驳必须按阶段增长，而不是只做单一总量预测。平台在 `rural_allocation` 内提供以下保证：

1. **冻结输入**：专班先发布不可变的资源版本（`POST /resource-versions`，住房/学位/医疗/接驳四类资源的交付存量与每日周转增量），再提交不可变家庭画像批次（`POST /household-batches`，内容哈希、只追加）。
2. **四阶段预演**：`POST /staging-plans` 携带目标曲线（准备、试入住、扩容、收敛的日期、入户数与指标门槛）、周转余量百分比和回退方案（最大回退深度、是否保留证据），系统按冻结版本计算每阶段容量来源（存量+周转累积）、需求分配和阻断原因。
3. **确认即预留**：`POST /staging-plans/{id}/confirm` 在单事务内完成可行性复核与逐阶段容量预留；预演存在阻断或该版本已被其他计划占用时整体拒绝。
4. **双闸门推进**：`POST /staging-plans/{id}/advance` 同时要求当前阶段服务指标达标率和下一阶段在推进日的周转余量；收敛阶段达标后经 `.../complete` 收尾。
5. **幂等回执**：入住回执（`POST /intake-receipts`）按家庭画像派生四类需求并扣减预留；指标回执（`POST /metric-receipts`）登记服务量。两者均按 `idempotency_key` 幂等，重复回执回放原结果，不重复扣减容量。
6. **异常回退**：`POST /staging-plans/{id}/rollback` 只能回退到方案允许的阶段深度，释放未被入住证据占用的预留，已发生的入住回执永久保留并继续占用容量；`GET /staging-plans/{id}/rollback-window` 查询可回退范围。
7. **版本失效**：发布新资源版本会自动把引用旧版本的未终结计划标记为 `invalidated`，不得再推进或据此新建计划。
8. **可追溯查询**：`GET /staging-plans/{id}/capacity-sources` 返回每阶段容量来源及预留/已用/已释放量，`.../blockers` 返回预演阻断与历次闸门评估结果，全部动作进入与既有业务共用的审计哈希链。
