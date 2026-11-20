本目录数据为本项目原创合成样例，采用仓库 MIT 许可；不含真实客户、人员或运营数据。样例无需联网生成或下载。

- `input.json`：v1 培养方案、学生与考试样例。
- `input_v2.json`：v2 转学分组合认定样例，包含互斥方案、整包额度成本、未被引用之外的未来凭据与空认定结果。
- `input_plan.json`：小班群体毕业修读计划样例，覆盖共享班额下”总体毕业人数优先于高优先级学生提前毕业”的取舍，以及未参与规划学生仍进入基线审核的情形。
- `ledger_init.json`：台账初始化样例（A 无先修、B 先修 A），配合 `ledger_batch_x.json`（撤销 a）、`ledger_batch_y.json`（补到更旧修订）、`ledger_batch_z.json`（同日恢复 a）演示撤销、乱序修订与同日不解锁。
- `ledger_init_v2.json`、`ledger_batch_v2.json`：v2 台账样例，补报本校 A 及格后最优认定方案由 `[“zz”]` 变为 `[“aa”]`，历史序号的方案保持不变。
