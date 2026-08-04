from typing import Literal

from pydantic import BaseModel, Field


Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT"]


class StreetProperties(BaseModel):
    block_face_id: str
    street_name: str
    borough: str
    side: Literal["LEFT", "RIGHT"]
    refuse_days: list[Weekday] = Field(min_length=1)
    source: str
    retrieved_at: str


class HealthResponse(BaseModel):
    status: str
    environment: str
    processed_records: int

