# FastAPI 请求级链路追踪与可观测性

本服务内置一套请求级可观测性能力：每个请求拥有可追踪的关联标识（trace id），
在同步处理、后台任务与对外调用之间一致传递；异常按类型分类；慢请求按可配置
阈值标记并记录分阶段耗时；日志支持采样与并发安全的轮转；指标与日志对同一
请求的结论一致。在此之上还提供：本地可复现的跨服务链路（真实调用下游并校验
回传标识）、响应返回后仍可观察的后台任务（结果可查、失败归类）、以及按标识
回放整条链路的时间线查询（有界存储、超限截断）。

## 快速开始

```bash
uv sync
uv run python main.py          # 监听 127.0.0.1:8000
uv run pytest -v               # 运行全部测试
```

## 追踪字段含义

每个请求结束时（未被采样丢弃的话）会向 `logs/requests.log` 写入一行 JSON：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 关联标识。客户端可用 `X-Trace-Id` 请求头传入（仅限 8–64 位字母数字/`-`/`_`，非法值会被替换为新生成的 id）；未传入时由服务端生成。响应头同样携带 `X-Trace-Id`。 |
| `method` / `path` | 请求方法与路径。 |
| `status` | 响应状态码。 |
| `duration_ms` | 请求总耗时（毫秒）。 |
| `slow` | 是否慢请求：`duration_ms >= slow_request_ms` 即为 `true`（含边界）。 |
| `slow_threshold_ms` | 判定慢请求时使用的阈值快照。 |
| `error_kind` | 异常分类：`none` / `business` / `validation` / `timeout` / `uncaught` / `downstream` / `trace_mismatch`。 |
| `stages` | 分阶段耗时列表 `[{"name", "duration_ms"}]`，由业务代码用 `timed_stage("name")` 记录，用于定位慢点。 |

### 异常分类规则

- `business`：抛出 `app.errors.BusinessError`（返回 409）。
- `validation`：FastAPI 参数校验失败（返回 422）。
- `timeout`：抛出 `app.errors.OperationTimeout`（返回 504）。
- `uncaught`：未被任何异常处理器捕获的异常（中间件兜底返回 500，body 含 `trace_id`）。
- `downstream`：下游调用连接失败或返回 5xx（上游返回 502）。
- `trace_mismatch`：下游回传的 `X-Trace-Id` 与发出的不一致（上游返回 502，
  body 含 `sent_trace_id` / `echoed_trace_id`）。
- `background`：后台任务执行失败（只出现在 `background` 片段与
  `/metrics` 的 `background_errors` 中，不影响已完成请求的响应）。

### 上下文传递

- 同步/异步处理：基于 `contextvars`，并发请求之间互不串号。
- 后台任务：用 `app.tasks.run_observed(name, coro_factory)` 调度，自动继承当前 trace，
  任务结束后写入 `background` 片段（含结果摘要），失败归类为 `background` 并计入指标，
  不会被悄悄吞掉；`app.propagate.run_in_background` 仍可用于不需要结果记录的简单场景。
- 对外调用：用 `app.propagate.trace_headers()` 获取要透传的请求头；
  `app.downstream_client.call_downstream()` 在此基础上还会校验下游回传的标识。
- 客户端传入的 trace id 只存入本请求的 context，校验失败即替换，不会影响其他请求。

## 跨服务链路（本地可复现）

`downstream.py` 是一个真实的本地下游服务（独立 FastAPI 应用，独立日志文件
`logs/downstream.log`，`service_name=downstream`），不依赖任何外网或凭据。

```bash
uv run python main.py --with-downstream   # 同进程起上游 :8000 + 下游 :8001
# 或者分开跑：uv run python downstream.py  （下游单独一个进程，:8001）
```

验证方式：

```bash
curl -s -H 'X-Trace-Id: demo-chain-00001' 'http://127.0.0.1:8000/relay?ms=30'
# => {"sent_trace_id":"demo-chain-00001","downstream":{"trace_id":"demo-chain-00001",...},"echo_matches":true}
grep demo-chain-00001 logs/downstream.log   # 下游自己的日志里能按同一标识查到
curl -s 'http://127.0.0.1:8000/traces/demo-chain-00001'   # 回放整条链
```

