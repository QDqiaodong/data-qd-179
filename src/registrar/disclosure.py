"""按院系与入学届公开毕业资格汇总（credit-disclosure）。

输入沿用 v1/v2 审核数据。每名学生是否计入"符合毕业资格"人数，直接取同一
输入、同一 ``as_of`` 下原审核（``registrar.service.run``）的
``graduation_eligible``——包括先修阻断与 v2 转学分选择——而不是用及格成绩
代替。

隐私模型是一张二维人数表：格子人数在 ``0..登记人数`` 之间，所有已公开的
格子、行合计、列合计与全校合计都是同一张表上对应格子的和。公开事实
（``prior`` 与本次依次发布的精确值/区间）逐步加入约束；每个受保护格子必须
在剩余可行表里各取到至少两个不同人数。可行性用带上下界的小型环流精确判断，
结点数是 ``院系 + 入学届 + 4``；每次判断只做常数次最大流，与完整候选表的
数量无关，绝不枚举或逐张构造人数表。
"""
from collections import deque

from . import service
from .io import integer, text
from .model import validate


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _scope(row, label, dept_index, cohort_index):
    """解析 {department, cohort} 口径；未知引用与空字符串都拒绝。"""
    if not isinstance(row, dict):
        raise ValueError(f"{label} rows must be objects")
    for field in ("department", "cohort"):
        if field not in row:
            raise ValueError(f"{label}.{field} is required")
    result = []
    for value, index, name in ((row["department"], dept_index, "department"),
                               (row["cohort"], cohort_index, "cohort")):
        if value is None:
            result.append(None)
            continue
        text(value, f"{label}.{name}")
        if value not in index:
            raise ValueError(f"unknown {label}.{name} reference: {value}")
        result.append(index[value])
    return tuple(result)


def _names(node, field):
    if field not in node:
        raise ValueError(f"disclosure.{field} is required")
    raw = node[field]
    if not isinstance(raw, list):
        raise ValueError(f"disclosure.{field} must be an array")
    if not 1 <= len(raw) <= 8:
        raise ValueError(f"disclosure.{field} must contain 1..8 items")
    names = []
    seen = set()
    for item in raw:
        text(item, f"disclosure.{field}")
        if item in seen:
            raise ValueError(f"duplicate disclosure.{field}: {item}")
        seen.add(item)
        names.append(item)
    return names


def _validate_disclosure(node, students):
    if not isinstance(node, dict):
        raise ValueError("disclosure must be an object")
    for field in ("departments", "cohorts", "memberships", "protected_cells",
                  "requests", "prior"):
        if field not in node:
            raise ValueError(f"disclosure.{field} is required")
    departments = _names(node, "departments")
    cohorts = _names(node, "cohorts")
    dept_index = {name: index for index, name in enumerate(departments)}
    cohort_index = {name: index for index, name in enumerate(cohorts)}

    raw_memberships = node["memberships"]
    if not isinstance(raw_memberships, list):
        raise ValueError("disclosure.memberships must be an array")
    memberships = {}
    for row in raw_memberships:
        if not isinstance(row, dict):
            raise ValueError("disclosure.memberships rows must be objects")
        for field in ("student_id", "department", "cohort"):
            if field not in row:
                raise ValueError(f"disclosure.memberships.{field} is required")
        sid = text(row["student_id"], "disclosure.memberships.student_id")
        if sid not in students:
            raise ValueError(f"unknown membership student: {sid}")
        if sid in memberships:
            raise ValueError(f"duplicate membership for student: {sid}")
        department = text(row["department"], "disclosure.memberships.department")
        cohort = text(row["cohort"], "disclosure.memberships.cohort")
        if department not in dept_index:
            raise ValueError(f"unknown membership department: {department}")
        if cohort not in cohort_index:
            raise ValueError(f"unknown membership cohort: {cohort}")
        memberships[sid] = (dept_index[department], cohort_index[cohort])
    if set(memberships) != set(students):
        raise ValueError("every student must have exactly one membership")

    raw_protected = node["protected_cells"]
    if not isinstance(raw_protected, list):
        raise ValueError("disclosure.protected_cells must be an array")
    if len(raw_protected) > 8:
        raise ValueError("disclosure.protected_cells allows at most 8 cells")
    protected = []
    seen = set()
    for row in raw_protected:
        scope = _scope(row, "disclosure.protected_cells", dept_index, cohort_index)
        if scope[0] is None or scope[1] is None:
            raise ValueError("protected cells must name one department and one cohort")
        if scope in seen:
            raise ValueError("duplicate protected cell")
        seen.add(scope)
        protected.append(scope)

    raw_requests = node["requests"]
    if not isinstance(raw_requests, list):
        raise ValueError("disclosure.requests must be an array")
    if len(raw_requests) > 12:
        raise ValueError("disclosure.requests allows at most 12 scopes")
    requests = []
    seen = set()
    for row in raw_requests:
        scope = _scope(row, "disclosure.requests", dept_index, cohort_index)
        if scope in seen:
            raise ValueError("duplicate requested scope")
        seen.add(scope)
        requests.append(scope)

    raw_prior = node["prior"]
    if not isinstance(raw_prior, list):
        raise ValueError("disclosure.prior must be an array")
    prior = {}
    for row in raw_prior:
        if not isinstance(row, dict):
            raise ValueError("disclosure.prior rows must be objects")
        for field in ("department", "cohort", "min", "max"):
            if field not in row:
                raise ValueError(f"disclosure.prior.{field} is required")
        scope = _scope(row, "disclosure.prior", dept_index, cohort_index)
        if scope in prior:
            raise ValueError("duplicate prior scope")
        low = integer(row["min"], "disclosure.prior.min", 0)
        high = integer(row["max"], "disclosure.prior.max", 0)
        if high < low:
            raise ValueError("disclosure.prior requires min <= max")
        prior[scope] = (low, high)
    return departments, cohorts, memberships, protected, requests, prior


