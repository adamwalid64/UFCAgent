"""Deterministic post-card scoring; never changes a forecast or calls a model."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


START = b"\n\n=== POST-CARD RESULTS (managed) ===\n"
END = "=== END POST-CARD RESULTS ===\n"
PAIR = re.compile(r"^([^\n|]+?) vs ([^\n|]+?)(?: \|[^\n]*)?\r?$(?=\n(?:Pick:|Prediction failed:))", re.M)


class IncompleteReportError(ValueError):
    """Do not append outcomes while the prediction writer may still be running."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, payload: dict) -> None:
    atomic_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))


def atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def event_key(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme not in {"http", "https"} or parsed.netloc not in {"ufcstats.com", "www.ufcstats.com"}
            or not re.fullmatch(r"/event-details/[a-f0-9]{16}", parsed.path)
            or parsed.query or parsed.fragment):
        raise ValueError("Expected a specific UFCStats event URL")
    return parsed.path.rsplit("/", 1)[-1]


def name_key(name: str) -> str:
    # Keep punctuation and suffixes: this deliberately does not fuzzy-match people.
    return " ".join(unicodedata.normalize("NFKC", name).casefold().split())


def pair_key(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((name_key(a), name_key(b))))


def original_bytes(path: Path) -> bytes:
    return path.read_bytes().split(START, 1)[0]


def archive_report(path: Path) -> Path:
    """Retain byte-identical forecasts, including user annotations and sidecars."""
    original = original_bytes(path)
    directory = path.parent / "archive" / digest(original)
    directory.mkdir(parents=True, exist_ok=True)
    for source, data in [(path, original), *[(p, p.read_bytes()) for p in
                         (path.with_suffix(".json"), path.with_suffix(".ledger.jsonl")) if p.is_file()]]:
        target = directory / source.name
        if target.exists():
            if target.read_bytes() != data:
                raise ValueError(f"Archived forecast differs from current source: {source}")
        else:
            with target.open("xb") as handle:
                handle.write(data)
    return directory / path.name


def _field(pattern: str, text: str, default=None):
    found = re.search(pattern, text, re.M)
    return found.group(1).strip() if found else default


