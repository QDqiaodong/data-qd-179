# 可回放的成绩更正台账（credit-ledger）

教务系统会反复补报、改分和撤销考试记录，网络重试会重复投递，来源修订也可能
乱序到达。台账在初始化时固定一份合法 v1/v2 审核输入的目录，之后只接收
attempts 的来源更正，为每个成功批次分配严格连续的接收序号，从而同时回答
两种口径的问题：

- **当前口径**：按最新已知成绩重新审核（`known_seq` 取当前序号）；
- **历史口径**：在当时已经收到的信息下，某个截至日会得到什么结果
  （`known_seq` 取历史序号，`as_of` 取该截至日）。

接收序号与截至日是两条独立的轴：今天补报过去的及格成绩会改变今天所知的
过去审核，但不能改变早前接收序号的结果；已收到但晚于 `as_of` 的考试仍按
原规则校验并计入 `ignored_future_attempts`。

## 服务入口

`registrar.ledger` 提供三个函数，`store` 均为台账文件路径：

- `initialize(store, data)` → `{"seq": 0}`：完整校验现有合法 v1/v2 审核
  输入后建立序号为 0 的台账。原始 `courses`、`students`、`program`、可选
  `transfer` 和默认 `as_of` 从此固定；原始每条 attempt 是该考试 ID 的
  revision=0。已存在的 store 一律拒绝并保留原内容；失败的初始化不留下
  可被当成成功台账打开的状态（先在临时文件中完整建库，再原子链接到目标
  路径，链接失败即清理）。
- `ingest(store, batch)` → 回执字典：接收一个更正批次，见下文契约。
- `audit(store, *, known_seq=None, as_of=None)` →
  `{"known_seq", "as_of", "versions", "audit"}`：回放审核，见下文。

台账只更正 attempts：学生身份、课程、培养方案、转学分凭据与方案都不能
通过更正更改。

## 批次契约

批次为 `{"batch_id", "expected_seq", "changes"}`，不允许契约之外的字段：

- `batch_id`：非空字符串（沿用原项目 ID 口径）。
- `expected_seq`：非负整数，拒绝布尔值；乐观并发前提，见下文。
- `changes`：最多 1000 项的数组，可以为空。每项必含 `attempt_id`、
  `revision`、`student_id`、`course_id`、`op`：
  - `revision` 为正整数（拒绝布尔值）；`op` 只取 `upsert` 或 `withdraw`。
  - `upsert` 另须 `date`、`mark`，采用原考试日期及分数口径（规范
    YYYY-MM-DD、0..100 整数、拒绝布尔值）；`withdraw` 不得含 `date` 或
    `mark`。
  - 学生和课程必须在初始化固定的目录中。
- 同一 `changes` 中不能重复 `(attempt_id, revision)`。

### 来源事实与身份

`(attempt_id, revision)` 在整个台账中表示同一条来源事实：以另一个
`batch_id` 再次投递完全相同的事实允许（按字段值比较，JSON 对象键顺序
不同不判冲突）；内容不一致则整批拒绝，即使这一 revision 早已被更高版本
压过。批中所有项均独立校验，不跳过旧版本、未来日期或最终未胜出的记录。

某个 `attempt_id` 的 `student_id`/`course_id` 在首次被台账知道后永远不变，
即使曾撤销、收到更高修订或该次输入只是过期修订：不许借更正把一次考试转给
另一学生或另一课程。首次看到某个新考试 ID 可以是 `withdraw`，它建立该 ID
的身份及撤销状态，随后更高修订可恢复。全台账已知考试 ID（包括撤销者）
最多 20000 个；原有课程、学生、transfer 与单个输入 JSON 的限制继续适用。

### 可见版本与整批投影校验

在某个接收序号 S，一次考试的可见版本是 initialize 及所有 seq≤S 的成功
批次中该考试已收到的**最大 revision**，不是最后到达的一条。可见版本为
`withdraw` 时该考试不进入审核；为 `upsert` 时以这一版本的 date/mark 和
固定身份形成原格式 attempt。较低版本可以晚到，也可以在 `withdraw` 之后
到达，仍被完整保存与校验，只是不改变当前投影。

一次新批次须以其全部 changes 共同作用后的可见状态通过原审核输入校验，
包括所有当前可见考试的学生/课程/日期唯一性：同一批中交换两次考试的日期
（最终不重复）合法；只移动其中一次（最终重复）则整批失败。被覆盖的旧
版本不再参与当前的同日冲突，但其历史事实永久保留、可回放。

### 并发前提与重放

每个首次成功的 `batch_id` 取得一个严格连续的新 seq，包括空批、全是重复
事实或全部落后于当前版本的批次；失败不消耗 seq。不同批次的 seq 按实际
提交顺序决定。

- 对尚未成功接收的 `batch_id`：仅当 `expected_seq` 等于提交时当前序号才
  可提交，不相等则整批拒绝。
- 对已成功接收的 `batch_id`：先判重放身份——若 `expected_seq` 与
  `changes` 的完整内容均与首次接收相同（changes 的排列没有业务意义，
  重排后视为相同批次），直接返回首次保存的原回执，不检查它如今是否
  过期，也不分配序号；否则报 `batch_id` 冲突。

无效输入、修订冲突、身份冲突、当前投影冲突和并发前提失败均不保存部分
事实、不抢占 `batch_id`、不改变 seq；修正后的同名批次仍可成功。

### 回执

`ingest` 返回并持久保存 `{batch_id, seq, previous_seq, changed}`。
`changed` 按 `attempt_id` 排序，每项
`{attempt_id, before_revision, after_revision, withdrawn}`，只列本批令
最大可见 revision 上升或令新 ID 首次可见的考试；之前未知时
`before_revision=null`，`after_revision` 为本次最大可见 revision，
`withdrawn` 表示提交后是否撤销。较低版本补到或事实重复不列入；即使高
版本只改了来源 revision 而 date/mark 未变，也要列入——回执描述来源版本
变动，并不冒充审核结果差异。回执在其 batch 原 seq 下永久固定。