# ---------------------------------------------------------------------------
# 精确可行性：带上下界环流。结点只有 院系 + 入学届 + 4 个
# ---------------------------------------------------------------------------

def _dinic(graph, count, source, sink):
    flow = 0
    infinity = 1 << 60
    while True:
        level = [-1] * count
        level[source] = 0
        queue = deque([source])
        while queue:
            node = queue.popleft()
            for edge in graph[node]:
                if edge[1] > 0 and level[edge[0]] < 0:
                    level[edge[0]] = level[node] + 1
                    queue.append(edge[0])
        if level[sink] < 0:
            return flow
        cursor = [0] * count

        def augment(node, available):
            if node == sink:
                return available
            edges = graph[node]
            while cursor[node] < len(edges):
                edge = edges[cursor[node]]
                target = edge[0]
                if edge[1] > 0 and level[target] == level[node] + 1:
                    pushed = augment(target, min(available, edge[1]))
                    if pushed:
                        edge[1] -= pushed
                        graph[target][edge[2]][1] += pushed
                        return pushed
                cursor[node] += 1
            return 0

        while True:
            pushed = augment(source, infinity)
            if not pushed:
                break
            flow += pushed


class _Infeasible(Exception):
    pass


class _Shape:
    """登记人数表的不变量；口径的登记人数上限由它给出。"""

    __slots__ = ("caps", "rows", "columns", "total", "departments", "cohorts")

    def __init__(self, caps):
        self.caps = caps
        d = self.departments = len(caps)
        c = self.cohorts = len(caps[0]) if d else 0
        self.rows = [sum(row) for row in caps]
        self.columns = [sum(caps[i][j] for i in range(d)) for j in range(c)]
        self.total = sum(self.rows)

    def cap(self, scope):
        i, j = scope
        if i is None and j is None:
            return self.total
        if j is None:
            return self.rows[i]
        if i is None:
            return self.columns[j]
        return self.caps[i][j]