def read_report(path: str | Path, *, event_url: str | None = None) -> dict:
    path = Path(path).resolve()
    raw = original_bytes(path)
    text = raw.decode("utf-8-sig")
    cutoff = _field(r"^Evidence cutoff: strictly before (\d{4}-\d{2}-\d{2})", text)
    day = _field(r"^Event date: (\d{4}-\d{2}-\d{2})", text)
    if not day:
        day = _field(r"\| (\d{4}-\d{2}-\d{2})$", text.splitlines()[0])
    source = _field(r"^Source: (\S+)", text)
    if source and event_url and event_key(source) != event_key(event_url):
        raise ValueError(f"Report belongs to another event: {path.name}")
    source = source or event_url
    predictions = []
    matches = list(PAIR.finditer(text))
    for i, match in enumerate(matches):
        block = text[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(text)]
        winner = _field(r"^Pick: (.+)$", block)
        error = _field(r"^Prediction failed: (.+)$", block)
        if not winner and not error:
            continue  # e.g. the report's repeated single-fight title
        prediction = {"fighter_a": match[1].strip(), "fighter_b": match[2].strip()}
        if error:
            prediction["error"] = error
        else:
            ending = re.search(r"^Predicted ending: (.+), round (\d+)\r?$", block, re.M)
            probability = _field(r"^Confidence: ([\d.]+)%", block)
            if not ending or probability is None:
                raise ValueError(f"Incomplete forecast fields: {prediction} in {path.name}")
            prediction.update(predicted_winner=winner, win_probability=float(probability) / 100,
                              predicted_method=ending[1], predicted_round=int(ending[2]))
            distance = _field(r"Scheduled distance assumes ([35]) rounds", block)
            if distance:
                prediction["scheduled_rounds"] = int(distance)
        predictions.append(prediction)
    mode = "legacy_recorded_prediction"
    generated = _field(r"^(?:Generated|Status checked): (.+)$", text)
    ledger = path.with_suffix(".ledger.jsonl")
    if ledger.is_file():
        records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not records or records[-1].get("kind") != "complete":
            raise IncompleteReportError(f"Prediction run is incomplete: {path.name}; completed picks remain saved")
        header = records[0]
        if header.get("kind") != "run":
            raise ValueError(f"Invalid forecast ledger: {ledger}")
        metadata = header.get("card") or {}
        ledger_source = metadata.get("event_url")
        if source and ledger_source and event_key(source) != event_key(ledger_source):
            raise ValueError("Ledger and text report disagree on event identity")
        source = ledger_source or source
        day = metadata.get("event_date") or day
        cutoff = header["as_of_date"]
        mode = header["analysis_mode"]
        generated = header["generated_at"]
        saved = [record["prediction"] for record in records if record["kind"] == "prediction"]
        # A ledger must agree with the readable picks; never silently score a different report.
        if len(saved) != len(predictions):
            raise ValueError("Ledger and text report have different forecast counts")
        for visible, structured in zip(predictions, saved):
            for key in ("fighter_a", "fighter_b", "predicted_winner", "predicted_method", "predicted_round"):
                if visible.get(key) != structured.get(key):
                    raise ValueError(f"Ledger and text report disagree on {key}")
            if "error" not in visible and abs(visible["win_probability"] - structured["win_probability"]) > 0.000500001:
                raise ValueError("Ledger and text report disagree on confidence")
        predictions = saved
    if not predictions:
        raise ValueError(f"No published predictions or failed attempts in {path.name}")
    if not cutoff:
        raise ValueError(f"Report has no evidence cutoff: {path.name}")
    keys = [pair_key(p["fighter_a"], p["fighter_b"]) for p in predictions]
    if len(keys) != len(set(keys)):
        raise ValueError("Duplicate matchup in prediction report")
    for prediction in predictions:
        if "error" not in prediction:
            if name_key(prediction["predicted_winner"]) not in pair_key(prediction["fighter_a"], prediction["fighter_b"]):
                raise ValueError("Predicted winner is not a participant")
            p = prediction["win_probability"]
            if not math.isfinite(p) or not 0.5 <= p <= 1:
                raise ValueError("Invalid predicted-winner probability")
    return {"path": str(path), "original_sha256": digest(raw), "title": text.splitlines()[0],
            "event_url": source, "event_date": day, "as_of_date": cutoff,
            "generated_at": generated, "analysis_mode": mode, "predictions": predictions}


def method_group(method: str | None) -> str | None:
    value = (method or "").strip().upper()
    if re.search(r"\b(?:KO|TKO)\b", value):
        return "KO/TKO"
    if value in {"SUB", "SUBMISSION"} or value.startswith("SUBMISSION"):
        return "Submission"
    if "DECISION" in value or value in {"U-DEC", "S-DEC", "M-DEC", "DEC", "UD", "SD", "MD"}:
        return "Decision"
    if value in {"DQ", "DISQUALIFICATION"}:
        return "DQ"
    if value == "OTHER":
        return "Other"
    return None


def validate_results(results: dict) -> None:
    event_key(results["event_url"])
    datetime.strptime(results["event_date"], "%Y-%m-%d")
    if not results.get("fetched_at") or not results.get("fights"):
        raise ValueError("Result snapshot must include its fetch time and fights")
    ids, pairs = set(), set()
    for fight in results["fights"]:
        key = pair_key(fight["fighter_a_name"], fight["fighter_b_name"])
        if key[0] == key[1] or key in pairs or fight["fight_id"] in ids:
            raise ValueError("Duplicate or invalid bout in result snapshot")
        pairs.add(key)
        ids.add(fight["fight_id"])
        if fight.get("winner_name") and name_key(fight["winner_name"]) not in key:
            raise ValueError("Source winner is not a participant")
        if fight.get("ending_round") is not None and not 1 <= fight["ending_round"] <= 10:
            raise ValueError("Invalid source ending round")
        if fight.get("result") not in {"completed", "draw", "no_contest", "pending"}:
            raise ValueError("Unknown source result status")
        if fight["result"] in {"draw", "no_contest", "pending"} and fight.get("winner_name"):
            raise ValueError("Source result status contradicts the posted winner")


