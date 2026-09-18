from __future__ import annotations

import copy
import importlib.util
import sys
import unittest
from pathlib import Path


_CHECKS_PATH = Path(__file__).with_name("checks.py")
_CHECKS_SPEC = importlib.util.spec_from_file_location(
    "assay_isolation_checks", _CHECKS_PATH
)
assert _CHECKS_SPEC and _CHECKS_SPEC.loader
_CHECKS_MODULE = importlib.util.module_from_spec(_CHECKS_SPEC)
sys.modules[_CHECKS_SPEC.name] = _CHECKS_MODULE
_CHECKS_SPEC.loader.exec_module(_CHECKS_MODULE)
validate_expected = _CHECKS_MODULE.validate_expected


def valid_fixture() -> tuple[dict, dict]:
    def case(name: str, value: object) -> dict:
        return {"case": name, "ok": True, "value": value}

    def denied(name: str, sqlstate: str) -> dict:
        return {"case": name, "ok": False, "sqlstate": sqlstate}

    cases = [
        case("omit_where_select", 2),
        case(
            "omit_where_update",
            {"updated": 2, "tenant_b_before": 30, "tenant_b_after": 30},
        ),
        case("omit_where_delete", {"before": 2, "deleted": 2, "after": 0}),
        denied("wrong_tenant_insert", "42501"),
        case(
            "with_check_policy_behavior",
            {
                "explicit_with_check_true_plus_broad_select": {
                    "ok": True,
                    "value": {"updated": 1},
                    "observed_tenant": "tenant-b",
                },
                "omitted_with_check": {"ok": False, "sqlstate": "42501"},
            },
        ),
        case(
            "cross_join_lookup", {"cross_join_rows": 4, "expected_cross_join_rows": 4}
        ),
        case(
            "duplicate_idempotency_key",
            {"duplicate_rejected": True, "sqlstate": "23505"},
        ),
        denied("cross_tenant_composite_fk", "23503"),
        case(
            "missing_context",
            {"visible_rows": 0, "insert": {"ok": False, "sqlstate": "42501"}},
        ),
        case(
            "context_after_rollback",
            {
                "inside_visible": 2,
                "after_visible_without_context": 0,
                "setting_after": "",
            },
        ),
        case(
            "context_after_commit",
            {
                "inside_visible": 3,
                "after_visible_without_context": 0,
                "setting_after": "",
            },
        ),
        case("same_role_can_spoof_tenant_guc", {"spoofed_tenant_rows": 3}),
    ]
    roles = [
        {
            "role": "tenant-a",
            "current_user": "tenant-a",
            "visible_rows": 2,
            "cross_tenant_insert": {"ok": False},
            "set_role_other_tenant": {"ok": False, "sqlstate": "42501"},
            "current_user_after_reset": "tenant-a",
            "guc_spoof_visible_rows": 2,
        },
        {
            "role": "tenant-b",
            "current_user": "tenant-b",
            "visible_rows": 3,
            "cross_tenant_insert": {"ok": False},
            "set_role_other_tenant": {"ok": False, "sqlstate": "42501"},
            "current_user_after_reset": "tenant-b",
            "guc_spoof_visible_rows": 3,
        },
    ]
    all_roles = ["owner", "app", "tenant-a", "tenant-b", "bypass"]
    app_attempts = [
        {
            "target": target,
            "ok": False,
            "sqlstate": "42501",
            "current_user_after_reset": "app",
        }
        for target in all_roles
        if target != "app"
    ]
    result = {
        "environment": {"roles": all_roles, "app_role": "app"},
        "catalog": {"memberships": []},
        "security_matrix": {"cases": cases},
        "role_matrix": {
            "per_tenant_roles": roles,
            "app_setrole_attempts": app_attempts,
            "table_owner_unforced_rows": 2,
            "table_owner_forced_rows": 0,
            "bypassrls_rows": 5,
        },
    }
    scan = {
        "scan_nodes": [{"Node Type": "Index Scan", "Shared Hit Blocks": 1}],
        "rows_removed_by_filter": [0],
    }
    performance = {
        "cases": [
            {
                "tenant": tenant,
                "expected_rows": expected_rows,
                "rls": {
                    "result": expected_rows,
                    "n": 20,
                    "samples_ms": [0.1] * 20,
                    "plan_summary": scan,
                },
                "baseline_explicit_predicate_bypassrls": {
                    "result": expected_rows,
                    "n": 20,
                    "samples_ms": [0.1] * 20,
                    "plan_summary": scan,
                },
                "plan_equivalence": {"same_result": True},
                "plan_classification": "equivalent",
            }
            for tenant, expected_rows in (("tenant-a", 100), ("tenant-b", 100000))
        ]
    }
    return result, performance


class ValidateExpectedChallenges(unittest.TestCase):
    def valid_fixture_for_mutation(self) -> tuple[dict, dict]:
        result, performance = valid_fixture()
        validate_expected(result, performance, "tenant-b")
        return result, performance

    def test_positive_fixture_passes(self) -> None:
        self.valid_fixture_for_mutation()

    def test_rejects_missing_result_case(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        result["security_matrix"]["cases"].pop(0)
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_tenant_role_guc_spoof(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        result["role_matrix"]["per_tenant_roles"][0]["guc_spoof_visible_rows"] = 3
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_empty_performance_samples(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        performance["cases"][0]["rls"]["n"] = 0
        performance["cases"][0]["rls"]["samples_ms"] = []
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_empty_performance_matrix(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        performance["cases"] = []
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_empty_app_escalation_matrix(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        result["role_matrix"]["app_setrole_attempts"] = []
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_duplicate_performance_tenant(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        performance["cases"][1] = copy.deepcopy(performance["cases"][0])
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")

    def test_rejects_general_error_instead_of_rls_denial(self) -> None:
        result, performance = self.valid_fixture_for_mutation()
        result["security_matrix"]["cases"][3] = {
            "case": "wrong_tenant_insert",
            "ok": False,
            "sqlstate": "XX000",
        }
        with self.assertRaises(AssertionError):
            validate_expected(result, performance, "tenant-b")