class _Circulation:
    """``facts``（口径 -> (min, max)）下的一张可行环流。

    scope 为 ``(院系下标|None, 入学届下标|None)``，每个 scope 对应恰好一条
    环流边：格子是 行→列，行合计是 源→行，列合计是 列→汇，全校是 汇→源。
    构造后可用 :meth:`value_range` 精确读出该口径在剩余可行表中的取值区间。
    """

    def __init__(self, shape, facts):
        self.shape = shape
        self.facts = facts
        d = shape.departments
        c = shape.cohorts
        self.source = 0
        self.row_base = 1
        self.column_base = self.row_base + d
        self.sink = self.column_base + c
        self.count = self.sink + 3
        graph = [[] for _ in range(self.count)]
        demand = [0] * self.count
        self.refs = {}

        def connect(u, v, capacity):
            graph[u].append([v, capacity, len(graph[v])])
            graph[v].append([u, 0, len(graph[u]) - 1])
            return len(graph[u]) - 1

        def bounded(scope, u, v, fallback):
            low, high = facts.get(scope, fallback)
            low = max(low, fallback[0])
            high = min(high, fallback[1])
            if low > high:
                raise _Infeasible
            index = connect(u, v, high - low)
            demand[u] -= low
            demand[v] += low
            self.refs[scope] = (u, v, index, high)

        self.feasible = False
        try:
            for i in range(d):
                bounded((i, None), self.source, self.row_base + i,
                        (0, shape.rows[i]))
            for i in range(d):
                for j in range(c):
                    bounded((i, j), self.row_base + i, self.column_base + j,
                            (0, shape.caps[i][j]))
            for j in range(c):
                bounded((None, j), self.column_base + j, self.sink,
                        (0, shape.columns[j]))
            bounded((None, None), self.sink, self.source, (0, shape.total))
        except _Infeasible:
            self.graph = None
            return
        super_source = self.sink + 1
        super_sink = self.sink + 2
        need = 0
        for node in range(super_source):
            balance = demand[node]
            if balance > 0:
                connect(super_source, node, balance)
                need += balance
            elif balance < 0:
                connect(node, super_sink, -balance)
        self.graph = graph
        self.feasible = _dinic(graph, self.count, super_source, super_sink) == need

    def _residual(self, skip):
        """去掉超级结点与 ``skip`` 边后的残余网络（真实边已守恒）。"""
        count = self.sink + 1
        graph = [[] for _ in range(count)]

        def add(u, v, capacity):
            if capacity <= 0:
                return
            graph[u].append([v, capacity, len(graph[v])])
            graph[v].append([u, 0, len(graph[u]) - 1])

        for scope, (u, v, index, _upper) in self.refs.items():
            if scope == skip:
                continue
            edge = self.graph[u][index]
            add(u, v, edge[1])
            add(v, u, self.graph[v][edge[2]][1])
        return graph

    def value_range(self, scope):
        """剩余可行表中该口径可取的最小/最大人数；无可行表返回 None。"""
        if not self.feasible:
            return None
        u, v, index, upper = self.refs[scope]
        forward = self.graph[u][index][1]           # 还能增加的上限
        reverse = self.graph[v][self.graph[u][index][2]][1]  # 还能减少的上限
        here = upper - forward
        if forward == 0 and reverse == 0:
            return (here, here)
        # 增加目标边要沿 v→u 找增广；减少目标边要沿 u→v 找增广。
        gain_up = min(forward, _dinic(self._residual(scope), self.sink + 1, v, u))
        gain_down = min(reverse, _dinic(self._residual(scope), self.sink + 1, u, v))
        return (here - gain_down, here + gain_up)

    def has_two_values(self, scope):
        result = self.value_range(scope)
        return result is not None and result[0] < result[1]


# ---------------------------------------------------------------------------
# 披露决策
# ---------------------------------------------------------------------------

_EXACT = "exact"
_BAND = "band"


def _intersect(facts, scope, low, high):
    existing = facts.get(scope)
    if existing is not None:
        low = max(low, existing[0])
        high = min(high, existing[1])
    merged = dict(facts)
    merged[scope] = (low, high)
    return merged


def _protected_two(circulation, protected):
    return all(circulation.has_two_values(scope) for scope in protected)


def _safe_after(shape, facts, scope, low, high, protected):
    """把 scope 的 [low, high] 加入约束后，所有受保护格子是否仍有两个取值。

    返回 True / False；组为空时返回 None（该公布结果目前不可能出现）。
    """
    trial = _intersect(facts, scope, low, high)
    circulation = _Circulation(shape, trial)
    if not circulation.feasible:
        return None
    return _protected_two(circulation, protected)


def _exact_candidates(low, high):
    """精确值档：逐个整数。极值优先只加快发现不安全组，不改变结论。"""
    if low > high:
        return []
    ordered = [low]
    if high != low:
        ordered.append(high)
    ordered.extend(range(low + 1, high))
    return [(value, value) for value in ordered]