def summarize(rows: list[dict]) -> dict:
    summary = {"attempted": len(rows), "published": sum(not r.get("error") for r in rows),
               "failed_predictions": sum(bool(r.get("error")) for r in rows),
               "unscored": sum(r["winner_correct"] is None for r in rows)}
    for field in ("winner", "method", "round", "exact"):
        scored = [r[f"{field}_correct"] for r in rows if r[f"{field}_correct"] is not None]
        summary[field] = {"correct": sum(scored), "scored": len(scored),
                          "accuracy": sum(scored) / len(scored) if scored else None}
    probabilities = [r for r in rows if r["winner_correct"] is not None]
    summary["brier_score"] = (sum((r["win_probability"] - int(r["winner_correct"])) ** 2
                                   for r in probabilities) / len(probabilities) if probabilities else None)
    # Chosen-winner binary Brier: 0 is ideal, 0.25 is a 50/50 prediction.
    return summary


def score_report(report: dict, results: dict) -> dict:
    validate_results(results)
    if not report.get("event_url") or event_key(report["event_url"]) != event_key(results["event_url"]):
        raise ValueError("Report must explicitly identify the same event as the results")
    if report.get("event_date") and report["event_date"] != results["event_date"]:
        raise ValueError("Report date differs from source event date")
    if report["as_of_date"] > results["event_date"]:
        raise ValueError("Prediction used a cutoff after the event date; cannot score as a pre-card forecast")
    pairs = {pair_key(f["fighter_a_name"], f["fighter_b_name"]): f for f in results["fights"]}
    ids = {f["fight_id"]: f for f in results["fights"]}
    rows = []
    for prediction in report["predictions"]:
        key = pair_key(prediction["fighter_a"], prediction["fighter_b"])
        actual = pairs.get(key)
        fid = prediction.get("fight_id")
        if fid and fid in ids:
            candidate = ids[fid]
            if pair_key(candidate["fighter_a_name"], candidate["fighter_b_name"]) != key:
                raise ValueError("Fight ID matches but the participants changed; manual reconciliation is required")
            actual = candidate
        elif fid and actual and actual["fight_id"] != fid:
            raise ValueError("Names match but the source fight ID changed; manual reconciliation is required")
        row = {k: prediction[k] for k in ("fighter_a", "fighter_b", "predicted_winner", "win_probability",
                                          "predicted_method", "predicted_round", "error") if k in prediction}
        row.update(actual=actual, winner_correct=None, method_correct=None, round_correct=None, exact_correct=None)
        if "error" in prediction:
            row["status"] = "FAILED PREDICTION"
        elif actual is None:
            row["status"] = "UNMATCHED / removed or changed pairing"
        elif actual["result"] in {"draw", "no_contest"}:
            row["status"] = f"UNSCORED / {actual['result']} (no binary winner)"
        elif actual["result"] == "pending" or not actual.get("winner_name"):
            row["status"] = "PENDING / no posted winner"
        else:
            row["winner_correct"] = name_key(prediction["predicted_winner"]) == name_key(actual["winner_name"])
            row["status"] = "RIGHT" if row["winner_correct"] else "WRONG"
            actual_method = method_group(actual.get("method_of_victory"))
            predicted_method = method_group(prediction["predicted_method"])
            if actual_method is not None and predicted_method is not None:
                row["method_correct"] = actual_method == predicted_method
            if actual.get("ending_round") is not None:
                row["round_correct"] = actual["ending_round"] == prediction["predicted_round"]
            values = [row[f"{field}_correct"] for field in ("winner", "method", "round")]
            if all(v is not None for v in values):
                row["exact_correct"] = all(values)
        rows.append(row)
    return {"schema_version": 1, "report_path": report["path"], "original_sha256": report["original_sha256"],
            "analysis_mode": report["analysis_mode"], "prediction_generated_at": report["generated_at"],
            "event_url": results["event_url"], "event_name": results["event_name"], "event_date": results["event_date"],
            "results_fetched_at": results["fetched_at"], "evaluated_at": utc_now(),
            "source_card_fights": len(results["fights"]),
            "summary": summarize(rows), "fights": rows}


