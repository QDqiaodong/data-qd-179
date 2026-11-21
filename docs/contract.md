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

## 小班群体毕业修读计划（`credit-plan`，独立报告 `schema_version: 1`）

### 输入契约

在 v1/v2 原字段之外增加顶层 `planning` 对象（必含 `student_ids`、`terms`）；原字段的全部上限、排序与错误行为不变。规划先对同一输入完整执行原审核（`registrar.service.run`），其结果是不可变起点：存在 `transfer` 时只采用截至日已选中的 `recognition_plan`，规划不改变认定方案、不预支未来认定，也不重新选择历史成绩。晚于 `as_of` 的考试与认定仍只按原审核的未来记录处理，不能作为计划可依赖的预计通过。

- `student_ids`：无重复、全部存在的学生 ID 字符串数组，最多 3 人，可以为空。数组顺序是教务给定的优先顺序，具有业务含义，不按 ID 重排。未列入的学生仍参与原输入校验和当前审核，但不参与未来选课、不占班额。
- `terms`：最多 3 个未来学期的数组，可以为空，每项含 `id`、`start`、`end`、`credit_limit`、`offerings`。
  - `id` 为非空字符串且全局唯一；`start`/`end` 为规范 YYYY-MM-DD，满足 `as_of < start <= end`；按 `start` 排序后严格要求前一期 `end <` 后一期 `start`（相邻、同日均视为重叠而拒绝）。
  - `credit_limit` 为每名计划学生该期可修学分的非负整数上限，拒绝布尔值与浮点。
  - `offerings` 为 `{course_id, capacity}` 数组；课程必须存在、同一学期不得重复；`capacity` 为非负整数，是该课该期供全部计划学生**共享**的总名额，0 表示没有可用名额。每期允许不开课，未列出的课不能选。
- 整份规划最多出现 6 门不同开课课程、9 个课程-学期配对；超出产品边界即拒绝整份输入，不返回近似结果。新增字段的类型错误、布尔冒充整数、重复项、未知引用、缺失字段、日期错误或学期重叠等均拒绝整份输入；其他学生的数据以及原本就在未来的考试和认定仍按现有规则完整校验。

### 计划规则

- 每名学生最初已完成的课程是其当前审核 `selected_passes` 中的课程集合。每个未来课程对一名学生最多安排一次；已经取得学分的课程不允许重复安排。
- 某期修读一门课，其全部先修必须在截至日已经完成，或在严格更早一期的计划结束时完成；同一期的选课互不解锁，假设及格在该期 `end` 同时生效并供以后学期使用。
- 选课必须属于该期 `offerings`；学生该期各门课 `credits` 之和不得超过该期 `credit_limit`；某课该期的全部选课人数不得超过 `capacity`。假设及格无需补验分数，不写入 `attempts`、不伪造 `selected_passes`、不改动当前审核。
- 毕业判定沿用原 `program.required_courses` 与 `minimum_credits`，未来课程使用原本校 `credits`/`category`，一门课只计一次。当前已符合毕业条件者毕业期序号为 0；其余人取首次满足全部原要求学期的 1-based 序号；至最后一期仍不满足视为本次未安排毕业。空计划合法，不能毕业不是输入错误；不修改培养方案，也不放宽先修、负荷或班额。

### 精确最优与个人可行性

在全体计划学生**同时可履行**的安排中按以下顺序逐级比较，前一项严格优先：

1. 最后一期结束时毕业人数最多（含截至日已符合条件者）；
2. 各学生能否毕业的 0/1 向量按 `student_ids` 给定顺序取字典序最大（不得为照顾高优先级学生而减少总人数）；
3. 同一毕业标志下，毕业期序号向量字典序最小，未毕业者使用哨兵 `len(terms)+1`，当前已毕业用 0；
4. 全体未来安排的总学分最少，再取总课程人次数最少（排除无助于已选毕业目标的多余选课）；
5. 仍平局时，把全部选课写成 `[学期序号, student_id, course_id]` 并按 Python 元组顺序排序，取整个列表字典序最小者。

求解按全体联合枚举完成，不返回独立学生计划的拼接或局部贪心结果。另为每名计划学生计算 `individual_graduation_possible`：只让该学生使用当前开课、日期、个人学分上限和原 `capacity`（其他学生不占名额，`capacity=0` 不可用）时，是否存在截至最后一期毕业的安排；当前已毕业者恒为 `true`。该判断不改变群体最优安排，个人安排不并入群体输出。据此区分“本身不可行（unreachable）”与“个人可行但群体资源取舍中延期（capacity_deferred）”。

### `credit-plan` 报告

报告 `schema_version` 为 1，包含 `as_of`、`baseline_audit`、`terms`、`students`、`objective`：

