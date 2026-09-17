from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    control_plane_url: str = (
        "http://ai-infra-platform-api.ai-platform.svc.cluster.local:8000"
    )
    request_timeout_seconds: float = 240.0
    default_policy: str = "weighted_random"
    default_max_tokens: int = 128


@lru_cache
def get_settings() -> Settings:
    return Settings()