def _band_candidates(scope_cap, low, high):
    """固定两人数段 [0,1]、[2,3]…；末段只按登记人数截断。"""
    bands = [(start, min(start + 1, scope_cap))
             for start in range(0, scope_cap + 1, 2)]
    # 与当前可行区间相交才可能是非空组；各段本身仍保持固定边界。
    return [band for band in bands if band[0] <= high and band[1] >= low]


def _choose(shape, facts, protected, scope, real, tier):
    """尝试一档；整档安全且覆盖真实值才返回公布值，否则 None。"""
    if not protected:
        # 没有受保护格子时任何非空组都安全：精确档必可用，区间档取真实值
        # 所在的那一段。这样规模用例完全不随候选值数量增长。
        if tier == _EXACT:
            return (real, real)
        cap = shape.cap(scope)
        start = real - (real % 2)
        return (start, min(start + 1, cap))
    circulation = _Circulation(shape, facts)
    feasible = circulation.value_range(scope)
    if feasible is None:
        return None
    low, high = feasible
    candidates = (_exact_candidates(low, high) if tier == _EXACT
                  else _band_candidates(shape.cap(scope), low, high))
    chosen = None
    for start, end in candidates:
        safe = _safe_after(shape, facts, scope, start, end, protected)
        if safe is None:
            continue  # 空组
        if not safe:
            return None  # 任一非空组不安全：整档跳过，与真实值无关
        if start <= real <= end:
            chosen = (start, end)
    return chosen


def report(data):
    if "disclosure" not in data:
        raise ValueError("disclosure is required")
    baseline = service.run(data)  # 同一输入、同一 as_of 的原审核
    _cutoff, _courses, students, _attempts, _program, _transfer = validate(data)
    departments, cohorts, memberships, protected, requests, prior = \
        _validate_disclosure(data["disclosure"], students)

    d, c = len(departments), len(cohorts)
    caps = [[0] * c for _ in range(d)]
    eligible = [[0] * c for _ in range(d)]
    flags = {row["student_id"]: 1 if row["graduation_eligible"] else 0
             for row in baseline["students"]}
    for sid in students:
        i, j = memberships[sid]
        caps[i][j] += 1
        eligible[i][j] += flags[sid]
    shape = _Shape(caps)

    def registered(scope):
        return shape.cap(scope)

    def actual(scope):
        i, j = scope
        if i is None and j is None:
            return sum(sum(row) for row in eligible)
        if j is None:
            return sum(eligible[i])
        if i is None:
            return sum(eligible[k][j] for k in range(d))
        return eligible[i][j]

    for scope in protected:
        if registered(scope) <= 0:
            raise ValueError("protected cells must have registered students")

    # --- prior：范围检查，且必须与本次真实汇总相容、不得撤回 --------------
    facts = {}
    for scope, (low, high) in prior.items():
        if high > registered(scope):
            raise ValueError("disclosure.prior bound exceeds the registered count")
        facts[scope] = (low, high)
    for scope, (low, high) in facts.items():
        if not low <= actual(scope) <= high:
            raise ValueError("disclosure.prior conflicts with the audit snapshot")

    # 若公开事实已把任一受保护格子定死，拒绝整份简报（不输出任何真值）。
    initial = _Circulation(shape, facts)
    if not initial.feasible:
        raise ValueError("disclosure.prior makes the snapshot infeasible")
    if not _protected_two(initial, protected):
        raise ValueError("published facts already determine a protected cell")

    items = []
    for scope in requests:
        real = actual(scope)
        published = _choose(shape, facts, protected, scope, real, _EXACT)
        status = _EXACT
        if published is None:
            published = _choose(shape, facts, protected, scope, real, _BAND)
            status = _BAND
        if published is None:
            status = "withheld"
            bounds = None
        else:
            bounds = [published[0], published[1]]
            facts = _intersect(facts, scope, published[0], published[1])
        i, j = scope
        items.append({
            "department": None if i is None else departments[i],
            "cohort": None if j is None else cohorts[j],
            "students": registered(scope),
            "status": status,
            "bounds": bounds,
        })

    enrollment = [
        {"department": departments[i], "cohort": cohorts[j],
         "students": caps[i][j]}
        for i in range(d) for j in range(c)
    ]
    enrollment.sort(key=lambda row: (row["department"], row["cohort"]))
    return {
        "schema_version": 1,
        "as_of": baseline["as_of"],
        "enrollment": enrollment,
        "items": items,
    }
