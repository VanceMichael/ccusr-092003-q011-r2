# 东盟AI项目本地验收平台

智慧交通、电网巡检和算力项目在当地保留原始数据，平台只交换需求基线、环境摘要、系统版本与签名验收结果。

本服务通过 HTTP/JSON 接口交换业务记录，并使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 提供不含真实身份的本地示例，`contracts/entities.json` 记录字段约定，`docs/domain.md` 描述业务规则。

## 平台保证的规则

1. **不接收原始业务数据**：只存当地受控引用编号与 `sha256` 摘要；写入载荷递归扫描到 `records`/`payload`/`video`/`plate` 等原始字段名即返回 `422`。
2. **先约定指标后验收**：每个指标必须由当地机构与供应方双方同意，否则轮次不能提交结论（`409 metric_not_agreed`）。
3. **失败重测另开轮次**：轮次判定为 `passed`/`failed` 后冻结，重测开新一轮，历史轮次全部保留。
4. **升级版本即失效**：供应方登记新版本时，项目下所有进行中（`open`）轮次自动转为 `invalidated`；已结束轮次不受影响。
5. **证据仅相关方可见**：轮次、证据、摘要对不相关参与方返回 `403`，列表与组合视图中也不出现他方项目。
6. **四阶段严格分离**：项目阶段为 `demo`（展品）/`pilot`（试点）/`deployment`（部署）/`acceptance`（正式验收），`/v1/overview` 四桶分列，演示不会被误报为可交付。

## 本地开发

运行 `make migrate` 初始化数据文件，`make test` 执行自动化检查（20 个端到端测试），`make run` 启动服务。也可以使用 `docker compose up --build` 在隔离容器中运行，宿主机端口由 `APP_PORT` 调整。

## 接口速览

所有 `/v1/*` 接口需要 `Authorization: Bearer <token>`（首个管理者登记除外）。令牌在参与方登记时一次性返回，库内只存其 SHA-256。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| POST | `/v1/parties` | 公开（仅首次，须 administrator）/管理者 | 登记参与方，返回一次性令牌 |
| GET | `/v1/parties` | 管理者 | 参与方名录 |
| POST | `/v1/projects` | 管理者 | 登记项目（指定阶段与双方） |
| GET | `/v1/projects[?stage=]` | 相关方/管理者 | 项目列表（可按阶段过滤） |
| GET | `/v1/projects/{ref}` | 相关方/管理者 | 项目详情 |
| POST/GET | `/v1/projects/{ref}/revisions` | 供应方 / 相关方 | 登记版本+适用声明（POST 会失效进行中轮次） |
| POST/GET | `/v1/projects/{ref}/baselines` | 机构 / 相关方 | 需求基线+数据摘要（仅聚合值） |
| POST/GET | `/v1/projects/{ref}/environments` | 机构 / 相关方 | 语言、网络、基础设施环境说明 |
| POST/GET | `/v1/projects/{ref}/milestones` | 任一方 / 相关方 | 里程碑 |
| POST/GET | `/v1/milestones/{id}/metrics` | 机构或供应方 / 相关方 | 提出指标（提出方即同意） |
| POST | `/v1/metrics/{id}/agree` | 对方 | 同意指标，达成双方约定 |
| POST/GET | `/v1/projects/{ref}/rounds` | 任一方 / 相关方 | 开启/列出验收轮次（绑定基准版本） |
| GET | `/v1/rounds/{id}` | 相关方/管理者 | 轮次详情，含指标结果与证据摘要 |
| POST | `/v1/rounds/{id}/evidence` | 任一方 | 登记证据：`digest`(sha256) + `controlled_ref` |
| POST | `/v1/rounds/{id}/conclusion` | 任一方 | 提交实测值，自动判定 passed/failed 并签名 |
| GET | `/v1/projects/{ref}/summary` | 相关方/管理者 | 项目全量摘要 |
| GET | `/v1/overview` | 已登录 | 组合视图：展品/试点/部署/正式验收分列 |
| GET | `/health` | 公开 | 健康检查 |

错误统一为 `{"error": "<code>", "message": "..."}`，状态码：400 校验失败、403 无权限、404 不存在、409 状态冲突（含指标未双方同意）、422 夹带原始数据。

## 典型流程

```bash
# 1. 首个管理者（空库自助）
curl -s -X POST localhost:8080/v1/parties -d '{"ref":"ADMIN","name":"PMO","role":"administrator"}'
# 2. 管理者登记当地机构与供应方，再创建 acceptance/pilot 阶段项目
# 3. 供应方 POST .../revisions；机构 POST .../baselines 与 .../environments
# 4. 任一方建里程碑；一方提指标，另一方 POST /v1/metrics/{id}/agree
# 5. POST .../rounds 开轮次 → POST /v1/rounds/{id}/evidence 交摘要
# 6. POST /v1/rounds/{id}/conclusion 交实测值得出判定；失败则再开一轮
```

结论响应中的 `signature_digest` 是对规范化结论体（项目、里程碑、轮次、版本号、逐项指标结果、判定、时间）的 SHA-256，可与双方各自留存的本地材料摘要比对。
