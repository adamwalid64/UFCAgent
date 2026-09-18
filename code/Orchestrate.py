import os
from langchain_openai import OpenAI
from typing import TypedDict
from langgraph.graph import StateGraph, END

# Define Toy state shape
class State(TypedDict):
    # Event Details
    cardName: str
    fighterA: str
    fighterB: str
    cardDate: str
    


    # Routing States
    needs_databaseQuery: bool

    # Final Result
    cardAnalysis: str

# Node function definitions
def start_node(state: State) -> dict:
    # decide something based on state["input_text"]
    ...
    return {"needs_databaseQuery": ...}

# Node for querying SQL Database
def databaseQuery(state: State, ) -> dict:
    # LOGIC for querying SQL Database and returning structured data
    pass



# # Routing function
# def route(state: State) -> str:
#     if state["needs_databaseQuery"]:
#         return "databaseQuery"
#     return "node_a"

# # Graph assembly
# from langgraph.graph import StateGraph, END

# graph = StateGraph(ToyState)
# graph.add_node("start", start_node)
# graph.add_node("node_a", node_a)
# graph.add_node("node_b", node_b)

# graph.set_entry_point("start")
# graph.add_conditional_edges("start", route, {"node_a": "node_a", "node_b": "node_b"})
# graph.add_edge("node_a", END)
# graph.add_edge("node_b", END)

# app = graph.compile()


# # Run Graph
# result = app.invoke({"input_text": "hello world", "needs_b": False, "result": ""})
# print(result)