- `GET /relay` 用 httpx 真实调用下游 `GET /work`，请求头携带当前 `X-Trace-Id`；
  下游中间件接受该标识，处理日志与片段都落在同一标识下。
- 上游校验下游响应头里回传的 `X-Trace-Id` 与发出去的一致：不一致返回
  `502 {"error_kind": "trace_mismatch", "sent_trace_id", "echoed_trace_id"}`，
  并归类进指标与回放；下游连接失败/5xx 归类为 `downstream`。
- 并发请求各自携带自己的标识，上下游之间不会串号（有 20 线程并发用例覆盖）。

## 后台任务可观察

`POST /jobs?ms=50&fail=false` 在响应返回后继续执行后台任务：

- 任务通过 `run_observed` 调度，继承调度时的 trace context，并持有强引用，
  请求结束不会把任务弄丢。
- 任务结束后写入 `background` 片段：`detail.result` 为结果摘要（截断到 200 字符）；
  失败时 `error_kind=background`、`detail.error` 为异常信息，同时计入
  `/metrics` 的 `background_total` / `background_errors`，不会被吞掉。
- 片段与入口请求共享同一 trace id，回放时能与归属请求对上。

## 按标识回放 `GET /traces/{trace_id}`

把属于同一标识的入口请求、下游调用、后台任务片段合并成一条按开始时间排序的时间线：

| 字段 | 含义 |
| --- | --- |
| `found` | 是否找到该标识。 |
| `segment_count` / `segment_counts` | 片段总数与按 `kind`（`request`/`downstream`/`background`）的分布。 |
| `timeline` | 片段列表，每条含 `kind`/`service`/`name`/`started_at`/`duration_ms`/`error_kind`/`detail`。 |
| `started_at` / `total_duration_ms` | 链路起点与端到端总耗时（最晚结束 − 最早开始）。 |
| `slowest_segment` | 耗时最长的片段（`kind`/`service`/`name`/`duration_ms`），用于定位慢在哪一段。 |
| `errors` | 所有 `error_kind != none` 的片段来源（`kind`/`service`/`name`），异常来源一目了然。 |
| `truncated` / `limits` | 是否发生截断，以及当前上限配置。 |

边界行为：

- 标识不存在、或片段已因超出 `segment_max_traces` 被淘汰（等价于日志轮转清掉历史）
  时，返回 `404 {"detail": "trace not found or already evicted", "found": false}`，
  不会报错。
- 上限与截断：片段存于有界内存存储，两个维度防爆——`segment_max_traces`
  （默认 1000，超出淘汰最旧的整条 trace）与 `segment_max_per_trace`
  （默认 100，单条 trace 超出后新片段丢弃并置 `truncated=true`）。
  因此单个标识挂再多下游调用/任务，响应也不会超过 `segment_max_per_trace` 条。
  上限可运行中通过 `POST /config` 调整。
- 片段记录不受采样影响：即使请求日志被采掉，回放仍可用（片段是内存态，重启即清空）。

## 配置项

配置可在运行中通过 `POST /config` 修改（也可直接改 `app.config.settings_holder` /
`app.sampling.sampling_policy`），对之后的请求生效，行为可预期。

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `slow_request_ms` | 500 | 慢请求阈值（毫秒），`>=` 即判定为慢。 |
| `sample_rate` | 1.0 | 采样率，取值 `[0, 1]`，越界抛 `ValueError`。 |
| `log_dir` / `log_file` | `logs` / `requests.log` | 日志目录与文件名。 |
| `rotation_max_bytes` | 1048576 | 单文件最大字节数，超过即轮转。 |
| `rotation_backup_count` | 3 | 保留的历史文件个数。 |
| `service_name` | `upstream` | 片段与日志里标识本服务的名字。 |
| `downstream_url` | `http://127.0.0.1:8001` | `/relay` 调用的下游地址。 |
| `segment_max_traces` | 1000 | 回放存储最多保留的 trace 条数，超出淘汰最旧。 |
| `segment_max_per_trace` | 100 | 单条 trace 的片段上限，超出丢弃并标记截断。 |

### 采样策略

