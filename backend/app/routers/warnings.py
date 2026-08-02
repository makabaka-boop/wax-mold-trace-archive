from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from datetime import datetime, timedelta

from ..database import get_db
from .. import models, schemas, auth

router = APIRouter(prefix="/warnings", tags=["预警中心"])


def get_pending_inspect_since(db: Session, batch) -> "datetime.date":
    """批次进入待质检的时间：取最近一次 trim/rework 工艺记录时间；
    没有则回退到实际开工时间，再回退到创建时间。用于统一质检超期口径。"""
    last_entry = db.query(models.ProcessRecord).filter(
        models.ProcessRecord.batch_id == batch.id,
        models.ProcessRecord.type.in_(["trim", "rework"])
    ).order_by(models.ProcessRecord.record_time.desc()).first()
    if last_entry:
        return last_entry.record_time.date()
    if batch.actual_start_date:
        return batch.actual_start_date.date()
    return batch.created_at.date()


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
        days_since = (now - get_pending_inspect_since(db, batch)).days

        if days_since > cycle_days:
            overdue_days = days_since - cycle_days
            warnings.append(schemas.WarningItem(
                type="overdue_inspection",
                level="high" if overdue_days >= 3 else "medium",
                title=f"批次【{batch.code}】质检超期",
                content=f"已超期 {overdue_days} 天，质检周期为 {cycle_days} 天",
                related_id=batch.id,
                related_type="batch",
                created_at=datetime.now()
            ).model_dump())

    return warnings


def check_rework_no_conclusion(db: Session) -> list:
    warnings = []
    threshold_days = 3
    now = datetime.now().date()

    # 以返工闭环记录为准：等待复检（waiting_inspection）但迟迟没有质检结论闭环的返工。
    # 不再依赖 ProcessRecord.type=="rework"，避免与闭环状态口径打架。
    waiting_reworks = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status == "waiting_inspection"
    ).all()

    for rework in waiting_reworks:
        base_time = rework.actual_finish_time or rework.updated_at or rework.created_at
        days_since = (now - base_time.date()).days
        if days_since > threshold_days:
            warnings.append(schemas.WarningItem(
                type="rework_no_conclusion",
                level="high" if days_since >= 7 else "medium",
                title=f"批次【{rework.batch.code}】返工后无结论",
                content=f"第 {rework.rework_no} 次返工已提交复检 {days_since} 天仍无质检结论，超过阈值 {threshold_days} 天",
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
        # 每个批次取"最新一次"质检结论作为该批次的通过判定，
        # 再按质检时间倒序对批次取样，避免用任意历史通过记录污染通过率。
        batches = db.query(models.Batch).filter(
            models.Batch.style_id == style.id
        ).all()

        batch_latest = []
        for b in batches:
            if not b.inspection_records:
                continue
            latest = max(b.inspection_records, key=lambda ir: ir.inspect_time)
            batch_latest.append((latest.inspect_time, bool(latest.is_pass)))

        if len(batch_latest) < min_batches * 2:
            continue

        batch_latest.sort(key=lambda x: x[0], reverse=True)

        recent = batch_latest[:min_batches]
        earlier = batch_latest[min_batches:min_batches * 2]

        recent_pass = sum(1 for _, passed in recent if passed)
        earlier_pass = sum(1 for _, passed in earlier if passed)

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
    warnings = []
    now = datetime.now()

    from .reworks import rework_overdue_filter
    overdue_reworks = db.query(models.ReworkRecord).filter(
        rework_overdue_filter(now)
    ).all()

    for rework in overdue_reworks:
        # 与仪表盘/返工统计口径一致：只要已过预计完成时间即计入，超期天数按天向下取整（至少 0 天）
        overdue_days = max(0, (now - rework.expected_finish_time).days)
        warnings.append(schemas.WarningItem(
            type="rework_overdue",
            level="high" if overdue_days >= 3 else "medium",
            title=f"批次【{rework.batch.code}】返工超期",
            content=f"第 {rework.rework_no} 次返工已超期 {overdue_days} 天，责任人：{rework.responsible.name}",
            related_id=rework.batch_id,
            related_type="batch",
            created_at=datetime.now()
        ).model_dump())

    return warnings


def check_multiple_reworks(db: Session) -> list:
    warnings = []
    threshold = 2

    # 返工次数排除已取消记录，与返工闭环列表口径保持一致
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
