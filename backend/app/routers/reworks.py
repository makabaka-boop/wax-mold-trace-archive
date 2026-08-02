from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import Optional
from datetime import datetime

from ..database import get_db
from .. import models, schemas, auth

router = APIRouter(prefix="/reworks", tags=["返工闭环"])

REWORK_STATUS_MAP = {
    "pending": "待处理",
    "processing": "处理中",
    "waiting_inspection": "待复检",
    "completed": "已完成",
    "cancelled": "已取消"
}

REWORK_STATUS_COLOR_MAP = {
    "pending": "#f59e0b",
    "processing": "#3b82f6",
    "waiting_inspection": "#8b5cf6",
    "completed": "#10b981",
    "cancelled": "#6b7280"
}

ACTIVE_STATUSES = ("pending", "processing", "waiting_inspection")
TERMINAL_STATUSES = ("completed", "cancelled")

ALLOWED_TRANSITIONS = {
    "pending": ("processing", "waiting_inspection", "cancelled"),
    "processing": ("waiting_inspection", "cancelled"),
    "waiting_inspection": ("completed", "cancelled"),
    "completed": (),
    "cancelled": (),
}


def _get_active_rework(db: Session, batch_id: int) -> Optional[models.ReworkRecord]:
    return db.query(models.ReworkRecord).filter(
        models.ReworkRecord.batch_id == batch_id,
        models.ReworkRecord.status.in_(ACTIVE_STATUSES)
    ).order_by(models.ReworkRecord.created_at.desc()).first()


def _ensure_transition(current: str, target: str):
    if target not in ALLOWED_TRANSITIONS.get(current, ()):
        raise HTTPException(
            status_code=400,
            detail=f"返工记录当前状态为「{REWORK_STATUS_MAP.get(current, current)}」，不允许变更为「{REWORK_STATUS_MAP.get(target, target)}」"
        )


def _serialize_rework(record: models.ReworkRecord) -> dict:
    data = schemas.ReworkRecordWithDetails.model_validate(record).model_dump()
    data["initiator"] = schemas.User.model_validate(record.initiator).model_dump()
    data["responsible"] = schemas.User.model_validate(record.responsible).model_dump()
    data["batch_code"] = record.batch.code
    data["style_name"] = record.batch.style.name
    data["status_name"] = REWORK_STATUS_MAP.get(record.status, record.status)
    data["status_color"] = REWORK_STATUS_COLOR_MAP.get(record.status, "#6b7280")
    return data


@router.get("", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_rework_records(
    batch_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    responsible_id: Optional[int] = Query(None),
    keyword: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(models.ReworkRecord)

    if batch_id:
        query = query.filter(models.ReworkRecord.batch_id == batch_id)
    if status:
        if status not in REWORK_STATUS_MAP:
            raise HTTPException(status_code=400, detail="无效的返工状态")
        query = query.filter(models.ReworkRecord.status == status)
    if responsible_id:
        query = query.filter(models.ReworkRecord.responsible_id == responsible_id)
    if keyword:
        query = query.join(models.Batch).filter(models.Batch.code.contains(keyword))

    records = query.order_by(models.ReworkRecord.created_at.desc()).all()

    result = [_serialize_rework(r) for r in records]
    return schemas.ApiResponse(data={"items": result})


@router.get("/stats", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_rework_stats(db: Session = Depends(get_db)):
    now = datetime.now()

    pending_count = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status.in_(["pending", "processing"])
    ).count()

    overdue_count = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status.in_(["pending", "processing"]),
        models.ReworkRecord.expected_finish_time.isnot(None),
        models.ReworkRecord.expected_finish_time < now
    ).count()

    waiting_inspection_count = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.status == "waiting_inspection"
    ).count()

    total_count = db.query(models.ReworkRecord).count()

    stats = schemas.ReworkStats(
        pending_rework=pending_count,
        overdue_rework=overdue_count,
        waiting_inspection=waiting_inspection_count,
        total_rework=total_count
    )

    return schemas.ApiResponse(data=stats.model_dump())


@router.get("/{rework_id}", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_all)])
def get_rework_detail(rework_id: int, db: Session = Depends(get_db)):
    record = db.query(models.ReworkRecord).filter(models.ReworkRecord.id == rework_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="返工记录不存在")

    return schemas.ApiResponse(data=_serialize_rework(record))


