from __future__ import annotations

from typing import Any


def plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "Node Type",
        "Index Name",
        "Actual Rows",
        "Actual Loops",
        "Plan Rows",
        "Rows Removed by Filter",
        "Rows Removed by Index Recheck",
        "Shared Hit Blocks",
        "Shared Read Blocks",
        "Shared Dirtied Blocks",
        "Shared Written Blocks",
        "Filter",
        "Index Cond",
    )
    nodes: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        nodes.append({key: node[key] for key in fields if key in node})
        for child in node.get("Plans", []):
            visit(child)

    visit(plan["Plan"])
    scans = [node for node in nodes if node["Node Type"].endswith("Scan")]
    return {
        "planning_time_ms": plan.get("Planning Time"),
        "execution_time_ms": plan.get("Execution Time"),
        "scan_nodes": scans,
        "scan_index_names": [node.get("Index Name") for node in scans],
        "scan_actual_rows": [node.get("Actual Rows") for node in scans],
        "scan_shared_hit_blocks": [node.get("Shared Hit Blocks") for node in scans],
        "scan_shared_read_blocks": [node.get("Shared Read Blocks") for node in scans],
        "rows_removed_by_filter": [
            node.get("Rows Removed by Filter", 0) for node in scans
        ],
    }


def validate_expected(
    result: dict[str, Any], performance: dict[str, Any], tenant_b: str
) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise AssertionError(message)

    def require_denied(
        observation: dict[str, Any], sqlstate: str, message: str
    ) -> None:
        require(
            observation.get("ok") is False and observation.get("sqlstate") == sqlstate,
            message,
        )

    security_cases = result["security_matrix"]["cases"]
    security_names = [case["case"] for case in security_cases]
    require(len(security_names) == len(set(security_names)), "duplicate security cases")
    expected_security_names = {
        "omit_where_select",
        "omit_where_update",
        "omit_where_delete",
        "wrong_tenant_insert",
        "with_check_policy_behavior",
        "cross_join_lookup",
        "duplicate_idempotency_key",
        "cross_tenant_composite_fk",
        "missing_context",
        "context_after_rollback",
        "context_after_commit",
        "same_role_can_spoof_tenant_guc",
    }
    require(
        set(security_names) == expected_security_names,
        "security case matrix is incomplete or unexpected",
    )
    cases = {case["case"]: case for case in security_cases}
    require(
        cases["omit_where_select"]["value"] == 2,
        "tenant SELECT omission was not isolated",
    )
    require(
        cases["omit_where_update"]["value"]
        == {"updated": 2, "tenant_b_before": 30, "tenant_b_after": 30},
        "tenant UPDATE omission was not isolated",
    )
    require(
        cases["omit_where_delete"]["value"] == {"before": 2, "deleted": 2, "after": 0},
        "tenant DELETE omission was not isolated",
    )
    require_denied(
        cases["wrong_tenant_insert"],
        "42501",
        "wrong-tenant insert was allowed or raised a general error",
    )
    policy = cases["with_check_policy_behavior"]["value"]
    weak = policy["explicit_with_check_true_plus_broad_select"]
    require(
        weak["ok"]
        and weak["value"]["updated"] == 1
        and weak["observed_tenant"] == tenant_b,
        "weak policy regression did not move the row",
    )
    require_denied(
        policy["omitted_with_check"],
        "42501",
        "omitted WITH CHECK did not inherit USING",
    )
    cross_join = cases["cross_join_lookup"]["value"]
    require(
        cross_join["cross_join_rows"] == cross_join["expected_cross_join_rows"] == 4,
        "cross-join leaked another tenant",
    )
    require(
        cases["duplicate_idempotency_key"]["value"]
        == {"duplicate_rejected": True, "sqlstate": "23505"},
        "duplicate idempotency key was allowed",
    )
    require_denied(
        cases["cross_tenant_composite_fk"],
        "23503",
        "cross-tenant composite FK was allowed or raised a general error",
    )
    missing = cases["missing_context"]["value"]
    require(missing["visible_rows"] == 0, "missing context exposed rows")
    require_denied(
        missing["insert"],
        "42501",
        "missing context did not produce the expected RLS denial",
    )
    for label, visible in (("context_after_rollback", 2), ("context_after_commit", 3)):
        value = cases[label]["value"]
        require(
            value["inside_visible"] == visible
            and value["after_visible_without_context"] == 0
            and value["setting_after"] == "",
            f"{label} leaked pooled context",
        )
    require(
        cases["same_role_can_spoof_tenant_guc"]["value"]["spoofed_tenant_rows"] == 3,
        "GUC spoof counterexample disappeared",
    )
    role = result["role_matrix"]
    require(
        [item["visible_rows"] for item in role["per_tenant_roles"]] == [2, 3],
        "per-tenant roles saw the wrong cardinality",
    )
    require(
        all(not item["cross_tenant_insert"]["ok"] for item in role["per_tenant_roles"]),
        "per-tenant role crossed tenant boundary",
    )
    require(
        all(item["current_user"] == item["role"] for item in role["per_tenant_roles"]),
        "tenant connection identity was not the login role",
    )
    require(
        all(
            not item["set_role_other_tenant"]["ok"]
            and item["set_role_other_tenant"]["sqlstate"] == "42501"
            for item in role["per_tenant_roles"]
        ),
        "tenant role could escalate to another tenant",
    )
    require(
        all(
            item["current_user_after_reset"] == item["role"]
            for item in role["per_tenant_roles"]
        ),
        "RESET ROLE changed tenant login identity",
    )
    require(
        all(
            item["guc_spoof_visible_rows"] == item["visible_rows"]
            for item in role["per_tenant_roles"]
        ),
        "tenant role policy followed spoofable GUC",
    )
    app_role = result["environment"]["app_role"]
    all_roles = result["environment"]["roles"]
    expected_targets = set(all_roles) - {app_role}
    attempts = role["app_setrole_attempts"]
    targets = [item.get("target") for item in attempts]
    require(
        len(all_roles) == 5 and len(set(all_roles)) == 5 and len(expected_targets) == 4,
        "role catalog is not the expected five-role fixture",
    )
    require(
        len(attempts) == 4
        and len(targets) == len(set(targets))
        and set(targets) == expected_targets,
        "app escalation matrix is incomplete or duplicated",
    )
    require(
        all(
            not item["ok"]
            and item["sqlstate"] == "42501"
            and item["current_user_after_reset"] == app_role
            for item in attempts
        ),
        "base app role could escalate with SET ROLE",
    )
    require(
        result["catalog"]["memberships"] == [],
        "fixture roles unexpectedly have role memberships",
    )
    require(
        role["table_owner_unforced_rows"] == 2
        and role["table_owner_forced_rows"] == 0
        and role["bypassrls_rows"] == 5,
        "RLS bypass counterexamples changed",
    )
    performance_cases = performance["cases"]
    require(
        len(performance_cases) == 2,
        "performance matrix must contain exactly two tenant cases",
    )
    require(
        len({case["tenant"] for case in performance_cases}) == 2,
        "performance tenant cases are duplicated",
    )
    require(
        sorted(case["expected_rows"] for case in performance_cases) == [100, 100000],
        "performance cardinality cases are incomplete",
    )
    for case in performance_cases:
        rls = case["rls"]
        baseline = case["baseline_explicit_predicate_bypassrls"]
        equivalence = case["plan_equivalence"]
        require(
            case["expected_rows"] == rls["result"] == baseline["result"],
            f"query cardinality mismatch for {case['tenant']}",
        )
        require(
            equivalence["same_result"], f"query result mismatch for {case['tenant']}"
        )
        require(
            case["plan_classification"] in ("equivalent", "non-equivalent-plan"),
            f"unclassified plan comparison for {case['tenant']}",
        )
        require(
            rls["n"] >= 20 and baseline["n"] >= 20,
            f"insufficient timing samples for {case['tenant']}",
        )
        for measured in (rls["plan_summary"], baseline["plan_summary"]):
            require(measured["scan_nodes"], f"missing scan plan for {case['tenant']}")
            require(
                all(
                    "Shared Hit Blocks" in node or "Shared Read Blocks" in node
                    for node in measured["scan_nodes"]
                ),
                f"missing buffer counters for {case['tenant']}",
            )
            require(
                "rows_removed_by_filter" in measured,
                f"missing filter counters for {case['tenant']}",
            )