def actual_text(actual: dict | None) -> str:
    if actual is None:
        return "No matching posted bout"
    winner = actual.get("winner_name") or actual["result"].replace("_", " ").upper()
    return (f"{winner}; {actual.get('method_of_victory') or 'method not posted'}, "
            f"round {actual.get('ending_round') or '?'}, {actual.get('ending_time') or 'time not posted'}")


def metric_text(metric: dict) -> str:
    if not metric["scored"]:
        return "0/0 (unscored)"
    return f"{metric['correct']}/{metric['scored']} ({metric['accuracy']:.1%})"


def annotate_report(report: dict, scored: dict, *, snapshot_path: Path) -> Path:
    path = Path(report["path"])
    base = original_bytes(path)
    if digest(base) != report["original_sha256"]:
        raise ValueError("Report changed during evaluation; no annotation was written")
    archive = archive_report(path)
    scored["original_archive"] = str(archive)
    scored["results_snapshot"] = str(snapshot_path)
    summary = scored["summary"]
    lines = [f"Event: {scored['event_name']} | {scored['event_date']}",
             f"Source: {scored['event_url']}", f"Results fetched: {scored['results_fetched_at']}",
             f"Analysis mode: {scored['analysis_mode']}", f"Original forecast archive: {archive}",
             f"Original SHA-256: {scored['original_sha256']}",
             f"Source card: {scored['source_card_fights']} bouts; this report: {summary['attempted']} prediction attempts.",
             f"Winner accuracy: {metric_text(summary['winner'])}",
             f"Method accuracy: {metric_text(summary['method'])}; round accuracy: {metric_text(summary['round'])}",
             f"Exact winner + method + round: {metric_text(summary['exact'])}",
             f"Coverage: {summary['published']}/{summary['attempted']} predictions published; "
             f"{summary['failed_predictions']} failed; {summary['unscored']} unscored."]
    if summary["brier_score"] is not None:
        lines.append(f"Winner probability Brier score: {summary['brier_score']:.4f} (lower is better; 50/50 = 0.25).")
    lines.extend(["Method and round are scored independently of the winner; exact requires all three.",
                  "Draws/no contests have no binary-winner score; unpublished/missing results remain unscored.",
                  "Forecasts and their explanations above are unchanged. An outcome alone does not prove the explanation."])
    if scored["analysis_mode"] == "retrospective_replay":
        lines.append("RETROSPECTIVE: generated after the event. Exclude this run from prospective calibration.")
    for row in scored["fights"]:
        lines.extend(["", f"{row['fighter_a']} vs {row['fighter_b']}", f"Winner prediction: {row['status']}"])
        if row.get("error"):
            lines.append(f"No published pick: {row['error']}")
        else:
            lines.append(f"Original pick: {row['predicted_winner']} ({row['win_probability']:.1%}); "
                         f"{row['predicted_method']}, round {row['predicted_round']}")
        lines.append(f"Actual result: {actual_text(row['actual'])}")
        label = lambda value: "UNSCORED" if value is None else "RIGHT" if value else "WRONG"
        lines.append(f"Method: {label(row['method_correct'])}; round: {label(row['round_correct'])}; "
                     f"exact prediction: {label(row['exact_correct'])}.")
        if row["actual"] and row["actual"].get("source_fight_url"):
            lines.append(f"Fight source: {row['actual']['source_fight_url']}")
    atomic_bytes(path, base + START + ("\n".join(lines) + "\n" + END).encode("utf-8"))
    return archive


