"""样本覆盖对照、缺口补抽、重启核对和并发确认接口。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.sample_coverage import (
    SampleCoverageService,
    load_repository,
    run_narrative,
    save_repository,
    seed_narrative_repository,
)

router = APIRouter(prefix="/api/sample-coverage", tags=["样本覆盖对照"])

STATE_PATH = Path(
    os.getenv("SAMPLE_COVERAGE_STATE_FILE", "/tmp/shiyin-sample-coverage-state.json")
)


def load_initial_service() -> SampleCoverageService:
    """真实进程重启时从原子 JSON 状态恢复交付台账和未确认补抽单。"""

    if STATE_PATH.exists():
        try:
            return SampleCoverageService(load_repository(STATE_PATH))
        except (OSError, ValueError, KeyError):
            # 状态文件损坏不应挡住工作台；退回种子场景，但保留损坏文件供排查。
            backup = STATE_PATH.with_suffix(".corrupt.json")
            try:
                STATE_PATH.replace(backup)
            except OSError:
                pass
    return SampleCoverageService(seed_narrative_repository())


_service = load_initial_service()


def persist() -> None:
    save_repository(_service.repo, STATE_PATH)


@router.get("")
def coverage_state() -> dict[str, Any]:
    """返回可展开覆盖明细、补抽单、交付台账和操作时间线。"""

    return _service.state()


@router.post("/deduplicate")
def deduplicate_current_logs() -> dict[str, Any]:
    """deduplicate 入口：明确返回重复组及每组实际代表。"""

    groups = _service.deduplicate()
    return {
        "ok": True,
        "duplicate_group_count": sum(1 for group in groups if group["duplicate_count"]),
        "groups": groups,
    }


class SamplePayload(BaseModel):
    order_id: str | None = Field(default=None)


@router.post("/sample")
def sample_gap(payload: SamplePayload | None = None) -> dict[str, Any]:
    """sample 串联入口：未给单号时先按覆盖明细领取优先缺口，再实际取样。"""

    try:
        if payload and payload.order_id:
            order_id = payload.order_id
        else:
            order = _service.claim_gap()
            order_id = order.order_id
        result = _service.sample(order_id)
        persist()
        record = result["delivery"]
        return {
            "ok": True,
            "created": result["created"],
            "message": "sample 已核对缺口并实际取样" if result["created"] else "原行已派出，未重复发送",
            "delivery": {
                "delivery_id": record.delivery_id,
                "line_id": record.line_id,
                "source_id": record.source_id,
                "idempotency_key": record.idempotency_key,
            },
            "order": _service.order_dict(result["order"]),
        }
    except (RuntimeError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/reset")
def reset_scenario() -> dict[str, Any]:
    """恢复到题设初始状态。"""

    global _service
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    _service = SampleCoverageService(seed_narrative_repository())
    persist()
    return {"ok": True, "message": "样本覆盖场景已重置", "state": _service.state()}


@router.post("/scenarios/run")
def run_full_scenario() -> dict[str, Any]:
    """运行去重、覆盖统计、重启、更正插入和两轮并发确认剧本。"""

    global _service
    service, output = run_narrative()
    _service = service
    persist()
    return {"ok": True, "message": "完整覆盖补抽剧本已运行", "output": output, "state": _service.state()}


@router.post("/claims")
def claim_gap() -> dict[str, Any]:
    try:
        order = _service.claim_gap()
        persist()
        return {"ok": True, "message": "缺口已由优先来源领用", "order": _service.order_dict(order)}
    except (RuntimeError, KeyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/claims/{order_id}/sample")
def sample_claim(order_id: str) -> dict[str, Any]:
    try:
        result = _service.sample_for_order(order_id)
        persist()
        record = result["delivery"]
        return {
            "ok": True,
            "created": result["created"],
            "message": "补抽入口已核对缺口并实际取样" if result["created"] else "原行已在交付台账，未重复派出",
            "delivery": {
                "delivery_id": record.delivery_id,
                "line_id": record.line_id,
                "source_id": record.source_id,
                "idempotency_key": record.idempotency_key,
                "carrier_status": record.carrier_status,
            },
            "order": _service.order_dict(result["order"]),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"补抽单 {order_id} 不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/claims/{order_id}/reconcile")
def reconcile_claim(order_id: str) -> dict[str, Any]:
    try:
        records = _service.reconcile_deliveries(order_id)
        persist()
        return {
            "ok": True,
            "message": "已核清采样交付并写回覆盖回执",
            "verified_line_ids": [record.line_id for record in records],
            "state": _service.state(),
        }
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"补抽单 {order_id} 不存在") from exc


@router.post("/claims/{order_id}/detail-ack")
def ack_detail(order_id: str) -> dict[str, Any]:
    try:
        order = _service.acknowledge_detail(order_id)
        persist()
        return {"ok": True, "message": "已按实际采用明细确认领用范围", "order": _service.order_dict(order)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"补抽单 {order_id} 不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class ConfirmPayload(BaseModel):
    actor: str = Field(default="duty")


@router.post("/claims/{order_id}/confirm")
def confirm_claim(order_id: str, payload: ConfirmPayload | None = None) -> dict[str, Any]:
    try:
        actor = payload.actor if payload else "duty"
        result = _service.confirm_claim(order_id, actor=actor)
        persist()
        return result
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"补抽单 {order_id} 不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/restart")
def restart_service() -> dict[str, Any]:
    _service.restart()
    persist()
    return {"ok": True, "message": "已模拟重开：交付台账保留，待核对状态已重置", "state": _service.state()}


@router.post("/correction")
def apply_correction() -> dict[str, Any]:
    correction = _service.apply_narrative_correction()
    persist()
    return {"ok": True, "message": "分组更正已写入新明细版本", "correction": correction, "state": _service.state()}
