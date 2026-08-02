from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from .. import models, schemas, auth

router = APIRouter(prefix="/batches", tags=["质检记录"])


@router.post("/{batch_id}/inspect", response_model=schemas.ApiResponse, dependencies=[Depends(auth.allow_inspector)])
def record_inspection(
    batch_id: int,
    record_in: schemas.InspectionRecordCreate,
    current_user: models.User = Depends(auth.get_current_active_user),
    db: Session = Depends(get_db)
):
    batch = db.query(models.Batch).filter(models.Batch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="批次不存在")

    if batch.status != "pending_inspect":
        raise HTTPException(status_code=400, detail="当前状态不允许质检")

    record = models.InspectionRecord(
        batch_id=batch_id,
        inspector_id=current_user.id,
        inspect_time=record_in.inspect_time,
        dimension_deviation=record_in.dimension_deviation,
        surface_flatness=record_in.surface_flatness,
        is_pass=record_in.is_pass,
        opinion=record_in.opinion
    )
    db.add(record)

    if record_in.is_pass:
        batch.status = "deliverable"
        batch.actual_end_date = record_in.inspect_time
        batch.review_status = "pending_review"

        # 复检通过即代表本批次返工全部有效，关闭所有未闭环返工单，
        # 避免历史遗留单继续影响预警与统计
        open_reworks = db.query(models.ReworkRecord).filter(
            models.ReworkRecord.batch_id == batch_id,
            models.ReworkRecord.status.in_(["pending", "processing", "waiting_inspection"])
        ).all()
        for rework in open_reworks:
            rework.status = "completed"
            if not rework.actual_finish_time:
                rework.actual_finish_time = record_in.inspect_time
    else:
        batch.status = "reworking"
        batch.review_status = "not_required"
        # 复检不通过：待复检单退回处理中，由同一闭环单继续返工直至通过，
        # 避免悬挂的待复检单阻塞后续返工流程
        waiting_reworks = db.query(models.ReworkRecord).filter(
            models.ReworkRecord.batch_id == batch_id,
            models.ReworkRecord.status == "waiting_inspection"
        ).all()
        for rework in waiting_reworks:
            rework.status = "processing"

    batch.inspector_id = current_user.id
    db.commit()

    message = "质检通过，批次已进入可交付状态，待交付复核" if record_in.is_pass else "质检未通过，批次进入返工状态"
    return schemas.ApiResponse(message=message)
