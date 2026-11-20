"""子进程工作器：被 test_ledger_concurrency 以独立进程方式调用。

用法：python -m tests.ledger_worker STORE BATCH_JSON [HOOK] [ARG]
HOOK 模拟故障或延时：sleep-begin / sleep-before-commit（ARG 为秒数与可选
标记文件）、exit-before-commit / exit-after-commit（os._exit 模拟进程被杀）、
raise-before-commit（异常抛出）。结果以一行 JSON 写 stdout。
"""
import json
import os
import sys
import time


def _hook(spec, arg):
    if spec.startswith("sleep-"):
        point = spec[len("sleep-"):].replace("-", "_")
        seconds, _, marker = arg.partition(",")

        def hook():
            if marker:
                open(marker, "w").close()
            time.sleep(float(seconds))
        return point, hook
    if spec.startswith("exit-"):
        point = spec[len("exit-"):].replace("-", "_")
        return point, lambda: os._exit(42)
    if spec.startswith("raise-"):
        point = spec[len("raise-"):].replace("-", "_")
        def hook():
            raise RuntimeError("injected fault")
        return point, hook
    raise ValueError(f"unknown hook: {spec}")


def main():
    store, batch_path = sys.argv[1], sys.argv[2]
    from registrar import ledger
    if len(sys.argv) > 3:
        point, hook = _hook(sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else "")
        ledger._fault_hooks[point] = hook
    with open(batch_path) as stream:
        node = json.load(stream)
    try:
        receipt = ledger.ingest(store, node)
    except Exception as exc:  # 结果经 stdout 传回父进程断言
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(2)
    print(json.dumps({"ok": True, "receipt": receipt}))


if __name__ == "__main__":
    main()
