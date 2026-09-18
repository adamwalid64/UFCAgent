"""Boilerplate for asking natural-language questions about UFC fight data."""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

from langchain.agents import create_agent
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.utilities import SQLDatabase
from langchain_openai import ChatOpenAI
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool


# 1. Configuration: resolve the database relative to this file, not the shell.
DATABASE_PATH = Path(__file__).resolve().parents[1] / "ufc_fights_fresh.db"

SYSTEM_PROMPT = """
You answer questions about completed UFC fights using a SQLite database.
List the available tables first, then inspect relevant schemas before writing SQL.
Use the SQL query checker before executing a query. Correct SQL errors and retry.
Only run read-only queries. Never modify data or schema.
Select only relevant columns and limit detail queries to 10 rows unless the user
requests more. Aggregate over all matching rows when answering aggregate questions.
Use stable fighter IDs for joins; names may not be unique.
Do not treat missing statistics as zero or invent results that are not in the data.
For historical predictions, only use information available before the target date;
current fighter totals and the target bout's outcomes are not pre-fight features.
Answer from query results and include the SQL used so the answer can be checked.
If the database cannot answer the question, explain what information is missing.
"""


# 2. Database connection: SQLite enforces read-only access.
def connect_read_only(db_path: Path) -> sqlite3.Connection:
    return sqlite3.connect(
        db_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        check_same_thread=False,
    )


def build_sql_agent(db_path: Path = DATABASE_PATH, *, model: str | None = None):
    """Build once and reuse the returned agent for multiple questions."""
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError(f"Database does not exist: {db_path}")
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Set OPENAI_API_KEY before creating the SQL agent.")

    engine = create_engine(
        "sqlite://",
        creator=lambda: connect_read_only(db_path),
        poolclass=NullPool,
    )
    database = SQLDatabase(engine, sample_rows_in_table_info=2)

    # 3. Model: use a chat model that supports tool calling.
    llm = ChatOpenAI(model=model or os.getenv("OPENAI_MODEL", "gpt-4.1-mini"))

    # 4. Tools: list tables, inspect schemas, check SQL, and execute SQL.
    toolkit = SQLDatabaseToolkit(db=database, llm=llm)

    # 5. Agent: the model chooses tools until it can answer the question.
    return create_agent(
        model=llm,
        tools=toolkit.get_tools(),
        system_prompt=SYSTEM_PROMPT,
    )


def ask_sql_agent(agent, question: str) -> str:
    """Entry point for a future node in Orchestrate.py."""
    if not question.strip():
        raise ValueError("Provide a non-empty question.")
    result = agent.invoke(
        {"messages": [{"role": "user", "content": question}]},
        config={"recursion_limit": 30},
    )
    return result["messages"][-1].text


# 6. Standalone example: python code/SQLAgent.py "How many fights are stored?"
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("question", help="Natural-language question about UFC data")
    parser.add_argument("--model", default=None, help="Override OPENAI_MODEL")
    args = parser.parse_args()
    agent = build_sql_agent(model=args.model)
    print(ask_sql_agent(agent, args.question))


if __name__ == "__main__":
    main()
