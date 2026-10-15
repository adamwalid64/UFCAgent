"""API-reported token accounting, including failed structured responses."""

from __future__ import annotations

import time
from threading import Event


QUOTA_EXHAUSTED = Event()


class QuotaExhaustedError(RuntimeError):
    """The provider reported exhausted credits; stop all further model calls in this process."""


class ModelCallLimitError(RuntimeError):
    """The per-fight paid-request budget has been used."""


def is_quota_error(exc):
    message = str(exc).casefold()
    return getattr(exc, "code", None) == "insufficient_quota" or "insufficient_quota" in message or "no credits remaining" in message


PRICING = {"source": "https://developers.openai.com/api/docs/models/gpt-5.4",
           "checked_on": "2026-10-11", "input_per_million": 2.5,
           "cached_input_per_million": 0.25, "output_per_million": 15.0,
           "note": "Standard USD estimate, not an invoice; excludes regional premiums and unreported transport retries."}


class UsageMeter:
    def __init__(self, max_calls=None, transient_retries=0):
        self.calls = []
        if max_calls is not None and max_calls < 1:
            raise ValueError("max_calls must be positive")
        self.max_calls = max_calls
        if transient_retries not in (0, 1):
            raise ValueError("transient_retries must be 0 or 1")
        self.transient_retries = transient_retries

    @property
    def remaining_calls(self):
        return None if self.max_calls is None else max(0, self.max_calls - len(self.calls))

    def invoke(self, runner, messages, phase, model):
        for attempt in range(self.transient_retries + 1):
            try:
                return self._invoke_once(runner, messages, phase, model)
            except Exception as exc:
                status = getattr(exc, "status_code", None)
                transient = (isinstance(exc, (TimeoutError, ConnectionError)) or
                             type(exc).__name__ in {"APITimeoutError", "APIConnectionError"} or
                             status in {408, 409, 429} or (isinstance(status, int) and 500 <= status < 600))
                if (attempt == self.transient_retries or not transient or self.remaining_calls == 0
                        or isinstance(exc, QuotaExhaustedError) or is_quota_error(exc)):
                    raise
                self.calls[-1]["retry_scheduled"] = True
                time.sleep(1)

    def _invoke_once(self, runner, messages, phase, model):
        if QUOTA_EXHAUSTED.is_set():
            raise QuotaExhaustedError("Model calls stopped: API credits were exhausted earlier in this run")
        if self.max_calls is not None and len(self.calls) >= self.max_calls:
            raise ModelCallLimitError(f"Model call budget ({self.max_calls} per fight) exhausted; no further request sent. "
                                      "Use --max-model-calls to explicitly allow more attempts.")
        started = time.monotonic()
        record = {"phase": phase, "model": model, "usage_reported": False}
        try:
            response = runner.invoke(messages)
            if isinstance(response, dict) and "raw" in response and "parsed" in response:
                raw = response["raw"]
                usage = getattr(raw, "usage_metadata", None)
                if usage:
                    inputs, outputs = usage["input_tokens"], usage["output_tokens"]
                    cached = (usage.get("input_token_details") or {}).get("cache_read", 0) or 0
                    reasoning = (usage.get("output_token_details") or {}).get("reasoning", 0) or 0
                    record.update(usage_reported=True, input_tokens=inputs, output_tokens=outputs,
                                  cached_input_tokens=cached, reasoning_tokens=reasoning,
                                  response_id=(getattr(raw, "response_metadata", {}) or {}).get("id"))
                    if model == "gpt-5.4" or model.startswith("gpt-5.4-20"):
                        factor_in, factor_out = (2, 1.5) if inputs > 272000 else (1, 1)
                        record["estimated_usd"] = ((inputs - cached) * 2.5 * factor_in
                                                    + cached * .25 * factor_in + outputs * 15 * factor_out) / 1e6
                if response.get("parsing_error"):
                    raise ValueError(str(response["parsing_error"]))
                return response["parsed"]
            return response  # Scripted/offline models do not report token usage.
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            if is_quota_error(exc):
                QUOTA_EXHAUSTED.set()
                record["quota_exhausted"] = True
                raise QuotaExhaustedError("API credits exhausted; further model calls stopped. " + str(exc)) from exc
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)

    def summary(self):
        totals = {key: sum(c.get(key, 0) for c in self.calls) for key in (
            "input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")}
        return {**totals, "total_tokens": totals["input_tokens"] + totals["output_tokens"],
                "request_count": len(self.calls), "usage_complete": all(c["usage_reported"] for c in self.calls),
                "estimated_usd": (sum(c["estimated_usd"] for c in self.calls)
                                  if self.calls and all("estimated_usd" in c for c in self.calls) else None),
                "known_estimated_usd": sum(c.get("estimated_usd", 0) for c in self.calls),
                "pricing": PRICING, "calls": [dict(c) for c in self.calls]}
