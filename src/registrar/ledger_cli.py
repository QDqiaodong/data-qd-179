"""credit-ledger 命令行：init / ingest / audit 三个子命令。

init 与 ingest 成功时向 stdout 输出一份 JSON 返回值；audit 沿用原项目
原子新建报告的方式（不覆盖已有目标），并向 stdout 输出一行定位摘要。
错误一律退出 2 并给出可识别原因，不输出伪成功结果。输入 JSON 沿用原有
防重复键、非有限数与 8 MB 大小限制的加载方式。
"""
import argparse
import json
import sqlite3

from . import ledger
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(prog="credit-ledger",
                                     description="可回放的成绩更正台账")
    sub = parser.add_subparsers(dest="command", required=True)
    p_init = sub.add_parser("init", help="以合法 v1/v2 审核输入建立序号为 0 的台账")
    p_init.add_argument("--store", required=True)
    p_init.add_argument("--source", required=True)
    p_ingest = sub.add_parser("ingest", help="接收一个更正批次并输出回执")
    p_ingest.add_argument("--store", required=True)
    p_ingest.add_argument("--source", required=True)
    p_audit = sub.add_parser("audit", help="回放指定接收序号与截至日的审核")
    p_audit.add_argument("--store", required=True)
    p_audit.add_argument("--known-seq", type=int, default=None)
    p_audit.add_argument("--as-of", default=None)
    p_audit.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = ledger.initialize(args.store, load(args.source))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        elif args.command == "ingest":
            result = ledger.ingest(args.store, load(args.source))
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:
            result = ledger.audit(args.store, known_seq=args.known_seq,
                                  as_of=args.as_of)
            publish(args.output, result)
            print(json.dumps({"known_seq": result["known_seq"],
                              "as_of": result["as_of"],
                              "summary": result["audit"]["summary"]},
                             ensure_ascii=False, sort_keys=True))
    except (OSError, ValueError, KeyError, TypeError, OverflowError,
            sqlite3.Error) as exc:
        parser.exit(2, f"credit-ledger: {exc}\n")


if __name__ == "__main__":
    main()
