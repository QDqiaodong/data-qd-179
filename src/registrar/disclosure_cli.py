"""credit-disclosure 命令行：按院系与入学届的毕业资格公开简报。

沿用原有有界 JSON 加载与原子新建报告方式：成功退出 0，输入或业务规则无效、
输出已存在时退出 2 且保留原输出目标。判断隐私只用本快照与所列公开事实，
不读取原始成绩作为公开输入，也不输出任何保密人数或逐生记录。
"""
import argparse

from .disclosure import report
from .io import load, publish


def main():
    parser = argparse.ArgumentParser(description="院系与入学届毕业资格公开简报")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = report(load(args.source))
        publish(args.output, result)
        summary = {
            "enrollment_cells": len(result["enrollment"]),
            "items": len(result["items"]),
            "withheld": sum(1 for row in result["items"]
                            if row["status"] == "withheld"),
        }
        print(summary)
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"credit-disclosure: {exc}\n")


if __name__ == "__main__":
    main()
