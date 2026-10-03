# FastAPI 请求级链路追踪与可观测性

本服务内置一套请求级可观测性能力：每个请求拥有可追踪的关联标识（trace id），
在同步处理、后台任务与对外调用之间一致传递；异常按类型分类；慢请求按可配置
阈值标记并记录分阶段耗时；日志支持采样与并发安全的轮转；指标与日志对同一
请求的结论一致。

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
| `error_kind` | 异常分类：`none` / `business` / `validation` / `timeout` / `uncaught`。 |
| `stages` | 分阶段耗时列表 `[{"name", "duration_ms"}]`，由业务代码用 `timed_stage("name")` 记录，用于定位慢点。 |

### 异常分类规则

- `business`：抛出 `app.errors.BusinessError`（返回 409）。
- `validation`：FastAPI 参数校验失败（返回 422）。
- `timeout`：抛出 `app.errors.OperationTimeout`（返回 504）。
- `uncaught`：未被任何异常处理器捕获的异常（中间件兜底返回 500，body 含 `trace_id`）。

### 上下文传递

- 同步/异步处理：基于 `contextvars`，并发请求之间互不串号。
- 后台任务：用 `app.propagate.run_in_background(coro_factory)` 调度，自动继承当前 trace。
- 对外调用：用 `app.propagate.trace_headers()` 获取要透传的请求头。
- 客户端传入的 trace id 只存入本请求的 context，校验失败即替换，不会影响其他请求。

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

手动验证示例：

```bash
curl -i -H 'X-Trace-Id: demo-00000001' http://127.0.0.1:8000/orders \
     -X POST -H 'content-type: application/json' -d '{"item":"a","quantity":99}'
curl http://127.0.0.1:8000/slow?ms=600
curl http://127.0.0.1:8000/metrics
cat logs/requests.log
```

## 已知限制

- 指标为进程内存计数，重启即清零，多进程部署时各进程独立计数。
- 采样是无状态随机采样，不保证按 trace id 的哈希一致性（同一 id 在不同
  请求间可能被不同判定）。
- 后台任务在请求结束后仍写日志时，会使用任务自己继承的 context，
  但请求级汇总日志只包含请求生命周期内已完成的阶段。
- 日志轮转按文件大小触发，保留个数之外的历史内容会被删除。
- trace id 信任边界：客户端可伪造合法格式的 id，本服务只保证隔离与格式校验，
  不保证 id 的全局唯一性来源可信。
