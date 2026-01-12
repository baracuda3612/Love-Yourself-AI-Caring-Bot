from typing import Dict

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from app.db import SessionLocal, User
from app.scheduler import cancel_plan_step_jobs, reschedule_plan_steps
from app.time_slots import TimeSlotError, update_user_time_slots

app = FastAPI()


class TimeSlotsPayload(BaseModel):
    MORNING: str
    DAY: str
    EVENING: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "MORNING": self.MORNING,
            "DAY": self.DAY,
            "EVENING": self.EVENING,
        }


@app.post("/user/time-slots")
def set_user_time_slots(
    payload: TimeSlotsPayload,
    user_id: int = Query(..., description="User ID to update"),
) -> Dict[str, int]:
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            raise HTTPException(status_code=404, detail="user_not_found")
        try:
            updated_step_ids, active_step_ids = update_user_time_slots(
                db, user, payload.to_dict()
            )
        except TimeSlotError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        db.commit()

    cancel_plan_step_jobs(active_step_ids)
    reschedule_plan_steps(active_step_ids)

    return {"updated_steps": len(updated_step_ids)}
