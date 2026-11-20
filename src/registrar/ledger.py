"""可回放的成绩更正台账（credit-ledger 的服务入口）。

初始化把一份合法 v1/v2 审核输入的目录（courses/students/program/可选 transfer/
默认 as_of）固定在序号为 0 的台账中，原始每条 attempt 记为该考试 ID 的
revision=0；之后只接收 attempts 的来源更正批次（upsert/withdraw，revision 为
正整数）。每个首次成功的 batch_id 取得严格连续的接收序号 seq；任一历史 seq 的
可见投影都可以回放并重走原审核（含 v2 认定最优选择）。

存储是 store 路径上的单个 SQLite 文件（WAL 模式）：

- ``meta``：格式标识与当前已提交序号；
- ``catalog``：初始化时固定的原输入目录（整体读取属于"当前数据"）；
- ``facts``：全部来源事实，主键 (attempt_id, revision)，只在提交时追加，
  按 (attempt_id, revision) 主键点查，不做全表线性读取；
- ``current_state``：当前投影（每个已知考试 ID 一行，含固定身份与当前可见
  版本），规模不超过 20000 行，允许整体读取；
- ``batches``：每个成功批次一行（seq 主键、batch_id 唯一索引、重放身份
  canon、永久固定的回执 JSON），按 batch_id 索引点查。

接收在 ``BEGIN IMMEDIATE`` 事务内完成校验与提交：同一 store 上的并发写者被
SQLite 序列化，读者（WAL 快照）只见到完整提交之前或之后的状态。进程在提交前
被杀，事务整体回滚；提交后被杀，重开台账即见完整批次，重试同一批次按
batch_id 索引找回原回执。
"""
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from . import service
from .io import integer, text
from .model import parse_day, validate

FORMAT = "registrar-ledger-v1"
MAX_CHANGES = 1000
MAX_ATTEMPT_IDS = 20000

