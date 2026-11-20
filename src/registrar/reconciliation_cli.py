"""credit-reconcile 命令行：匿名答卷对账与毕业结论核验。

沿用原项目的有界 JSON 加载与原子新建报告方式：成功退出 0，输入或业务
规则无效、输出已存在时退出 2 且保留原输出目标。
"""
import argparse

from .io import load, publish
from .reconciliation import reconcile


def main():
    parser = argparse.ArgumentParser(description="匿名答卷对账与毕业结论核验")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = reconcile(load(args.source))
        publish(args.output, result)
        summary = {
            "waves": len(result["waves"]),
            "students": len(result["students"]),
            "needs_review": sum(1 for row in result["students"]
                                if row["graduation_status"] == "needs_review"),
            "eligible": sum(1 for row in result["students"]
                            if row["graduation_status"] == "eligible"),
            "ineligible": sum(1 for row in result["students"]
                              if row["graduation_status"] == "ineligible"),
        }
        print(summary)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"credit-reconcile: {exc}\n")


if __name__ == "__main__":
    main()
