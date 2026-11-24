"""多来源样本覆盖对照与补抽状态机。

这个模块只依赖标准库，负责四件可重算的事情：

1. ``deduplicate``：按指纹给出原始日志、去重代表和重复组；
2. ``build_report``：给出来源分母，以及原始 / 去重 / 已归并三段数目和代表；
3. ``claim_gap`` / ``sample_for_order``：按实际采用的明细选择缺口来源并实际派出原行；
4. ``confirm_claim``：用锁和幂等键保证同一轮并发确认只形成一个补抽结果。

采样交付台账与覆盖对照中的回执分开保存：派出 C1 但回执未进对照时，重启后必须先
``reconcile_deliveries`` 核清交付，不能把未确认回执误当成“未派出”而重复发送。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Callable
from uuid import uuid4
import json
import os
import tempfile


# ---------------------------------------------------------------------------
# 数据载体
# ---------------------------------------------------------------------------


@dataclass
class CoverageLine:
    """参与覆盖对照的一条原始日志。"""

    line_id: str
    source_id: str
    occurred_at: datetime
    fingerprint: str
    group_key: str
    raw_text: str = ""
    confirmed: bool = True
    # 分组更正只能改 group_key；来源来路固定保留在 provenance_source_id。
    provenance_source_id: str = ""

    def __post_init__(self) -> None:
        if not self.provenance_source_id:
            self.provenance_source_id = self.source_id


@dataclass
class DeliveryRecord:
    """采样交付台账中的独立记录。"""

    delivery_id: str
    line_id: str
    source_id: str
    idempotency_key: str
    dispatched_at: datetime
    carrier_status: str = "delivered"
    verified: bool = True
    receipt: str = "sampling-ledger"


@dataclass
class SupplementOrder:
    """一次补抽领用及其两个独立确认状态。"""

    order_id: str
    round_no: int
    quota: int
    source_id: str
    group_key: str
    line_id: str
    detail_revision: str
    idempotency_key: str
    adopted_detail: dict[str, Any]
    occupied_at_claim: list[str]
    status: str = "已领取"
    detail_confirmed: bool = False
    delivery_confirmed: bool = False
    delivery_id: str | None = None
    result: dict[str, Any] | None = None
    confirm_calls: int = 0
    duplicate_confirmations: int = 0
    conflict_with_current: dict[str, str] | None = None
    created_at: datetime = field(default_factory=datetime.now)
    confirmed_at: datetime | None = None


class CoverageRepository:
    """进程内状态仓库；重启演示可丢弃 service，但共享本仓库。"""

    def __init__(self) -> None:
        self.lock = RLock()
        self.lines: dict[str, CoverageLine] = {}
        self.deliveries: dict[str, DeliveryRecord] = {}
        self.idempotency_index: dict[str, str] = {}
        self.orders: dict[str, SupplementOrder] = {}
        self.details: dict[str, dict[str, Any]] = {}
        self.applied_receipt_ids: set[str] = set()
        self.timeline: list[dict[str, Any]] = []
        self.corrections: list[dict[str, Any]] = []
        self.current_revision = "r1-initial"
        self.delivery_trust_verified = True
        self.next_round = 1

    def add_line(self, line: CoverageLine) -> None:
        self.lines[line.line_id] = line

    def log(self, stage: str, message: str, **data: Any) -> None:
        event = {
            "seq": len(self.timeline) + 1,
            "at": datetime.now().isoformat(timespec="seconds"),
            "stage": stage,
            "message": message,
            **data,
        }
        self.timeline.append(event)


def repository_to_dict(repo: CoverageRepository) -> dict[str, Any]:
    """把重启后仍需核清的领用、交付和更正状态写成 JSON 安全结构。"""

    return {
        "lines": [line_brief(line) for line in repo.lines.values()],
        "deliveries": [
            {
                "delivery_id": rec.delivery_id,
                "line_id": rec.line_id,
                "source_id": rec.source_id,
                "idempotency_key": rec.idempotency_key,
                "dispatched_at": iso(rec.dispatched_at),
                "carrier_status": rec.carrier_status,
                "verified": rec.verified,
                "receipt": rec.receipt,
            }
            for rec in repo.deliveries.values()
        ],
        "orders": [
            {
                "order_id": order.order_id,
                "round_no": order.round_no,
                "quota": order.quota,
                "source_id": order.source_id,
                "group_key": order.group_key,
                "line_id": order.line_id,
                "detail_revision": order.detail_revision,
                "idempotency_key": order.idempotency_key,
                "adopted_detail": order.adopted_detail,
                "occupied_at_claim": order.occupied_at_claim,
                "status": order.status,
                "detail_confirmed": order.detail_confirmed,
                "delivery_confirmed": order.delivery_confirmed,
                "delivery_id": order.delivery_id,
                "result": order.result,
                "confirm_calls": order.confirm_calls,
                "duplicate_confirmations": order.duplicate_confirmations,
                "conflict_with_current": order.conflict_with_current,
                "created_at": iso(order.created_at),
                "confirmed_at": iso(order.confirmed_at),
            }
            for order in repo.orders.values()
        ],
        "details": repo.details,
        "applied_receipt_ids": sorted(repo.applied_receipt_ids),
        "timeline": repo.timeline,
        "corrections": repo.corrections,
        "current_revision": repo.current_revision,
        "delivery_trust_verified": repo.delivery_trust_verified,
        "next_round": repo.next_round,
    }


def save_repository(repo: CoverageRepository, path: str | Path) -> None:
    """原子写入状态文件，避免真实服务重启时读到半截 JSON。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(repository_to_dict(repo), ensure_ascii=False, indent=2, default=str)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_repository(path: str | Path) -> CoverageRepository:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    repo = CoverageRepository()
    for item in data.get("lines", []):
        repo.add_line(
            CoverageLine(
                line_id=item["line_id"],
                source_id=item["source_id"],
                occurred_at=parse_dt(item["occurred_at"]) or datetime.now(),
                fingerprint=item["fingerprint"],
                group_key=item["group_key"],
                raw_text=item.get("raw_text", ""),
                confirmed=item.get("confirmed", True),
                provenance_source_id=item.get("provenance_source_id") or item["source_id"],
            )
        )
    for item in data.get("deliveries", []):
        record = DeliveryRecord(
            delivery_id=item["delivery_id"],
            line_id=item["line_id"],
            source_id=item["source_id"],
            idempotency_key=item["idempotency_key"],
            dispatched_at=parse_dt(item.get("dispatched_at")) or datetime.now(),
            carrier_status=item.get("carrier_status", "delivered"),
            verified=item.get("verified", True),
            receipt=item.get("receipt", "sampling-ledger"),
        )
        repo.deliveries[record.line_id] = record
        repo.idempotency_index[record.idempotency_key] = record.delivery_id

    for item in data.get("orders", []):
        order = SupplementOrder(
            order_id=item["order_id"],
            round_no=int(item["round_no"]),
            quota=int(item.get("quota", 1)),
            source_id=item["source_id"],
            group_key=item["group_key"],
            line_id=item["line_id"],
            detail_revision=item["detail_revision"],
            idempotency_key=item["idempotency_key"],
            adopted_detail=item.get("adopted_detail", {}),
            occupied_at_claim=list(item.get("occupied_at_claim", [])),
            status=item.get("status", "已领取"),
            detail_confirmed=item.get("detail_confirmed", False),
            delivery_confirmed=item.get("delivery_confirmed", False),
            delivery_id=item.get("delivery_id"),
            result=item.get("result"),
            confirm_calls=int(item.get("confirm_calls", 0)),
            duplicate_confirmations=int(item.get("duplicate_confirmations", 0)),
            conflict_with_current=item.get("conflict_with_current"),
            created_at=parse_dt(item.get("created_at")) or datetime.now(),
            confirmed_at=parse_dt(item.get("confirmed_at")),
        )
        repo.orders[order.order_id] = order

    repo.details = data.get("details", {})
    repo.applied_receipt_ids = set(data.get("applied_receipt_ids", []))
    repo.timeline = data.get("timeline", [])
    repo.corrections = data.get("corrections", [])
    repo.current_revision = data.get("current_revision", "r1-initial")
    repo.delivery_trust_verified = bool(data.get("delivery_trust_verified", True))
    repo.next_round = int(data.get("next_round", len(repo.orders) + 1))
    return repo


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if isinstance(value, datetime) else None


def parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def line_brief(line: CoverageLine) -> dict[str, Any]:
    return {
        "line_id": line.line_id,
        "source_id": line.source_id,
        "provenance_source_id": line.provenance_source_id,
        "occurred_at": iso(line.occurred_at),
        "fingerprint": line.fingerprint,
        "group_key": line.group_key,
        "raw_text": line.raw_text,
        "confirmed": line.confirmed,
    }


def select_priority_gap(report: dict[str, Any]) -> dict[str, Any] | None:
    """从覆盖对照明细中选择缺口来源和代表。

    来源优先级：实际可补未覆盖组数降序；平局时最早候选时间、来源编号。
    来源内部：最早未覆盖组中的最早待补原行。
    """

    source_rows: list[tuple[int, str, str, dict[str, Any]]] = []
    for source in report["sources"]:
        gap_groups = [
            group
            for group in source["merged_groups"]
            if not group["covered"] and group.get("candidate_line_id")
        ]
        if not gap_groups:
            continue
        first_group = min(
            gap_groups,
            key=lambda g: (
                g.get("candidate_occurred_at") or "9999",
                g.get("candidate_line_id") or "",
                g["group_key"],
            ),
        )
        source_rows.append(
            (
                len(gap_groups),
                first_group.get("candidate_occurred_at") or "9999",
                source["source_id"],
                {
                    "source_id": source["source_id"],
                    "uncovered_group_count": len(gap_groups),
                    "group_key": first_group["group_key"],
                    "line_id": first_group["candidate_line_id"],
                    "candidate_occurred_at": first_group.get("candidate_occurred_at"),
                    "pending_line_ids": first_group.get("pending_line_ids", []),
                },
            )
        )
    if not source_rows:
        return None
    source_rows.sort(key=lambda item: (-item[0], item[1], item[2]))
    choice = source_rows[0][3]
    choice["ranking"] = [
        {"source_id": source_id, "uncovered_group_count": count}
        for count, _ts, source_id, _choice in sorted(
            source_rows, key=lambda item: (-item[0], item[1], item[2])
        )
    ]
    return choice


