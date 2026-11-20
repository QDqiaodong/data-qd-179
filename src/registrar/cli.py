import argparse
from .io import load, publish
from .service import run


def main():
    parser = argparse.ArgumentParser(description="学分与先修课审核")
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        result = run(load(args.source))
        publish(args.output, result)
        print(result["summary"])
    except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
        parser.exit(2, f"credit-audit: {exc}\n")


if __name__ == "__main__":
    main()
