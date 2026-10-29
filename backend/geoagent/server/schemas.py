from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel


class FeedbackCreate(BaseModel):
    rating: Literal["up", "down"]
    reason: str = ""
    message_id: Optional[str] = None
    question: str = ""
    answer: str = ""


class ConversationCreate(BaseModel):
    title: Optional[str] = None
    model: Optional[str] = None


class ModelSwitch(BaseModel):
    model: str


class UserMessage(BaseModel):
    content: str
    # 会话选择的智能体（sql / elder_care / chat）；留空则走 RouterNode 兜底
    agent: Optional[str] = None


class MessageOut(BaseModel):
    role: str
    content: str
    ts: str
    model: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
