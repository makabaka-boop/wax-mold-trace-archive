from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from datetime import datetime, timedelta

from ..database import get_db
from .. import models, schemas, auth

router = APIRouter(prefix="/warnings", tags=["预警中心"])


# ============ 统一统计口径（仪表盘 / 预警中心 / 返工闭环共用，保证同一条件下数量一致） ============

def get_pending_inspect_since(batch) -> datetime:
    """批次最近一次进入待质检(pending_inspect)状态的时间。

    进入路径：修边完成、返工完成工艺记录、返工闭环提交复检；
    均无记录时回退为批次创建时间。
    """
    candidates = [
        pr.record_time for pr in batch.process_records
        if pr.type in ("trim", "rework") and pr.record_time is not None
    ]
    candidates += [
        rw.actual_finish_time for rw in batch.rework_records
        if rw.actual_finish_time is not None
    ]
    return max(candidates) if candidates else batch.created_at


def count_open_reworks(db: Session, batch_ids=None) -> int:
    """未闭环返工（pending+processing）数量，与返工闭环列表同口径"""
    query = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status.in_(["pending", "processing"])
    )
    if batch_ids is not None:
        query = query.filter(models.ReworkRecord.batch_id.in_(batch_ids))
    return query.count()


def count_overdue_reworks(db: Session, batch_ids=None) -> int:
    """超期返工数量：pending/processing 且预计完成时间早于当前时刻（与返工超期预警同口径）"""
    query = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status.in_(["pending", "processing"]),
        models.ReworkRecord.expected_finish_time.isnot(None),
        models.ReworkRecord.expected_finish_time < datetime.now()
    )
    if batch_ids is not None:
        query = query.filter(models.ReworkRecord.batch_id.in_(batch_ids))
    return query.count()


def count_waiting_rework_inspections(db: Session, batch_ids=None) -> int:
    """待复检返工（waiting_inspection）数量"""
    query = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status == "waiting_inspection"
    )
    if batch_ids is not None:
        query = query.filter(models.ReworkRecord.batch_id.in_(batch_ids))
    return query.count()


def get_all_warnings_internal(db: Session) -> list:
    warnings = []

    bubble_warnings = check_bubble_concentration(db)
    overdue_warnings = check_overdue_inspection(db)
    rework_warnings = check_rework_no_conclusion(db)
    pass_rate_warnings = check_pass_rate_drop(db)
    unreviewed_warnings = check_unreviewed_delivery(db)
    rework_overdue_warnings = check_rework_overdue(db)
    multiple_rework_warnings = check_multiple_reworks(db)

    warnings.extend(bubble_warnings)
    warnings.extend(overdue_warnings)
    warnings.extend(rework_warnings)
    warnings.extend(pass_rate_warnings)
    warnings.extend(unreviewed_warnings)
    warnings.extend(rework_overdue_warnings)
    warnings.extend(multiple_rework_warnings)

    return warnings


def check_unreviewed_delivery(db: Session) -> list:
    warnings = []
    threshold_days = 3
    now = datetime.now().date()

    pending_batches = db.query(models.Batch).filter(
        models.Batch.status == "deliverable",
        models.Batch.review_status == "pending_review"
    ).all()

    for batch in pending_batches:
        base_date = batch.actual_end_date.date() if batch.actual_end_date else batch.created_at.date()
        days_since = (now - base_date).days

        if days_since > threshold_days:
            overdue_days = days_since - threshold_days
            warnings.append(schemas.WarningItem(
                type="unreviewed_delivery",
                level="high" if overdue_days >= 7 else "medium",
                title=f"批次【{batch.code}】待交付复核超期",
                content=f"已超期 {overdue_days} 天未完成交付复核，超过阈值 {threshold_days} 天",
                related_id=batch.id,
                related_type="batch",
                created_at=datetime.now()
            ).model_dump())

    return warnings


