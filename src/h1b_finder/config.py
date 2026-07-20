from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent.parent


class Secrets(BaseSettings):
    """API credentials — from .env (gitignored), never config.yaml."""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    adzuna_app_id: str | None = None
    adzuna_app_key: str | None = None
    anthropic_api_key: str | None = None


class Paths(BaseModel):
    raw: str
    parquet: str
    db: str


class IngestConfig(BaseModel):
    fiscal_years: list[int]
    case_statuses_positive: list[str]


class CapExemptConfig(BaseModel):
    off_cycle_window: list[str]
    off_cycle_min_n: int
    off_cycle_min_ratio: float


class ScoringConfig(BaseModel):
    version: str
    weights: dict[str, float]


class WageConfig(BaseModel):
    year: str


class AdzunaJobsConfig(BaseModel):
    role_queries: list[str]
    top_sponsors_n: int
    cap_exempt_min_score: float
    req_per_sec: float


class AtsJobsConfig(BaseModel):
    req_per_sec: float
    consecutive_failures_limit: int


class SocPredictConfig(BaseModel):
    embedding_min_confidence: float
    haiku_batch_size: int
    haiku_model: str


class JobsConfig(BaseModel):
    adzuna: AdzunaJobsConfig
    ats: AtsJobsConfig
    soc_predict: SocPredictConfig


class Config(BaseModel):
    target_soc_codes: list[str]
    paths: Paths
    ingest: IngestConfig
    capexempt: CapExemptConfig
    scoring: ScoringConfig
    wage: WageConfig
    jobs: JobsConfig

    @property
    def raw_dir(self) -> Path:
        return ROOT / self.paths.raw

    @property
    def parquet_dir(self) -> Path:
        return ROOT / self.paths.parquet

    @property
    def db_path(self) -> Path:
        return ROOT / self.paths.db


def load_config(path: Path | None = None) -> Config:
    raw: dict[str, Any] = yaml.safe_load((path or ROOT / "config.yaml").read_text())
    return Config.model_validate(raw)