## 回放审核

`audit` 返回 `{known_seq, as_of, versions, audit}`：

- `known_seq` 缺省为调用开始时选定的一次完整已提交当前序号；显式给出时
  必须是 0..当前序号的整数，越界报错而不夹取。
- `as_of` 缺省为初始化输入的默认日期，也可指定任意规范 YYYY-MM-DD。
- `versions` 按 `attempt_id` 排序，覆盖该 `known_seq` 时已知的所有 ID
  （包括撤销者），每项 `{attempt_id, revision, withdrawn}`；该时点尚不
  存在的考试不出现。
- `audit` 是使用该接收序号的可见未撤销考试、固定的其他原输入、指定
  `as_of` 调用原审核（`registrar.service.run`）得到的完整报告：真正重走
  先修资格与 v2 认定最优选择。改低或撤销 A 的早期及格会级联令 B、C 失去
  先修资格；更正后的早期及格可以解锁后来的本校考试；同日通过仍不互相
  解锁。同一 `known_seq` 配不同 `as_of` 也可能选出不同的转学分方案。

历史查询不使用当前的赢家或当前的转学分选择，也不回写当前状态；除来源
投影外，报告的字段、排序、分数和毕业口径与原审核完全一致。

## 持久化与故障语义

台账是 store 路径上的单个 SQLite 文件（WAL 模式），新进程可直接继续打开：

- `meta`：格式标识与当前已提交序号。
- `catalog`：初始化固定的原输入目录（JSON）。
- `facts`：全部来源事实，主键 `(attempt_id, revision)`，只在提交时追加，
  另有序号索引供历史回放。
- `current_state`：当前投影，每个已知考试 ID 一行（固定身份与当前可见
  版本），不超过 20000 行。
- `batches`：每个成功批次一行（seq 主键、`batch_id` 唯一索引、重放身份
  的规范化内容、永久固定的回执 JSON）。

接收在 `BEGIN IMMEDIATE` 事务内完成全部校验与提交：同一 store 上两个
独立进程以相同 `expected_seq` 接收不同 `batch_id`，至多一个成功；WAL
快照保证读者只读到完整提交之前或之后的状态，不会混合不同提交的版本与
审核。进程在持久化期间被终止后，重新打开台账得到未提交或完整提交的一
种状态；若提交已落盘但调用者尚未拿到响应，用原批重试即可按 `batch_id`
索引找回原回执。允许留下不参与任何可见结果的临时文件（如 WAL 侧文件）；
不承诺在硬件断电、磁盘损坏或人工改写台账后继续服务。

### 增长约束

已知考试 ID 不超过 20000，成功批次与修订累计可达 100000。新批接收只
读取：当前序号、`batch_id` 索引点查、当前投影整体（≤20000 行）、固定
目录，以及每条 change 一次 `(attempt_id, revision)` 主键点查；写入量随
批大小增长。旧 `batch_id` 的精确重试只读 `meta` 与一条 `batches` 索引
记录。两者都不线性读取或重写全部历史事实、全部旧回执或全部旧快照；
历史 `audit` 允许回放（按序号索引范围扫描），不要求增量审核。测试
`tests/test_ledger_concurrency.py` 用逐表行数统计与
`EXPLAIN QUERY PLAN` 给出可观测证据：在远超活跃投影规模的合成历史下，
接收与重试的读取量与历史总量无关。

## 命令行

安装后提供 `credit-ledger`，三个子命令：

```sh
credit-ledger init   --store PATH --source JSON
credit-ledger ingest --store PATH --source JSON
credit-ledger audit  --store PATH [--known-seq N] [--as-of DAY] --output JSON
```

- `init`/`ingest` 成功退出 0，向 stdout 输出一份 JSON 返回值
  （`{"seq": 0}` 或回执）。
- `audit` 成功退出 0，沿用原项目原子新建报告的方式写出完整返回值
  （不覆盖已有目标），stdout 输出一行定位摘要。
- 错误退出 2 并给出可识别原因，不输出伪成功结果；输入 JSON 沿用原有
  防重复键、非有限数与 8 MB 大小限制的加载方式。
- `ingest` 的提交不依赖额外报告文件发布；调用者未拿到 stdout 时用原批
  重试即可找回原回执。

## 端到端演示

```sh
credit-ledger init --store run/ledger.db --source fixtures/ledger_init.json
credit-ledger audit --store run/ledger.db --output run/ledger-r0.json
credit-ledger ingest --store run/ledger.db --source fixtures/ledger_batch_x.json
credit-ledger ingest --store run/ledger.db --source fixtures/ledger_batch_y.json
credit-ledger ingest --store run/ledger.db --source fixtures/ledger_batch_z.json
credit-ledger audit --store run/ledger.db --output run/ledger-r3.json
credit-ledger audit --store run/ledger.db --known-seq 0 --output run/ledger-h0.json
```

预期与规格用例一致：seq=0 可毕业；批 X 撤销 a 后 b 被先修阻断、不能
毕业；批 Y 只补到更旧的 revision=1，`changed` 为空仍取得 seq=2；批 Z
把 a 恢复为与 b 同日的 95 分，a 计入学分但同日 b 仍被阻断；`--known-seq 0`
的历史查询依然可毕业。v2 演示见 `fixtures/ledger_init_v2.json` 与
`fixtures/ledger_batch_v2.json`：补报本校 A 及格后，最优认定方案由
`["zz"]` 变为 `["aa"]`，而 seq=0 的历史方案保持不变。
