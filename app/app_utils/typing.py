"""Pydantic models for API types."""

import uuid
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Feedback(BaseModel):
    """Feedback model for the /feedback endpoint."""

    score: int | float
    text: Optional[str] = None
    log_type: Literal["feedback"] = "feedback"
    service_name: Literal["sophee-agent"] = "sophee-agent"
    user_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