# ---------------------------------------------------------------------------
# 主服务
# ---------------------------------------------------------------------------


class SampleCoverageService:
    def __init__(self, repo: CoverageRepository | None = None) -> None:
        self.repo = repo or CoverageRepository()

    # ---------- 去重与覆盖明细 ----------

    def deduplicate(
        self, lines: list[CoverageLine] | None = None
    ) -> list[dict[str, Any]]:
        """按 fingerprint 给出去重组；每组保留最早原行作为实际代表。"""

        lines = list(self.repo.lines.values()) if lines is None else lines
        by_fp: dict[str, list[CoverageLine]] = {}
        for line in lines:
            by_fp.setdefault(line.fingerprint, []).append(line)

        groups: list[dict[str, Any]] = []
        for fingerprint, members in by_fp.items():
            ordered = sorted(members, key=lambda x: (x.occurred_at, x.line_id))
            representative = ordered[0]
            groups.append(
                {
                    "fingerprint": fingerprint,
                    "representative_id": representative.line_id,
                    "member_ids": [line.line_id for line in ordered],
                    "duplicate_ids": [line.line_id for line in ordered[1:]],
                    "duplicate_count": max(0, len(ordered) - 1),
                    "representative": line_brief(representative),
                    "members": [line_brief(line) for line in ordered],
                }
            )
        groups.sort(key=lambda g: (g["representative"]["occurred_at"] or "", g["representative_id"]))
        return groups

    def build_report(self) -> dict[str, Any]:
        """计算当前多来源样本覆盖对照。"""

        with self.repo.lock:
            lines = sorted(
                self.repo.lines.values(),
                key=lambda line: (line.source_id, line.occurred_at, line.line_id),
            )
            # 重启后、核对独立交付台账前，只能先依据对照内已收到的回执。
            occupied = (
                set(self.repo.deliveries)
                if self.repo.delivery_trust_verified
                else set(self.repo.applied_receipt_ids)
            )
            sources: list[dict[str, Any]] = []

            for source_id in sorted({line.source_id for line in lines}):
                source_lines = [line for line in lines if line.source_id == source_id]
                dedup_groups = self.deduplicate(source_lines)
                deduped_ids = [group["representative_id"] for group in dedup_groups]
                line_by_id = {line.line_id: line for line in source_lines}

                raw_member_to_dedup_group: dict[str, str] = {}
                for group in dedup_groups:
                    for line_id in group["member_ids"]:
                        raw_member_to_dedup_group[line_id] = group["representative_id"]

                merged: dict[str, list[CoverageLine]] = {}
                for line_id in deduped_ids:
                    line = line_by_id[line_id]
                    merged.setdefault(line.group_key, []).append(line)

                merged_groups: list[dict[str, Any]] = []
                for group_key, group_lines in merged.items():
                    ordered = sorted(
                        group_lines, key=lambda line: (line.occurred_at, line.line_id)
                    )
                    representative = ordered[0]
                    deduped_member_ids = [line.line_id for line in ordered]
                    raw_member_ids: list[str] = []
                    for line_id in deduped_member_ids:
                        fp_group = next(
                            group
                            for group in dedup_groups
                            if group["representative_id"] == line_id
                        )
                        raw_member_ids.extend(fp_group["member_ids"])
                    raw_member_ids = sorted(
                        set(raw_member_ids),
                        key=lambda line_id: (
                            line_by_id[line_id].occurred_at,
                            line_id,
                        ),
                    )
                    pending = [
                        line_id
                        for line_id in deduped_member_ids
                        if line_id not in occupied
                    ]
                    candidate_id = pending[0] if pending else None
                    delivered_ids = [
                        line_id
                        for line_id in raw_member_ids
                        if line_id in self.repo.deliveries
                    ]
                    receipt_ids = [
                        line_id
                        for line_id in raw_member_ids
                        if line_id in self.repo.applied_receipt_ids
                    ]
                    merged_groups.append(
                        {
                            "group_key": group_key,
                            "representative_policy": "earliest",
                            "representative_id": representative.line_id,
                            "representative": line_brief(representative),
                            "deduped_member_ids": deduped_member_ids,
                            "raw_member_ids": raw_member_ids,
                            "pending_line_ids": pending,
                            "candidate_line_id": candidate_id,
                            "candidate_occurred_at": (
                                iso(line_by_id[candidate_id].occurred_at)
                                if candidate_id
                                else None
                            ),
                            "delivered_line_ids": delivered_ids,
                            "receipt_line_ids": receipt_ids,
                            "covered": any(line_id in occupied for line_id in deduped_member_ids),
                            "receipt_confirmed_covered": bool(receipt_ids),
                            "delivery_without_comparison_receipt": [
                                line_id
                                for line_id in delivered_ids
                                if line_id not in self.repo.applied_receipt_ids
                            ],
                        }
                    )

                merged_groups.sort(
                    key=lambda g: (g["representative"]["occurred_at"] or "", g["group_key"])
                )
                uncovered = [g for g in merged_groups if not g["covered"] and g["candidate_line_id"]]
                sources.append(
                    {
                        "source_id": source_id,
                        # 三段分母分别保留：原始总量、进入去重的量、覆盖组分母。
                        "raw_denominator": len(source_lines),
                        "raw_count": len(source_lines),
                        "dedup_denominator": len(source_lines),
                        "dedup_count": len(deduped_ids),
                        "duplicate_group_count": sum(
                            1 for group in dedup_groups if group["duplicate_count"]
                        ),
                        "duplicate_line_count": sum(
                            group["duplicate_count"] for group in dedup_groups
                        ),
                        "merged_denominator": len(merged_groups),
                        "merged_group_count": len(merged_groups),
                        "covered_group_count": len(merged_groups) - len(uncovered),
                        "uncovered_group_count": len(uncovered),
                        "coverage_rate": (
                            (len(merged_groups) - len(uncovered)) / len(merged_groups)
                            if merged_groups
                            else 1.0
                        ),
                        "raw_lines": [line_brief(line) for line in source_lines],
                        "dedup_groups": dedup_groups,
                        "merged_groups": merged_groups,
                        "representatives": {
                            "raw": [line.line_id for line in source_lines],
                            "dedup": deduped_ids,
                            "merged": [g["representative_id"] for g in merged_groups],
                        },
                    }
                )

            squeezed_out: dict[str, list[dict[str, Any]]] = {
                "dedup_duplicate_lines": [],
                "merged_non_representative_lines": [],
                "uncovered_groups": [],
            }
            for source in sources:
                for dedup_group in source["dedup_groups"]:
                    for line_id in dedup_group["duplicate_ids"]:
                        squeezed_out["dedup_duplicate_lines"].append(
                            {
                                "source_id": source["source_id"],
                                "line_id": line_id,
                                "kept_representative_id": dedup_group["representative_id"],
                                "reason": "same_fingerprint_duplicate",
                            }
                        )
                for group in source["merged_groups"]:
                    for line_id in group["deduped_member_ids"]:
                        if line_id != group["representative_id"]:
                            squeezed_out["merged_non_representative_lines"].append(
                                {
                                    "source_id": source["source_id"],
                                    "line_id": line_id,
                                    "group_key": group["group_key"],
                                    "kept_representative_id": group["representative_id"],
                                    "reason": "merged_into_group_representative",
                                }
                            )
                    if not group["covered"] and group["candidate_line_id"]:
                        squeezed_out["uncovered_groups"].append(
                            {
                                "source_id": source["source_id"],
                                "group_key": group["group_key"],
                                "candidate_line_id": group["candidate_line_id"],
                                "reason": "not_covered_by_initial_or_supplement_quota",
                            }
                        )

            all_dedup_groups = self.deduplicate(lines)
            report = {
                "revision": self.repo.current_revision,
                "selection_basis": (
                    "delivery_reconciled"
                    if self.repo.delivery_trust_verified
                    else "coverage_receipts_pending_reconciliation"
                ),
                "denominators": {
                    "raw": len(lines),
                    "dedup": len(all_dedup_groups),
                    "merged_groups": sum(s["merged_group_count"] for s in sources),
                },
                "counts": {
                    "raw": len(lines),
                    "dedup": len(all_dedup_groups),
                    "duplicate_groups": sum(
                        1 for group in all_dedup_groups if group["duplicate_count"]
                    ),
                    "merged_groups": sum(s["merged_group_count"] for s in sources),
                    "covered_groups": sum(s["covered_group_count"] for s in sources),
                    "uncovered_groups": sum(s["uncovered_group_count"] for s in sources),
                },
                "sources": sources,
                "duplicate_groups": all_dedup_groups,
                "squeezed_out": squeezed_out,
                "priority_gap": None,
                "delivery_trust_verified": self.repo.delivery_trust_verified,
                "unreceipted_deliveries": [
                    line_id
                    for line_id in self.repo.deliveries
                    if line_id not in self.repo.applied_receipt_ids
                ],
            }
            report["priority_gap"] = select_priority_gap(report)
            return report

    def snapshot_detail(self, revision: str) -> dict[str, Any]:
        report = self.build_report()
        report["revision"] = revision
        # 深拷贝式 JSON 往返会丢 datetime；这里报告已经全部是可 JSON 化结构，
        # 直接用 repr 安全的 copy.deepcopy 即可。
        import copy

        snapshot = copy.deepcopy(report)
        with self.repo.lock:
            self.repo.details[revision] = snapshot
            self.repo.current_revision = revision
        return snapshot

    # ---------- 补抽领用、取样、交付核对、确认 ----------

    def claim_gap(self) -> SupplementOrder:
        with self.repo.lock:
            if not self.repo.delivery_trust_verified:
                raise RuntimeError("重启后必须先核对采样交付台账，再领取剩余缺口")
            report = self.build_report()
            choice = report.get("priority_gap")
            if choice is None:
                raise RuntimeError("没有可补抽的未覆盖组")

            round_no = self.repo.next_round
            self.repo.next_round += 1
            order_id = f"SUP-{round_no:04d}"
            key = f"quota-round-{round_no}"
            order = SupplementOrder(
                order_id=order_id,
                round_no=round_no,
                quota=1,
                source_id=choice["source_id"],
                group_key=choice["group_key"],
                line_id=choice["line_id"],
                detail_revision=report["revision"],
                idempotency_key=key,
                adopted_detail=report,
                occupied_at_claim=sorted(self.repo.deliveries),
            )
            self.repo.orders[order_id] = order
            self.repo.log(
                "claim",
                f"值班员领取 {choice['source_id']} 来源缺口，候选原行 {choice['line_id']}",
                order_id=order_id,
                source_id=order.source_id,
                line_id=order.line_id,
                revision=order.detail_revision,
                ranking=choice.get("ranking"),
            )
            return order

    def recompute_order_choice(self, order: SupplementOrder) -> dict[str, Any] | None:
        """从该单实际采用并保存的明细重算选择。"""

        import copy

        detail = copy.deepcopy(order.adopted_detail)
        # 领用后新派出的行不属于领用时上下文；用领用时占用集合重算。
        for source in detail.get("sources", []):
            for group in source.get("merged_groups", []):
                pending = [
                    line_id
                    for line_id in group.get("deduped_member_ids", [])
                    if line_id not in set(order.occupied_at_claim)
                ]
                group["pending_line_ids"] = pending
                group["candidate_line_id"] = pending[0] if pending else None
                group["covered"] = any(
                    line_id in set(order.occupied_at_claim)
                    for line_id in group.get("deduped_member_ids", [])
                )
        return select_priority_gap(detail)

    def sample(self, order_id: str) -> dict[str, Any]:
        """补抽入口的短名称：核对采用明细中的缺口并实际取样。"""

        return self.sample_for_order(order_id)

    def sample_for_order(self, order_id: str) -> dict[str, Any]:
        """补抽入口：先从实际采用明细核对缺口，再实际派出行。"""

        with self.repo.lock:
            order = self.repo.orders.get(order_id)
            if order is None:
                raise KeyError(order_id)

            recomputed = self.recompute_order_choice(order)
            if not recomputed or recomputed["line_id"] != order.line_id:
                raise RuntimeError("采用明细已无法重算出该补抽行，拒绝取样")

            if order.delivery_id and order.delivery_id in self.repo.deliveries:
                record = self.repo.deliveries[order.delivery_id]
                return {"delivery": record, "created": False, "order": order}

            existing = self.repo.deliveries.get(order.line_id)
            if existing is not None:
                order.delivery_id = existing.delivery_id
                return {"delivery": existing, "created": False, "order": order}

            line = self.repo.lines[order.line_id]
            key = f"{order.idempotency_key}:sample"
            record = DeliveryRecord(
                delivery_id=f"DLV-{order.line_id}",
                line_id=line.line_id,
                source_id=line.provenance_source_id,
                idempotency_key=key,
                dispatched_at=datetime.now(),
            )
            self.repo.deliveries[line.line_id] = record
            self.repo.idempotency_index[key] = record.delivery_id
            order.delivery_id = record.delivery_id
            # 派出成功不等于覆盖对照已收到回执；在核对/回执落库前，对照仍按旧回执展示。
            self.repo.delivery_trust_verified = False
            self.repo.log(
                "sample",
                f"补抽入口核对缺口后实际派出 {line.line_id}",
                order_id=order.order_id,
                line_id=line.line_id,
                source_id=record.source_id,
                delivery_id=record.delivery_id,
            )
            return {"delivery": record, "created": True, "order": order}

    def restart(self) -> None:
        """模拟进程/页面重开：清空未完成的核对标记，但保留台账和领用单。"""

        with self.repo.lock:
            self.repo.delivery_trust_verified = False
            self.repo.log(
                "restart",
                "服务重启：采样台账与领用单保留，覆盖对照中尚未取回的回执不假定为已确认",
                unreceipted=list(
                    line_id
                    for line_id in self.repo.deliveries
                    if line_id not in self.repo.applied_receipt_ids
                ),
            )

    def reconcile_deliveries(self, order_id: str | None = None) -> list[DeliveryRecord]:
        """向独立采样台账核清真实交付，并把可取回的回执写入覆盖对照。"""

        with self.repo.lock:
            if order_id is not None:
                order = self.repo.orders.get(order_id)
                if order is None:
                    raise KeyError(order_id)
                line_ids = [order.line_id]
            else:
                line_ids = list(self.repo.deliveries)

            verified: list[DeliveryRecord] = []
            for line_id in line_ids:
                record = self.repo.deliveries.get(line_id)
                if record is None:
                    continue
                record.verified = True
                record.carrier_status = "delivered"
                self.repo.applied_receipt_ids.add(line_id)
                verified.append(record)
                for order in self.repo.orders.values():
                    if order.line_id == line_id:
                        order.delivery_confirmed = True
                        order.delivery_id = record.delivery_id

            self.repo.delivery_trust_verified = True
            self.repo.log(
                "reconcile",
                "已从采样交付台账核清真实交付并补入覆盖回执",
                verified_line_ids=[record.line_id for record in verified],
            )
            return verified

    def acknowledge_detail(self, order_id: str) -> SupplementOrder:
        """确认实际采用的领用明细；新到更正不会改写这张旧单的选择。"""

        with self.repo.lock:
            order = self.repo.orders[order_id]
            recomputed = self.recompute_order_choice(order)
            if not recomputed or recomputed["line_id"] != order.line_id:
                raise RuntimeError("采用明细重算失败")
            current = self.build_report()
            current_choice = current.get("priority_gap")
            if current_choice and current_choice["line_id"] != order.line_id:
                order.conflict_with_current = {
                    "adopted_revision": order.detail_revision,
                    "adopted_line_id": order.line_id,
                    "current_revision": current["revision"],
                    "current_line_id": current_choice["line_id"],
                    "resolution": "沿用原领用明细",
                }
            order.detail_confirmed = True
            self.repo.log(
                "detail-ack",
                f"明细确认沿用 {order.detail_revision} 的选择 {order.line_id}",
                order_id=order.order_id,
                conflict=order.conflict_with_current,
            )
            return order

    def confirm_claim(self, order_id: str, actor: str = "duty") -> dict[str, Any]:
        """并发安全地确认一轮补抽；重复调用只返回同一个结果。"""

        with self.repo.lock:
            order = self.repo.orders.get(order_id)
            if order is None:
                raise KeyError(order_id)
            order.confirm_calls += 1

            if order.status == "已确认" and order.result is not None:
                order.duplicate_confirmations += 1
                return {
                    "ok": True,
                    "deduplicated": True,
                    "message": "该轮补抽名额已确认，重复确认只返回原结果",
                    "result": order.result,
                    "confirm_calls": order.confirm_calls,
                }

            # 最终确认前再次按该单实际采用的明细重算。
            recomputed = self.recompute_order_choice(order)
            if not recomputed or recomputed["line_id"] != order.line_id:
                raise RuntimeError("采用明细无法重算出领用候选")

            # 独立交付确认：先查真实台账；没有派出记录时，补抽入口在此实际取样。
            record = self.repo.deliveries.get(order.line_id)
            created_dispatch = False
            if record is None:
                sampled = self.sample_for_order(order_id)
                record = sampled["delivery"]
                created_dispatch = bool(sampled["created"])
            self.repo.applied_receipt_ids.add(record.line_id)
            self.repo.delivery_trust_verified = True
            order.delivery_id = record.delivery_id
            order.delivery_confirmed = True
            order.detail_confirmed = True

            result_id = f"RES-{order.order_id}"
            order.result = {
                "result_id": result_id,
                "order_id": order.order_id,
                "quota_key": order.idempotency_key,
                "source_id": order.source_id,
                "group_key": order.group_key,
                "line_id": order.line_id,
                "detail_revision": order.detail_revision,
                "delivery_id": record.delivery_id,
                "actual_dispatched_line_id": record.line_id,
                "created_dispatch_in_confirmation": created_dispatch,
                "actor": actor,
            }
            order.status = "已确认"
            order.confirmed_at = datetime.now()
            self.repo.log(
                "confirm",
                f"确认 {order.order_id}，唯一补抽结果为 {order.line_id}",
                **order.result,
            )
            return {
                "ok": True,
                "deduplicated": False,
                "message": "补抽名额已确认",
                "result": order.result,
                "confirm_calls": order.confirm_calls,
            }

    # ---------- 剧本更正 ----------

    def apply_narrative_correction(self) -> dict[str, Any]:
        """插入题设分组更正：B2 独立成组，并新增更晚的 B3 未覆盖组。"""

        with self.repo.lock:
            b2 = self.repo.lines["B2"]
            old_group = b2.group_key
            b2.group_key = "G-B2-SOLO"

            if "B3" not in self.repo.lines:
                b3 = CoverageLine(
                    line_id="B3",
                    source_id="B",
                    occurred_at=datetime(2026, 11, 24, 9, 20, 0),
                    fingerprint="fp-B3-late-confirmed-gap",
                    group_key="G-B3-LATE",
                    raw_text="B source later confirmed uncovered group",
                    confirmed=True,
                )
                self.repo.add_line(b3)

            correction = {
                "correction_id": "CORR-0001",
                "inserted_between": "claim-and-confirmation",
                "changes": [
                    {
                        "line_id": "B2",
                        "from_group_key": old_group,
                        "to_group_key": "G-B2-SOLO",
                    },
                    {
                        "op": "add_confirmed_group",
                        "line_id": "B3",
                        "group_key": "G-B3-LATE",
                        "occurred_at": iso(self.repo.lines["B3"].occurred_at),
                    },
                ],
                "immutable": ["provenance_source_id", "existing_delivery_source"],
            }
            self.repo.corrections.append(correction)
            self.repo.current_revision = "r2-corrected"
            self.repo.log(
                "correction",
                "分组更正到达：B2 独立成组，B 新增更晚的已确认未覆盖组 B3",
                correction_id=correction["correction_id"],
            )
            return correction

    # ---------- 序列化 ----------

    def order_dict(self, order: SupplementOrder) -> dict[str, Any]:
        return {
            "order_id": order.order_id,
            "round_no": order.round_no,
            "quota": order.quota,
            "source_id": order.source_id,
            "group_key": order.group_key,
            "line_id": order.line_id,
            "detail_revision": order.detail_revision,
            "idempotency_key": order.idempotency_key,
            "status": order.status,
            "detail_confirmed": order.detail_confirmed,
            "delivery_confirmed": order.delivery_confirmed,
            "delivery_id": order.delivery_id,
            "result": order.result,
            "confirm_calls": order.confirm_calls,
            "duplicate_confirmations": order.duplicate_confirmations,
            "conflict_with_current": order.conflict_with_current,
            "occupied_at_claim": order.occupied_at_claim,
            "created_at": iso(order.created_at),
            "confirmed_at": iso(order.confirmed_at),
            "recomputed_choice": self.recompute_order_choice(order),
        }

    def state(self) -> dict[str, Any]:
        report = self.build_report()
        return {
            "report": report,
            "orders": [self.order_dict(order) for order in self.repo.orders.values()],
            "deliveries": [
                {
                    "delivery_id": rec.delivery_id,
                    "line_id": rec.line_id,
                    "source_id": rec.source_id,
                    "idempotency_key": rec.idempotency_key,
                    "dispatched_at": iso(rec.dispatched_at),
                    "carrier_status": rec.carrier_status,
                    "verified": rec.verified,
                }
                for rec in self.repo.deliveries.values()
            ],
            "applied_receipt_ids": sorted(self.repo.applied_receipt_ids),
            "corrections": self.repo.corrections,
            "timeline": self.repo.timeline,
        }


