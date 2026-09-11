#!/usr/bin/env python3
"""Report execution completion KPI from saved journal JSONL or AA event exports.

Example: python3 scripts/report_voice_task_metrics.py journal.jsonl --device lamp
The settling horizon selects mature turns; it is never an execution timeout.
No network calls are made. Completion measures execution, not answer correctness.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter


LOSS_COUNTERS = ("hal_dropped_total", "hal_failed_total", "telemetry_dropped_total",
                 "telemetry_failed_total", "unknown_owner_playbacks")


def _value(value):
    if isinstance(value, dict):
        for key in ("string_value", "int_value", "double_value", "float_value"):
            if key in value and value[key] is not None:
                return value[key]
    return value


def _bool(value):
    if value is True or value == "true" or value == 1:
        return True
    if value is False or value == "false" or value == 0:
        return False
    return None


def normalize(row, default_device=None):
    """Normalize one journal, AA, or already normalized row; ignore other logs."""
    observed_at_ms = row.get("observed_at_ms")
    if observed_at_ms is not None:
        observed_at_ms = int(observed_at_ms)
    elif row.get("__REALTIME_TIMESTAMP") is not None:
        observed_at_ms = int(row["__REALTIME_TIMESTAMP"]) // 1000
    elif row.get("event_timestamp") is not None:
        observed_at_ms = int(float(row["event_timestamp"]) * 1000)
    data = row.get("data") or {}
    device = (row.get("device_id") or row.get("_HOSTNAME") or data.get("user_pseudo_id")
              or row.get("user_pseudo_id") or default_device)
    if "MESSAGE" in row:
        message = row["MESSAGE"]
        # journald JSON encodes messages containing control bytes as byte arrays.
        if isinstance(message, list):
            message = bytes(message).decode("utf-8", errors="replace")
        if not isinstance(message, str):
            return None
        # os-server's console handler preserves ANSI field colors in journald.
        message = re.sub(r"\x1b\[[0-9;]*m", "", message)
        hal_event = re.search(r'\[telemetry\] (voice_metrics_\w+)\s+(\{.*)', message)
        if hal_event:
            params = json.loads(hal_event.group(2))
            return {"event_name": hal_event.group(1), "params": params,
                    "device_id": device, "observed_at_ms": observed_at_ms,
                    "event_id": params.get("event_id", row.get("event_id", ""))}
        if "[telemetry] event" not in message:
            return None
        name = re.search(r'\bevent_name=(?:"([^"]+)"|([^\s]+))', message)
        payload = re.search(r'\bparams=', message)
        if not name or not payload:
            return None
        params, _ = json.JSONDecoder().raw_decode(message[payload.end():])
        if isinstance(params, str):
            params = json.loads(params)
        event_id = re.search(r'\bevent_id=(?:"([^"]+)"|([^\s]+))', message)
        return {"event_name": name.group(1) or name.group(2), "params": params,
                "device_id": device, "observed_at_ms": observed_at_ms,
                "event_id": (event_id.group(1) or event_id.group(2)) if event_id else ""}
    params = row.get("params", data.get("event_params", {}))
    if isinstance(params, str):
        params = json.loads(params)
    if isinstance(params, list):
        params = {item["key"]: _value(item.get("value")) for item in params}
    return {"event_name": row.get("event_name"), "params": params,
            "device_id": device, "observed_at_ms": observed_at_ms,
            "event_id": row.get("event_id", params.get("event_id", ""))}


def report(rows, now_ms, settle_seconds=1800, default_device=None, include_synthetic=False):
    interactions = {}
    executions = []
    coverage = Counter()
    losses = {}
    for row in rows:
        try:
            event = normalize(row, default_device)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            coverage["malformed_events"] += 1
            continue
        if not event or not event["event_name"]:
            continue
        observed_at_ms = event["observed_at_ms"]
        if observed_at_ms is not None and observed_at_ms > now_ms:
            coverage["future_events_excluded"] += 1
            continue
        if observed_at_ms is None:
            # Untimestamped exports remain usable, but cannot establish which
            # snapshot existed at a historical cutoff. Surface that limitation.
            coverage["missing_observation_timestamp_events"] += 1
        params = event["params"]
        try:
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
            params = dict(params)
            for field in ("task_revision", "task_started_at_ms", "execution_at_ms", *LOSS_COUNTERS):
                if field in params and params[field] is not None:
                    params[field] = int(params[field])
            params["_event_id"] = str(event.get("event_id") or "")
        except (ValueError, TypeError, OverflowError):
            coverage["malformed_events"] += 1
            continue
        device = event["device_id"]
        if not device:
            coverage["missing_device_events"] += 1
            continue
        device_losses = losses.setdefault(device, Counter())
        for field in LOSS_COUNTERS:
            device_losses[field] = max(device_losses[field], params.get(field) or 0)
        if event["event_name"] not in ("voice_metrics_interaction", "voice_metrics_task_execution"):
            continue
        interaction_id = params.get("interaction_id")
        if not include_synthetic and str(interaction_id or "").startswith("vi-smoke-"):
            coverage["synthetic_events_excluded"] += 1
            continue
        if event["event_name"] == "voice_metrics_task_execution":
            if str(params.get("schema_version")) == "1":
                if params.get("execution_at_ms") is None:
                    coverage["malformed_events"] += 1
                    continue
                executions.append((device, params))
            continue
        if not interaction_id:
            coverage["missing_interaction_id_events"] += 1
            continue
        key = (device, str(interaction_id))
        # Revision is authoritative; canonical JSON makes equal revisions independent
        # of export ordering. Producers should never change an existing revision.
        rank = (params.get("task_revision") or 0, json.dumps(params, sort_keys=True))
        if key not in interactions or rank > interactions[key][0]:
            interactions[key] = (rank, params)

    indexed = {}
    for device, execution in executions:
        for field in ("run_id", "interaction_id"):
            identifier = execution.get(field)
            if identifier:
                indexed.setdefault((device, field, str(identifier)), []).append(execution)
    devices = {}
    cutoff = now_ms - settle_seconds * 1000
    for (device, interaction_id), (_, turn) in interactions.items():
        counts = devices.setdefault(device, Counter())
        if str(turn.get("task_schema_version")) != "1":
            counts["legacy_turns_excluded"] += 1
            continue
        counts["versioned_turns"] += 1
        if _bool(turn.get("task_eligible")) is False:
            counts["ineligible_turns"] += 1
            continue
        if _bool(turn.get("task_eligibility_known")) is not True:
            counts["eligibility_unknown_turns"] += 1
        started = turn.get("task_started_at_ms")
        if started is None:
            counts["missing_start_time_turns_excluded"] += 1
            continue
        if int(started) > cutoff:
            counts["fresh_pending_turns"] += 1
            continue
        candidates = list(indexed.get((device, "interaction_id", interaction_id), []))
        if turn.get("run_id"):
            candidates.extend(indexed.get((device, "run_id", str(turn["run_id"])), []))
        if turn.get("route") == "realtime_handled":
            candidates = [e for e in candidates if e.get("evidence") == "realtime_turn_done"]
        candidates = [e for e in candidates if (e.get("execution_at_ms") or 0) <= now_ms]
        priority = {"unknown": 0, "completed": 1, "failed": 2}
        selected = max(candidates, key=lambda e: (
            e.get("execution_at_ms") or 0, priority.get(e.get("outcome"), 0),
            e["_event_id"]), default={})
        # A cleanup end is not proof that an earlier terminal failure was
        # repaired. A recovered error has outcome unknown, not failed.
        outcome = ("failed" if any(e.get("outcome") == "failed" for e in candidates)
                   else selected.get("outcome", "incomplete"))
        if outcome not in ("completed", "failed", "unknown", "incomplete"):
            outcome = "unknown"
        # A terminal completion proves that dispatch landed, even when transport
        # acknowledgement was lost. Without it dispatch failure counts as failed.
        if not selected and turn.get("failure_reason"):
            outcome = "failed"
        counts["eligible_mature_turns"] += 1
        counts[outcome + "_turns"] += 1

    def summarize(counts):
        result = dict(counts)
        for key in ("eligible_mature_turns", "completed_turns", "failed_turns", "unknown_turns",
                    "incomplete_turns", "fresh_pending_turns", "ineligible_turns",
                    "legacy_turns_excluded", "versioned_turns", "eligibility_unknown_turns",
                    "missing_start_time_turns_excluded", *LOSS_COUNTERS):
            result.setdefault(key, 0)
        denominator = result["eligible_mature_turns"]
        result["kpi3_pct"] = result["completed_turns"] * 100 / denominator if denominator else None
        result["target_pct"] = 85
        result["meets_target"] = result["completed_turns"] * 100 >= denominator * 85 if denominator else None
        return result

    aggregate = Counter()
    for device, counters in losses.items():
        devices.setdefault(device, Counter()).update(counters)
    for counts in devices.values():
        aggregate.update(counts)
    return {"now_ms": now_ms, "settle_seconds": settle_seconds,
            "definition": "Execution finished without observed terminal errors; semantic correctness is not assessed.",
            "aggregate": summarize(aggregate),
            "devices": {key: summarize(value) for key, value in sorted(devices.items())},
            "coverage": dict(coverage)}


def read_rows(filenames):
    """Stream exports; retain only metric state, never the complete journal."""
    for filename in filenames or ["-"]:
        stream = sys.stdin if filename == "-" else open(filename, encoding="utf-8")
        try:
            for number, line in enumerate(stream, 1):
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError as error:
                        raise ValueError(f"{filename}:{number}: invalid JSON: {error}") from error
        finally:
            if stream is not sys.stdin:
                stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="JSONL exports (stdin when omitted)")
    parser.add_argument("--device", help="Fallback identity for rows without a device/hostname")
    parser.add_argument("--now-ms", type=int, default=int(time.time() * 1000))
    parser.add_argument("--settle-seconds", type=int, default=1800)
    parser.add_argument("--include-synthetic", action="store_true")
    args = parser.parse_args()
    if args.settle_seconds < 0:
        parser.error("--settle-seconds must be nonnegative")
    try:
        result = report(read_rows(args.files), args.now_ms, args.settle_seconds,
                        args.device, args.include_synthetic)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