def comparison_text(evaluations: list[dict]) -> str:
    lines = [f"{evaluations[0]['event_name']} | {evaluations[0]['event_date']} - model evaluation", "",
             "Each row describes a saved run. Replays were generated AFTER the event with pre-event SQL evidence.",
             "Retrospective scores cannot establish prospective improvement or calibrate future confidence.",
             "A single card is a small sample. Recorded timestamps alone do not prove a prediction preceded a bout.",
             "Method/round scores ignore the winner; exact requires winner, method and round all correct.", ""]
    for index, evaluated in enumerate(evaluations, 1):
        s = evaluated["summary"]
        lines.extend([f"Run {index}: {Path(evaluated['report_path']).name}",
                      f"Mode: {evaluated['analysis_mode']}; published {s['published']}/{s['attempted']}; failed {s['failed_predictions']}",
                      f"Winner {metric_text(s['winner'])}; method {metric_text(s['method'])}; "
                      f"round {metric_text(s['round'])}; exact {metric_text(s['exact'])}",
                      f"Brier score: {s['brier_score']:.4f}" if s['brier_score'] is not None else "Brier score: unscored", ""])
        if s["failed_predictions"]:
            reasons = {}
            for row in evaluated["fights"]:
                if row.get("error"):
                    reason = row["error"].split(":", 1)[0]
                    reasons[reason] = reasons.get(reason, 0) + 1
            lines.extend(["Failure reasons: " + "; ".join(f"{count} x {reason}" for reason, count in reasons.items()), ""])
    # Pairwise comparisons use exactly the same scored bouts; missing predictions stay visible above.
    for i, left in enumerate(evaluations):
        for j, right in enumerate(evaluations[i + 1:], i + 1):
            a = {pair_key(r['fighter_a'], r['fighter_b']): r for r in left["fights"]}
            b = {pair_key(r['fighter_a'], r['fighter_b']): r for r in right["fights"]}
            keys = sorted(k for k in a.keys() & b.keys()
                          if a[k]["winner_correct"] is not None and b[k]["winner_correct"] is not None)
            lines.append(f"Paired run {i + 1} vs run {j + 1}: {len(keys)} shared scored fights")
            if keys:
                sa, sb = summarize([a[k] for k in keys]), summarize([b[k] for k in keys])
                lines.append(f"Winner: {metric_text(sa['winner'])} -> {metric_text(sb['winner'])}; "
                             f"change {sb['winner']['accuracy'] - sa['winner']['accuracy']:+.1%}")
                lines.append(f"Brier: {sa['brier_score']:.4f} -> {sb['brier_score']:.4f} (lower is better)")
                lines.append(f"Exact: {metric_text(sa['exact'])} -> {metric_text(sb['exact'])}")
            lines.append("")
    lines.append("FIGHT-BY-FIGHT COMPARISON")
    pairs = sorted({pair_key(r['fighter_a'], r['fighter_b']) for e in evaluations for r in e['fights']})
    for key in pairs:
        matching = [(i, r) for i, e in enumerate(evaluations, 1) for r in e['fights']
                    if pair_key(r['fighter_a'], r['fighter_b']) == key]
        row = matching[0][1]
        lines.extend(["", f"{row['fighter_a']} vs {row['fighter_b']}", f"Actual: {actual_text(row['actual'])}"])
        for index, row in matching:
            lines.append(f"Run {index}: {row.get('predicted_winner', 'WITHHELD')} - {row['status']}"
                         + (f"; {row['predicted_method']} R{row['predicted_round']}" if not row.get('error') else ""))
    return "\n".join(lines) + "\n"
