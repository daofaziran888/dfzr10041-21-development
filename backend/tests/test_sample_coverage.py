"""样本覆盖对照、重启核对、更正冲突和并发确认的回归测试。"""
from __future__ import annotations

import os
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.sample_coverage import (  # noqa: E402
    SampleCoverageService,
    run_narrative,
    seed_narrative_repository,
)


class SampleCoverageNarrativeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = seed_narrative_repository()
        self.service = SampleCoverageService(self.repo)

    def test_initial_denominators_duplicate_groups_and_priority(self) -> None:
        report = self.service.build_report()

        self.assertEqual(
            report["counts"],
            {
                "raw": 5,
                "dedup": 4,
                "duplicate_groups": 1,
                "merged_groups": 3,
                "covered_groups": 2,
                "uncovered_groups": 1,
            },
        )
        by_source = {item["source_id"]: item for item in report["sources"]}
        self.assertEqual(by_source["A"]["raw_denominator"], 2)
        self.assertEqual(by_source["A"]["dedup_count"], 1)
        self.assertEqual(by_source["A"]["representatives"]["merged"], ["A1"])
        self.assertEqual(by_source["B"]["raw_denominator"], 2)
        self.assertEqual(by_source["B"]["dedup_count"], 2)
        self.assertEqual(by_source["B"]["representatives"]["merged"], ["B1"])
        self.assertEqual(by_source["C"]["uncovered_group_count"], 1)
        self.assertEqual(report["priority_gap"]["source_id"], "C")
        self.assertEqual(report["priority_gap"]["line_id"], "C1")

    def test_restart_reconciles_c1_and_correction_refreshes_to_b2(self) -> None:
        claim = self.service.claim_gap()
        self.assertEqual(claim.line_id, "C1")
        sampled = self.service.sample_for_order(claim.order_id)
        self.assertTrue(sampled["created"])
        self.assertIn("C1", self.repo.deliveries)
        self.assertNotIn("C1", self.repo.applied_receipt_ids)

        self.service.restart()
        reopened = SampleCoverageService(self.repo)
        reopened.apply_narrative_correction()

        # 未核台账前，对照仍只按已有回执展示；业务入口不得直接再发 C1。
        stale = reopened.build_report()
        self.assertIn("C1", stale["unreceipted_deliveries"])

        verified = reopened.reconcile_deliveries(claim.order_id)
        self.assertEqual([record.line_id for record in verified], ["C1"])
        reopened.acknowledge_detail(claim.order_id)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: reopened.confirm_claim(claim.order_id), range(8)))

        self.assertEqual({r["result"]["line_id"] for r in results}, {"C1"})
        self.assertEqual({r["result"]["result_id"] for r in results}, {"RES-SUP-0001"})
        self.assertEqual(sum(r["deduplicated"] for r in results), 7)
        self.assertEqual(len([line for line in self.repo.deliveries if line == "C1"]), 1)

        corrected = reopened.snapshot_detail("r2-test")
        self.assertEqual(corrected["counts"]["raw"], 6)
        self.assertEqual(corrected["counts"]["dedup"], 5)
        self.assertEqual(corrected["counts"]["merged_groups"], 5)
        self.assertEqual(corrected["priority_gap"]["source_id"], "B")
        self.assertEqual(corrected["priority_gap"]["line_id"], "B2")

        recomputed = reopened.recompute_order_choice(self.repo.orders[claim.order_id])
        self.assertEqual(recomputed["line_id"], "C1")
        self.assertEqual(claim.conflict_with_current["current_line_id"], "B2")

    def test_refreshed_detail_claims_b2_and_concurrent_confirm_dispatches_once(self) -> None:
        _service, output = run_narrative()
        self.assertEqual(output["round2_refreshed_claim"]["line_id"], "B2")
        self.assertEqual(output["round2_concurrent_confirmation"]["unique_lines"], ["B2"])
        self.assertEqual(len(output["round2_concurrent_confirmation"]["unique_result_ids"]), 1)
        self.assertEqual(output["actual_delivery_ledger_lines"], ["A1", "B1", "B2", "C1"])
        self.assertTrue(output["checks"]["b1_excluded_from_all_candidates"])

    def test_full_narrative_assertions(self) -> None:
        _service, output = run_narrative()
        self.assertEqual(output["actual_supplement_lines"], ["C1", "B2"])
        self.assertTrue(output["checks"]["round1_single_result"])
        self.assertTrue(output["checks"]["round2_single_result"])

    def test_state_roundtrip_keeps_dispatched_c1(self) -> None:
        import tempfile
        from pathlib import Path

        from app.core.sample_coverage import load_repository, save_repository

        claim = self.service.claim_gap()
        self.service.sample_for_order(claim.order_id)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coverage-state.json"
            save_repository(self.repo, path)
            restored = load_repository(path)

        self.assertIn("C1", restored.deliveries)
        self.assertNotIn("C1", restored.applied_receipt_ids)
        restored_service = SampleCoverageService(restored)
        self.assertEqual(
            restored_service.build_report()["unreceipted_deliveries"], ["C1"]
        )


if __name__ == "__main__":
    unittest.main()