# ---------------------------------------------------------------------------
# 题设种子数据与完整剧本
# ---------------------------------------------------------------------------


def seed_narrative_repository() -> CoverageRepository:
    repo = CoverageRepository()
    specs = [
        CoverageLine(
            "A1", "A", datetime(2026, 11, 24, 8, 50, 0),
            "fp-A-dup-url", "G-A", "A first representative",
        ),
        CoverageLine(
            "A2", "A", datetime(2026, 11, 24, 8, 50, 30),
            "fp-A-dup-url", "G-A", "A exact duplicate log",
        ),
        CoverageLine(
            "B1", "B", datetime(2026, 11, 24, 9, 0, 0),
            "fp-B1", "G-B-OLD", "B first delivered representative",
        ),
        CoverageLine(
            "B2", "B", datetime(2026, 11, 24, 9, 1, 0),
            "fp-B2", "G-B-OLD", "B second log initially merged into B1 group",
        ),
        CoverageLine(
            "C1", "C", datetime(2026, 11, 24, 9, 10, 0),
            "fp-C1", "G-C1", "C uncovered candidate",
        ),
    ]
    for line in specs:
        repo.add_line(line)

    # 初始采样比例 0.5：去重后 4 条，阈值 2，按时间取 A1、B1；
    # 归并后实际代表也是 A1、B1，C1 因此未入选。
    for line_id, source_id in [("A1", "A"), ("B1", "B")]:
        key = f"initial-sample-{line_id}"
        repo.deliveries[line_id] = DeliveryRecord(
            delivery_id=f"DLV-{line_id}",
            line_id=line_id,
            source_id=source_id,
            idempotency_key=key,
            dispatched_at=datetime(2026, 11, 24, 9, 15, 0),
        )
        repo.idempotency_index[key] = f"DLV-{line_id}"
        repo.applied_receipt_ids.add(line_id)

    repo.log(
        "seed",
        "载入多来源日志：A=2（含重复），B=2（初态同组），C=1；初始代表 A1/B1 已交付",
    )
    service = SampleCoverageService(repo)
    service.snapshot_detail("r1-initial")
    return repo


