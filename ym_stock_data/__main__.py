"""CLI 入口: python3 -m ym_stock_data"""
import sys
from .fetch import fetch, list_supported


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("用法: python3 -m ym_stock_data <data_type> [key=val ...]")
        print(f"\n支持的数据类型 ({len(list_supported())}):")
        for name, desc in list_supported().items():
            print(f"  {name:20s} {desc}")
        return

    data_type = args[0]
    kwargs = {}
    for a in args[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            kwargs[k] = v

    result = fetch(data_type, **kwargs)
    import json
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
