# data-qd-179 · 学分与先修课审核

按考试日期核先修资格与重修选分，联合计算类别学分、可报读课程和培养方案毕业资格。输入含顶层 `transfer` 时启用 v2 转学分组合认定：在互斥的整包认定方案中选择全局最优、可追溯的一组，并用该方案从头重审全部考试。另提供 `credit-plan` 小班群体毕业修读计划：在不可变的当前审核之上，联合求解最多 3 名学生在最多 3 个未来学期中的同时可履行选课安排，兼顾跨学期先修、个人学分负荷与共享班额，并给出精确最优与个人可行性区分。

新增 `credit-ledger` 可回放的成绩更正台账：初始化固定 v1/v2 输入目录后持续接收 attempts 的来源更正（补报、改分、撤销），每个成功批次取得严格连续的接收序号；既按最新已知成绩重新审核，也能回答"在当时已收到的信息下、某个截至日会得到什么结果"。本地 SQLite 持久化，跨进程原子提交与精确重放，不使用外部服务。

另提供 `credit-disclosure` 院系×入学届毕业资格公开简报：沿用同一份 v1/v2 审核快照，按教务优先序发布真实精确人数或固定区间；每个受保护小组在整份简报发布后仍有至少两个可能值，已公开事实则用 `prior` 累积，绝不枚举完整候选表。

另提供 `credit-reconcile` 匿名答卷对账与毕业结论核验：阅卷批次在只允许给定候选链接、答卷与座位各用一次的约束下，先最大化匹配对数再最大化签收（verified）链接数，保留全部并列最优匹配；按学生对相关批次的本地投影枚举可能归档并从头重走 v1/v2 审核（不构造整届笛卡尔积，单生审核次数恒有 4³=64 的上界），输出学分区间、毕业确定性与可复算见证。

## 独立安装与测试

Python >=3.11，本批实测3.12.14。运行与unittest仅用标准库，构建工具精确固定。每仓独立venv，可复用公共下载缓存。

```sh
uv venv --python 3.12 .venv
uv pip install --offline --python .venv/bin/python -r requirements-build.lock
uv pip install --offline --python .venv/bin/python --no-build-isolation --no-deps .
uv pip check --python .venv/bin/python
.venv/bin/python -I -m unittest discover -s tests -v
```

以上离线命令需要缓存已有setuptools==80.9.0和wheel==0.45.1；无缓存时先准备这两个公共构建工具，不声称空缓存可离线安装。修改源码后重装本仓。

## 端到端演示

在仓库根目录执行，输出路径必须不存在。重复演示请换新路径。

```sh
.venv/bin/credit-audit --source fixtures/input.json --output run/report.json
.venv/bin/credit-audit --source fixtures/input_v2.json --output run/report-v2.json
.venv/bin/credit-plan --source fixtures/input_plan.json --output run/report-plan.json
.venv/bin/credit-ledger init --store run/ledger.db --source fixtures/ledger_init.json
.venv/bin/credit-ledger ingest --store run/ledger.db --source fixtures/ledger_batch_x.json
.venv/bin/credit-ledger audit --store run/ledger.db --known-seq 0 --output run/ledger-h0.json
.venv/bin/credit-reconcile --source fixtures/input_reconcile.json --output run/report-reconcile.json
.venv/bin/credit-disclosure --source fixtures/input_disclosure.json --output run/report-disclosure.json
```

v1 预期：2名合成学生、1名符合毕业条件、已计12学分、1次考试因先修不足被阻断，报告 `schema_version` 为 1。

v2 预期：报告 `schema_version` 为 2；学生 synthetic-A 的互斥方案中选中 `zz`（认定 A，解锁 03-01 的 C 考试），已计 9 学分并毕业，加权成绩 `230/3`；synthetic-B 仅有未来方案，输出空 `recognition_plan`，不报业务错误。

群体计划预期（`input_plan.json`）：s1 截至日仅通过 B、s2 无学分，优先序 `[s1, s2]`；t1 仅开 A（班额 1），t2 开 A、B（各班额 1），两期个人上限均为 3 学分。若让 s1 先占 t1 的 A，s2 无法在 t2 同时修两门；精确最优把 t1 的 A 让给 s2，s1 在 t2 修 A、s2 在 t2 修 B，两人均在第 2 期毕业，新增 9 学分、3 人次。报告 `schema_version` 为 1，`baseline_audit` 为完整原审核（含未参与规划的 s3），`objective` 为 `{graduates: 2, graduation_flags: [1, 1], completion_indices: [2, 2], planned_credits: 9, enrollments: 3}`。

台账预期（`ledger_init.json` 等）：init 输出 `{"seq": 0}`；批 X（撤销 a）取得 seq=1 后 b 被先修阻断；`--known-seq 0` 的历史审核依然可毕业。更多更正批次与 v2 重新认定演示见[台账文档](docs/ledger.md)。

对账预期（`input_reconcile.json`）：3 个阅卷批次，`wave-amber` 的高分/低分答卷与甲、乙的 A 课座位两两可链接且无签收，两个最优匹配并列；甲因后来的 B 考试是否被先修阻断而为 `needs_review`（学分区间 0..6），两份见证中 B 的状态分别为 `passed` 与 `blocked_prerequisite`；丙的一份及格 D 答卷可去两个座位之一，归属不确定但确定取得 2 学分、毕业结论为 `ineligible`；甲的未来 D 座位照常对账却计入 `ignored_future_attempts`。规则、见证口径与审核次数界限见[对账文档](docs/reconciliation.md)。

简报预期（`input_disclosure.json`）：2个院系×2个入学届、9名合成学生；`design/2023` 的 2 人中恰 1 人毕业已先公开，受保护格为 `computing/2024`。按请求顺序输出：`computing` 合计与 `computing/2023` 发 `[2,3]`，`design` 合计精确 2，全校合计发 `[4,5]`，`2024` 列合计隐藏，`computing/2024` 发 `[0,1]`。

输入或业务规则无效、输出已存在时退出2，保留原目标；成功退出0。报告/数据库先完整生成再原子新建，无覆盖。run/为忽略目录。

## 契约与边界

详见[数据契约](docs/contract.md)、[台账文档](docs/ledger.md)、[对账文档](docs/reconciliation.md)与[简报文档](docs/disclosure.md)。无 `transfer` 时完全兼容 v1 的输入、报告字段、排序与错误行为；出现 `transfer` 时按 v2 契约输出 `schema_version: 2`，新增整包认定可行性、逐日重审、全局最优选择和 `recognition_plan`。含 `planning` 时由 `credit-plan` 输出独立的 `schema_version: 1` 群体计划报告，内嵌完整原审核作为 `baseline_audit`；原 `credit-audit` 与 `registrar.service.run` 的行为对既有输入保持不变。含必填 `disclosure` 对象时由 `credit-disclosure` 输出独立的 `schema_version: 1` 简报：登记人数表、按 `requests` 顺序的精确/区间/隐藏条目，不含逐生信息或原审核报告；原 `credit-audit`、`credit-plan`、`credit-ledger`、`credit-reconcile` 的入口、规则与结果不变。含必填 `reconciliation.waves`（允许空数组）时由 `credit-reconcile` 输出独立的 `schema_version: 1` 对账报告，原 `credit-audit`、`credit-plan`、`credit-ledger` 的入口、规则与结果不变。`credit-ledger` 在固定目录之上只更正 attempts，接收序号与截至日两条轴独立回放；课程及及格线由输入给定，规则是本仓明确模型；不代表学校政策，不含真实学生数据。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
