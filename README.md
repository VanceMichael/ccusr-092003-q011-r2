# 东盟AI项目本地验收

智慧交通、电网巡检和算力项目在当地保留原始数据，平台只交换需求基线、环境说明、**数据摘要**、系统版本、适用声明与**签名验收结果**。不接收任何原始业务数据：数据库里只有受控引用、`sha256` 摘要、行数与抽象字段说明。

核心规则：

- 指标必须由**当地机构与供应方双方确认**后，才能开启验收；
- **失败重测另开轮次**（轮次号递增，历史保留）；
- 供应方登记更高系统版本时，所有尚未完成的轮次自动**作废**（`superseded`），须针对新版本重开；
- 通过结论必须由**双方签名**；证据摘要只对项目名册内参与方可见；
- 组合视图把**展品 / 试点 / 部署 / 正式验收**严格分列，演示不会被报成正式交付。

本服务仅依赖 Python 标准库，通过 HTTP JSON 接口交换业务记录，使用 SQLite 文件保存状态。`PORT` 指定监听端口，`DATABASE_PATH` 指定数据文件；`fixtures/example.json` 提供不含真实身份的报文样例，`contracts/entities.json` 记录字段与端点约定。

## 本地开发

```bash
make migrate   # 初始化/升级数据文件（按顺序执行 migrations/*.sql）
make test      # 执行全部自动化检查
make run       # 启动服务（默认 8080 端口）
```

也可以 `docker compose up --build`，宿主机端口由 `APP_PORT` 调整。

## 接口与典型流程

除 `/health` 与 `POST /parties` 外，所有接口都需要 `Authorization: Bearer <token>`（令牌以 sha256 形式保存）。请求体上限 1 MiB。

```bash
# 1. 双方登记（角色：local_institution / supplier）
curl -s localhost:8080/parties -d '{
  "party_id":"PARTNER-A","role":"local_institution",
  "label":"曼谷交通局","token":"local-token-至少16位"}'
curl -s localhost:8080/parties -d '{
  "party_id":"SUPPLIER-X","role":"supplier",
  "label":"供应方","token":"supplier-token-至少16位"}'

# 2. 当地机构建项目并把供应方加入名册
curl -s localhost:8080/projects -H "Authorization: Bearer local-token-至少16位" -d '{
  "project_id":"BKK-SMART-TRAFFIC","title":"泰国智慧交通试点"}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/participants \
  -H "Authorization: Bearer local-token-至少16位" -d '{"party_id":"SUPPLIER-X"}'

# 3. 当地机构登记需求基线、环境说明、数据摘要（只给 sha256，不给原始数据）
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/baselines -H "Authorization: Bearer local-token-至少16位" -d '{
  "baseline_id":"BL-1","requirements":{"language":"th","accuracy_target":0.95}}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/environments -H "Authorization: Bearer local-token-至少16位" -d '{
  "note_id":"ENV-1","languages":["th"],"network":{"uplink_mbps":4},
  "infrastructure":{"edge_nodes":2}}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/data-summaries -H "Authorization: Bearer local-token-至少16位" -d '{
  "summary_id":"DS-1","dataset_ref":"BKK-TRANSIT-2026-09",
  "digest":"<64位十六进制sha256>","row_count":100000,
  "schema_note":{"fields":["trip_count","hour"]}}'

# 4. 供应方登记版本与适用声明（登记 revision=2 会作废旧轮次）
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/versions -H "Authorization: Bearer supplier-token-至少16位" -d '{
  "version_id":"V-1","revision":1,"label":"edge-1.0"}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/claims -H "Authorization: Bearer supplier-token-至少16位" -d '{
  "claim_id":"CLAIM-1","version_id":"V-1","scope":"曼谷试点路口",
  "languages":["th"],"network_conditions":{"min_uplink_mbps":2},
  "infrastructure_conditions":{"edge_nodes":2},"limitations":""}'

# 5. 建里程碑、约指标，双方各自确认
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/milestones -H "Authorization: Bearer local-token-至少16位" -d '{
  "milestone_id":"FA-1","milestone_ref":"FA-1","title":"正式验收",
  "stage":"formal_acceptance"}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/milestones/FA-1/metrics \
  -H "Authorization: Bearer local-token-至少16位" -d '{
  "metric_id":"M-ACC","metric_key":"accuracy","name":"泰语场景准确率",
  "comparison":"gte","threshold":0.95}'
curl -s -X POST localhost:8080/projects/BKK-SMART-TRAFFIC/milestones/FA-1/metrics/M-ACC/consent \
  -H "Authorization: Bearer local-token-至少16位"
curl -s -X POST localhost:8080/projects/BKK-SMART-TRAFFIC/milestones/FA-1/metrics/M-ACC/consent \
  -H "Authorization: Bearer supplier-token-至少16位"

# 6. 开轮 → 提交实测值与双方签名（判定不达标则 fail，重测另开新轮）
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/milestones/FA-1/rounds \
  -H "Authorization: Bearer local-token-至少16位" -d '{"round_id":"R-1","revision":1}'
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/rounds/R-1/conclusion \
  -H "Authorization: Bearer local-token-至少16位" -d '{
  "conclusion_id":"C-1","measurements":{"accuracy":0.96},
  "signatures":[
    {"party_id":"PARTNER-A","signature_algorithm":"ed25519",
     "signature_value":"...","signed_payload_digest":"<sha256>"},
    {"party_id":"SUPPLIER-X","signature_algorithm":"ed25519",
     "signature_value":"...","signed_payload_digest":"<sha256>"}]}'

# 7. 证据摘要（原始包留在当地受控存储）；非名册参与方 GET 项目返回 403
curl -s localhost:8080/projects/BKK-SMART-TRAFFIC/evidence -H "Authorization: Bearer local-token-至少16位" -d '{
  "evidence_id":"E-1","title":"第1轮现场证据包",
  "digest":"<64位十六进制sha256>","storage_ref":"local-vault://BKK/R-1",
  "round_id":"R-1"}'

# 8. 管理者视图：四阶段分列
curl -s localhost:8080/portfolio/combo -H "Authorization: Bearer local-token-至少16位"
```

错误响应统一为 `{"error": "...", "message": "..."}`，状态码：400 报文/字段错误、401 未鉴权、403 非名册或角色不符、404 资源不存在、409 状态冲突（指标未确认、轮次已关闭、版本升级作废等）、413 报文过大。
