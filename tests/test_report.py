import unittest

from assay.report import chrome_trace, markdown_report


class ReportTests(unittest.TestCase):
    def test_markdown_is_safe_and_complete(self):
        report = {
            "schema": "assay.report/v1", "name": "x | <tag> `code`\nnext", "status": "verified",
            "duration_ms": 12, "candidate": "c", "warnings": ["<warn>|\n"],
            "tasks": [{"id": "t1", "status": "verified", "cached": True, "key": "k",
                       "reason": "because | yes", "description": "desc", "source": ["src/a.py:7", "/etc/passwd", "../bad.md"],
                       "duration_ms": 2, "start_ms": 1, "input_files": ["a|b"], "needs": ["dep"],
                       "command": ["python", "x`\ny"], "returncode": 0, "stdout": "out | <b>\nline", "stderr": "err"}],
            "scenarios": [{"name": "s", "status": "rejected", "invariant": "i", "source": "tests/x.py:3", "seed": 1,
                           "events": [{"step": 1, "action": "go", "before": {"x": 1}, "after": {"x": 2}, "result": "bad", "work": {"n": 2}}]}],
            "benchmarks": [{"name": "b", "samples_ms": [1, 2, 3, 4], "source": "bench.md", "notes": "virtual time 99"}],
        }
        text = markdown_report(report)
        self.assertIn("reuse (cached; not newly executed)", text)
        self.assertIn("verified", text)
        self.assertIn("rejected", text)
        self.assertIn("[src/a.py:7](https://github.com/project-kikkuli/assay/blob/main/src/a.py#L7)", text)
        self.assertIn("rejected source", text)
        self.assertIn("p50 (ms)", text)
        self.assertIn("Samples", text)
        self.assertIn("<br>", text)
        self.assertNotIn("javascript:", text.lower())

    def test_links_validation_and_configurable_repository(self):
        text = markdown_report({"repo_url": "https://github.com/example/assay/", "tasks": [{
            "id": "t", "source": ["ok.md:1", "ok.py:0", "http://evil/x.py", "a\\b.py", "a:bad.py",
                                      "a/./b.py", "a/../b.py", "a\x01.py", "[x].py"],
        }]})
        self.assertIn("https://github.com/example/assay/blob/main/ok.md#L1", text)
        self.assertNotIn("#L0", text)
        self.assertNotIn("http://evil/x.py](", text)
        self.assertIn("\\[x\\].py", text)
        self.assertIn("rejected source", text)

    def test_tables_have_separator_rows_and_strict_samples(self):
        text = markdown_report({"tasks": [{"id": "t"}], "benchmarks": [{
            "name": "b", "samples_ms": [1, "2", None, float("nan"), -1, 3],
        }]})
        self.assertIn("| --- | --- | --- | --- |", text)
        self.assertIn("| b | 2 | 2.000 | 2.900 |", text)
        self.assertNotIn("| b | 6 |", text)

    def test_inline_code_normalizes_newlines_and_handles_embedded_runs(self):
        text = markdown_report({"tasks": [{"id": "t", "command": ["abc```def\nnext"]}]})
        self.assertNotIn("def\nnext", text)
        self.assertIn("abc```def next", text)

    def test_empty_inputs(self):
        self.assertIn("Tasks", markdown_report({}))
        trace = chrome_trace({})
        self.assertIn("traceEvents", trace)
        self.assertEqual(trace["traceEvents"][0]["ph"], "M")

    def test_trace_task_spans_and_projection(self):
        trace = chrome_trace({"tasks": [{"id": "a", "status": "unresolved", "cached": False,
                                         "key": "key", "source": ["a.py"], "start_ms": 4, "duration_ms": 2}],
                              "scenarios": [{"name": "s", "status": "verified", "events": [
                                  {"step": 1, "action": "one", "duration_ms": 3},
                                  {"step": 2, "action": "two", "duration_ms": 1}]}]})
        task = next(e for e in trace["traceEvents"] if e.get("name") == "a")
        self.assertEqual((task["ph"], task["ts"], task["dur"], task["pid"]), ("X", 4000, 2000, 1))
        projected = [e for e in trace["traceEvents"] if e.get("cat") == "scenario" and e.get("ph") == "X"]
        self.assertEqual([e["ts"] for e in projected], [0, 3000])
        self.assertTrue(projected[0]["args"]["projection"])


if __name__ == "__main__":
    unittest.main()
