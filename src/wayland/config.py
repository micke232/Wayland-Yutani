"""Explicit typed configuration; no CLI account discovery or shared agent directories."""

import json
from decimal import Decimal
from pathlib import Path

from pydantic import Field, model_validator

from .models import InstrumentType, Model, TradingMode


class Settings(Model):
    mode: TradingMode = TradingMode.PAPER
    live_enabled: bool = False
    account_allowlist: tuple[str, ...] = ()
    allowed_symbols: tuple[str, ...] = ("ORCL",)
    allowed_instruments: tuple[InstrumentType, ...] = (InstrumentType.LONG_PUT, InstrumentType.PUT_SPREAD)
    max_position_sek: Decimal = Field(default=Decimal(10000), gt=0, allow_inf_nan=False)
    max_daily_loss_sek: Decimal = Field(default=Decimal(2000), gt=0, allow_inf_nan=False)
    max_trades_per_day: int = Field(default=3, gt=0)
    max_open_positions: int = Field(default=1, gt=0)
    max_market_data_age_seconds: int = Field(default=10, gt=0)
    max_fx_age_seconds: int = Field(default=60, gt=0)
    max_bid_ask_fraction: Decimal = Field(default=Decimal(".20"), gt=0, le=1)
    min_volume: int = Field(default=1, ge=0)
    min_open_interest: int = Field(default=1, ge=0)
    min_dte: int = Field(default=7, ge=1)
    max_dte: int = Field(default=90, ge=1)
    fee_per_contract_sek: Decimal = Field(default=Decimal(15), ge=0, allow_inf_nan=False)
    # A conservative floor; must be calibrated against actual broker commissions before paper execution.
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = Field(default=4002, ge=1, le=65535)
    ibkr_client_id: int = Field(default=37, gt=0)
    ibkr_account: str = ""
    openai_model: str = ""
    analysis_timeout_seconds: int = Field(default=30, gt=0, le=120)
    analysis_cooldown_seconds: int = Field(default=300, gt=0)
    rebound_fraction: Decimal = Field(default=Decimal(".01"), gt=0, lt=1)
    reversal_fraction: Decimal = Field(default=Decimal(".005"), gt=0, lt=1)

    @model_validator(mode="after")
    def limits(self):
        if self.min_dte > self.max_dte:
            raise ValueError("min_dte must not exceed max_dte")
        if any(
            i not in (InstrumentType.LONG_PUT, InstrumentType.PUT_SPREAD) for i in self.allowed_instruments
        ):
            raise ValueError("unbounded instruments are not supported")
        return self


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate_json(path.read_text()) if path else Settings()


def app_root(path: Path | None = None) -> Path:
    root = (path or Path.home() / ".wayland").expanduser().resolve()
    # Explicit worktree-local runtime directories are allowed during development.
    if (
        any(part in (".codex-dashboard", ".codex", ".copilot") for part in root.parts)
        and ".runtime" not in root.parts
    ):
        raise ValueError("Wayland must not use development-agent state directories")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("config", "state", "logs", "data"):
        (root / name).mkdir(exist_ok=True, mode=0o700)
    return root


def example_config() -> str:
    return json.dumps(Settings().model_dump(mode="json"), indent=2)
