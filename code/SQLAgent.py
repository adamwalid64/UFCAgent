"""Compatibility entry point: python code/SQLAgent.py FIGHTER_A FIGHTER_B."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sql_agent.__main__ import main
from sql_agent.graph import SQLAnalysisAgent, build_sql_agent

__all__ = ["SQLAnalysisAgent", "build_sql_agent"]


if __name__ == "__main__":
    raise SystemExit(main())