- `baseline_audit`：同一输入调用原审核服务的完整结果，成绩与认定来源保持原样。
- `terms`：按时间排序，每项 `{term_id, enrollments, seat_usage}`。`enrollments` 为 `{student_id, course_id}` 数组，按学生 ID、课程 ID 排序；`seat_usage` 为 `{course_id, used, capacity}` 数组，覆盖该期全部 `offerings`（含零使用项），按课程 ID 排序；无选课输出空数组。
- `students`：按 `student_ids` 给定顺序，每项含 `student_id`、`status`、`individual_graduation_possible`、`graduation_term_index`、`graduation_term_id`、`planned_courses`、`projected_completed_courses`、`projected_credits`、`missing_required`、`credit_deficits`。
  - `status` 仅取 `already_eligible`（当前已毕业）、`scheduled`（群体安排毕业）、`capacity_deferred`（群体未安排但个人可行）、`unreachable`（个人也不可行）。
  - `graduation_term_index` 为 0、实际毕业期序号或未毕业时 `null`；`graduation_term_id` 仅在实际未来毕业时为该期 ID，否则 `null`。
  - `planned_courses` 为 `{term_id, course_id}` 数组，按学期序号、课程 ID 排序；`projected_completed_courses` 为截至最后一期已完成课程 ID 升序数组；`projected_credits` 按该集合以原学分求和；`missing_required`、`credit_deficits` 沿用原审核排序与缺口口径。
- `objective` 为 `{graduates, graduation_flags, completion_indices, planned_credits, enrollments}`，分别对应上述比较所用的毕业人数、按优先序的 0/1 数组、带未毕业哨兵的毕业期序号数组、总新增学分与课程人次数，且必须与明细一致。`student_ids` 为空时两个数组为空，人数/学分/人次均为 0，但仍输出全部 `terms` 与 `baseline_audit`。

## 匿名答卷对账与毕业结论核验（`credit-reconcile`，独立报告 `schema_version: 1`）

完整规则、见证口径与审核次数界限见[对账文档](reconciliation.md)，此处仅列与数据契约相关的硬性约束：

- 输入在 v1/v2 原字段之外新增**必填**顶层 `reconciliation.waves`，合法空数组表示没有待对账资料；缺失 `reconciliation` 或 `waves`、类型错误均拒绝整份输入。
- 最多 120 批，每批最多 6 个座位、6 张答卷；批次 ID、座位 ID、答卷 ID 分别在各自类别内全局唯一；座位 ID 不得与已确认考试 ID 重复。座位与已确认考试合计不超过 20000 条，且合在一起时 `(student_id, course_id, date)` 不得重复。
- 候选只允许引用同批座位；同张答卷重复列同一座位、未知学生或课程、非布尔 `verified`、越界或非整数 `mark`、非规范日期一律拒绝。每个座位最多被 3 张答卷列为候选，每名学生全输入最多有 3 个待对账座位。
- 新字段的结构错误即使落在永不匹配的答卷或晚于 `as_of` 的座位上也照样拒绝；截至日只影响后续学分审核，不豁免校验。
- 对账每批独立：先最大化匹配对数，再最大化 verified 链接数，保留全部并列最优；分数、毕业结论与输入顺序不参与对账。报告 `waves` 按批次 ID、座位按座位 ID、学生按学生 ID 排序，见证平局按 `sheet_id` 向量字典序（`null` 在所有字符串之后）确定，输入重排不改变报告。

## 院系与入学届毕业资格公开简报（`credit-disclosure`，独立报告 `schema_version: 1`）

完整规则、披露模型与验收例见[简报文档](disclosure.md)，此处仅列数据契约：

- 在 v1/v2 原字段之外新增**必填**顶层 `disclosure`；其余输入上限、排序与错误行为不变，缺失时拒绝整份输入。
- 毕业资格人数以同一输入、同一 `as_of` 下原审核的 `graduation_eligible` 为准（含先修阻断与 v2 认定），不以及格成绩代替。
- `disclosure` 必含 `departments`、`cohorts`（各 1..8、互不重复、非空字符串）、`memberships`（每名现有学生恰好一次、无未知引用）、`protected_cells`（至多 8 个有登记学生的格子口径）、`requests`（至多 12 个不重复口径，可为空）、`prior`（同一口径最多一项的 `{min,max}` 整数约束，拒绝布尔值，`0<=min<=max<=登记人数`）。
- 所有格子登记人数（含零人格）公开。已公开的 `prior` 必须与真实汇总相容且不得足以定值任一受保护格子，否则拒绝且不输出任何未发布真值或个体记录。
- 请求口径依次尝试精确值、固定两人数段、隐藏；某档任一非空组不安全即整档弃用。区间末段只按登记人数截断。
- 报告仅含 `{schema_version, as_of, enrollment, items}`；`enrollment` 按 `(department, cohort)` 字符串顺序覆盖全部格子，`items` 按 `requests` 顺序，`bounds` 为 `[n,n]`、公布区间或 `null`。除 `requests` 顺序外，新输入数组重排不改变报告。
- 可行性用精确的带上下界环流判断，工作量与完整候选表数量无关；不输出候选表。

## 通用边界

由相同合格事实（v1 考试，v2 考试加整包认定）驱动成绩、学分、报读和毕业结果，保留每次考试资格原因与选中来源。单次离线方案审核，调用者给定规则，不等于任何真实学校政策。CLI 只原子新建报告；错误退出 2，旧目标不动；输出已存在时保持原目标不变，成功为原子新建。
