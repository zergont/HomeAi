# packages/storage/models.py
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Thread(Base):
    __tablename__ = "threads"

    id = Column(String(64), primary_key=True)
    title = Column(String(256), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # Summary fields
    summary = Column(Text, nullable=True)
    summary_updated_at = Column(DateTime, nullable=True)
    summary_lang = Column(String(10), nullable=True)
    summary_quality = Column(String(10), nullable=True)  # ok|draft
    is_summarizing = Column(Boolean, default=False)
    summary_source_hash = Column(String(64), nullable=True)
    last_summary_run_at = Column(Integer, nullable=True)

    # Relationships
    messages = relationship("Message", back_populates="thread", cascade="all, delete-orphan")
    responses = relationship("Response", back_populates="thread", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(String(64), primary_key=True)
    thread_id = Column(String(64), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(32))  # system|user|assistant|tool
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    # token usage (optional)
    input_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)

    thread = relationship("Thread", back_populates="messages")

    __table_args__ = (
        CheckConstraint("role in ('system','user','assistant','tool')", name="ck_messages_role"),
    )


class Response(Base):
    __tablename__ = "responses"

    id = Column(String(64), primary_key=True)  # resp_<uuid>
    thread_id = Column(String(64), ForeignKey("threads.id", ondelete="CASCADE"), nullable=False, index=True)
    request_json = Column(Text, nullable=False)
    response_json = Column(Text, nullable=False)

    status = Column(String(32))
    model = Column(String(128))
    provider_name = Column(String(64))
    provider_base_url = Column(String(256), nullable=True)
    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    cost = Column(Numeric(12, 6), default=Decimal("0.000000"))

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    thread = relationship("Thread", back_populates="responses")


class Profile(Base):
    __tablename__ = "profile"

    id = Column(Integer, primary_key=True, default=1)

    # White-list fields (A/C/D and B subset)
    display_name = Column(String(128), nullable=True)
    preferred_language = Column(String(32), nullable=True)
    tone = Column(String(32), nullable=True)
    timezone = Column(String(64), nullable=True)
    region_coarse = Column(String(64), nullable=True)
    work_hours = Column(String(256), nullable=True)
    ui_format_prefs = Column(Text, nullable=True)       # JSON serialized
    goals_mood = Column(Text, nullable=True)
    decisions_tasks = Column(Text, nullable=True)
    brevity = Column(String(32), nullable=True)
    format_defaults = Column(Text, nullable=True)       # JSON serialized
    interests_topics = Column(Text, nullable=True)      # JSON serialized
    workflow_tools = Column(Text, nullable=True)        # JSON serialized
    os = Column(String(64), nullable=True)
    runtime = Column(String(64), nullable=True)
    hardware_hint = Column(String(128), nullable=True)

    # Metadata
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    source = Column(String(64), nullable=True)
    confidence = Column(Integer, nullable=True)


# Memory state and summaries
class MemoryState(Base):
    __tablename__ = "memory_state"

    thread_id = Column(String(64), ForeignKey("threads.id", ondelete="CASCADE"), primary_key=True)
    last_compacted_message_id = Column(String(64), nullable=True)
    l1_tokens = Column(Integer, default=0)
    l2_tokens = Column(Integer, default=0)
    l3_tokens = Column(Integer, default=0)
    updated_at = Column(Integer, nullable=False, default=lambda: int(datetime.utcnow().timestamp()))


class L2Summary(Base):
    __tablename__ = "l2_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(String(64), ForeignKey("threads.id", ondelete="CASCADE"), index=True, nullable=False)
    start_message_id = Column(String(64), nullable=False)
    end_message_id = Column(String(64), nullable=False)
    text = Column(Text, nullable=False)
    tokens = Column(Integer, nullable=False)
    created_at = Column(Integer, nullable=False, default=lambda: int(datetime.utcnow().timestamp()))


class L3MicroSummary(Base):
    __tablename__ = "l3_microsummaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(String(64), ForeignKey("threads.id", ondelete="CASCADE"), index=True, nullable=False)
    start_l2_id = Column(Integer, nullable=False)
    end_l2_id = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    tokens = Column(Integer, nullable=False)
    created_at = Column(Integer, nullable=False, default=lambda: int(datetime.utcnow().timestamp()))


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id = Column(Integer, primary_key=True)
    thread_id = Column(String, index=True, nullable=False)
    attempt_id = Column(String, nullable=False)  # uuid попытки
    tool_name = Column(String, index=True, nullable=False)
    args_json = Column(Text, nullable=False)
    args_hash = Column(String, index=True, nullable=False)  # sha256 канонизированных аргументов
    result_text = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="done")  # done|error
    created_at = Column(Integer, nullable=False)
