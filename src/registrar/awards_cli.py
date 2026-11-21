"""credit-awards 命令行：主辅修联合审核。

沿用原项目的有界 JSON 加载与原子新建报告方式：成功退出 0，输入或业务
规则无效、输出已存在时退出 2 且保留原输出目标。
"""
import argparse

from .awards import review
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(description="主辅修联合审核")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = review(load(args.source))
        publish(args.output, result)
        summary = {
            "students": len(result["students"]),
            "primary_eligible": sum(1 for row in result["students"]
                                    if row["primary"]["eligible"]),
            "secondary_eligible": sum(1 for row in result["students"]
                                      if row["secondary"]["eligible"]),
            "joint_eligible": sum(
                1 for row in result["students"]
                if row["primary"]["eligible"] and row["secondary"]["eligible"]),
        }
        print(summary)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"credit-awards: {exc}\n")


if __name__ == "__main__":
    main()
