from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, and_
from datetime import datetime, timedelta
from typing import Optional

from ..database import get_db
from .. import models, schemas, auth

router = APIRouter(prefix="/warnings", tags=["预警中心"])


def _get_pending_inspect_since(db: Session, batch_id: int) -> Optional[datetime]:
    latest_entry = db.query(models.ProcessRecord).filter(
        models.ProcessRecord.batch_id == batch_id,
        models.ProcessRecord.type.in_(["trim", "rework"])
    ).order_by(models.ProcessRecord.record_time.desc()).first()
    if latest_entry:
        return latest_entry.record_time
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    return batch.created_at if batch else None


def get_all_warnings_internal(
    db: Session,
    style_id: Optional[int] = None,
    status: Optional[str] = None,
    technician_id: Optional[int] = None,
    start_date=None,
    end_date=None,
    keyword: Optional[str] = None
) -> list:
    warnings = []

    warnings.extend(check_bubble_concentration(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_overdue_inspection(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_rework_no_conclusion(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_pass_rate_drop(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_unreviewed_delivery(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_rework_overdue(db, style_id, status, technician_id, start_date, end_date, keyword))
    warnings.extend(check_multiple_reworks(db, style_id, status, technician_id, start_date, end_date, keyword))

    return warnings


def _batch_filter(query, style_id, status, technician_id, start_date, end_date, keyword):
    if style_id:
        query = query.filter(models.Batch.style_id == style_id)
    if status:
        query = query.filter(models.Batch.status == status)
    if technician_id:
        query = query.filter(models.Batch.technician_id == technician_id)
    if start_date:
        query = query.filter(models.Batch.planned_start_date >= start_date)
    if end_date:
        query = query.filter(models.Batch.planned_end_date <= end_date)
    if keyword:
        query = query.filter(models.Batch.code.contains(keyword))
    return query


def check_unreviewed_delivery(db: Session, style_id=None, status=None, technician_id=None,
                              start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    threshold_days = 3
    now = datetime.now().date()

    query = db.query(models.Batch).filter(
        models.Batch.status == "deliverable",
        models.Batch.review_status == "pending_review"
    )
    query = _batch_filter(query, style_id, status, technician_id, start_date, end_date, keyword)
    pending_batches = query.all()

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


def check_bubble_concentration(db: Session, style_id=None, status=None, technician_id=None,
                               start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    threshold = 5

    query = db.query(
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
    )
    query = _batch_filter(query, style_id, status, technician_id, start_date, end_date, keyword)
    results = query.group_by(
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


def check_overdue_inspection(db: Session, style_id=None, status=None, technician_id=None,
                             start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    now = datetime.now().date()

    query = db.query(models.Batch).filter(
        models.Batch.status == "pending_inspect"
    )
    query = _batch_filter(query, style_id, status, technician_id, start_date, end_date, keyword)
    pending_batches = query.all()

    for batch in pending_batches:
        cycle = db.query(models.InspectionCycle).filter(
            models.InspectionCycle.style_id == batch.style_id
        ).first()

        cycle_days = cycle.cycle_days if cycle else 7

        since_time = _get_pending_inspect_since(db, batch.id)
        if not since_time:
            continue
        since_date = since_time.date() if isinstance(since_time, datetime) else since_time
        days_since = (now - since_date).days

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


def check_rework_no_conclusion(db: Session, style_id=None, status=None, technician_id=None,
                               start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    threshold_days = 3
    now = datetime.now()

    batch_query = db.query(models.Batch).filter(
        models.Batch.status == "reworking"
    )
    batch_query = _batch_filter(batch_query, style_id, status, technician_id, start_date, end_date, keyword)
    rework_batch_ids = [b.id for b in batch_query.all()]

    if not rework_batch_ids:
        return warnings

    active_reworks = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.batch_id.in_(rework_batch_ids),
        models.ReworkRecord.status.in_(["pending", "processing"])
    ).all()

    for rework in active_reworks:
        base_time = rework.updated_at or rework.created_at
        if rework.status == "processing" and rework.updated_at:
            base_time = rework.updated_at
        else:
            base_time = rework.created_at

        days_since = (now - base_time).days
        if days_since > threshold_days:
            warnings.append(schemas.WarningItem(
                type="rework_no_conclusion",
                level="high" if days_since >= 7 else "medium",
                title=f"批次【{rework.batch.code}】返工无结论",
                content=f"第{rework.rework_no}次返工已持续 {days_since} 天未提交复检，超过阈值 {threshold_days} 天，责任人：{rework.responsible.name}",
                related_id=rework.batch_id,
                related_type="batch",
                created_at=datetime.now()
            ).model_dump())

    return warnings


def check_pass_rate_drop(db: Session, style_id=None, status=None, technician_id=None,
                         start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    threshold_drop = 0.2
    min_batches = 5

    style_query = db.query(models.Style)
    if style_id:
        style_query = style_query.filter(models.Style.id == style_id)
    styles = style_query.all()

    for style in styles:
        batch_query = db.query(models.Batch).filter(
            models.Batch.style_id == style.id
        )
        batch_query = _batch_filter(batch_query, None, status, technician_id, start_date, end_date, keyword)
        batches = batch_query.all()

        latest_results = []
        for batch in batches:
            latest_ir = db.query(models.InspectionRecord).filter(
                models.InspectionRecord.batch_id == batch.id
            ).order_by(models.InspectionRecord.inspect_time.desc()).first()
            if latest_ir:
                latest_results.append((batch, latest_ir))

        latest_results.sort(key=lambda x: x[1].inspect_time, reverse=True)

        if len(latest_results) < min_batches * 2:
            continue

        recent = latest_results[:min_batches]
        earlier = latest_results[min_batches:min_batches * 2]

        recent_pass = sum(1 for _, ir in recent if ir.is_pass)
        earlier_pass = sum(1 for _, ir in earlier if ir.is_pass)

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
def get_all_warnings(
    style_id: Optional[int] = None,
    status: Optional[str] = None,
    technician_id: Optional[int] = None,
    start_date=None,
    end_date=None,
    keyword: Optional[str] = None,
    db: Session = Depends(get_db)
):
    warnings = get_all_warnings_internal(db, style_id, status, technician_id, start_date, end_date, keyword)
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


def check_rework_overdue(db: Session, style_id=None, status=None, technician_id=None,
                         start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    now = datetime.now()

    batch_query = db.query(models.Batch)
    batch_query = _batch_filter(batch_query, style_id, status, technician_id, start_date, end_date, keyword)
    filtered_batch_ids = [b.id for b in batch_query.all()]

    if not filtered_batch_ids:
        return warnings

    overdue_reworks = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.batch_id.in_(filtered_batch_ids),
        models.ReworkRecord.status.in_(["pending", "processing"]),
        models.ReworkRecord.expected_finish_time.isnot(None),
        models.ReworkRecord.expected_finish_time < now
    ).all()

    for rework in overdue_reworks:
        overdue_days = (now - rework.expected_finish_time).days
        if overdue_days > 0:
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


def check_multiple_reworks(db: Session, style_id=None, status=None, technician_id=None,
                           start_date=None, end_date=None, keyword=None) -> list:
    warnings = []
    threshold = 2

    batch_query = db.query(models.Batch)
    batch_query = _batch_filter(batch_query, style_id, status, technician_id, start_date, end_date, keyword)
    filtered_batch_ids = [b.id for b in batch_query.all()]

    if not filtered_batch_ids:
        return warnings

    results = db.query(
        models.ReworkRecord.batch_id,
        models.Batch.code.label("batch_code"),
        func.count(models.ReworkRecord.id).label("rework_count")
    ).join(
        models.Batch, models.ReworkRecord.batch_id == models.Batch.id
    ).filter(
        models.ReworkRecord.batch_id.in_(filtered_batch_ids),
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