_SCHEMA = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE catalog (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE facts (
           attempt_id TEXT NOT NULL,
           revision INTEGER NOT NULL,
           student_id TEXT NOT NULL,
           course_id TEXT NOT NULL,
           op TEXT NOT NULL,
           date TEXT,
           mark INTEGER,
           seq INTEGER NOT NULL,
           PRIMARY KEY (attempt_id, revision)
       )""",
    "CREATE INDEX facts_by_seq ON facts (seq)",
    """CREATE TABLE current_state (
           attempt_id TEXT PRIMARY KEY,
           max_revision INTEGER NOT NULL,
           student_id TEXT NOT NULL,
           course_id TEXT NOT NULL,
           op TEXT NOT NULL,
           date TEXT,
           mark INTEGER
       )""",
    """CREATE TABLE batches (
           seq INTEGER PRIMARY KEY,
           batch_id TEXT NOT NULL UNIQUE,
           canon TEXT NOT NULL,
           receipt TEXT NOT NULL
       )""",
]

# 测试注入点：键为 "begin" / "before_commit" / "after_commit"，值为可调用对象。
# 生产路径不设置任何钩子；故障测试借此在真实持久化边界受控中断或延时。
_fault_hooks = {}

# 测试可替换连接工厂以观测实际存储访问（如逐表统计读取行数）。
_connection_factory = sqlite3.connect


def _fault_hook(point):
    hook = _fault_hooks.get(point)
    if hook is not None:
        hook()


def _canon(value):
    """确定性 JSON 编码：对象键排序、紧凑分隔符，用于重放身份与存储。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _connect(path):
    return _connection_factory(str(path), timeout=30.0, isolation_level=None)


def _open(store):
    """打开既有台账并校验格式；不存在或不是台账时报错，绝不凭空创建。"""
    path = Path(store)
    if not path.exists():
        raise ValueError(f"ledger store does not exist: {store}")
    try:
        conn = _connect(path)
    except sqlite3.Error as exc:
        raise ValueError(f"invalid ledger store: {exc}") from None
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'format'").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise ValueError(f"invalid ledger store: {exc}") from None
    if row is None or row[0] != FORMAT:
        conn.close()
        raise ValueError("invalid ledger store: bad format")
    return conn


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise ValueError(f"invalid ledger store: missing meta {key}")
    return row[0]


def _catalog(conn):
    row = conn.execute(
        "SELECT value FROM catalog WHERE key = 'input'").fetchone()
    if row is None:
        raise ValueError("invalid ledger store: missing catalog")
    return json.loads(row[0])


def _rebuilt_input(catalog, attempts, as_of):
    data = {"as_of": as_of, "courses": catalog["courses"],
            "students": catalog["students"], "program": catalog["program"],
            "attempts": attempts}
    if "transfer" in catalog:
        data["transfer"] = catalog["transfer"]
    return data


def initialize(store, data):
    """以合法 v1/v2 审核输入建立序号为 0 的台账，返回 {"seq": 0}。

    已存在的 store 一律拒绝并保留原内容；失败时不留下可被当成成功台账
    打开的状态（先在临时文件中完整建库，再原子链接到目标路径）。
    """
    validate(data)  # 完整原校验，未通过时不触碰磁盘
    catalog = {"as_of": data["as_of"], "courses": data["courses"],
               "students": data["students"], "program": data["program"]}
    if "transfer" in data:
        catalog["transfer"] = data["transfer"]
    store = Path(store)
    if store.exists():
        raise ValueError(f"ledger store already exists: {store}")
    store.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".ledger-init-", dir=store.parent)
    os.close(fd)
    try:
        conn = _connect(temp)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN")
            for statement in _SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO meta VALUES ('format', ?)", (FORMAT,))
            conn.execute("INSERT INTO meta VALUES ('seq', '0')")
            conn.execute("INSERT INTO catalog VALUES ('input', ?)",
                         (_canon(catalog),))
            for attempt in data["attempts"]:
                row = (attempt["id"], 0, attempt["student_id"],
                       attempt["course_id"], "upsert", attempt["date"],
                       attempt["mark"], 0)
                conn.execute(
                    "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
                conn.execute(
                    "INSERT INTO current_state VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row[:-1])
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        try:
            os.link(temp, store)  # 原子新建；目标已存在则失败且互不影响
        except FileExistsError:
            raise ValueError(f"ledger store already exists: {store}") from None
    finally:
        for suffix in ("", "-wal", "-shm"):
            leftover = temp + suffix
            if os.path.exists(leftover):
                os.unlink(leftover)
    return {"seq": 0}


def _validate_change(node, index):
    """校验单条更正项并返回规范化字典；字段集合按 op 精确限定。"""
    label = f"changes[{index}]"
    if not isinstance(node, dict):
        raise ValueError(f"{label} must be an object")
    for field in ("attempt_id", "revision", "student_id", "course_id", "op"):
        if field not in node:
            raise ValueError(f"{label}.{field} is required")
    op = node["op"]
    if op not in ("upsert", "withdraw"):
        raise ValueError(f"{label}.op must be upsert or withdraw")
    allowed = {"attempt_id", "revision", "student_id", "course_id", "op"}
    if op == "upsert":
        allowed |= {"date", "mark"}
    extra = set(node) - allowed
    if extra:
        raise ValueError(f"{label} has undefined fields: {sorted(extra)}")
    change = {"attempt_id": text(node["attempt_id"], f"{label}.attempt_id"),
              "revision": integer(node["revision"], f"{label}.revision", 1),
              "student_id": text(node["student_id"], f"{label}.student_id"),
              "course_id": text(node["course_id"], f"{label}.course_id"),
              "op": op}
    if op == "upsert":
        for field in ("date", "mark"):
            if field not in node:
                raise ValueError(f"{label}.{field} is required")
        change["date"] = parse_day(node["date"]).isoformat()
        mark = integer(node["mark"], f"{label}.mark")
        if mark > 100:
            raise ValueError(f"{label}.mark exceeds 100")
        change["mark"] = mark
    return change


def _validate_batch(batch):
    """校验批次结构，返回 (batch_id, expected_seq, 规范化 changes)。"""
    if not isinstance(batch, dict):
        raise ValueError("batch must be an object")
    for field in ("batch_id", "expected_seq", "changes"):
        if field not in batch:
            raise ValueError(f"batch.{field} is required")
    extra = set(batch) - {"batch_id", "expected_seq", "changes"}
    if extra:
        raise ValueError(f"batch has undefined fields: {sorted(extra)}")
    batch_id = text(batch["batch_id"], "batch_id")
    expected_seq = integer(batch["expected_seq"], "expected_seq", 0)
    raw_changes = batch["changes"]
    if not isinstance(raw_changes, list):
        raise ValueError("changes must be an array")
    if len(raw_changes) > MAX_CHANGES:
        raise ValueError(f"at most {MAX_CHANGES} changes per batch")
    changes = [_validate_change(node, i) for i, node in enumerate(raw_changes)]
    seen = set()
    for change in changes:
        key = (change["attempt_id"], change["revision"])
        if key in seen:
            raise ValueError(f"duplicate (attempt_id, revision) in changes: {key}")
        seen.add(key)
    return batch_id, expected_seq, changes


def _batch_canon(expected_seq, changes):
    """重放身份：expected_seq 与 changes 全内容的规范化形式。

    changes 的排列没有业务意义，按键序与内容排序后编码；JSON 对象键顺序
    不同不会判为不同批次。
    """
    ordered = sorted(changes, key=_canon)
    return _canon({"expected_seq": expected_seq, "changes": ordered})


def _load_current_state(conn):
    """整体读取当前投影（<=20000 行，属"当前数据"例外）。"""
    return {row[0]: row[1:] for row in conn.execute(
        "SELECT attempt_id, max_revision, student_id, course_id, op, date, mark"
        " FROM current_state")}


def ingest(store, batch):
    """接收一个更正批次，返回并持久保存回执。

    失败（无效输入、修订冲突、身份冲突、当前投影冲突、并发前提失败）不保存
    部分事实、不抢占 batch_id、不改变 seq；同一 batch_id 的精确重放返回首次
    保存的原回执。
    """
    batch_id, expected_seq, changes = _validate_batch(batch)
    canon = _batch_canon(expected_seq, changes)
    conn = _open(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _fault_hook("begin")
        try:
            current_seq = int(_meta(conn, "seq"))
            row = conn.execute(
                "SELECT canon, receipt FROM batches WHERE batch_id = ?",
                (batch_id,)).fetchone()
            if row is not None:
                if row[0] == canon:
                    conn.rollback()
                    return json.loads(row[1])
                raise ValueError(f"batch_id conflict: {batch_id}")
            if expected_seq != current_seq:
                raise ValueError(
                    f"expected_seq {expected_seq} does not match current seq"
                    f" {current_seq}")
            state = _load_current_state(conn)
            catalog = _catalog(conn)
            students = {row["id"] for row in catalog["students"]}
            courses = {row["id"] for row in catalog["courses"]}
            known_facts = {}
            new_identities = {}
            for change in changes:
                aid = change["attempt_id"]
                if change["student_id"] not in students:
                    raise ValueError(f"unknown change student: {change['student_id']}")
                if change["course_id"] not in courses:
                    raise ValueError(f"unknown change course: {change['course_id']}")
                identity = (change["student_id"], change["course_id"])
                existing = state.get(aid)
                if existing is not None:
                    if identity != (existing[1], existing[2]):
                        raise ValueError(
                            f"identity conflict for attempt {aid}: student/course"
                            " of an exam id never change")
                else:
                    first = new_identities.setdefault(aid, identity)
                    if first != identity:
                        raise ValueError(
                            f"identity conflict for attempt {aid}: student/course"
                            " of an exam id never change")
                key = (aid, change["revision"])
                fact = conn.execute(
                    "SELECT student_id, course_id, op, date, mark FROM facts"
                    " WHERE attempt_id = ? AND revision = ?", key).fetchone()
                known_facts[key] = fact
                if fact is not None:
                    stored = {"student_id": fact[0], "course_id": fact[1],
                              "op": fact[2], "date": fact[3], "mark": fact[4]}
                    incoming = {"student_id": change["student_id"],
                                "course_id": change["course_id"],
                                "op": change["op"],
                                "date": change.get("date"),
                                "mark": change.get("mark")}
                    if stored != incoming:
                        raise ValueError(
                            f"revision conflict: ({aid}, {change['revision']})"
                            " already recorded with different content")
            if len(state) + len(new_identities) > MAX_ATTEMPT_IDS:
                raise ValueError(f"at most {MAX_ATTEMPT_IDS} known attempt ids")
            # 共同作用后的可见投影：每个考试 ID 取已收到的最大 revision。
            projection = dict(state)
            for change in changes:
                aid = change["attempt_id"]
                current = projection.get(aid)
                if current is None or change["revision"] > current[0]:
                    projection[aid] = (change["revision"], change["student_id"],
                                       change["course_id"], change["op"],
                                       change.get("date"), change.get("mark"))
            attempts = [{"id": aid, "student_id": value[1],
                         "course_id": value[2], "date": value[4],
                         "mark": value[5]}
                        for aid, value in sorted(projection.items())
                        if value[3] == "upsert"]
            validate(_rebuilt_input(catalog, attempts, catalog["as_of"]))
            changed = []
            for aid in sorted({change["attempt_id"] for change in changes}):
                before = state.get(aid)
                after = projection[aid]
                if before is None or after[0] > before[0]:
                    changed.append({
                        "attempt_id": aid,
                        "before_revision": before[0] if before is not None else None,
                        "after_revision": after[0],
                        "withdrawn": after[3] == "withdraw"})
            receipt = {"batch_id": batch_id, "seq": current_seq + 1,
                       "previous_seq": current_seq, "changed": changed}
            for change in changes:
                aid = change["attempt_id"]
                key = (aid, change["revision"])
                if known_facts[key] is None:
                    conn.execute(
                        "INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (aid, change["revision"], change["student_id"],
                         change["course_id"], change["op"], change.get("date"),
                         change.get("mark"), current_seq + 1))
            # 当前投影只随最大可见 revision 上升而推进，按投影胜者整体写回。
            for aid in {change["attempt_id"] for change in changes}:
                before = state.get(aid)
                after = projection[aid]
                if before is None or after[0] > before[0]:
                    conn.execute(
                        "INSERT OR REPLACE INTO current_state"
                        " VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (aid, after[0], after[1], after[2], after[3],
                         after[4], after[5]))
            conn.execute(
                "INSERT INTO batches VALUES (?, ?, ?, ?)",
                (current_seq + 1, batch_id, canon, _canon(receipt)))
            conn.execute("UPDATE meta SET value = ? WHERE key = 'seq'",
                         (str(current_seq + 1),))
            _fault_hook("before_commit")
            conn.commit()
            _fault_hook("after_commit")
            return receipt
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def _visible_facts(conn, known_seq):
    """回放指定接收序号的可见版本：每个已知 ID 在 seq<=known_seq 中的最大 revision。"""
    return conn.execute(
        """SELECT f.attempt_id, f.revision, f.student_id, f.course_id, f.op,
                  f.date, f.mark
           FROM facts f
           JOIN (SELECT attempt_id, MAX(revision) AS max_revision
                 FROM facts WHERE seq <= ? GROUP BY attempt_id) AS winners
             ON f.attempt_id = winners.attempt_id
            AND f.revision = winners.max_revision""",
        (known_seq,)).fetchall()


def audit(store, *, known_seq=None, as_of=None):
    """按接收序号与截至日两条独立的轴回放审核，返回完整报告。

    known_seq 缺省为调用开始时的一次完整已提交当前序号；显式给出时必须是
    0..当前序号的整数，越界报错而不夹取。as_of 缺省为初始化固定的默认日期。
    历史查询只读，不回写当前状态。
    """
    if known_seq is not None:
        integer(known_seq, "known_seq", 0)
    if as_of is not None:
        as_of = parse_day(as_of).isoformat()
    conn = _open(store)
    try:
        conn.execute("BEGIN")
        try:
            current_seq = int(_meta(conn, "seq"))
            if known_seq is None:
                known_seq = current_seq
            elif known_seq > current_seq:
                raise ValueError(
                    f"known_seq {known_seq} exceeds current seq {current_seq}")
            catalog = _catalog(conn)
            rows = _visible_facts(conn, known_seq)
        finally:
            conn.rollback()
    finally:
        conn.close()
    versions = []
    attempts = []
    for aid, revision, student_id, course_id, op, day, mark in rows:
        withdrawn = op == "withdraw"
        versions.append({"attempt_id": aid, "revision": revision,
                         "withdrawn": withdrawn})
        if not withdrawn:
            attempts.append({"id": aid, "student_id": student_id,
                             "course_id": course_id, "date": day, "mark": mark})
    versions.sort(key=lambda item: item["attempt_id"])
    attempts.sort(key=lambda item: item["id"])
    used_as_of = as_of if as_of is not None else catalog["as_of"]
    report = service.run(_rebuilt_input(catalog, attempts, used_as_of))
    return {"known_seq": known_seq, "as_of": used_as_of,
            "versions": versions, "audit": report}
