# data-qd-179 · 学分与先修课审核

按考试日期核先修资格与重修选分，联合计算类别学分、可报读课程和培养方案毕业资格。输入含顶层 `transfer` 时启用 v2 转学分组合认定：在互斥的整包认定方案中选择全局最优、可追溯的一组，并用该方案从头重审全部考试。

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
```

v1 预期：2名合成学生、1名符合毕业条件、已计12学分、1次考试因先修不足被阻断，报告 `schema_version` 为 1。

v2 预期：报告 `schema_version` 为 2；学生 synthetic-A 的互斥方案中选中 `zz`（认定 A，解锁 03-01 的 C 考试），已计 9 学分并毕业，加权成绩 `230/3`；synthetic-B 仅有未来方案，输出空 `recognition_plan`，不报业务错误。

输入或业务规则无效、输出已存在时退出2，保留原目标；成功退出0。报告/数据库先完整生成再原子新建，无覆盖。run/为忽略目录。

## 契约与边界

详见[数据契约](docs/contract.md)。无 `transfer` 时完全兼容 v1 的输入、报告字段、排序与错误行为；出现 `transfer` 时按 v2 契约输出 `schema_version: 2`，新增整包认定可行性、逐日重审、全局最优选择和 `recognition_plan`。课程及及格线由输入给定，规则是本仓明确模型；不代表学校政策，不含真实学生数据。

样例均为本仓原创合成数据，来源见[说明](fixtures/SOURCES.md)，代码/样例MIT许可。业务演示和测试不访问网络，不需要容器或外部服务。