def check_bubble_concentration(db: Session) -> list:
    warnings = []
    threshold = 5

    results = db.query(
        models.ProcessRecord.batch_id,
        models.Batch.style_id,
        models.Style.name.label("style_name"),
        func.sum(models.ProcessRecord.bubble_count).label("total_bubbles")
    ).join(
        models.Batch, models.ProcessRecord.batch_id == models.Batch.id
    ).join(
        models.Style, models.Batch.style_id == models.Style.id
    ).filter(
        models.ProcessRecord.type == "bubble",
        models.ProcessRecord.bubble_count.isnot(None),
        models.Batch.status != "delivered"
    ).group_by(
        models.ProcessRecord.batch_id, models.Batch.style_id, models.Style.name
    ).having(
        func.sum(models.ProcessRecord.bubble_count) >= threshold
    ).all()

    for r in results:
        warnings.append(schemas.WarningItem(
            type="bubble_concentration",
            level="high" if r.total_bubbles >= 10 else "medium",
            title=f"款式【{r.style_name}】气泡集中",
            content=f"批次气泡总数达到 {r.total_bubbles} 个，超过阈值 {threshold} 个",
            related_id=r.batch_id,
            related_type="batch",
            created_at=datetime.now()
        ).model_dump())

    return warnings


def check_overdue_inspection(db: Session) -> list:
    warnings = []
    now = datetime.now().date()

    pending_batches = db.query(models.Batch).filter(
        models.Batch.status == "pending_inspect"
    ).all()

    for batch in pending_batches:
        cycle = db.query(models.InspectionCycle).filter(
            models.InspectionCycle.style_id == batch.style_id
        ).first()

        cycle_days = cycle.cycle_days if cycle else 7
        # 以批次最近一次进入待质检状态的时间为基准，而非批次创建时间
        since = get_pending_inspect_since(batch).date()
        days_since = (now - since).days

        if days_since > cycle_days:
            overdue_days = days_since - cycle_days
            warnings.append(schemas.WarningItem(
                type="overdue_inspection",
                level="high" if overdue_days >= 3 else "medium",
                title=f"批次【{batch.code}】质检超期",
                content=f"进入待质检已超期 {overdue_days} 天，质检周期为 {cycle_days} 天",
                related_id=batch.id,
                related_type="batch",
                created_at=datetime.now()
            ).model_dump())

    return warnings


def check_rework_no_conclusion(db: Session) -> list:
    """返工处理中但迟迟未提交复检，以返工闭环单状态为准（不看工艺记录）"""
    warnings = []
    threshold_days = 3
    now = datetime.now()

    processing_reworks = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status == "processing"
    ).all()

    for rework in processing_reworks:
        base_time = rework.updated_at or rework.created_at
        days_since = (now - base_time).days
        if days_since > threshold_days:
            warnings.append(schemas.WarningItem(
                type="rework_no_conclusion",
                level="high" if days_since >= 7 else "medium",
                title=f"批次【{rework.batch.code}】返工处理无结论",
                content=f"第 {rework.rework_no} 次返工已处理 {days_since} 天未提交复检，超过阈值 {threshold_days} 天",
                related_id=rework.batch_id,
                related_type="batch",
                created_at=datetime.now()
            ).model_dump())

    return warnings


def check_pass_rate_drop(db: Session) -> list:
    warnings = []
    threshold_drop = 0.2
    min_batches = 5

    styles = db.query(models.Style).all()

    for style in styles:
        # 每个批次只取其最新一条质检记录，按质检时间倒序取样
        latest_records = []
        batches = db.query(models.Batch).filter(models.Batch.style_id == style.id).all()
        for batch in batches:
            if batch.inspection_records:
                latest = max(batch.inspection_records, key=lambda ir: (ir.inspect_time, ir.id))
                latest_records.append(latest)

        if len(latest_records) < min_batches * 2:
            continue

        latest_records.sort(key=lambda ir: (ir.inspect_time, ir.id), reverse=True)
        recent = latest_records[:min_batches]
        earlier = latest_records[min_batches:min_batches * 2]

        recent_pass = sum(1 for ir in recent if ir.is_pass)
        earlier_pass = sum(1 for ir in earlier if ir.is_pass)

        recent_rate = recent_pass / len(recent)
        earlier_rate = earlier_pass / len(earlier)

        if earlier_rate - recent_rate >= threshold_drop and earlier_rate >= 0.6:
            drop_percent = round((earlier_rate - recent_rate) * 100, 1)
            warnings.append(schemas.WarningItem(
                type="pass_rate_drop",
                level="high",
                title=f"款式【{style.name}】通过率下降",
                content=f"近期通过率 {round(recent_rate*100, 1)}%，较前期下降 {drop_percent}%",
                related_id=style.id,
                related_type="style",
                created_at=datetime.now()
            ).model_dump())

    return warnings