def _concurrent_confirm(service: SampleCoverageService, order_id: str, workers: int = 8):
    from concurrent.futures import ThreadPoolExecutor

    barrier_results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(service.confirm_claim, order_id, f"worker-{i}") for i in range(workers)]
        for future in futures:
            barrier_results.append(future.result())

    unique_result_ids = {item["result"]["result_id"] for item in barrier_results}
    unique_lines = {item["result"]["line_id"] for item in barrier_results}
    return {
        "workers": workers,
        "responses": barrier_results,
        "unique_result_ids": sorted(unique_result_ids),
        "unique_lines": sorted(unique_lines),
        "deduplicated_responses": sum(1 for item in barrier_results if item["deduplicated"]),
    }


def run_narrative() -> tuple[SampleCoverageService, dict[str, Any]]:
    """运行题设完整剧本，并返回可继续操作的 service 与摘要。"""

    repo = seed_narrative_repository()
    service = SampleCoverageService(repo)
    output: dict[str, Any] = {"steps": [], "checks": {}}

    initial = service.build_report()
    output["initial_report"] = initial
    output["initial_sample_gate"] = {
        "dedup_count": initial["counts"]["dedup"],
        "sample_ratio": 0.5,
        "threshold": max(1, int(initial["counts"]["dedup"] * 0.5)),
        "time_ordered_dedup_representatives": ["A1", "B1", "B2", "C1"],
        "selected_before_merge": ["A1", "B1"],
        "delivered_merged_representatives": ["A1", "B1"],
        "not_selected": ["C1"],
    }

    # 第一轮：旧明细领取 C1；补抽 sample 实际派出 C1。
    claim1 = service.claim_gap()
    output["round1_claim"] = service.order_dict(claim1)
    sample1 = service.sample_for_order(claim1.order_id)
    output["round1_sample"] = {
        "created": sample1["created"],
        "delivery_id": sample1["delivery"].delivery_id,
        "line_id": sample1["delivery"].line_id,
    }
    output["report_after_dispatch_before_receipt"] = service.build_report()

    # 覆盖对照未取回 C1 回执时重启；新 service 共享同一本持久/内存仓库。
    service.restart()
    reopened = SampleCoverageService(repo)

    # 更正插在领用与最终确认之间。
    correction = reopened.apply_narrative_correction()
    output["correction_between_claim_and_confirmation"] = correction

    # 重开后先核清 C1 的真实交付，再做任何缺口判断/确认。
    verified = reopened.reconcile_deliveries(claim1.order_id)
    output["reconciled_delivery_lines"] = [record.line_id for record in verified]
    reopened.acknowledge_detail(claim1.order_id)

    concurrent1 = _concurrent_confirm(reopened, claim1.order_id)
    output["round1_concurrent_confirmation"] = {
        "workers": concurrent1["workers"],
        "unique_result_ids": concurrent1["unique_result_ids"],
        "unique_lines": concurrent1["unique_lines"],
        "deduplicated_responses": concurrent1["deduplicated_responses"],
    }
    output["round1_order"] = reopened.order_dict(repo.orders[claim1.order_id])
    output["old_detail_recomputed_choice"] = reopened.recompute_order_choice(
        repo.orders[claim1.order_id]
    )

    corrected = reopened.snapshot_detail("r2-corrected-reconciled")
    output["corrected_report"] = corrected

    # 第二轮验证：实际采用更新后的整份对照和新一条额度。
    claim2 = reopened.claim_gap()
    output["round2_refreshed_claim"] = reopened.order_dict(claim2)
    concurrent2 = _concurrent_confirm(reopened, claim2.order_id)
    output["round2_concurrent_confirmation"] = {
        "workers": concurrent2["workers"],
        "unique_result_ids": concurrent2["unique_result_ids"],
        "unique_lines": concurrent2["unique_lines"],
        "deduplicated_responses": concurrent2["deduplicated_responses"],
    }
    output["round2_order"] = reopened.order_dict(repo.orders[claim2.order_id])

    final_state = reopened.state()
    output["final_state"] = final_state
    output["actual_delivery_ledger_lines"] = sorted(repo.deliveries)
    output["actual_supplement_lines"] = [
        order.line_id
        for order in repo.orders.values()
        if order.result and order.line_id not in {"A1", "B1"}
    ]

    # 所有候选轮次都排除已交付 B1。
    candidate_lines_across_revisions: set[str] = set()
    for detail_id in ["r1-initial", "r2-corrected-reconciled"]:
        detail = repo.details[detail_id]
        if detail.get("priority_gap"):
            candidate_lines_across_revisions.add(detail["priority_gap"]["line_id"])
        for source in detail["sources"]:
            for group in source["merged_groups"]:
                if group.get("candidate_line_id"):
                    candidate_lines_across_revisions.add(group["candidate_line_id"])
    output["candidate_lines_across_revisions"] = sorted(candidate_lines_across_revisions)

    checks = {
        "initial_counts": initial["counts"],
        "initial_priority": initial["priority_gap"],
        "initial_duplicate_groups": [
            {"fingerprint": g["fingerprint"], "member_ids": g["member_ids"]}
            for g in initial["duplicate_groups"]
        ],
        "corrected_counts": corrected["counts"],
        "corrected_priority": corrected["priority_gap"],
        "round1_line": claim1.line_id,
        "round2_line": claim2.line_id,
        "delivery_lines": sorted(repo.deliveries),
        "b1_excluded_from_all_candidates": "B1" not in candidate_lines_across_revisions,
        "round1_single_result": len(concurrent1["unique_result_ids"]) == 1,
        "round2_single_result": len(concurrent2["unique_result_ids"]) == 1,
    }
    output["checks"] = checks

    # 可执行断言：剧本本身必须满足题设。
    assert initial["counts"] == {
        "raw": 5,
        "dedup": 4,
        "duplicate_groups": 1,
        "merged_groups": 3,
        "covered_groups": 2,
        "uncovered_groups": 1,
    }
    assert initial["priority_gap"]["source_id"] == "C"
    assert initial["priority_gap"]["line_id"] == "C1"
    assert claim1.line_id == "C1"
    assert len(concurrent1["unique_result_ids"]) == 1
    assert sorted(repo.deliveries) == ["A1", "B1", "B2", "C1"]
    assert corrected["counts"] == {
        "raw": 6,
        "dedup": 5,
        "duplicate_groups": 1,
        "merged_groups": 5,
        "covered_groups": 3,
        "uncovered_groups": 2,
    }
    assert corrected["priority_gap"]["source_id"] == "B"
    assert corrected["priority_gap"]["line_id"] == "B2"
    assert claim2.line_id == "B2"
    assert "B1" not in candidate_lines_across_revisions

    return reopened, output
