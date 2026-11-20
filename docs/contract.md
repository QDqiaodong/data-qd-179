# 学分与课程资格审核

## v1（无 `transfer` 字段，`schema_version: 1`）

输入一个培养方案program、课程courses、合成学生students、考试attempts及as_of。课程只有一个category、正整数credits、0..100及格线pass_mark和无环且无重复的prerequisites。最大200课程、2000学生、20000次考试。方案required_courses为已有课程的无重复子集；minimum_credits为已有category的非负整数下限。考试有唯一id、student_id、course_id、规范YYYY-MM-DD日期和0..100整数mark；同一学生/课程/日期不得重复。未知引用或先修环拒绝整个输入。

按日期分组判定每名学生考试：先修必须在严格更早日期有合格pass；同日先修不解锁当日后继课程。先修不满足的考试记blocked_prerequisite，即使分数很高也不得取得学分或解锁下游；先修满足但低于及格线记failed。as_of之后考试先校验结构但不参与成绩资格、学分或先修判断，记ignored_future_attempts。无先修根课程正常参与。

各课只计一次，选择最高的合格及格分；同分按最早日期/ID。重修不累加学分。credited_weighted_mark是已计学分课程的学分加权分数（Fraction字符串），不等同GPA，未及格课程不进入该数值。毕业资格同时要求全部required_courses和所有类别学分下限。eligible_uncredited_courses以截止日已取得的先修课凭据列出尚未取得学分但能报读的课，不考虑未来考试。

## v2（出现顶层 `transfer`，`schema_version: 2`）

### 输入契约

`transfer` 为必含 `evidence`、`options`、`credit_limit` 三键的对象，与 v1 字段并列；其余 v1 上限与规则不变。

- `evidence`：外校凭据数组，每项 `{id, student_id, date}`，`date` 为学习完成日（规范 YYYY-MM-DD）。id 在数组内全局唯一，最多 20000 条；`student_id` 必须存在。凭据允许不被任何方案引用。
- `options`：可认定方案数组，每项 `{id, student_id, evidence_ids, course_ids, effective_date}`。id 在数组内全局唯一，最多 2000 条；每名学生最多 12 个方案。
  - `evidence_ids`、`course_ids` 均为非空、无重复的字符串数组，引用必须存在；每项凭据必须属于该方案学生，课程必须是现有本校课程。
  - `effective_date` 为规范日期，且不得早于所引用任一凭据的 `date`。
- `credit_limit`：非负整数，拒绝布尔值，按学生分别适用。
- 未来凭据、未来方案同样完整校验，结构错误一律使整个输入失败；截至日不可用不会让坏数据被忽略。
- 缺失字段、错误类型、重复 ID、无效引用、非规范或越界日期等都使整个输入失败（退出码 2，不产生报告）。

### 整包认定与可行性

- 方案不可拆分：选中后其全部凭据被占用、全部 `course_ids` 被认定；不存在逐条接受后再扣重的路径。
- 仅 `effective_date <= as_of` 的方案可被选中；未来方案永不入选，但仍是完整校验对象。
- 同一学生的选中方案之间既不能共享凭据，也不能共享本校课程。
- 方案的额度成本是其全部目标课程**原有 credits 之和**；即使目标课程另有本校及格成绩，仍消耗完整额度。所选方案成本总和不得超过该学生的 `credit_limit`。
- 不存在必须采用的方案；空方案集总是允许，没有可用方案时正常输出空 `recognition_plan`，不报业务错误。

### 逐日先修与学分

- 转学分等同于在 `effective_date` 获得对应本校课程的及格凭据：无数值成绩，且不补验目标课程自身的先修（外校等效资格认定）。多课程方案的目标课程同日同时生效。
- 转学分仅解锁**严格晚于** `effective_date` 的本校考试，同日不解锁；同日的本校考试之间也依旧互不解锁。
- 本校考试继续按 v1 逐日规则判定。缺先修的高分考试记 `blocked_prerequisite`，不计学分、不解锁下游；后来的转学分或后来的本校及格都不能追溯修复它。
- 选定方案后从头重审所有截至日考试；不能用尚未取得资格的本校成绩反过来证明自己的先修。
- 一门课最终只计一次学分。有合法本校及格时，按“最高分、同分最早日期、再按考试 ID”的 v1 规则取本校来源；即使转学分曾在更早日期解锁过其他课程，那段早期先修事实仍然保留。没有合法本校及格但被选中方案覆盖时，以转学分为最终来源。
- 两类来源都使用本校课程的 credits/category，参与 required_courses、类别下限、毕业资格与 eligible_uncredited_courses 的计算。
- `credited_weighted_mark` 仅对最终来源为本校考试的课程做学分加权，仍为 Fraction 字符串；没有此类课程时为 `null`。转学分不以零分填补，也不进入分母。

### 全局最优选择

每名学生独立枚举可行方案集，并对每个候选集执行上述完整考试重审，按以下优先级逐级比较，取最优：

1. 能够毕业（`graduation_eligible`）；
2. 已覆盖 required_courses 的课程数更多；
3. 已满足的类别学分更多：对 `program.minimum_credits` 每个键取 `min(实得该类别学分, 该类别下限)` 后求和，未设下限的类别不贡献；
4. 最终总学分更多；
5. 认定额度成本更少；
6. 选中方案个数更少；
7. 仍平局时，把每组方案 ID 按 Python 字符串顺序排序，取列表字典序最小者。

前四项使用该组方案实际引发的完整审核结果，而非只计算直接转入学分。输入排列顺序不影响选中方案与报告；不修改培养方案规则来制造可行解。

### v2 报告

- 保留 v1 报告与 `summary` 的全部指标；summary 的学分、毕业人数、阻断考试数均来自最终选定方案对应的重审结果。
- 每名学生增加：
  ```json
  "recognition_plan": {
    "option_ids": ["..."],
    "charged_credits": 0,
    "objective": {
      "graduation_eligible": false,
      "required_covered": 0,
      "capped_category_credits": 0,
      "credits": 0
    }
  }
  ```
  `option_ids` 升序且只含最终选中项；`objective` 为上述前四项的实际值。空方案集也照常输出。
- `selected_passes` 每项保留 `course_id`、`attempt_id`、`mark`、`credits`，并增加：
  - 本校来源：`source="exam"`，保留原 `attempt_id`/`mark`，`option_id=null`；
  - 转学分来源：`source="transfer"`，`attempt_id=null`、`mark=null`，`option_id` 为提供该课的选中方案 ID。
- `selected_passes` 仍按课程 ID 排序；其余列表沿用 v1 排序规则。不添加无法对应输入的伪考试。
- `recognition_plan` 加上输入和原有 `attempt_audit` 应足以重现该学生结果。

## 通用边界

由相同合格事实（v1 考试，v2 考试加整包认定）驱动成绩、学分、报读和毕业结果，保留每次考试资格原因与选中来源。单次离线方案审核，调用者给定规则，不等于任何真实学校政策。CLI 只原子新建报告；错误退出 2，旧目标不动；输出已存在时保持原目标不变，成功为原子新建。
