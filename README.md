# ai-infra-platform-router

**决策执行器**：它是「控制层算出的路由决策」与「真实推理后端」之间缺的那一环。

## 它解决什么问题

在这之前，系统是这样断的：

- 控制层 `ai-infra-platform-api` 能算出**该选哪个后端**（`GET /api/v1/routing/candidates` 返回带分数的排名），
  并把决策**记录**进 PostgreSQL —— 但它**从不调用任何推理后端**。
- `ollama` / `vLLM` 真实后端在集群里跑着、能生成 token —— 但**没有任何东西调它们**。

于是"让请求**有依据地**路由"这句话，只完成了"有依据"（决策），没完成"路由"（执行）。

**router 补的就是这一刀**：拿决策 → 选一个真实后端 → 把 prompt 打过去 → 拿回 token → 把这次请求和决策写进控制层。

## 接口

### `POST /v1/route`

请求头：`X-API-Key: <控制层签发的 key>`（**必带**，router 拿它去问控制层，透传鉴权）

```json
{
  "model_name": "qwen2.5-3b",
  "prompt": "只回答两个字：你好",
  "policy": "least_latency",
  "max_tokens": 32,
  "engine_models": { "ollama-a": "qwen2.5:3b", "vllm-b": "/models/qwen2.5-3b-awq" }
}
```

响应：

```json
{
  "request_id": "008469de-...",
  "recorded": true,
  "backend": { "id": 2, "name": "vllm-b", "engine": "openai", "url": "http://vllm-3b-service:8000" },
  "decision": {
    "policy": "least_latency",
    "candidate_ids": [2, 1],
    "chosen_id": 2,
    "score_snapshot": { "ranked": [ ... ] },
    "fallback_reason": null
  },
  "output": "你好",
  "usage": { "prompt_tokens": 35, "completion_tokens": 2 },
  "timings": { "total_ms": 2244.1, "engine_model": "/models/qwen2.5-3b-awq" }
}
```

### `GET /healthz` / `GET /readyz`

`/readyz` 会去探控制层的 `/healthz`（**公开端点，不需要 key**）。
**控制层不可达时 router 报 `503`** —— 这是刻意的：router 依赖控制层的决策，依赖不在就不该接流量。

## 它不做的事（边界）

| 不做 | 为什么 |
|---|---|
| 自己算分 / 自己挑策略 | 打分是控制层的职责（`ai-infra-platform-api/src/service/scoring.py`）。**router 只执行，不决策** —— 否则就有两套真相 |
| 存自己的凭据 | 用调用方传来的 `X-API-Key`（透传）。集群里不需要多一份密钥 |
| 改 schema | 它只调控制层的 HTTP 接口，不碰库 |
| 流式返回 | MVP 用 `stream: false` / 非流式 `chat/completions`。**流式是明确的未做项** |

## 已知的保真度缺口（不粉饰）

1. **引擎侧模型名没进 schema。** 控制层的 `model` 表存的是逻辑名（`qwen2.5-3b`），
   而 ollama 要 `qwen2.5:3b`、vLLM 要 `/models/qwen2.5-3b-awq`。现在靠请求里的 `engine_models` 映射兜住。
   **正规修法**是给 db 仓的 `backend_model` 关联表加一列 `engine_model`。
2. **不是所有策略都真的实现了**：`weighted_random` 按分数加权采样；其余三种（`least_latency` /
   `round_robin` / `session_sticky`）**退化成"取排名第一"**。`round_robin` 需要状态、
   `session_sticky` 需要会话表，本轮没做。
3. **`api_key_id` 记为 null**：router 不知道这把 key 在库里的 id（要额外查一次 keys 列表）。请求日志因此少一个维度。
4. **没有对 router 自身的鉴权**：它只做"是否带了 `X-API-Key`"的检查，**真正的校验由控制层完成**。
   所以 router 自己不是安全边界——它不能代替控制层的鉴权。

## 怎么跑

```bash
pip install -r requirements-dev.txt
python -m pytest -q                      # 12 条，纯函数，0.09s
uvicorn src.main:app --host 0.0.0.0 --port 8000
```

环境变量（都有默认值，默认指向集群内的控制层）：

| 变量 | 默认 |
|---|---|
| `CONTROL_PLANE_URL` | `http://ai-infra-platform-api.ai-platform.svc.cluster.local:8000` |
| `REQUEST_TIMEOUT_SECONDS` | `240` |
| `DEFAULT_POLICY` | `weighted_random` |
| `DEFAULT_MAX_TOKENS` | `128` |
