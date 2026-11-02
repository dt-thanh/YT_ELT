"""
Gateway: the only way studio code reaches an LLM (spec §12).

  task --route--> model, max output tokens
       --budget-> worst-case cost of THIS call + what the job already spent <= ceiling?
                  no  -> BudgetExceeded, nothing is sent
       --call---> LLMClient (logs llm_calls with job_id and agent, feature 'studio')
       --parse--> Pydantic model (structured output); invalid -> GatewayError

The worst case is computed before the call: input tokens estimated generously from
the message length, output tokens at the route's maximum. A model with no price in
llm_pricing cannot be bounded, so it is refused.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Callable, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from youtube_intel.config import ConfigError
from youtube_intel.llm import LLMClient, estimate_cost

logger = logging.getLogger(__name__)

DEFAULT_STUDIO_PATH = Path(__file__).resolve().parents[2] / "config" / "studio.yaml"
BUDGETS = ("short", "long", "standalone")
M = TypeVar("M", bound=BaseModel)


class GatewayError(Exception):
    def __init__(self, message: str, *, call_id: str | None = None):
        super().__init__(message)
        self.call_id = call_id


class BudgetExceeded(GatewayError):
    """The call could push the job over its ceiling; it was not made."""


@dataclass(frozen=True)
class Route:
    task: str
    model: str
    max_output_tokens: int


@dataclass(frozen=True)
class GatewayConfig:
    routes: dict[str, Route]
    budget_usd: dict[str, Decimal]
    monthly_usd: Decimal | None
    chars_per_token: float


def load_gateway_config(path: Path | str | None = None) -> GatewayConfig:
    path = Path(path) if path else DEFAULT_STUDIO_PATH
    raw = (yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}).get("gateway")
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: thiếu khối gateway")
    models = raw.get("models") or {}
    routes = {}
    for task, r in (raw.get("routes") or {}).items():
        tier = (r or {}).get("tier")
        if tier not in models or not models[tier]:
            raise ConfigError(f"{path}: gateway.routes.{task}: tầng {tier!r} chưa có model")
        routes[task] = Route(task, str(models[tier]), int(r.get("max_output_tokens", 1000)))
    budget = raw.get("budget_usd") or {}
    missing = set(BUDGETS) - set(budget)
    if missing:
        raise ConfigError(f"{path}: gateway.budget_usd thiếu {sorted(missing)}")
    monthly = budget.get("monthly")
    return GatewayConfig(
        routes=routes,
        budget_usd={k: Decimal(str(budget[k])) for k in BUDGETS},
        monthly_usd=None if monthly is None else Decimal(str(monthly)),
        chars_per_token=float(raw.get("chars_per_token", 3)),
    )


class Gateway:
    """One per process. `client_for(model)` returns an LLMClient for that model;
    `job_spent(job_id)` / `month_spent()` read what was already spent (llm_calls)."""

    def __init__(self, cfg: GatewayConfig, *, client_for: Callable[[str], LLMClient],
                 pricing, job_spent: Callable[[str], Decimal],
                 month_spent: Callable[[], Decimal] | None = None):
        self.cfg = cfg
        self._client_for = client_for
        self._pricing = tuple(pricing)
        self._job_spent = job_spent
        self._month_spent = month_spent

    def worst_case_usd(self, route: Route, messages: list[dict]) -> Decimal | None:
        chars = len(json.dumps(messages, ensure_ascii=False))
        tokens_in = int(chars / self.cfg.chars_per_token) + 1
        return estimate_cost(route.model, tokens_in, route.max_output_tokens, self._pricing)

    def check_budget(self, task: str, messages: list[dict], *, job_id: str,
                     budget: str) -> Route:
        route = self.cfg.routes.get(task)
        if route is None:
            raise GatewayError(f"no route for task {task!r} in config/studio.yaml")
        if budget not in BUDGETS:
            raise GatewayError(f"unknown budget {budget!r}; expected one of {BUDGETS}")
        worst = self.worst_case_usd(route, messages)
        if worst is None:
            raise BudgetExceeded(f"model {route.model!r} has no price in llm_pricing: "
                                 "its cost cannot be bounded")
        spent = Decimal(self._job_spent(job_id) or 0)
        ceiling = self.cfg.budget_usd[budget]
        if spent + worst > ceiling:
            raise BudgetExceeded(f"{task}: job {job_id} spent ${spent:.4f}, this call could cost "
                                 f"${worst:.4f}, ceiling ${ceiling} ({budget})")
        if self.cfg.monthly_usd is not None and self._month_spent is not None:
            month = Decimal(self._month_spent() or 0)
            if month + worst > self.cfg.monthly_usd:
                raise BudgetExceeded(f"{task}: month spent ${month:.4f} + ${worst:.4f} "
                                     f"> monthly ceiling ${self.cfg.monthly_usd}")
        return route

    def structured(self, task: str, messages: list[dict], schema: type[M], *, job_id: str,
                   agent: str, budget: str = "standalone",
                   prompt_version: str | None = None) -> tuple[M, str]:
        """One call whose answer must parse as `schema`. Returns (object, call_id)."""
        uuid.UUID(job_id)                                   # job ids are UUIDs (llm_calls)
        route = self.check_budget(task, messages, job_id=job_id, budget=budget)
        client = self._client_for(route.model)
        fmt = {"type": "json_schema",
               "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}}
        resp = client.chat(messages, feature="studio", prompt_version=prompt_version,
                           response_format=fmt, max_tokens=route.max_output_tokens,
                           temperature=0, job_id=job_id, agent=agent)
        try:
            obj = schema.model_validate_json(resp.content or "")
        except ValidationError as e:
            client.mark(resp.call_id, validation_status="failed")
            raise GatewayError(f"{task}: output does not match {schema.__name__}: "
                               f"{e.errors()[:3]}", call_id=resp.call_id) from e
        client.mark(resp.call_id, validation_status="passed")
        return obj, resp.call_id


def job_spent_from_db(db) -> Callable[[str], Decimal]:
    """Spend of a job so far: logged LLM calls (media_jobs join here in P4)."""
    from youtube_intel import repository as repo

    def spent(job_id: str) -> Decimal:
        with db.connect() as conn:
            with repo.dict_cursor(conn) as cur:
                cur.execute("SELECT COALESCE(sum(est_cost_usd), 0) AS usd FROM yti.llm_calls "
                            "WHERE job_id = %s", (job_id,))
                return Decimal(cur.fetchone()["usd"])
    return spent


def gateway_from_config(cfg, db, *, gateway_cfg: GatewayConfig | None = None) -> Gateway:
    """Production wiring: OpenAI clients logging to yti.llm_calls."""
    from youtube_intel.llm import PostgresSink

    if not cfg.secrets.openai_api_key:
        raise GatewayError("OPENAI_API_KEY is not set")
    clients: dict[str, LLMClient] = {}

    def client_for(model: str) -> LLMClient:
        if model not in clients:
            clients[model] = LLMClient(api_key=cfg.secrets.openai_api_key, model=model,
                                       pricing=cfg.settings.llm_pricing, sink=PostgresSink(db))
        return clients[model]

    return Gateway(gateway_cfg or load_gateway_config(), client_for=client_for,
                   pricing=cfg.settings.llm_pricing, job_spent=job_spent_from_db(db))
