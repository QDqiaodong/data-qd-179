import argparse
from .io import load, publish
from .planning import plan


def main():
    parser = argparse.ArgumentParser(description="小班群体毕业修读计划")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = plan(load(args.source))
        publish(args.output, result)
        print(result["objective"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"credit-plan: {exc}\n")


if __name__ == "__main__":
    main()
