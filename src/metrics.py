from prometheus_client import Counter, Histogram

ROUTE_TOTAL = Counter(
    "router_route_total",
    "route 端点被调用的次数",
    ["backend", "engine", "policy", "status"],
)

ROUTE_DURATION = Histogram(
    "router_route_duration_seconds",
    "route 端到端耗时（含问控制层 + 后端生成 + 写回）",
    ["backend", "policy"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

CANDIDATES = Histogram(
    "router_candidates_count",
    "每次请求拿到的候选后端数量",
    ["model"],
    buckets=(0, 1, 2, 3, 5, 10),
)

RECORD_FAILURES = Counter(
    "router_record_failures_total",
    "把请求与决策写回控制层失败的次数",
)

UPSTREAM_ERRORS = Counter(
    "router_upstream_errors_total",
    "调用真实后端失败的次数",
    ["reason"],
)


def observe_route(
    backend: str,
    engine: str,
    policy: str,
    status: str,
    seconds: float,
    candidates: int,
    model: str,
) -> None:
    ROUTE_TOTAL.labels(backend=backend, engine=engine, policy=policy, status=status).inc()
    ROUTE_DURATION.labels(backend=backend, policy=policy).observe(seconds)
    CANDIDATES.labels(model=model).observe(candidates)


def observe_record_failure() -> None:
    RECORD_FAILURES.inc()


def observe_upstream_error(reason: str) -> None:
    UPSTREAM_ERRORS.labels(reason=reason).inc()
