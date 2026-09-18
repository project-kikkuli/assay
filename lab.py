#!/usr/bin/env python3
"""Reader for recorded Assay evidence; replay is explicit and subprocess-backed."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from statistics import median
from typing import Any, Callable


ROOT = Path(__file__).resolve().parent
STATE_PATH = Path("out/lab/state.json")
HOLDOUT_PATH = Path("experiments/fullstack/HOLDOUT.md")
HOLDOUT_JSON_PATH = Path("experiments/fullstack/results/holdout.json")
PREPARE_SOURCE = Path("tools/prepare_lab.py")
SHOW_CHOICES = (
    "gate",
    "boundaries",
    "policy",
    "isolation",
    "outbox",
    "compatibility",
    "scale",
)
REPLAY_CHOICES = ("gate", "policy", "isolation", "outbox", "compatibility", "legacy")
SOURCES = {
    "gate": Path("experiments/fullstack/gate.py"),
    "policy": Path("experiments/cedar/run.py"),
    "isolation": Path("experiments/isolation/run.py"),
    "outbox": Path("experiments/outbox/run_actual.py"),
    "compatibility": Path("experiments/compatibility/verifier.py"),
    "legacy": Path("demo"),
}
JSON_PATHS = {
    "gate": Path("experiments/fullstack/results/gate.json"),
    "boundaries": Path("experiments/boundaries/results.json"),
    "signup": Path("experiments/boundaries/signup-results.json"),
    "policy": Path("experiments/cedar/evidence.json"),
    "isolation": Path("experiments/isolation/results.json"),
    "isolation_perf": Path("experiments/isolation/performance.json"),
    "outbox_model": Path("experiments/outbox/results/model-normal.json"),
    "outbox_actual": Path("experiments/outbox/results/actual-replay.json"),
    "compatibility": Path("experiments/compatibility/results.json"),
    "scale": Path("experiments/marimo/results.json"),
}
ALLOWED_ENV = {
    "gate": ("ASSAY_PG_DSN", "ASSAY_MAIL_HTTP", "ASSAY_MAIL_PORT"),
    "policy": ("KIKKULI_CEDAR_IMAGE", "KIKKULI_CEDAR_SCRATCH"),
    "isolation": (),
    "outbox": (),
    "compatibility": (),
    "legacy": (),
}
STAGES = ("original", "naive_rename", "expand", "bridge", "contract")
MATRIX_COLUMNS = ("old_write", "old_read", "new_write", "new_read")
GATE_VERDICTS = {"supported", "rejected", "unresolved", "unknown", "failed"}


def path_for(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def read_json(path: Path) -> tuple[Any | None, str]:
    """Read recorded JSON defensively; never turns absent data into a claim."""
    target = path_for(path)
    try:
        return json.loads(target.read_text()), ""
    except FileNotFoundError:
        return None, "unavailable"
    except (OSError, UnicodeError) as exc:
        return None, f"unavailable ({exc})"
    except json.JSONDecodeError as exc:
        return None, f"unsupported (malformed JSON: {exc})"


def read_text(path: Path) -> tuple[str | None, str]:
    target = path_for(path)
    try:
        return target.read_text(), ""
    except FileNotFoundError:
        return None, "unavailable"
    except (OSError, UnicodeError) as exc:
        return None, f"unavailable ({exc})"


def load_object(
    path: Path, validator: Callable[[dict[str, Any]], str] | None = None
) -> tuple[dict[str, Any] | None, str]:
    value, error = read_json(path)
    if error:
        return None, error
    if not isinstance(value, dict):
        return None, "unsupported (expected JSON object)"
    try:
        problem = validator(value) if validator else ""
    except (AttributeError, KeyError, IndexError, TypeError):
        problem = "invalid schema"
    return (None, f"unsupported ({problem})") if problem else (value, "")


def number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def text(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def brief(value: Any, limit: int = 96) -> str:
    value = text(value)
    return value if len(value) <= limit else value[: limit - 3] + "..."


def seconds(value: Any) -> str:
    return f"{value:.1f}s" if number(value) else "unavailable"


def precise_seconds(value: Any) -> str:
    return f"{value:.3f}s" if number(value) else "unavailable"


def milliseconds(value: Any) -> str:
    return f"{value:.3f}ms" if number(value) else "unavailable"


def table(headers: list[str], rows: list[list[Any]]) -> str:
    cells = [[text(cell) for cell in row] for row in rows]
    widths = [len(header) for header in headers]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [" | ".join(header.ljust(widths[i]) for i, header in enumerate(headers))]
    lines.append("-+-".join("-" * width for width in widths))
    lines.extend(
        " | ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in cells
    )
    return "\n".join(lines)


def validate_gate(data: dict[str, Any]) -> str:
    runs = data.get("runs")
    if (
        data.get("verdict") not in GATE_VERDICTS
        or not isinstance(runs, list)
        or not runs
    ):
        return "missing verdict/runs"
    for run in runs:
        if (
            not isinstance(run, dict)
            or run.get("verdict") not in GATE_VERDICTS
            or not number(run.get("seconds"))
        ):
            return "invalid run shape"
    for key in ("source_unchanged", "policy_unchanged"):
        if key in data and not isinstance(data[key], bool):
            return f"{key} is not boolean"
    if data["verdict"] == "supported":
        if any(run["verdict"] != "supported" for run in runs):
            return "supported aggregate has a non-supported run"
        if (
            data.get("source_unchanged") is not True
            or data.get("policy_unchanged") is not True
        ):
            return "supported aggregate has source/policy drift"
    return ""


def validate_boundaries(data: dict[str, Any]) -> str:
    claims, observations = data.get("claims"), data.get("observations")
    if (
        data.get("application_verdict") not in {"supported", "rejected", "unresolved"}
        or not isinstance(claims, dict)
        or not isinstance(observations, dict)
    ):
        return "missing verdict/claims/observations"
    if any(not isinstance(value, bool) for value in claims.values()):
        return "claims are not boolean"
    null_status = (observations.get("null_update") or {}).get("http_status")
    offset_status = (observations.get("negative_offset") or {}).get("http_status")
    if (
        not isinstance(null_status, int)
        or isinstance(null_status, bool)
        or not isinstance(offset_status, int)
        or isinstance(offset_status, bool)
    ):
        return "invalid HTTP status observation"
    checks = {
        "invalid_title_is_client_error": 400 <= null_status < 500,
        "invalid_offset_is_client_error": 400 <= offset_status < 500,
        "all_owned_items_reachable": (observations.get("pagination") or {}).get(
            "all_items_reachable"
        ),
        "stored_title_does_not_execute": (
            observations.get("title_rendering") or {}
        ).get("safely_rendered"),
    }
    if any(
        name in claims and not isinstance(value, bool) for name, value in checks.items()
    ):
        return "invalid boolean observation"
    for name, observed in checks.items():
        if name in claims and claims[name] != observed:
            return f"claim contradicts observation: {name}"
    return ""


def validate_policy(data: dict[str, Any]) -> str:
    results, failures = data.get("results"), data.get("failures")
    if (
        not isinstance(results, list)
        or not results
        or not isinstance(failures, list)
        or not isinstance(data.get("assertions_passed"), bool)
    ):
        return "missing results/failures/assertion status"
    if data["assertions_passed"] and failures:
        return "assertions_passed contradicts failures"
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("name"), str)
        or not isinstance(row.get("semantic"), str)
        for row in results
    ):
        return "invalid result row"
    return ""


def validate_isolation(data: dict[str, Any]) -> str:
    cases = (data.get("security_matrix") or {}).get("cases")
    roles = data.get("role_matrix")
    if (
        not isinstance(data.get("status"), str)
        or not isinstance(cases, list)
        or not cases
        or not isinstance(roles, dict)
    ):
        return "missing status/security matrix/role matrix"
    if any(
        not isinstance(row, dict)
        or not isinstance(row.get("case"), str)
        or not isinstance(row.get("ok"), bool)
        for row in cases
    ):
        return "invalid security case"
    if not isinstance(roles.get("per_tenant_roles"), list) or any(
        not isinstance(row, dict) for row in roles["per_tenant_roles"]
    ):
        return "missing per-tenant role rows"
    return ""


def validate_isolation_performance(data: dict[str, Any]) -> str:
    cases = data.get("cases")
    if not count(data.get("repetitions")) or not isinstance(cases, list) or not cases:
        return "missing performance cases/repetitions"
    for case in cases:
        if not isinstance(case, dict) or not case:
            return "invalid performance case"
        probes = [
            probe
            for probe in case.values()
            if isinstance(probe, dict) and "p95_ms" in probe
        ]
        if not probes or any(not number(probe.get("p95_ms")) for probe in probes):
            return "invalid performance probe"
    return ""


def validate_outbox_model(data: dict[str, Any]) -> str:
    required = (
        "model_version",
        "model_states",
        "max_queue",
        "max_outstanding",
        "outstanding_bound",
        "found_counterexample",
    )
    if (
        any(key not in data for key in required)
        or not isinstance(data.get("model_version"), str)
        or any(not isinstance(data.get(key), int) for key in required[1:5])
        or not isinstance(data.get("found_counterexample"), bool)
    ):
        return "missing model bound fields"
    if (
        data["max_queue"] > data["outstanding_bound"]
        or data["max_outstanding"] > data["outstanding_bound"]
    ):
        return "model maxima exceed bound"
    return ""


def validate_outbox_actual(data: dict[str, Any]) -> str:
    variants, replay = data.get("actual_variants"), data.get("actual_replay")
    if not isinstance(variants, dict) or not isinstance(replay, dict):
        return "missing actual variants/replay"
    normal, ghost = variants.get("normal"), variants.get("ghost-delivery")
    if not isinstance(normal, dict) or not isinstance(ghost, dict):
        return "missing normal/ghost probes"
    normal_inv, ghost_inv = normal.get("invariant"), ghost.get("invariant")
    if not isinstance(normal_inv, dict) or not isinstance(ghost_inv, dict):
        return "missing probe invariants"

    def outcome(row: Any) -> Any:
        return (
            row.get("stdout", {}).get("outcome")
            if isinstance(row, dict) and isinstance(row.get("stdout"), dict)
            else None
        )

    if (
        outcome(normal.get("first_consume")) != "committed"
        or outcome(normal.get("second_consume")) != "dedup_skipped"
    ):
        return "normal outcomes contradict expected completion"
    if not (
        normal_inv.get("holds") is True
        and normal_inv.get("outbox") == [{"command_id": "cmd-1", "status": "published"}]
        and normal_inv.get("processed") == ["evt:cmd-1"]
        and normal_inv.get("ledger_counts") == [{"command_id": "cmd-1", "count": 1}]
    ):
        return "normal completion evidence is incomplete or contradictory"
    if (
        outcome(ghost.get("ghost_consume")) != "rejected_missing_outbox"
        or ghost_inv.get("holds") is not True
        or ghost_inv.get("ledger_counts")
        or ghost_inv.get("processed")
    ):
        return "ghost challenge is not a clean blocked positive"
    retry = outcome(replay.get("retry_after_marked_consume"))
    replay_inv = replay.get("invariant")
    if (
        retry != "dedup_skipped"
        or not isinstance(replay_inv, dict)
        or replay_inv.get("holds") is not False
        or replay_inv.get("ledger_counts") != []
        or replay_inv.get("processed_without_ledger") != ["evt:cmd-1"]
    ):
        return "broken retry evidence is incomplete"
    return ""


def validate_compatibility(data: dict[str, Any]) -> str:
    detail, gate, counts = (
        data.get("matrix_detail"),
        data.get("gate"),
        data.get("counts"),
    )
    if (
        not isinstance(detail, dict)
        or not isinstance(gate, dict)
        or not isinstance(counts, dict)
    ):
        return "missing matrix/gate/counts"
    if any(
        stage not in detail or not isinstance(detail[stage], dict) for stage in STAGES
    ) or any(
        stage not in gate or not isinstance(gate[stage], bool) for stage in STAGES
    ):
        return "incomplete compatibility matrix"
    if any(
        column not in detail[stage] or not isinstance(detail[stage][column], dict)
        for stage in STAGES
        for column in MATRIX_COLUMNS
    ):
        return "incomplete matrix cells"
    return ""


def validate_scale(data: dict[str, Any]) -> str:
    warm = data.get("warm_tests")
    if (
        not isinstance(warm, list)
        or not warm
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not isinstance(row.get("status"), str)
            for row in warm
        )
    ):
        return "missing warm test records"
    for row in warm:
        counts = row.get("counts")
        if counts is not None and not isinstance(counts, dict):
            return "invalid warm test counts"
    profile = data.get("direct_vitest_profile")
    if profile is not None:
        if not isinstance(profile, dict):
            return "invalid direct profile"
        all_profile = profile.get("all")
        if all_profile is not None and (
            not isinstance(all_profile, dict)
            or not isinstance(all_profile.get("status"), str)
            or not count(all_profile.get("tests"))
            or not number(all_profile.get("duration_s"))
        ):
            return "invalid direct profile all run"
    return ""


def record(name: str) -> tuple[dict[str, Any] | None, str]:
    validators = {
        "gate": validate_gate,
        "boundaries": validate_boundaries,
        "policy": validate_policy,
        "isolation": validate_isolation,
        "outbox_model": validate_outbox_model,
        "outbox_actual": validate_outbox_actual,
        "compatibility": validate_compatibility,
        "scale": validate_scale,
    }
    return load_object(JSON_PATHS[name], validators.get(name))


def holdout_summary() -> str:
    matrix, matrix_error = read_json(HOLDOUT_JSON_PATH)
    if matrix_error:
        matrix_text = f"matrix={matrix_error}"
    elif isinstance(matrix, dict) and isinstance(matrix.get("matrix"), list):
        rows = matrix["matrix"]
        caught = sum(
            isinstance(row, dict) and row.get("oracle") == "caught" for row in rows
        )
        matrix_text = f"matrix oracle catches {caught}/{len(rows)}"
    else:
        matrix_text = "matrix=unsupported (missing matrix)"

    content, error = read_text(HOLDOUT_PATH)
    if error:
        return (
            f"holdout: {HOLDOUT_PATH} — {matrix_text}; initial comparison unavailable"
        )
    match = re.search(
        r"not the\s+(?P<initial>\d+/\d+)",
        content or "",
        flags=re.IGNORECASE,
    )
    initial_text = (
        f"initial author-shared {match.group('initial')}"
        if match
        else "initial comparison unavailable"
    )
    return f"holdout: {HOLDOUT_PATH} — {matrix_text}; {initial_text}; challenge set, not holdout proof"


def numeric_values(values: list[Any]) -> list[float]:
    return [value for value in values if number(value)]


def numeric_range(values: list[Any], suffix: str) -> str:
    valid = numeric_values(values)
    if not valid:
        return "unavailable"
    low, high = min(valid), max(valid)
    return f"{low:.1f}–{high:.1f}{suffix}"


def default_summary(name: str, key: str) -> str:
    data, error = record(key)
    if data is None:
        return error
    if name == "gate":
        run_times = [run.get("seconds") for run in data["runs"]]
        hosted, hosted_error = load_object(
            Path("experiments/fullstack/results/gate-hosted-first.json"), validate_gate
        )
        hosted_note = (
            f"hosted first={seconds(hosted['runs'][-1]['seconds'])} {hosted['verdict']}"
            if hosted is not None else f"hosted first={hosted_error}"
        )
        return (
            f"{len(data['runs'])} runs {numeric_range(run_times, 's')}; "
            f"verdict={data['verdict']}; {hosted_note}"
        )
    if name == "boundaries":
        return (
            f"verdict={data['application_verdict']}; "
            f"cost={seconds(data.get('seconds'))}"
        )
    if name == "policy":
        symbolic = [
            row
            for row in data["results"]
            if row.get("semantic") in {"proved", "counterexample"}
        ]
        elapsed = [row.get("elapsed_ms") for row in data["results"]]
        cost = (
            seconds(sum(elapsed) / 1000)
            if elapsed and all(number(value) for value in elapsed)
            else "unavailable"
        )
        return f"{len(symbolic)} symbolic checks; cost={cost}"
    if name == "isolation":
        performance, performance_error = load_object(
            JSON_PATHS["isolation_perf"], validate_isolation_performance
        )
        if performance is None:
            return f"status={data['status']}; performance={performance_error}"
        p95 = []
        for case in performance.get("cases", []):
            if isinstance(case, dict):
                for probe in case.values():
                    if isinstance(probe, dict) and number(probe.get("p95_ms")):
                        p95.append(probe["p95_ms"])
        cost = numeric_range(p95, "ms")
        return (
            f"status={data['status']}; repetitions="
            f"{performance.get('repetitions', 'unavailable')}; p95={cost}"
        )
    if name == "outbox":
        model, model_error = record("outbox_model")
        if model is None:
            return f"actual recorded; model={model_error}"
        elapsed = data.get("actual_variants_elapsed_ms")
        normal_holds = data["actual_variants"]["normal"]["invariant"].get("holds")
        return (
            f"normal holds={normal_holds}; model states={model['model_states']}; "
            f"SQL/TypeScript cost={milliseconds(elapsed)}"
        )
    if name == "compatibility":
        counts = data.get("counts", {})
        return (
            f"positive={text(counts.get('positive_checks'))}; "
            f"expected counterexamples={text(counts.get('expected_counterexamples'))}; "
            f"unexpected={text(counts.get('unexpected_faults'))}; "
            f"cost={seconds(data.get('total_seconds'))}"
        )
    if name == "scale":
        frontend = next(
            (row for row in data["warm_tests"] if row.get("name") == "frontend_tests"),
            None,
        )
        direct = data.get("direct_vitest_profile")
        direct_all = direct.get("all") if isinstance(direct, dict) else None
        if not isinstance(direct_all, dict):
            direct_all = {}
        frontend_status = (
            direct_all.get("status", frontend.get("status"))
            if isinstance(frontend, dict)
            else direct_all.get("status", "unavailable")
        )
        frontend_counts = (
            direct_all if direct_all.get("tests") is not None else (frontend or {})
        )
        frontend_tests = frontend_counts.get("tests", "unavailable")
        frontend_cost = direct_all.get("duration_s")
        if not number(frontend_cost) and isinstance(frontend, dict):
            frontend_cost = frontend.get("duration_s")
        python = next(
            (row for row in data["warm_tests"] if row.get("name") == "python_tests"),
            None,
        )
        python_status = (
            python.get("status") if isinstance(python, dict) else "unavailable"
        )
        return (
            f"frontend={frontend_status} {text(frontend_tests)} tests in "
            f"{seconds(frontend_cost)}; python={python_status}"
        )
    return "unsupported"


def policy_summary_rows(results: list[dict[str, Any]]) -> list[list[Any]]:
    groups: dict[tuple[str, str], list[Any]] = {}
    for row in results:
        key = (row["name"], row["semantic"])
        groups.setdefault(key, []).append(row.get("elapsed_ms"))

    summary_rows = []
    for (name, semantic), elapsed_values in groups.items():
        if elapsed_values and all(number(value) for value in elapsed_values):
            median_elapsed = milliseconds(median(elapsed_values))
            elapsed_range = f"{min(elapsed_values):.3f}–{max(elapsed_values):.3f}ms"
        else:
            median_elapsed = "unavailable"
            elapsed_range = "unavailable"
        summary_rows.append(
            [name, semantic, len(elapsed_values), median_elapsed, elapsed_range]
        )
    return summary_rows


def show_error(label: str, path: Path, error: str) -> None:
    print(f"{label}: {error} ({path})")


def digest_prefix(value: Any) -> str:
    if isinstance(value, str) and value:
        return value[:12]
    return "unavailable"


def recorded_test_count(record: dict[str, Any]) -> str:
    tests = record.get("tests")
    if not isinstance(tests, dict):
        return "unavailable"
    if count(tests.get("actual_passed")) and count(tests.get("expected")):
        return f"{tests['actual_passed']}/{tests['expected']}"
    if count(tests.get("passed")) and count(tests.get("collected")):
        return f"{tests['passed']}/{tests['collected']}"
    return "unavailable"


def show_gate() -> None:
    data, error = record("gate")
    if data is None:
        show_error("gate", JSON_PATHS["gate"], error)
        return
    rows = [
        [f"run {i + 1}", run["verdict"], seconds(run["seconds"])]
        for i, run in enumerate(data["runs"])
    ]
    print(f"gate: recorded verdict={data['verdict']}")
    print(table(["run", "verdict", "seconds"], rows))
    latest = data["runs"][-1]
    records = latest.get("records")
    if isinstance(records, list) and all(isinstance(row, dict) for row in records):
        print("latest run: actual counts per check")
        print(
            table(
                ["check", "status", "tests passed/expected", "seconds"],
                [
                    [
                        row.get("check", "unavailable"),
                        row.get("status", "unavailable"),
                        recorded_test_count(row),
                        seconds(row.get("seconds")),
                    ]
                    for row in records
                ],
            )
        )
        slowest = sorted(
            records,
            key=lambda row: row.get("seconds", -1)
            if number(row.get("seconds"))
            else -1,
            reverse=True,
        )
        print("latest run: slowest stage wall times; overlapping stages are not summed")
        print(
            table(
                ["stage/check", "seconds"],
                [
                    [row.get("check", "unavailable"), seconds(row.get("seconds"))]
                    for row in slowest
                ],
            )
        )
    else:
        print("latest run details: unsupported (missing check records)")
    boundary = latest.get("boundary")
    print(
        f"sources: gate={SOURCES['gate']}; lifecycle=experiments/fullstack/lifecycle_tests.py"
    )
    print(
        "hashes: "
        f"source={digest_prefix(boundary.get('source_sha256') if isinstance(boundary, dict) else None)}; "
        f"built_artifact={digest_prefix(latest.get('built_artifact_sha256'))}; "
        f"reused_build={digest_prefix(boundary.get('reused_build_sha256') if isinstance(boundary, dict) else None)}"
    )
    print(holdout_summary())
    print("replay is separate; this view does not execute the gate.")


def show_boundaries() -> None:
    data, error = record("boundaries")
    if data is None:
        show_error("boundaries", JSON_PATHS["boundaries"], error)
    else:
        print(f"boundaries: recorded application_verdict={data['application_verdict']}")
        print(
            table(
                ["claim", "recorded"],
                [[name, value] for name, value in data["claims"].items()],
            )
        )
    signup, signup_error = load_object(JSON_PATHS["signup"])
    if signup is not None and isinstance(signup.get("observations"), dict):
        print(
            f"signup probe: recorded verdict={signup.get('application_verdict', 'unavailable')}"
        )
    elif signup is not None:
        print("signup probe: unsupported (missing observations)")
    elif signup_error:
        print(f"signup probe: {signup_error}")


def show_policy() -> None:
    data, error = record("policy")
    if data is None:
        show_error("policy", JSON_PATHS["policy"], error)
        return
    print(
        f"policy: recorded admission={data.get('admission', 'unavailable')} assertions_passed={data['assertions_passed']}"
    )
    print(
        table(
            ["check", "semantic", "count", "median", "range"],
            policy_summary_rows(data["results"] + data.get("warm_candidate_implies", [])),
        )
    )


def show_isolation() -> None:
    data, error = record("isolation")
    if data is None:
        show_error("isolation", JSON_PATHS["isolation"], error)
        return
    cases = data["security_matrix"]["cases"]
    print(f"isolation: recorded status={data['status']}")
    print(
        table(
            ["case", "ok", "sqlstate/value"],
            [
                [
                    case["case"],
                    case["ok"],
                    brief(
                        case.get("sqlstate")
                        or case.get("value")
                        or case.get("error")
                        or ""
                    ),
                ]
                for case in cases
            ],
        )
    )
    roles = data["role_matrix"]
    spoof = next(
        (
            case
            for case in cases
            if case.get("case") == "same_role_can_spoof_tenant_guc"
        ),
        None,
    )
    if spoof:
        print(f"app GUC spoof case: observed={spoof.get('value', 'unavailable')}")
    print(
        f"role bypass rows={roles.get('bypassrls_rows', 'unavailable')}; owner rows unforced={roles.get('table_owner_unforced_rows', 'unavailable')} forced={roles.get('table_owner_forced_rows', 'unavailable')}"
    )
    print(
        table(
            ["role", "visible", "guc_spoof_visible"],
            [
                [
                    row.get("role"),
                    row.get("visible_rows"),
                    row.get("guc_spoof_visible_rows"),
                ]
                for row in roles["per_tenant_roles"]
            ],
        )
    )
    performance, performance_error = load_object(
        JSON_PATHS["isolation_perf"], validate_isolation_performance
    )
    if performance is not None and isinstance(performance.get("cases"), list):
        print(
            f"performance: recorded repetitions={performance.get('repetitions', 'unavailable')}; timings are observations"
        )
        plan_rows = []
        for case in performance["cases"]:
            baseline = case.get("baseline_explicit_predicate_bypassrls")
            rls = case.get("rls")
            results = "unavailable"
            if isinstance(baseline, dict) and isinstance(rls, dict):
                results = f"{text(baseline.get('result'))}/{text(rls.get('result'))}"
            plan_rows.append(
                [
                    case.get("tenant", "unavailable"),
                    case.get("plan_classification", "unavailable"),
                    results,
                    brief(case.get("plan_equivalence", "unavailable")),
                ]
            )
        if plan_rows:
            print(
                table(
                    ["tenant", "classification", "result baseline/rls", "equivalence"],
                    plan_rows,
                )
            )
    elif performance_error:
        print(f"performance: {performance_error}")


def show_outbox() -> None:
    model, model_error = record("outbox_model")
    actual, actual_error = record("outbox_actual")
    if model is None:
        show_error("outbox model", JSON_PATHS["outbox_model"], model_error)
    else:
        print(
            f"outbox model: recorded version={model['model_version']} states={model['model_states']} queue<={model['max_queue']} outstanding<={model['outstanding_bound']}; no liveness claim"
        )
    if actual is None:
        show_error("outbox actual", JSON_PATHS["outbox_actual"], actual_error)
        return
    variants = actual["actual_variants"]
    normal = variants["normal"]
    inv = normal["invariant"]
    print(
        f"normal: first={normal['first_consume']['stdout']['outcome']} second={normal['second_consume']['stdout']['outcome']} published={inv['outbox']} processed={inv['processed']} ledger={inv['ledger_counts']}"
    )
    ghost = variants["ghost-delivery"]
    print(
        f"ghost delivery: {ghost['ghost_consume']['stdout']['outcome']} (expected blocked positive; invariant holds={ghost['invariant']['holds']})"
    )
    replay = actual["actual_replay"]
    print(
        f"broken retry: {replay['retry_after_marked_consume']['stdout']['outcome']}; ledger={replay['invariant']['ledger_counts']} processed_without_ledger={replay['invariant']['processed_without_ledger']}"
    )
    print(f"trace representation: {replay.get('note', 'unavailable')}")


def show_compatibility() -> None:
    data, error = record("compatibility")
    if data is None:
        show_error("compatibility", JSON_PATHS["compatibility"], error)
        return

    def cell(stage: str, column: str) -> str:
        value = data["matrix_detail"][stage][column]
        return (
            "ok"
            if value.get("ok") is True
            else f"FAIL({value.get('sqlstate') or value.get('message') or 'unknown'})"
        )

    print(f"compatibility: recorded counts={data['counts']} gate={data['gate']}")
    print(
        table(
            ["stage", *MATRIX_COLUMNS],
            [
                [stage, *[cell(stage, column) for column in MATRIX_COLUMNS]]
                for stage in STAGES
            ],
        )
    )
    print(
        f"unexpected_faults={data.get('unexpected_faults', 'unavailable')} lock={data.get('lock', 'unavailable')}"
    )


def show_scale() -> None:
    data, error = record("scale")
    if data is None:
        show_error("scale", JSON_PATHS["scale"], error)
        return
    rows = []
    for item in data["warm_tests"]:
        counts = item.get("counts") if isinstance(item.get("counts"), dict) else {}
        rows.append(
            [
                item["name"],
                item["status"],
                counts.get("passed", "unavailable"),
                counts.get("tests", "unavailable"),
                seconds(item.get("duration_s")),
            ]
        )
    print("scale: recorded test observations (status is not inferred from counts)")
    print(table(["probe", "status", "passed", "tests", "seconds"], rows))
    direct = data.get("direct_vitest_profile")
    direct_all = direct.get("all") if isinstance(direct, dict) else None
    if isinstance(direct_all, dict):
        print(
            "direct Vitest (no-cache): "
            f"status={direct_all.get('status', 'unavailable')} "
            f"tests={direct_all.get('tests', 'unavailable')} "
            f"cost={precise_seconds(direct_all.get('duration_s'))}"
        )
    else:
        print("direct Vitest (no-cache): unsupported (missing profile)")
    selected = data.get("selected_python_validation")
    if isinstance(selected, dict):
        after = selected.get("after")
        match = re.search(r"(\d+\s*/\s*\d+).*?selected tests passed", text(after))
        if match and number(selected.get("selected_test_s")):
            print(
                "selected Python after preparation: "
                f"{match.group(1).replace(' ', '')} tests passed; "
                f"cost={precise_seconds(selected['selected_test_s'])}"
            )
        else:
            print("selected Python after preparation: unsupported")
    else:
        print("selected Python after preparation: unavailable")
    optional = data.get("optional_python_tests")
    if isinstance(optional, dict):
        print(
            f"optional Python: status={optional.get('status', 'unavailable')} counts={optional.get('counts', 'unavailable')}"
        )
    for path, label in (
        (Path("experiments/marimo/vitest-affected.json"), "affected"),
        (Path("experiments/marimo/vitest-pure-node.json"), "pure-node"),
    ):
        extra, extra_error = load_object(path)
        if extra is None:
            print(f"{label}: {extra_error}")
        elif label == "affected":
            baseline, defect = extra.get("baseline"), extra.get("seeded_defect")
            print(
                f"affected: baseline={baseline.get('counts', 'unavailable') if isinstance(baseline, dict) else 'unavailable'} seeded_defect={defect.get('counts', 'unavailable') if isinstance(defect, dict) else 'unavailable'}"
            )
        else:
            runs = extra.get("runs") if isinstance(extra.get("runs"), list) else []
            print(
                f"pure-node: runs={[(run.get('seed'), run.get('status'), run.get('counts')) for run in runs if isinstance(run, dict)]}"
            )


SHOW_HANDLERS = {name: globals()[f"show_{name}"] for name in SHOW_CHOICES}


def replay_subject(explicit: str | None) -> tuple[str | None, str]:
    if explicit and explicit != "default":
        subject = Path(explicit)
    else:
        state, error = read_json(STATE_PATH)
        if error:
            return None, f"gate replay needs a valid {STATE_PATH}: {error}"
        if not isinstance(state, dict) or state.get("ready") is not True:
            return None, f"gate replay needs ready=true in {STATE_PATH}"
        if not isinstance(state.get("subject"), str):
            return None, f"gate replay needs string subject in {STATE_PATH}"
        subject = Path(state["subject"])
    if not subject.is_absolute():
        subject = ROOT / subject
    if not subject.is_dir():
        return None, f"subject is not a directory: {subject}"
    return str(subject.resolve()), ""


def replay_argv(
    name: str, subject: str | None = None, output: str | None = None
) -> list[str]:
    if name == "gate":
        return [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.14.6",
            "--with",
            "psycopg[binary]==3.3.4",
            str(SOURCES[name]),
            "--subject",
            str(subject),
            "--output",
            output or "out/lab/gate.json",
        ]
    if name == "policy":
        return [sys.executable, str(SOURCES[name])]
    if name in {"isolation", "outbox", "compatibility"}:
        return [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.14.6",
            "--with",
            "psycopg[binary]==3.3.4",
            str(SOURCES[name]),
        ]
    if name == "legacy":
        return ["./demo"]
    raise ValueError(f"unsupported replay target: {name}")


def replay_env(name: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "LC_ALL": "C",
        **{key: os.environ[key] for key in ALLOWED_ENV[name] if key in os.environ},
    }


def run_replay(
    name: str,
    subject: str | None = None,
    output: str | None = None,
    dry_run: bool = False,
) -> int:
    if name == "gate":
        subject, error = replay_subject(subject)
        if error:
            print(error, file=sys.stderr)
            return 2
    argv = replay_argv(name, subject, output)
    if dry_run:
        print(shlex.join(argv))
        return 0
    script = path_for(SOURCES[name])
    if not script.exists():
        print(f"missing replay source: {script}", file=sys.stderr)
        return 2
    print(f"replay: {shlex.join(argv)}")
    try:
        completed = subprocess.run(argv, cwd=ROOT, env=replay_env(name), check=False)
    except OSError as exc:
        print(f"could not start replay: {exc}", file=sys.stderr)
        return 127
    return completed.returncode


def prepare_argv(
    subject: str | None = None,
    services_external: bool = False,
    stop: bool = False,
) -> list[str]:
    argv = [sys.executable, str(PREPARE_SOURCE)]
    if subject is not None:
        argv.extend(["--subject", subject])
    if services_external:
        argv.append("--services-external")
    if stop:
        argv.append("--stop")
    return argv


def run_prepare(
    subject: str | None = None,
    services_external: bool = False,
    stop: bool = False,
) -> int:
    source = path_for(PREPARE_SOURCE)
    if not source.is_file():
        print(f"missing preparation source: {source}", file=sys.stderr)
        return 2
    argv = prepare_argv(subject, services_external, stop)
    print(f"prepare: {shlex.join(argv)}")
    try:
        completed = subprocess.run(argv, cwd=ROOT, check=False)
    except OSError as exc:
        print(f"could not start preparation: {exc}", file=sys.stderr)
        return 127
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recorded Assay evidence reader; replays are explicit."
    )
    sub = parser.add_subparsers(dest="command")
    show = sub.add_parser("show")
    show.add_argument("target", choices=SHOW_CHOICES)
    replay = sub.add_parser("replay")
    replay.add_argument("target", choices=REPLAY_CHOICES)
    replay.add_argument("--subject", default=None)
    replay.add_argument("--output", default=None)
    replay.add_argument("--dry-run", action="store_true")
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--subject", default=None)
    prepare.add_argument("--services-external", action="store_true")
    prepare.add_argument("--stop", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "show":
        SHOW_HANDLERS[args.target]()
        return 0
    if args.command == "replay":
        return run_replay(args.target, args.subject, args.output, args.dry_run)
    if args.command == "prepare":
        return run_prepare(args.subject, args.services_external, args.stop)
    print("RECORDED JSON (not a rerun)")
    keys = {
        name: ("outbox_actual" if name == "outbox" else name) for name in SHOW_CHOICES
    }
    for name, key in keys.items():
        summary = default_summary(name, key)
        print(f"{name}: {summary}; ./lab show {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