- `error_kind != none` 或 `slow == true` 的链路**永远保留**，不会被采掉。
- 其余请求按 `sample_rate` 随机保留；`0` 表示全部丢弃正常请求，`1` 表示全量保留。
- 运行中修改采样率只影响之后的判定，已完成的请求不受影响。
- 无论是否被采掉，每个请求都会计入 `/metrics`（`sampled_out` 字段记录被丢弃数），
  保证指标与日志结论一致。

### 日志轮转

`SafeRotatingFileHandler` 在 `emit` 外加锁，多线程/多任务并发写入时不会交错，
轮转检查与写文件串行化。注意：轮转会按 `backup_count` 淘汰最旧文件，
超出的历史内容会被删除（这是轮转的语义，不是丢失）。

## 验证方法

```bash
uv run pytest -v
```

测试覆盖（`tests/`）：

- `test_tracing.py`：trace id 生成/透传/非法替换、30 线程并发不串号、
  后台任务与对外调用头共享同一 trace。
- `test_classification.py`：四类异常分类、指标与日志结论一致。
- `test_slow.py`：慢请求标记与分阶段耗时、阈值边界、运行中改阈值生效。
- `test_sampling.py`：采样率 0/1 边界、错误与慢请求不被采掉、非法采样率拒绝、
  运行中改采样率行为可预期。
- `test_rotation.py`：8 线程 × 200 行并发写入不丢内容（不轮转时 1600/1600）、
  轮转时每一行都是完整 JSON（无交错）。
- `test_downstream.py`：跨服务标识一致（真实 HTTP 调用本地下游、下游日志同标识）、
  20 线程并发 relay 不串号、下游回错标识时上游返回 502 `trace_mismatch` 并归类、
  下游不可达归类为 `downstream`。
- `test_background.py`：后台任务结果按标识可查、失败归类进指标不吞掉、
  响应返回后任务不丢、并发任务各自归属正确。
- `test_replay.py`：入口+下游+后台合并为一条时间线（总耗时/最慢片段/异常来源）、
  未知标识与被淘汰标识都返回明确 404、单标识片段超限截断且有上限。

手动验证示例：

```bash
curl -i -H 'X-Trace-Id: demo-00000001' http://127.0.0.1:8000/orders \
     -X POST -H 'content-type: application/json' -d '{"item":"a","quantity":99}'
curl http://127.0.0.1:8000/slow?ms=600
curl http://127.0.0.1:8000/metrics
cat logs/requests.log
```

本地复现跨服务链路（一条命令起上下游，无需外网）：

```bash
uv run python main.py --with-downstream
curl -s -H 'X-Trace-Id: demo-chain-00001' 'http://127.0.0.1:8000/relay?ms=30'
curl -s -H 'X-Trace-Id: demo-chain-00001' -X POST 'http://127.0.0.1:8000/jobs?ms=80'
curl -s 'http://127.0.0.1:8000/traces/demo-chain-00001'   # 合并回放整条链
grep demo-chain-00001 logs/downstream.log                 # 下游日志按同标识可查
```

## 已知限制

- 指标为进程内存计数，重启即清零，多进程部署时各进程独立计数。
- 回放片段同样是进程内存态：重启即丢失；超出 `segment_max_traces` 的旧 trace
  会被淘汰，此时回放返回明确的 404（与日志轮转清掉历史等价）。
- 采样是无状态随机采样，不保证按 trace id 的哈希一致性（同一 id 在不同
  请求间可能被不同判定）；但片段记录不受采样影响，被采掉的请求仍可回放。
- 下游服务若作为独立进程运行（`python downstream.py`），其服务端片段在
  该进程自己的内存里，上游回放只能看到上游侧的 `downstream` 调用片段与下游
  日志文件；同进程运行（`--with-downstream`）时回放才能合并下游服务端片段。
- 请求级汇总日志只包含请求生命周期内已完成的阶段；后台任务的结果通过
  `background` 片段与回放体现，不回填到已写出的请求日志行。
- 日志轮转按文件大小触发，保留个数之外的历史内容会被删除。
- trace id 信任边界：客户端可伪造合法格式的 id，本服务只保证隔离与格式校验，
  不保证 id 的全局唯一性来源可信。
