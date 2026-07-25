from pydantic import BaseModel, HttpUrl, Field
from typing import Optional


class AuditRequest(BaseModel):
    url: HttpUrl = Field(..., description="The URL to audit")


class AuditResult(BaseModel):
    url: str
    status_code: int
    response_time_ms: float
    content_length_bytes: Optional[int] = None
    server_header: Optional[str] = None
    content_type: Optional[str] = None
    from_cache: bool = False
    checked_at: str


class ErrorResponse(BaseModel):
    error: str
    detail: str
    request_id: str
