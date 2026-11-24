#!/usr/bin/env python3
"""运行多来源样本覆盖、更正冲突、重启核对和并发确认剧本。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.sample_coverage import run_narrative  # noqa: E402


def print_source_reports(report: dict) -> None:
    print(f"明细版本：{report['revision']}；口径：{report['selection_basis']}")
    counts = report["counts"]
    print(
        "总数：原始 {raw} / 去重 {dedup} / 重复组 {duplicate_groups} / 归并组 {merged_groups} / "
        "已覆盖 {covered_groups} / 未覆盖 {uncovered_groups}".format(**counts)
    )
    for source in report["sources"]:
        print(
            f"  - 来源 {source['source_id']}: "
            f"原始分母={source['raw_denominator']}，原始={source['raw_count']}，"
            f"去重={source['dedup_count']}/{source['dedup_denominator']}，"
            f"归并组={source['merged_group_count']}，"
            f"覆盖={source['covered_group_count']}/{source['merged_denominator']}，"
            f"未覆盖={source['uncovered_group_count']}"
        )
        print(f"      原始代表/原行: {', '.join(source['representatives']['raw'])}")
        print(f"      去重代表: {', '.join(source['representatives']['dedup'])}")
        print(f"      归并代表: {', '.join(source['representatives']['merged'])}")
        for group in source["merged_groups"]:
            status = "已覆盖" if group["covered"] else "未覆盖"
            extra = f"，补抽候选={group['candidate_line_id']}" if group["candidate_line_id"] else ""
            print(
                f"      组 {group['group_key']}: 代表={group['representative_id']}，"
                f"成员={','.join(group['raw_member_ids'])}，状态={status}{extra}"
            )


def main() -> int:
    _service, output = run_narrative()

    print("=" * 78)
    print("阶段 1：初始多来源日志 → deduplicate → 覆盖对照")
    print("=" * 78)
    print_source_reports(output["initial_report"])
    gate = output["initial_sample_gate"]
    print(
        f"采样串联：去重后 {gate['dedup_count']} 条 × 0.5 → 阈值 {gate['threshold']}；"
        f"入选 {','.join(gate['selected_before_merge'])}；"
        f"已交付代表 {','.join(gate['delivered_merged_representatives'])}；"
        f"未入选 {','.join(gate['not_selected'])}"
    )

    print("\n重复组：")
    for group in output["checks"]["initial_duplicate_groups"]:
        print(f"  - {group['fingerprint'][:12]}…: {', '.join(group['member_ids'])}")

    print("\n" + "=" * 78)
    print("阶段 2：旧范围领用 → 实际派出 C1 → 未收回执重启 → 插入更正")
    print("=" * 78)
    claim1 = output["round1_claim"]
    print(f"领用单 {claim1['order_id']}: 来源={claim1['source_id']}，行={claim1['line_id']}，版本={claim1['detail_revision']}")
    print(f"sample 实际取样：{output['round1_sample']}")
    stale = output["report_after_dispatch_before_receipt"]
    print(
        f"未核台账前对照仍显示未回执派出：{stale['unreceipted_deliveries']}；"
        f"优先缺口={stale['priority_gap']['line_id'] if stale['priority_gap'] else '无'}"
    )
    print(f"重启与更正：{output['correction_between_claim_and_confirmation']['changes']}")
    print(f"先核清的真实交付：{', '.join(output['reconciled_delivery_lines'])}")
    conflict = claim1.get("conflict_with_current")
    # conflict is populated on the final order dict after ack/confirm.
    final_order1 = output["round1_order"]
    print(f"新旧范围冲突：{final_order1.get('conflict_with_current')}")
    conc1 = output["round1_concurrent_confirmation"]
    print(
        f"{conc1['workers']} 个并发确认：结果ID={conc1['unique_result_ids']}，"
        f"补抽行={conc1['unique_lines']}，去重响应={conc1['deduplicated_responses']}"
    )
    print(f"旧明细重算候选：{output['old_detail_recomputed_choice']['line_id']}")

    print("\n" + "=" * 78)
    print("阶段 3：更新整份对照后的数目、代表与新一条补抽")
    print("=" * 78)
    print_source_reports(output["corrected_report"])
    claim2 = output["round2_refreshed_claim"]
    print(f"更正后领用单 {claim2['order_id']}: 来源={claim2['source_id']}，行={claim2['line_id']}")
    conc2 = output["round2_concurrent_confirmation"]
    print(
        f"{conc2['workers']} 个并发确认：结果ID={conc2['unique_result_ids']}，"
        f"补抽行={conc2['unique_lines']}，去重响应={conc2['deduplicated_responses']}"
    )

    print("\n" + "=" * 78)
    print("回查")
    print("=" * 78)
    print(f"交付台账中的全部原行：{', '.join(output['actual_delivery_ledger_lines'])}")
    print(f"两轮实际补抽原行：{', '.join(output['actual_supplement_lines'])}")
    print(f"两版明细出现过的候选：{', '.join(output['candidate_lines_across_revisions'])}")
    print(f"B1 从候选排除：{output['checks']['b1_excluded_from_all_candidates']}")
    print(f"第一轮并发只保留一个结果：{output['checks']['round1_single_result']}")
    print(f"第二轮并发只保留一个结果：{output['checks']['round2_single_result']}")

    json_path = os.environ.get("COVERAGE_SCENARIO_JSON")
    if json_path:
        Path(json_path).write_text(
            json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"\n完整 JSON 已写入：{json_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
