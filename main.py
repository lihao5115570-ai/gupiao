from __future__ import annotations

import argparse
from pathlib import Path

from stock_monitor.ui import run_app


def main() -> None:
    parser = argparse.ArgumentParser(description="股票持仓监控助手")
    parser.add_argument("--background", action="store_true", help="隐藏主窗口并在后台监控")
    args = parser.parse_args()
    run_app(Path(__file__).resolve().parent, args.background)


if __name__ == "__main__":
    main()