@router.get("", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_all_warnings(db: Session = Depends(get_db)):
    warnings = get_all_warnings_internal(db)
    warnings.sort(key=lambda x: {"high": 0, "medium": 1, "low": 2}[x.get("level", "low")])
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/bubble-concentration", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_bubble_concentration_warnings(db: Session = Depends(get_db)):
    warnings = check_bubble_concentration(db)
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/overdue-inspection", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_overdue_inspection_warnings(db: Session = Depends(get_db)):
    warnings = check_overdue_inspection(db)
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/rework-no-conclusion", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_rework_no_conclusion_warnings(db: Session = Depends(get_db)):
    warnings = check_rework_no_conclusion(db)
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/pass-rate-drop", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_pass_rate_drop_warnings(db: Session = Depends(get_db)):
    warnings = check_pass_rate_drop(db)
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/unreviewed-delivery", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_unreviewed_delivery_warnings(db: Session = Depends(get_db)):
    warnings = check_unreviewed_delivery(db)
    return schemas.ApiResponse(data={"items": warnings})


def check_rework_overdue(db: Session) -> list:
    """返工超期：与仪表盘 overdue_rework、返工闭环统计完全同口径（expected_finish_time < 当前时刻）"""
    warnings = []
    now = datetime.now()

    overdue_reworks = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status.in_(["pending", "processing"]),
        models.ReworkRecord.expected_finish_time.isnot(None),
        models.ReworkRecord.expected_finish_time < now
    ).all()

    for rework in overdue_reworks:
        overdue_days = (now - rework.expected_finish_time).days
        overdue_text = f"{overdue_days} 天" if overdue_days > 0 else "不足 1 天"
        warnings.append(schemas.WarningItem(
            type="rework_overdue",
            level="high" if overdue_days >= 3 else "medium",
            title=f"批次【{rework.batch.code}】返工超期",
            content=f"第 {rework.rework_no} 次返工已超期 {overdue_text}，责任人：{rework.responsible.name}",
            related_id=rework.batch_id,
            related_type="batch",
            created_at=datetime.now()
        ).model_dump())

    return warnings


def check_multiple_reworks(db: Session) -> list:
    """多次返工：只统计有效返工（排除已取消记录）"""
    warnings = []
    threshold = 2

    results = db.query(
        models.ReworkRecord.batch_id,
        models.Batch.code.label("batch_code"),
        func.count(models.ReworkRecord.id).label("rework_count")
    ).join(
        models.Batch, models.ReworkRecord.batch_id == models.Batch.id
    ).filter(
        models.ReworkRecord.status != "cancelled"
    ).group_by(
        models.ReworkRecord.batch_id, models.Batch.code
    ).having(
        func.count(models.ReworkRecord.id) >= threshold
    ).all()

    for r in results:
        warnings.append(schemas.WarningItem(
            type="multiple_reworks",
            level="high" if r.rework_count >= 3 else "medium",
            title=f"批次【{r.batch_code}】多次返工",
            content=f"该批次已返工 {r.rework_count} 次，超过阈值 {threshold} 次",
            related_id=r.batch_id,
            related_type="batch",
            created_at=datetime.now()
        ).model_dump())

    return warnings


@router.get("/rework-overdue", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_rework_overdue_warnings(db: Session = Depends(get_db)):
    warnings = check_rework_overdue(db)
    return schemas.ApiResponse(data={"items": warnings})


@router.get("/multiple-reworks", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_multiple_reworks_warnings(db: Session = Depends(get_db)):
    warnings = check_multiple_reworks(db)
    return schemas.ApiResponse(data={"items": warnings})