@router.post("", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_technician)])
def create_rework(
    rework_in: schemas.ReworkRecordCreate,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    batch = db.query(models.Batch).filter(models.Batch.id == rework_in.batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    if batch.status in ("deliverable", "delivered"):
        raise HTTPException(status_code=400, detail="批次已进入可交付/已交付状态，不能发起返工")

    responsible = db.query(models.User).filter(models.User.id == rework_in.responsible_id).first()
    if not responsible:
        raise HTTPException(status_code=400, detail="所选责任人不存在")
    if responsible.role != "technician":
        raise HTTPException(status_code=400, detail="责任人必须为工艺员")

    active = _get_active_rework(db, rework_in.batch_id)
    if active:
        raise HTTPException(
            status_code=400,
            detail=f"该批次已有进行中的返工单（第{active.rework_no}次，状态：{REWORK_STATUS_MAP.get(active.status, active.status)}），请先完成或取消后再发起新的返工"
        )

    last_rework = db.query(models.ReworkRecord).filter(
        models.ReworkRecord.batch_id == rework_in.batch_id
    ).order_by(models.ReworkRecord.rework_no.desc()).first()
    rework_no = (last_rework.rework_no + 1) if last_rework else 1

    if not rework_in.rework_reason or not rework_in.rework_reason.strip():
        raise HTTPException(status_code=400, detail="返工原因不能为空")

    rework = models.ReworkRecord(
        **rework_in.model_dump(),
        rework_no=rework_no,
        initiator_id=current_user.id,
        status="pending"
    )
    db.add(rework)

    if batch.status != "reworking":
        batch.status = "reworking"

    db.commit()
    db.refresh(rework)

    return schemas.ApiResponse(
        message=f"第{rework_no}次返工已发起，责任人为{responsible.name}",
        data=schemas.ReworkRecord.model_validate(rework).model_dump()
    )


@router.put("/{rework_id}/start", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_technician)])
def start_rework(
    rework_id: int,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    rework = db.query(models.ReworkRecord).filter(models.ReworkRecord.id == rework_id).first()
    if not rework:
        raise HTTPException(status_code=404, detail="返工记录不存在")

    _ensure_transition(rework.status, "processing")

    if current_user.role != "admin" and rework.responsible_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有该返工单的责任人才能开始处理")

    rework.status = "processing"
    db.commit()

    return schemas.ApiResponse(message="已开始返工处理")


@router.put("/{rework_id}/submit-inspection", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_technician)])
def submit_rework_for_inspection(
    rework_id: int,
    rework_in: schemas.ReworkRecordComplete,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    rework = db.query(models.ReworkRecord).filter(models.ReworkRecord.id == rework_id).first()
    if not rework:
        raise HTTPException(status_code=404, detail="返工记录不存在")

    _ensure_transition(rework.status, "waiting_inspection")

    if current_user.role != "admin" and rework.responsible_id != current_user.id:
        raise HTTPException(status_code=403, detail="只有该返工单的责任人才能提交复检")

    if not rework_in.rework_result or not rework_in.rework_result.strip():
        raise HTTPException(status_code=400, detail="返工结果不能为空")

    rework.status = "waiting_inspection"
    rework.actual_finish_time = rework_in.actual_finish_time
    rework.rework_result = rework_in.rework_result

    batch = rework.batch
    batch.status = "pending_inspect"
    db.commit()

    return schemas.ApiResponse(message="返工完成，已提交复检")


@router.put("/{rework_id}/complete", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_inspector)])
def complete_rework(
    rework_id: int,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    rework = db.query(models.ReworkRecord).filter(models.ReworkRecord.id == rework_id).first()
    if not rework:
        raise HTTPException(status_code=404, detail="返工记录不存在")

    _ensure_transition(rework.status, "completed")

    rework.status = "completed"
    db.commit()

    return schemas.ApiResponse(message="返工复检完成")


@router.put("/{rework_id}/cancel", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_technician)])
def cancel_rework(
    rework_id: int,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    rework = db.query(models.ReworkRecord).filter(models.ReworkRecord.id == rework_id).first()
    if not rework:
        raise HTTPException(status_code=404, detail="返工记录不存在")

    _ensure_transition(rework.status, "cancelled")

    rework.status = "cancelled"
    db.commit()

    batch = rework.batch
    remaining_active = _get_active_rework(db, batch.id)
    if not remaining_active and batch.status == "reworking":
        batch.status = "pending_inspect"
        db.commit()

    return schemas.ApiResponse(message="返工记录已取消")
