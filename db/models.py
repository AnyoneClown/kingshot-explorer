"""Database models for the bot."""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class User(Base):
    """User model to track Discord users."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Discord user ID
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    discriminator: Mapped[str] = mapped_column(String(10), nullable=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Relationships
    translation_logs: Mapped[List["TranslationLog"]] = relationship("TranslationLog", back_populates="user")
    gift_code_redemptions: Mapped[List["GiftCodeRedemption"]] = relationship(
        "GiftCodeRedemption", back_populates="user"
    )
    registered_players: Mapped[List["RegisteredPlayer"]] = relationship("RegisteredPlayer", back_populates="added_by")

    def __repr__(self) -> str:
        return f"<User(id={self.id}, username={self.username})>"


class TranslationLog(Base):
    """Log of all translations performed by the bot."""

    __tablename__ = "translation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guild_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    source_language: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_language: Mapped[str] = mapped_column(String(50), nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    translation_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="manual"  # manual, reaction, command, etc.
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="translation_logs")

    def __repr__(self) -> str:
        return f"<TranslationLog(id={self.id}, user_id={self.user_id}, target_language={self.target_language})>"


class RegisteredPlayer(Base):
    """Unified player profile table for all known players."""

    __tablename__ = "registered_players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    player_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    kingdom: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    castle_level: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    added_by_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Relationships
    added_by: Mapped["User"] = relationship("User", back_populates="registered_players")

    def __repr__(self) -> str:
        return (
            f"<RegisteredPlayer(id={self.id}, player_id={self.player_id}, player_name={self.player_name}, "
            f"kingdom={self.kingdom}, castle_level={self.castle_level}, enabled={self.enabled})>"
        )


class GiftCodeRedemption(Base):
    """Log of all gift code redemptions."""

    __tablename__ = "gift_code_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    guild_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    player_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    gift_code: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    success: Mapped[bool] = mapped_column(default=False, nullable=False)
    response_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="gift_code_redemptions")

    def __repr__(self) -> str:
        return f"<GiftCodeRedemption(id={self.id}, user_id={self.user_id}, player_id={self.player_id}, gift_code={self.gift_code})>"


class GiftCode(Base):
    """Gift codes fetched from the 3rd party API."""

    __tablename__ = "gift_codes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Using ID from API
    code: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at_api: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return f"<GiftCode(id={self.id}, code={self.code})>"


class ScheduledReminder(Base):
    """Persisted Discord reminder schedule."""

    __tablename__ = "scheduled_reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    reminder_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    role_names_json: Mapped[str] = mapped_column(Text, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    repeat_every_days: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ScheduledReminder(id={self.id}, channel_id={self.channel_id}, reminder_time={self.reminder_time})>"
