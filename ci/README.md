# platform router CI —— ai-infra-platform-router 的镜像构建流水线

这是 `platform-ci` 家族的**第四个实例**（api / web / router）。它的存在理由只有一个：
**把"有依据的路由决策"从"只被记录"变成"被真正执行"**。

## 它做什么

```
Gitea push (ai-infra-platform-router)
        │  webhook
        v
Jenkins job `platform-router-ci`
        ├─ 1. Checkout        私有仓检出（凭据 gitea-scm）
        ├─ 2. Trigger Guard   只看是否碰了 src/ tests/ ci/ Dockerfile requirements* pytest.ini
        ├─ 3. Resolve Tag     platform/ai-infra-platform-router:<short sha>
        ├─ 4. Test            pip install -r requirements-dev.txt && pytest（纯函数，不打库）
        ├─ 5. Build           buildah bud（单阶段 python:3.11-slim）
        ├─ 6. Push            → k3d-sre-registry:5000
        ├─ 7. Verify In Registry
        └─ 8. Update GitOps   sed 改 router.yaml 的 image tag → ArgoCD 收敛
```

## 与 api 版、web 版的关系（一张表说清）

| | platform-ci（api） | platform-web-ci（web） | **platform-router-ci（router）** |
|---|---|---|---|
| 语言/产物 | Python + uvicorn | 静态文件 + nginx | Python + uvicorn |
| `Fetch Schema` | 有 | 无 | **无**（不拥有 schema） |
| `Migrate` | 有（跑迁移） | 无 | **无** |
| `Test` | pytest（82 用例，**打真 PG**） | `tsc` + vitest（20 条） | **pytest（12 条，纯函数，不打库）** |
| 构建阶段数 | 单阶段 | **多阶段**（node → nginx） | 单阶段 |
| buildah PVC | `buildah-storage-platform` | `buildah-storage-web` | **`buildah-storage-router`** |
| post 告警钩子 | 有 | 无 | **无**（与 web 一致） |

**四条流水线共用**的部分（这是刻意复制的骨架）：Pod 模板、Trigger Guard、
Build / Push / Verify In Registry / Update GitOps 四段、首次落地守卫。

**要留哪几节，取决于两个问题**：这个仓有没有 schema 所有权？它用什么语言、产物是什么？
—— 见 `platform-ci-onboard` skill 的同名小节。

## 为什么测试打的是纯函数

`src/picking.py` 里只有两个函数（`normalize` / `pick`），**不 import fastapi**。
好处有两层：

1. **测试不需要起 HTTP、不需要装 fastapi**，`pytest` 只要 0.09 秒
2. 加权采样这种逻辑**最容易写错**（边界、除零、被排除的候选），
   而它是"有依据的路由"这句话的落点——所以它值得有**分布断言**：
   给两个分数 0.9 / 0.1 的候选采 2000 次，命中比必须落在 0.85–0.95 之间。

**把纯逻辑从 HTTP 层拆出去，是为了让它可测；不是为了好看。**（api 仓的 `service/scoring.py` 同一个形状。）

## 需要什么才能复现

| 类型 | 名称 | 用途 |
|---|---|---|
| Jenkins 凭据 | `gitea-scm` | 私有仓 SCM 检出（与 api/web 共用） |
| Jenkins 凭据 | `platform-webhook-token` | GenericTrigger 的 token（共用） |
| K8s Secret | `jenkins/gitea-netrc` | Pod 内 clone/push 私有仓与 GitOps 仓（共用） |
| K8s ConfigMap | `jenkins/buildah-registries` | buildah 走 daocloud 镜像源（共用） |
| K8s PVC | `jenkins/buildah-storage-router` | buildah 的 vfs 存储（**router 独占**） |

**这个服务不需要任何 Secret**：它不连库、没有配置密码。它对控制层用的是**调用方传来的 `X-API-Key`**
（透传鉴权）——所以集群里不需要多存一份凭据。见 `docs/10_路由器.md`。
