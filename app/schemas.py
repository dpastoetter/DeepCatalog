"""Pydantic request/response models for the FastAPI surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AskHistoryTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1, max_length=4000)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)
    history: list[AskHistoryTurn] = Field(
        default_factory=list,
        max_length=8,
        description="Optional recent Q/A turns for follow-ups; retrieval uses question only.",
    )


class ProcessRequest(BaseModel):
    path: str = Field(..., min_length=1)


class ApiKeyRequest(BaseModel):
    api_key: str = Field(..., min_length=8)


class OAuthCompleteRequest(BaseModel):
    state: str = Field(..., min_length=8)
    callback: str = Field(..., min_length=1)


class CategorySetting(BaseModel):
    name: str = Field(..., min_length=1)
    folder: str = Field(..., min_length=1)


class BatchSetting(BaseModel):
    poll_interval_seconds: float = Field(
        default=30,
        ge=0,
        description="How often to scan the inbox for new files. 0 disables auto-scan.",
    )


class ReviewSetting(BaseModel):
    require_approval: bool = Field(
        default=True,
        description=(
            "Hold proposed filings for human approval before writing. "
            "Recommended: model classification from document text is untrusted; "
            "approval is the gate for consequential archive moves."
        ),
    )


class OcrSetting(BaseModel):
    mode: Literal["fast", "balanced", "maximum"] = Field(
        default="balanced",
        description="OCR accuracy mode: fast | balanced | maximum.",
    )
    tesseract: bool = Field(
        default=True,
        description="Try classical Tesseract before AI vision when available.",
    )


class SettingsRequest(BaseModel):
    source_dir: str = Field(..., min_length=1)
    categories: list[CategorySetting] = Field(..., min_length=1)
    batch: BatchSetting = Field(default_factory=BatchSetting)
    review: ReviewSetting = Field(default_factory=ReviewSetting)
    ocr: OcrSetting = Field(default_factory=OcrSetting)


class ReviewApproveRequest(BaseModel):
    filename: str | None = None
    doc_type: str | None = None
    doc_date: str | None = None
    subject: str | None = None
    counterparties: str | None = None
    reference_ids: list[str] | None = None
    amount: float | None = None
    currency: str | None = None
    summary: str | None = None


class ReviewRejectRequest(BaseModel):
    delete_file: bool = Field(
        default=True,
        description="Also remove the scan from the inbox so it is not reprocessed.",
    )


class SessionExchangeRequest(BaseModel):
    token: str = Field(..., min_length=8, description="DEEPCATALOG_API_TOKEN value")


class ClearDataRequest(BaseModel):
    confirmation: str = Field(
        ...,
        min_length=1,
        description='Must be exactly "DELETE ALL DEEPCATALOG DATA".',
    )


class ValidatePathRequest(BaseModel):
    path: str = Field(..., min_length=1)


class AutostartRequest(BaseModel):
    enabled: bool


class LlmProviderRequest(BaseModel):
    provider: str = Field(..., min_length=3)
    model: str | None = None
    embedding_model: str | None = None
    base_url: str | None = None
    allow_remote: bool = False


class OllamaEnableRequest(BaseModel):
    base_url: str | None = None
    chat_model: str | None = None
    embedding_model: str | None = None
    pull_missing: bool = False
    allow_remote: bool = False


class OllamaPullRequest(BaseModel):
    model: str = Field(..., min_length=1)


class ProcessCancelRequest(BaseModel):
    file_id: str = Field(..., min_length=1)


class ProcessRetryRequest(BaseModel):
    path: str = Field(..., min_length=1)


class OllamaRestartRequest(BaseModel):
    force: bool = False


class CloudDisclaimerRequest(BaseModel):
    accepted: bool = True
