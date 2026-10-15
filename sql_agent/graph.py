"""Evidence collection, research, drafting, independent review and finalization."""

from __future__ import annotations

import json
import os
import copy
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field

from .analytics import MatchupTools
from .database import MAX_QUERIES, ROOT, QuerySession
from .evidence import build_matchup_evidence
from .compact import FORMAT as COMPACT_FORMAT, INSTRUCTIONS as COMPACT_INSTRUCTIONS, pack_evidence
from .evaluation import MatchupAnalysis, PredictionReview, PredictionReviewError, validate_candidate, validate_review_claims, review_evidence_view
from .usage import UsageMeter


DEFAULT_MODEL = "gpt-5.4"
DEFAULT_REVIEW_MODEL = "gpt-5.4"
DEFAULT_REVIEW_MODE = "off"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_RESEARCH_MODE = "on"
DEFAULT_MAX_OUTPUT_TOKENS = 12000


def _strict_action_schema(schema: dict) -> None:
    # Optional operation arguments are represented by required, nullable keys.
    # Keep Python-side defaults for callers while emitting the API's strict schema.
    schema["required"] = list(schema["properties"])
    for field in schema["properties"].values():
        field.pop("default", None)


class Action(BaseModel):
    """One operation or finish. Nullable arguments apply only to their operation."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_strict_action_schema)
    action: Literal["describe_schema", "run_sql", "get_fighter_history", "get_career_stats",
                    "get_common_opponents", "get_mma_math_paths", "get_opponent_quality",
                    "get_style_matchup", "finish"]
    purpose: str = Field(description="Short explanation of the evidence requested by this operation")
    query: str | None = None
    fighter: str | None = None
    a: str | None = None
    b: str | None = None
    as_of_date: str | None = None
    max_depth: int | None = None
    recent_n: int | None = None


class DimensionFindings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    form: str
    stats: str
    opposition: str
    common_opponents: str
    mma_math: str
    style: str
    activity_and_age: str


class PredictionDraft(BaseModel):
    """Prediction payload. Identity, date and SQL audit are filled by the application."""

    model_config = ConfigDict(extra="forbid")
    predicted_winner: str = Field(description="One of the supplied fighter names or fighter IDs")
    win_probability: float = Field(ge=0.5, le=1.0, allow_inf_nan=False,
                                   description="Probability for the predicted winner")
    predicted_method: Literal["KO/TKO", "Submission", "Decision", "DQ", "Other"]
    predicted_round: int = Field(ge=1, le=5, description="Ending round; a decision ends in the scheduled final round")
    key_factors: list[str]
    dimension_findings: DimensionFindings
    caveats: list[str]
    summary: str = Field(min_length=1, description="Short plain-English explanation")
    opponent_quality_analysis: MatchupAnalysis
    style_history_analysis: MatchupAnalysis
    counterargument: str = Field(min_length=1, description="Evidence-based case for the other fighter, with uncertainty")
    confidence_justification: str = Field(min_length=1, description="Explain the confidence estimate in light of sample size and conflicting evidence")


class AgentState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    action: Action
    observation: dict[str, Any]
    done: bool
    result: dict[str, Any]
    evidence: dict[str, Any]
    observations: list[dict[str, Any]]
    candidate: dict[str, Any]
    review_history: list[dict[str, Any]]
    approved: bool
    revision_count: int


# Operational signatures, not analysis instructions. The user owns the system prompt.
TOOL_CONTRACTS = {
    "describe_schema": "No arguments. Column names/types of the cutoff-isolated SQL workspace.",
    "run_sql": "query: SELECT/WITH; purpose. At most row_limit rows, with a truncation flag.",
    "get_fighter_history": "fighter; optional as_of_date. Complete eligible per-bout history.",
    "get_career_stats": "fighter; optional as_of_date, recent_n (3..5). Career/recent rates and record.",
    "get_common_opponents": "a, b; optional as_of_date. Shared opponents and results.",
    "get_mma_math_paths": "a, b; optional as_of_date, max_depth (1..5; default 3). Counts, weights, examples.",
    "get_opponent_quality": "fighter; optional as_of_date. Derived Elo tiers, historical opposition and style evidence. Supplied matchup uses the mandatory evidence cache.",
    "get_style_matchup": "a, b; optional as_of_date. Current and historical style profiles with relevant bouts. Supplied matchup uses the mandatory evidence cache.",
    "finish": "No arguments. Transition to structured prediction synthesis.",
}


def build_graph(session: QuerySession, llm, fighter_a: dict, fighter_b: dict, *,
                review_prompt: str, review_llm=None, max_revisions: int = 2,
                scheduled_rounds: int = 3, on_progress: Callable[[str], None] | None = None,
                review_mode: str = DEFAULT_REVIEW_MODE, usage: UsageMeter | None = None,
                research_mode: str = DEFAULT_RESEARCH_MODE,
                on_initial_prediction: Callable[[dict], None] | None = None, coverage=None, central_node=None):
    planner = llm.with_structured_output(Action, method="function_calling", strict=True, include_raw=True)
    synthesizer = llm.with_structured_output(PredictionDraft, method="function_calling", strict=True, include_raw=True)
    reviewer = ((review_llm or llm).with_structured_output(PredictionReview, method="function_calling", strict=True, include_raw=True)
                if review_mode == "on" else None)
    usage = usage or UsageMeter()
    initial_emitted = False
    # Keep room for output-schema repair and deterministic draft corrections.
    requested_reserve = 2 * (max_revisions + 1) * (2 if review_mode == "on" else 1)
    finalization_reserve = min(requested_reserve, max(1, usage.max_calls // 2)) if usage.max_calls else 1

    def invoke(runner, messages, phase):
        model = (review_llm or llm) if phase == "review" else llm
        return usage.invoke(runner, messages, phase, getattr(model, "model_name", type(model).__name__))
    helpers = MatchupTools(session)

    def collect_evidence(state: AgentState):
        if on_progress:
            on_progress("  Building mandatory opponent-strength and historical style evidence...")
        rows = session.history_rows(None, "Mandatory point-in-time opponent-strength and style evidence")
        evidence = build_matchup_evidence(rows, fighter_a, fighter_b, session.as_of_date,
                                          timeout=max(15.0, session.timeout))
        observations = []
        if research_mode == "off":
            for name in ("get_common_opponents", "get_mma_math_paths"):
                try:
                    if not session.remaining:
                        result = {"unavailable": "SQL query budget exhausted; do not infer missing findings"}
                    else:
                        result = getattr(helpers, name)(fighter_a["fighter_id"], fighter_b["fighter_id"])
                except (ValueError, TimeoutError) as exc:
                    result = {"unavailable": str(exc)}
                observations.append({"operation": {"action": name}, "result": result})
        payload = {"mandatory_evidence": pack_evidence(evidence) if research_mode == "off" else evidence,
                   "remaining_queries": session.remaining}
        if research_mode == "off":
            payload.update(evidence_encoding=COMPACT_INSTRUCTIONS, supplemental_observations=observations)
        if coverage and (coverage.sources or coverage.caveats):
            payload["external_coverage"] = coverage.analyst_payload()
        return {"evidence": evidence, "observations": observations, "review_history": [], "revision_count": 0,
                "messages": [HumanMessage(content=json.dumps(payload, separators=(",", ":")))]}

    def plan(state: AgentState):
        if usage.remaining_calls is not None and usage.remaining_calls <= finalization_reserve:
            return {"action": Action(action="finish", purpose="Reserve remaining requests for drafting and validation repairs")}
        feedback = []
        for attempt in range(2):
            if usage.remaining_calls is not None and usage.remaining_calls <= finalization_reserve:
                action = Action(action="finish", purpose="Reserve the remaining request after a planning error")
                break
            try:
                action = Action.model_validate(invoke(planner, state["messages"] + feedback, "plan"))
                break
            except ValueError as exc:
                session.consume_attempt()
                feedback.append(HumanMessage(content=json.dumps({
                    "operation_validation_error": str(exc), "valid_operations": list(TOOL_CONTRACTS),
                    "remaining_queries": session.remaining,
                })))
                if not session.remaining:
                    action = Action(action="finish", purpose="Query budget exhausted after invalid model operation")
                    break
                if attempt:
                    raise
        if on_progress:
            on_progress(f"  {action.purpose}")
        return {"action": action, "messages": feedback}

    def query(state: AgentState):
        action = state["action"]
        if action.action == "finish":
            return {"observation": {"finished": True}}
        before = session.attempts
        try:
            session.check_date(action.as_of_date)
            if action.action == "describe_schema":
                result = session.describe_schema()
            elif action.action == "run_sql":
                result = session.run_sql(action.query, action.purpose)
            elif action.action in {"get_opponent_quality", "get_style_matchup"}:
                # Reuse one definition of strength/styles throughout research and
                # review; the older raw-record helper tiers use a different cohort.
                requested = ([session.resolve_fighter(action.fighter)] if action.action == "get_opponent_quality"
                             else [session.resolve_fighter(action.a or fighter_a["fighter_id"]),
                                   session.resolve_fighter(action.b or fighter_b["fighter_id"])])
                packet = state["evidence"]
                available = {packet[side]["fighter"]["fighter_id"]: packet[side] for side in ("a", "b")}
                if any(identity["fighter_id"] not in available for identity in requested):
                    other = requested[-1] if len(requested) == 2 else fighter_a
                    packet = build_matchup_evidence(session.history_rows(None, action.purpose), requested[0], other,
                                                     session.as_of_date, timeout=max(15.0, session.timeout))
                    available = {packet[side]["fighter"]["fighter_id"]: packet[side] for side in ("a", "b")}
                else:
                    session.consume_attempt()  # Cached reads still consume the tool budget.
                    # All of this evidence is already in the model's context. Keep
                    # the source fields intact there instead of duplicating them.
                    sides = {packet[side]["fighter"]["fighter_id"]: side for side in ("a", "b")}
                    return {"observation": {
                        "as_of_date": session.as_of_date,
                        "evidence_references": [{"fighter": identity,
                                                 "path": "mandatory_evidence." + sides[identity["fighter_id"]]}
                                                for identity in requested],
                        "note": "Use these complete fighter records already supplied in mandatory_evidence, including all bouts, opposition tiers and historical style groups. No new or changed evidence.",
                    }}
                contexts = [available[identity["fighter_id"]] for identity in requested]
                result = {"methodology": packet["methodology"], "limitations": packet["limitations"],
                          "fighters": contexts}
            else:
                params = {"as_of_date": action.as_of_date, "purpose": action.purpose}
                if action.action in {"get_fighter_history", "get_career_stats", "get_opponent_quality"}:
                    params["fighter"] = action.fighter
                else:
                    params.update(a=action.a or fighter_a["fighter_id"], b=action.b or fighter_b["fighter_id"])
                if action.action == "get_career_stats" and action.recent_n is not None:
                    params["recent_n"] = action.recent_n
                if action.action == "get_mma_math_paths" and action.max_depth is not None:
                    params["max_depth"] = action.max_depth
                result = getattr(helpers, action.action)(**params)
        except (ValueError, TypeError, AttributeError, TimeoutError) as exc:
            if session.attempts == before:
                session.consume_attempt()
            result = {"error": str(exc)}
        return {"observation": result}

    def observe(state: AgentState):
        payload = {"operation": state["action"].model_dump(exclude_none=True),
                   "result": state["observation"], "remaining_queries": session.remaining}
        return {"messages": [HumanMessage(content=json.dumps(payload, ensure_ascii=False))],
                "observations": state.get("observations", []) + [payload]}

    def decide(state: AgentState):
        return {"done": state["action"].action == "finish" or session.remaining == 0}

    def synthesize(state: AgentState):
        request = {
            "phase": "synthesize", "remaining_queries": session.remaining,
            "fighter_a": fighter_a, "fighter_b": fighter_b, "as_of_date": session.as_of_date,
            "scheduled_rounds": scheduled_rounds,
        }
        if research_mode == "on":
            request["matchup_focus_references"] = {side: [f"mandatory_evidence.{side}.{key}" for key in (
                "next_opponent_style", "history_against_next_opponent_tags", "similar_to_next_opponent")]
                for side in ("a", "b")}
        else:
            request["output_style"] = ("Be concise: aim for 350 words of prose in total, three key factors, "
                "one sentence per dimension, and two sentences per fighter's opposition/style assessment. "
                "Retain specific bout citations, counterevidence, confidence limits and method/round uncertainty. "
                "Use next_opponent_style and historical analogues already in mandatory_evidence.")
        messages = state["messages"] + [HumanMessage(content=json.dumps(request, separators=(",", ":")))]
        for attempt in range(2):
            try:
                draft = PredictionDraft.model_validate(invoke(synthesizer, messages, "revision" if state["revision_count"] else "draft"))
                break
            except ValueError as exc:
                if attempt or usage.remaining_calls == 0:
                    raise
                messages.append(HumanMessage(content=json.dumps({"output_validation_error": str(exc)})))
        return {"candidate": draft.model_dump()}

    def review(state: AgentState):
        nonlocal initial_emitted
        deterministic_issues = validate_candidate(state["candidate"], state["evidence"], scheduled_rounds)
        if coverage:
            deterministic_issues.extend(coverage.publication_issues(state["candidate"]))
            deterministic_issues = list(dict.fromkeys(deterministic_issues))
        if not deterministic_issues and not initial_emitted and on_initial_prediction:
            on_initial_prediction(package(state, deterministic=True))
            initial_emitted = True
        if review_mode == "off":
            if not deterministic_issues:
                return {"approved": True}
            if state["revision_count"] >= max_revisions or usage.remaining_calls == 0:
                raise PredictionReviewError("Deterministic validation failed: " + "; ".join(deterministic_issues))
            return {"approved": False, "revision_count": state["revision_count"] + 1,
                    "messages": [HumanMessage(content=json.dumps({"phase": "revise", "previous_draft": state["candidate"],
                        "required_corrections": deterministic_issues, "instruction": "Repair the invalid fields against the supplied evidence."}))]}
        if on_progress:
            on_progress(f"  Final evidence review {len(state['review_history']) + 1}...")
        # Fresh context: the reviewer gets the facts and draft, not the writer's
        # conversation or an instruction to agree with its conclusion.
        messages = [SystemMessage(content=review_prompt), HumanMessage(content=json.dumps({
            "phase": "review", "candidate": state["candidate"], "mandatory_evidence": review_evidence_view(state["evidence"]),
            "supplemental_observations": state["observations"], "scheduled_rounds": scheduled_rounds,
            "deterministic_issues": deterministic_issues,
            "external_coverage": coverage.analyst_payload() if coverage else None,
        }))]
        for attempt in range(2):
            verdict_payload = None
            try:
                verdict_payload = invoke(reviewer, messages, "review")
                verdict = PredictionReview.model_validate(verdict_payload)
                if (not all(verdict.checks.model_dump().values()) and not verdict.issues
                        and all(check.supported for check in verdict.claim_checks)):
                    raise ValueError("Every failed review check needs a concrete issue naming the unsupported claim and its correction")
                source_issues = validate_review_claims(verdict, state["evidence"], include_unsupported=False)
                if source_issues:
                    raise ValueError("; ".join(source_issues))
                break
            except ValueError as exc:
                if attempt:
                    raise PredictionReviewError(f"Final reviewer returned an invalid assessment: {exc}",
                                                review_history=state["review_history"]) from exc
                previous = verdict_payload.model_dump() if isinstance(verdict_payload, BaseModel) else verdict_payload
                messages.append(HumanMessage(content=json.dumps({"review_validation_error": str(exc),
                                                                  "previous_invalid_review": previous})))
        review_issues = validate_review_claims(verdict, state["evidence"])
        issues = list(dict.fromkeys(deterministic_issues + review_issues + verdict.issues + [
            f"Review check failed: {name}" for name, passed in verdict.checks.model_dump().items() if not passed]))
        approved = not issues
        audit = {**verdict.model_dump(), "deterministic_issues": deterministic_issues,
                 "review_evidence_issues": review_issues, "approved": approved}
        history = state["review_history"] + [audit]
        if not approved and state["revision_count"] >= max_revisions:
            raise PredictionReviewError(f"Prediction withheld after {len(history)} reviews: {'; '.join(issues)}",
                                        review_history=history)
        update = {"approved": approved, "review_history": history}
        if not approved:
            if on_progress:
                on_progress("  Review requested corrections; revising before publication...")
                on_progress(f"  First issue: {issues[0][:240]}")
            update.update(revision_count=state["revision_count"] + 1, messages=[HumanMessage(content=json.dumps({
                "phase": "revise", "previous_draft": state["candidate"], "required_corrections": issues,
                "instruction": "Revise the structured draft against the supplied evidence. Resolve every issue; do not invent missing evidence.",
            }))])
        return update

    def package(state: AgentState, *, deterministic: bool):
        result = copy.deepcopy(state["candidate"])
        winner = session.resolve_fighter(result["predicted_winner"])
        result.update(fighter_a=fighter_a["fighter_name"], fighter_b=fighter_b["fighter_name"],
                      as_of_date=session.as_of_date, predicted_winner=winner["fighter_name"],
                      scheduled_rounds=scheduled_rounds, queries_run=list(session.queries_run))
        if deterministic:
            result["evaluation"] = {"status": "validated", "mode": "deterministic", "review_rounds": 0,
                                    "revision_count": state["revision_count"], "history": [], "cautions": [],
                                    "summary": "Schema, identity, distance and historical references checked in code; no LLM self-review.",
                                    "deterministic_checks": {key: True for key in ("schema", "matchup_identity", "scheduled_distance", "historical_references", "cutoff_isolation")}}
        else:
            last_review = state["review_history"][-1]
            result["evaluation"] = {"status": "approved", "mode": "model_review", "review_rounds": len(state["review_history"]),
                                "revision_count": state["revision_count"], "checks": last_review["checks"],
                                "summary": last_review["summary"], "cautions": last_review["cautions"],
                                "history": state["review_history"]}
            result["caveats"].extend(last_review["cautions"])
        result["matchup_evidence"] = state["evidence"]
        if coverage and (coverage.sources or coverage.caveats):
            result["external_coverage"] = coverage.public()
            result["caveats"].extend(coverage.caveats)
            # Reviewer prose is also public. Raw segments never enter the result,
            # ledger, JSON report or the initial-prediction callback.
            result = coverage.redact_publication(result)
        result["model_usage"] = usage.summary()
        if usage.remaining_calls == 0:
            result["caveats"].append("Model request budget exhausted; no additional research or repair requests were available.")
        if not session.remaining:
            result["caveats"].append(f"Query budget ({session.max_queries}) exhausted; analysis may be incomplete.")
        if any("error" in item for item in session.queries_run):
            result["caveats"].append("Some SQL attempts failed; the query log includes their errors.")
        result["caveats"] = list(dict.fromkeys(result["caveats"]))
        return result

    def finalize(state: AgentState):
        if not state["approved"]:
            raise PredictionReviewError("Unapproved prediction cannot be finalized")
        return {"result": package(state, deterministic=review_mode == "off")}

    def central(state: AgentState):
        return {"result": central_node.finalize(state["result"]) if central_node else state["result"]}

    graph = StateGraph(AgentState)
    for name, node in (("collect_evidence", collect_evidence), ("plan", plan), ("query", query), ("observe", observe),
                       ("decide", decide), ("synthesize", synthesize), ("review", review), ("finalize", finalize), ("central", central)):
        graph.add_node(name, node)
    graph.add_edge(START, "collect_evidence")
    graph.add_conditional_edges("collect_evidence", lambda state: "plan" if research_mode == "on" and session.remaining else "synthesize")
    graph.add_edge("plan", "query")
    graph.add_edge("query", "observe")
    graph.add_edge("observe", "decide")
    graph.add_conditional_edges("decide", lambda state: "synthesize" if state["done"] else "plan")
    graph.add_edge("synthesize", "review")
    graph.add_conditional_edges("review", lambda state: "finalize" if state["approved"] else "synthesize")
    graph.add_edge("finalize", "central")
    graph.add_edge("central", END)
    return graph.compile()


class SQLAnalysisAgent:
    def __init__(self, db_path: str | Path = ROOT / "ufc_fights.db", *, llm=None,
                 model: str | None = None, prompt_path: str | Path = ROOT / "prompts/sql_agent_system.md",
                 max_queries: int = MAX_QUERIES, row_limit: int = 200, timeout: float = 5.0,
                 review_llm=None, review_model: str | None = None,
                 review_prompt_path: str | Path = ROOT / "prompts/sql_agent_review.md",
                 max_revisions: int = 2, reasoning_effort: str | None = None, model_timeout: float = 120.0,
                 review_mode: str | None = None, research_mode: str | None = None,
                 max_model_calls: int | None = None, max_output_tokens: int | None = None,
                 coverage_provider=None, market_context_path=None):
        load_dotenv(ROOT / ".env", override=False)
        self.db_path, self.prompt_path = Path(db_path), Path(prompt_path)
        if coverage_provider is None:
            from fight_coverage import CoveragePipeline
            coverage_provider = CoveragePipeline()
        self.coverage_provider = coverage_provider
        from .central import CentralNode
        self.central_node = CentralNode(market_context_path or os.getenv("CENTRAL_MARKET_CONTEXT_PATH"))
        self.max_queries, self.row_limit, self.timeout = max_queries, row_limit, timeout
        self.review_mode = review_mode or os.getenv("SQL_AGENT_REVIEW_MODE", DEFAULT_REVIEW_MODE)
        self.research_mode = research_mode or os.getenv("SQL_AGENT_RESEARCH_MODE", DEFAULT_RESEARCH_MODE)
        if self.research_mode not in {"on", "off"}:
            raise ValueError("research_mode must be on or off")
        default_calls = 24 if self.research_mode == "on" else 8 if self.review_mode == "on" else 1
        self.max_model_calls = int(max_model_calls if max_model_calls is not None else os.getenv("SQL_AGENT_MAX_MODEL_CALLS", default_calls))
        self.max_output_tokens = int(max_output_tokens if max_output_tokens is not None else os.getenv("SQL_AGENT_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS))
        if self.max_model_calls < 1 or self.max_output_tokens < 1:
            raise ValueError("Model call and output token limits must be positive")
        if self.review_mode not in {"on", "off"}:
            raise ValueError("review_mode must be on or off")
        if not 0 <= max_revisions <= 3:
            raise ValueError("max_revisions must be between 0 and 3")
        if model_timeout <= 0:
            raise ValueError("model_timeout must be positive")
        self.review_prompt_path, self.max_revisions = Path(review_prompt_path), max_revisions
        if llm is None:
            load_dotenv(ROOT / ".env", override=False)
            if not os.getenv("OPENAI_API_KEY"):
                raise ValueError("Set OPENAI_API_KEY in the repo's .env file or your environment before running the live model.")
            from langchain_openai import ChatOpenAI

            selected_model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
            review_model = review_model or os.getenv("OPENAI_REVIEW_MODEL", DEFAULT_REVIEW_MODEL)
            reasoning_effort = (reasoning_effort or os.getenv("OPENAI_REASONING_EFFORT")
                                or (DEFAULT_REASONING_EFFORT if selected_model == DEFAULT_MODEL else None))
            options = {"reasoning_effort": reasoning_effort} if reasoning_effort else {}
            llm = ChatOpenAI(model=selected_model,
                             timeout=model_timeout, max_retries=0, max_tokens=self.max_output_tokens, use_responses_api=True, **options)
        if self.review_mode == "on" and review_llm is None and review_model:
            load_dotenv(ROOT / ".env", override=False)
            from langchain_openai import ChatOpenAI
            review_llm = ChatOpenAI(model=review_model, timeout=model_timeout, max_retries=0,
                                    max_tokens=self.max_output_tokens, use_responses_api=True)
        self.llm = llm
        self.review_llm = review_llm or llm
        self.model_config = {"analysis_model": getattr(llm, "model_name", model or type(llm).__name__),
                             "review_model": (getattr(self.review_llm, "model_name", review_model or type(self.review_llm).__name__)
                                              if self.review_mode == "on" else None),
                             "review_mode": self.review_mode,
                             "reasoning_effort": getattr(llm, "reasoning_effort", reasoning_effort),
                             "max_revisions": max_revisions, "research_mode": self.research_mode,
                             "max_model_calls": self.max_model_calls, "max_output_tokens": self.max_output_tokens,
                             "evidence_encoding": COMPACT_FORMAT if self.research_mode == "off" else "full_json"}

    def predict(self, fighter_a: str, fighter_b: str, as_of_date: str | None = None, *,
                scheduled_rounds: int = 3, rounds_basis: str = "assumed standard distance",
                fighter_identities: list[dict[str, str]] | None = None,
                event_name: str | None = None,
                on_progress: Callable[[str], None] | None = None,
                on_initial_prediction: Callable[[dict], None] | None = None) -> dict:
        if scheduled_rounds not in (3, 5):
            raise ValueError("scheduled_rounds must be 3 or 5")
        prompt = self.prompt_path.read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError(f"System prompt is empty: {self.prompt_path}")
        review_prompt = self.review_prompt_path.read_text(encoding="utf-8").strip() if self.review_mode == "on" else ""
        if self.review_mode == "on" and not review_prompt:
            raise ValueError(f"Review prompt is empty: {self.review_prompt_path}")
        with QuerySession(self.db_path, as_of_date=as_of_date, max_queries=self.max_queries,
                          row_limit=self.row_limit, timeout=self.timeout,
                          fighter_identities=fighter_identities) as session:
            a, b = session.resolve_fighter(fighter_a), session.resolve_fighter(fighter_b)
            if a["fighter_id"] == b["fighter_id"]:
                raise ValueError("Choose two different fighters")
            from fight_coverage import FightCoverage
            try:
                coverage = self.coverage_provider.get_fight_coverage(
                    {"fighter_a": a["fighter_name"], "fighter_b": b["fighter_name"],
                     "event_name": event_name or ""}, session.as_of_date)
            except Exception as exc:
                coverage = FightCoverage(caveats=[f"YouTube coverage unavailable ({type(exc).__name__}); prediction uses database evidence."])
            usage = UsageMeter(max_calls=self.max_model_calls, transient_retries=1 if self.research_mode == "on" else 0)
            from fight_coverage.signals import extract_signals
            extract_signals(coverage, {"fighter_a": a["fighter_name"], "fighter_b": b["fighter_name"]}, self.llm, usage)

            def initial(result):
                result["model_config"] = {**self.model_config, "review_mode": "off", "review_model": None}
                if on_initial_prediction:
                    on_initial_prediction(result)

            graph = build_graph(session, self.llm, a, b, scheduled_rounds=scheduled_rounds,
                                review_prompt=review_prompt, review_llm=self.review_llm, max_revisions=self.max_revisions,
                                on_progress=(lambda message: on_progress(coverage.redact_publication(message))) if on_progress else None,
                                review_mode=self.review_mode, usage=usage,
                                research_mode=self.research_mode,
                                coverage=coverage,
                                central_node=self.central_node,
                                on_initial_prediction=initial if on_initial_prediction else None)
            request = {"request": "Predict this matchup's winner, confidence, ending method and round, with a justification.",
                       "fighter_a": a, "fighter_b": b, "as_of_date": session.as_of_date,
                       "scheduled_rounds": scheduled_rounds, "rounds_basis": rounds_basis,
                       "max_queries": self.max_queries, "row_limit": self.row_limit,
                       "operations": TOOL_CONTRACTS if self.research_mode == "on" else {},
                       "research_mode": self.research_mode}
            try:
                state = graph.invoke({"messages": [SystemMessage(content=prompt),
                                                HumanMessage(content=json.dumps(request))]},
                                 config={"recursion_limit": 4 * self.max_queries + 2 * self.max_revisions + 12})
            except Exception as exc:
                exc.model_usage = usage.summary()
                if coverage.sources:
                    exc.args = tuple(coverage.redact_publication(arg) for arg in exc.args)
                    if hasattr(exc, "review_history"):
                        exc.review_history = coverage.redact_publication(exc.review_history)
                raise
            result = state["result"]
            result["model_config"] = self.model_config.copy()
            if rounds_basis.startswith("assumed"):
                result["caveats"].append(f"Scheduled distance assumes {scheduled_rounds} rounds; verify the bout agreement.")
            return result


def build_sql_agent(db_path: str | Path = ROOT / "ufc_fights.db", **kwargs) -> SQLAnalysisAgent:
    return SQLAnalysisAgent(db_path, **kwargs)
