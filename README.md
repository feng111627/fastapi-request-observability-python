# FastAPI 请求级链路追踪与可观测性

在本地即可复现每个请求的完整链路、慢点位置和异常来源。

## 快速开始

```bash
uv sync
uv run python main.py          # 默认 127.0.0.1:8000，PORT 环境变量可改
uv run pytest tests/ -q        # 运行测试，末尾输出业务覆盖与用例结果汇总
```

## 追踪字段含义

每个请求产生一条 JSON 日志（`logs/app.log`），字段如下：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 链路关联标识。客户端可经 `X-Request-ID` 传入（须为 8–64 位字母数字/`-`/`_`，否则丢弃重新生成）；未传入时由服务端生成。同步处理、后台任务、对外调用共用同一标识，并回写到响应头 `X-Request-ID`。 |
| `method` / `path` | 请求方法与路径。 |
| `status_code` | 响应状态码。 |
| `duration_ms` | 请求总耗时（毫秒）。 |
| `stage_ms` | 分阶段耗时：`receive_ms`（请求体接收）、`app_ms`（路由与业务处理）、`send_ms`（响应发送），用于定位慢点位置。 |
| `error_class` | 异常分类：`none` / `business`（业务异常，4xx）/ `validation`（参数校验失败，422）/ `timeout`（超时，504）/ `uncaught`（未捕获异常，500）。 |
| `is_slow` | 是否慢请求：`duration_ms >= slow_threshold_ms`。 |
| `slow_threshold_ms` | 判定该请求时生效的慢请求阈值。 |

错误响应体同样携带 `error_class` 与 `trace_id`，可直接与日志对拍。

## 配置项

`observability.config.ObservabilityConfig`，经 `ConfigStore.update(...)` 在运行中整体替换（不可变快照），变更只影响之后的请求，行为可预期：

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `slow_threshold_ms` | 500 | 慢请求阈值（毫秒）。 |
| `sample_rate` | 1.0 | 采样率 0.0–1.0，仅作用于普通请求；**错误链路与慢请求永远保留**。采样按 `trace_id` 哈希取桶，同一标识决策稳定。 |
| `trace_header` | `X-Request-ID` | 链路标识请求/响应头名。 |
| `log_file` | `logs/app.log` | 日志文件路径。 |
| `log_max_bytes` | 10MB | 单个日志文件大小上限，超过即轮转。 |
| `log_backup_count` | 5 | 轮转保留的历史文件个数。 |

采样只影响日志输出；指标（`GET /metrics`）始终全量记录，且与日志消费同一条
`RequestRecord`，对同一请求的分类与慢标记结论必然一致。

## 验证方法

```bash
uv run pytest tests/ -q
```

测试覆盖：并发链路隔离（50 并发互不串号、客户端标识不污染其他请求）、
异常四分类、慢请求阈值与运行中变更、采样边界（0 / 0.5 / 1、错误必保留、
运行中改采样率）、日志并发写入与轮转。输出末尾的"业务覆盖与用例结果汇总"
列出每个用例覆盖的业务点与 PASS/FAIL 结果。

手工验证：

```bash
curl -H 'X-Request-ID: demo-0001' http://127.0.0.1:8000/        # 标识透传
curl http://127.0.0.1:8000/boom                                 # uncaught
curl 'http://127.0.0.1:8000/biz?fail=true'                      # business
curl 'http://127.0.0.1:8000/items/1?q=ab'                       # validation
curl http://127.0.0.1:8000/timeout                              # timeout
curl 'http://127.0.0.1:8000/slow?ms=600'                        # 慢请求
curl http://127.0.0.1:8000/outbound                             # 对外调用传播
curl http://127.0.0.1:8000/metrics                              # 指标快照
tail -f logs/app.log                                            # JSON 链路日志
```

## 已知限制

- **多进程日志轮转不安全**：`RotatingFileHandler` 的线程锁只保证同一进程内
  并发写入不丢内容、不交错；多进程（如 `uvicorn --workers N`）写同一文件需
  改用 `ConcurrentRotatingFileHandler` 或交由外部日志收集（stdout + agent）。
- **未捕获异常的响应头缺 `X-Request-ID`**：此类响应由最外层
  `ServerErrorMiddleware` 直接发出，不经过追踪中间件的响应头注入；但响应体
  与日志中仍有 `trace_id`，不影响排查。
- **指标为进程内实现**：重启即丢失，多副本部署需自行汇总或替换为
  Prometheus 等外部指标系统。
- **后台任务需显式绑定上下文**：用 `bind_to_current_context(fn)` 包装后再
  加入 `BackgroundTasks`；跨进程任务队列（Celery 等）需自行透传 `trace_id`。
- **超备份数的轮转会丢弃最旧文件**：这是轮转的预期行为，需按日志量合理
  设置 `log_max_bytes` 与 `log_backup_count`。
