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

每个请求结束时都会向 `logs/requests.log` 写入一行 JSON 入口记录：被采样采掉细节的
正常请求写的是精简锚点（`sampled=false`），其余为全量记录（`sampled=true`）：

| 字段 | 含义 |
| --- | --- |
| `trace_id` | 关联标识。客户端可用 `X-Trace-Id` 请求头传入（仅限 8–64 位字母数字/`-`/`_`，非法值会被替换为新生成的 id）；未传入时由服务端生成。响应头同样携带 `X-Trace-Id`。 |
| `method` / `path` | 请求方法与路径。 |
| `status` | 响应状态码。 |
| `duration_ms` | 请求总耗时（毫秒）。 |
| `slow` | 是否慢请求：`duration_ms >= slow_request_ms` 即为 `true`（含边界）。 |
| `slow_threshold_ms` | 判定慢请求时使用的阈值快照。 |
| `error_kind` | 异常分类：`none` / `business` / `validation` / `timeout` / `propagation` / `uncaught`。 |
| `sampled` | 入口片段是否保留全量细节：`true` 为采样保留（或报错/慢请求强制保留），`false` 为被采样率采掉细节后留下的**精简锚点**（见"采样与回放"）。下游调用、后台任务片段不带此字段（它们始终全量记录）。 |
| `stages` | 分阶段耗时列表 `[{"name", "duration_ms"}]`，由业务代码用 `timed_stage("name")` 记录，用于定位慢点。**仅在 `sampled=true` 的入口记录中出现。** |
| `service` | 产生该事件的服务名（`upstream` / `downstream`）。 |
| `seq` / `ts_ms` | 进程内单调递增序号与 epoch 毫秒时间戳，用于把同标识的多个事件合并排序成一条时间线。 |

除入口请求（`msg=request`）外，日志里还有两类片段，都带同一个 `trace_id`：

- `msg=downstream_call`：一次下游调用，含 `trace_sent`、`trace_received`、`matched`、
  `downstream_status`、`duration_ms`。
- `msg=background_task`：一个后台任务结束，含 `name`、`status`（`success`/`failed`）、
  `error_kind`、`error`、`result`、`duration_ms`。

### 异常分类规则

- `business`：抛出 `app.errors.BusinessError`（返回 409）。
- `validation`：FastAPI 参数校验失败（返回 422）。
- `timeout`：抛出 `app.errors.OperationTimeout`（返回 504）。
- `propagation`：下游调用回传的 trace id 与发出的不一致（`TracePropagationError`，返回 502）。
- `uncaught`：未被任何异常处理器捕获的异常（中间件兜底返回 500，body 含 `trace_id`）。

### 上下文传递

- 同步/异步处理：基于 `contextvars`，并发请求之间互不串号。
- 后台任务：用 `app.tasks.run_in_background(coro_factory, name=...)` 调度，自动继承当前 trace。
- 对外调用：用 `app.propagate.trace_headers()` 获取要透传的请求头，或直接用
  `app.traced_http.call_downstream()` 发起带校验的调用。
- 客户端传入的 trace id 只存入本请求的 context，校验失败即替换，不会影响其他请求。

## 跨服务链路验证（本地、离线可复现）

`GET /chain` 在处理请求时**真的发起一次 HTTP 调用**到本地的下游服务
（`app/downstream.py`，`/echo`），不依赖外网、真实凭据或付费服务：

1. 上游把当前 trace id 放进 `X-Trace-Id` 请求头发出；
2. 下游在自己的中间件里接收该 id，用它处理请求、写入**下游自己的日志**，
   并在响应体中回显（`received_trace_id` / `trace_id` / `header_trace_id`）；
3. 上游比对 `trace_received == trace_sent`：
   - 一致：返回 `matched: true`，并记录 `downstream_call` 片段；
   - 不一致：记一次 `trace_mismatches` 指标，抛 `TracePropagationError`，
     归类为 `propagation`（HTTP 502），错号被明确暴露而不是静默合并。
     `GET /chain?lie=true` 可让下游故意回显一个错误 id 来验证这条路径。

下游调用默认走**进程内隔离 ASGI 传输**（`IsolatedASGITransport`）：下游 app 运行在
全新的 `contextvars.Context` 里，id 只能通过请求头跨过去，排除"共享 context 变量
导致的假透传"。也可以用真实双进程（两个本地端口）验证，见下文"本地复现"。

下游自己的日志与上游写到同一个 JSON 日志文件时（演示配置），可直接按 trace id 查到
下游服务（`service=downstream`）的入口记录；双进程部署时则查下游进程自己的日志文件。

### 可观察的后台任务

`GET /background?fail=...` 调度的任务由 `app.tasks` 统一托管：

- 在复制的 context 中运行，响应返回后继续执行，且任务对象有强引用集合持有，
  请求结束不会丢任务；
- 无论成功失败都写一条 `background_task` 日志片段，结果按 trace id 存入有界内存存储；
- 失败按异常类型归类（名称含 timeout → `timeout`，其余 → `uncaught`）并计入
  `/metrics` 的 `background_total` / `background_failed` / `errors`，不会被静默吞掉；
- `GET /tasks/{trace_id}` 可按归属请求的标识查询任务结果（含结果文本或错误信息、耗时）。

## 按标识回放整条链路

`GET /replay/{trace_id}` 从当前日志文件及保留的轮转备份中，把属于该标识的
**入口请求、下游调用、后台任务**片段合并为一条按 `(ts_ms, seq)` 排序的时间线。

返回字段：

| 字段 | 含义 |
| --- | --- |
| `found` | 是否找到片段。`false` 时不报错，HTTP 200。 |
| `reason` | `found=false` 时的原因，见下表；只有真正发生过的事情才会报轮转。 |
| `segments_returned` | 本次实际返回的片段数。 |
| `segments_truncated` | 因超过上限被截断、未放入响应的片段数。 |
| `limits` | 本次生效的上限（见下）。 |
| `timeline` | 合并后的片段数组；每个片段含 `kind`（`entry`/`downstream`/`background`）、`service`、`seq`、`ts_ms` 及各自字段。入口片段另有 `sampled`（`true`/`false`）。 |
| `summary.total_duration_ms` | 上游入口请求总耗时。 |
| `summary.slow` / `slowest_stage` | 是否慢请求、最慢阶段名与耗时（慢在哪一段；被采掉细节的锚点入口没有 stages，此时最慢阶段为 `null`）。 |
| `summary.entry_error_kind` / `error_kinds` | 入口异常分类，以及时间线中出现过的全部异常来源分类（异常来自哪一类）。 |
| `summary.downstream_calls` / `downstream_mismatches` | 下游调用数与错号数。 |
| `summary.background_tasks` / `background_failed` | 后台任务数与失败数。 |

查不到（`found=false`）时 `reason` 的准确含义：

| reason | 含义 | 是否发生过轮转 |
| --- | --- | --- |
| `unknown` | 本进程**从未写过**带该标识的任何日志行（标识拼错、别的系统/进程的标识、或时间太久已超出"见过的标识"记忆上限）。日志文件可能完好，不能赖轮转。 | 否（或无法判断） |
| `rotated_away` | 本进程**确实写过**带该标识的日志行，但当前文件和所有保留的轮转备份里都已扫描不到——这些记录是被日志轮转按 `backup_count` 淘汰删除的。 | 是 |

判定依据是"**真正写入**日志的标识"集合（在事件成功落盘那一刻登记），而不是"进过本进程"的请求集合。因此被采样采掉细节的正常请求仍有锚点落盘、能正常回放；只有锚点也被轮转删除时才会报 `rotated_away`，没发生轮转绝不会误报。

上限 / 截断规则（`app/replay.py` 的 `ReplayLimits`，防止一个标识下片段过多撑爆
内存或响应）：

- `max_segments=200`：时间线最多 200 个片段，超出的只计数（`segments_truncated`），
  不读入内存；扫描日志时先用字符串包含快速过滤。
- `max_stages=50`：单个入口片段的 `stages` 最多 50 个，超出数量记在
  `stages_truncated`。
- `max_field_chars=512`：后台任务的 `error`/`result` 文本最多 512 字符，超出截断并
  标注多出多少字符。
- `max_files=10`：最多扫描当前文件 + 最近 9 个轮转备份。
- 后台任务结果存储另有总量 2000 条 / 单标识 100 条的上限（`TaskResultStore`），
  超出按最旧优先淘汰。

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

### 采样策略与采样下的回放

采样只裁剪正常请求的**细节**，从不删除链路本身：

- `error_kind != none` 或 `slow == true` 的链路**永远保留全量记录**（`sampled=true`，含 stages），不会被采掉。
- 其余正常请求按 `sample_rate` 随机保留全量记录（`sampled=true`）；被采掉时（`0` 表示全部正常请求都只留锚点，`1` 表示全量保留）仍写一条**精简锚点**（`msg=request, sampled=false`）：包含 `trace_id`、`method`、`path`、`status`、`duration_ms`、`slow`、`slow_threshold_ms`、`error_kind`、`service`、`seq`、`ts_ms`，但**不含 stages 等分阶段细节**。
- 下游调用片段（`downstream_call`）和后台任务片段（`background_task`）**始终全量记录**，与入口是否被采样无关。
- 运行中修改采样率只影响之后的判定，已完成的请求不受影响。
- 无论是否被采掉细节，每个请求都会计入 `/metrics`（`sampled_out` 按原口径记录被采掉细节的正常请求数），保证指标与日志结论一致。

**开启采样后的回放行为**：即使某个正常请求被采掉细节，拿它的 `trace_id` 回放仍能得到完整链路——入口锚点（`timeline` 中 `kind=entry, sampled=false`）、它触发的下游调用（`downstream`）、它挂的后台任务（`background`）都在，时间线排序、汇总计数（下游调用数、错号数、后台任务数/失败数）也都正常。区别仅在于锚点入口没有 stages，`summary.slowest_stage` 为 `null`；入口的 `status`、`duration_ms`、`slow`、`error_kind` 等结论字段仍可查。

**开启采样后的跨服务查询**：上游把 `X-Trace-Id` 透传给下游，下游在同一标识下留下的入口片段（`service=downstream`）与上游的下游调用片段都照常写入、照常可按同一标识查到；演示配置下二者写同一日志文件可直接合并，双进程时查下游进程自己的日志。采样不会让跨服务这条线断掉。进程内下游与上游各自独立做采样判定，互不影响（真实双进程同理）。

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
- `test_cross_service.py`：下游收到同一标识、下游日志可按标识查到、错号时
  返回 502 且指标/片段记录 `matched=false`、20 线程并发不错号。
- `test_background.py`：任务在响应后产出可按标识查询的结果、失败被归类统计
  且不被吞掉、任务进入回放时间线、15 个并发任务各自归属正确。
- `test_replay.py`：三类片段合并成有序时间线与汇总、标识不存在返回 `unknown`、
  轮转清理后返回 `rotated_away`、片段超限时截断计数、慢标记与最慢阶段。
- `test_classification.py`：四类（现为五类）异常分类、指标与日志结论一致。
- `test_slow.py`：慢请求标记与分阶段耗时、阈值边界、运行中改阈值生效。
- `test_sampling.py`：采样率 0/1 边界、错误与慢请求不被采掉（仍写全量记录）、
  被采掉的正常请求只留 `sampled=false` 锚点且锚点不含 stages、指标
  `sampled_out` 口径不变、非法采样率拒绝、运行中改采样率行为可预期。
- `test_sampled_replay.py`：采样率 0 时正常请求的入口锚点 + 下游调用 + 后台任务
  仍可按标识完整回放；跨服务下游片段在同一标识下仍可查；从未存在的标识在采样流量
  之后仍报 `unknown`（不误报轮转）；确实写过又被轮转删除的标识报 `rotated_away`；
  20 个并发（10 线程）采样请求的片段互不串号。
- `test_rotation.py`：8 线程 × 200 行并发写入不丢内容（不轮转时 1600/1600）、
  轮转时每一行都是完整 JSON（无交错）。

手动验证示例（单进程，下游为进程内隔离调用）：

```bash
uv run python main.py          # 监听 127.0.0.1:8000

curl -i -H 'X-Trace-Id: demo-00000001' http://127.0.0.1:8000/orders \
     -X POST -H 'content-type: application/json' -d '{"item":"a","quantity":99}'

# 1) 跨服务：真实调用下游并校验回传标识
curl -H 'X-Trace-Id: chain-demo-00001' http://127.0.0.1:8000/chain
curl -i 'http://127.0.0.1:8000/chain?lie=true' -H 'X-Trace-Id: chain-demo-0002'  # 期望 502 propagation

# 2) 后台任务：响应后继续跑，失败也可查
curl 'http://127.0.0.1:8000/background?fail=true' -H 'X-Trace-Id: bg-demo-000001'
curl http://127.0.0.1:8000/tasks/bg-demo-000001

# 3) 按标识回放整条链路
curl http://127.0.0.1:8000/replay/chain-demo-0001
curl http://127.0.0.1:8000/replay/no-such-id                 # found=false, reason=unknown
curl http://127.0.0.1:8000/metrics
cat logs/requests.log

# 4) 开启采样后回放：把正常请求细节全部采掉，链路仍可完整回放
curl -X POST http://127.0.0.1:8000/config -H 'content-type: application/json' \
     -d '{"sample_rate": 0.0}'
curl -H 'X-Trace-Id: sampled-chain-001' http://127.0.0.1:8000/chain
curl -H 'X-Trace-Id: sampled-bg-000001' 'http://127.0.0.1:8000/background?fail=true'
sleep 0.2
curl http://127.0.0.1:8000/replay/sampled-chain-001        # entry(sampled=false)+downstream
curl http://127.0.0.1:8000/replay/sampled-bg-000001         # entry 锚点 + failed 后台任务
curl http://127.0.0.1:8000/replay/no-such-id                # 仍是 unknown，不是 rotated_away
grep sampled-chain-001 logs/requests.log                    # 锚点 + 下游调用都在，无 stages
curl -X POST http://127.0.0.1:8000/config -H 'content-type: application/json' \
     -d '{"sample_rate": 1.0}'
```

### 本地双进程复现（真实 HTTP 套接字）

```bash
# 终端 A：下游服务
uv run python -m app.downstream        # 127.0.0.1:8001

# 终端 B：上游服务，通过环境变量指向真实下游
DOWNSTREAM_BASE_URL=http://127.0.0.1:8001 uv run python main.py   # 127.0.0.1:8000

# 终端 C
curl -H 'X-Trace-Id: real-proc-0001' http://127.0.0.1:8000/chain
curl -s 'http://127.0.0.1:8000/chain?lie=true' -H 'X-Trace-Id: real-proc-0002'
grep real-proc-0001 logs/requests.log   # 上游片段；下游片段在下游进程的日志文件里
```

## 已知限制

- 指标为进程内存计数，重启即清零，多进程部署时各进程独立计数。
- 采样是无状态随机采样，不保证按 trace id 的哈希一致性（同一 id 在不同
  请求间可能被不同判定）。
- 后台任务在请求结束后仍写日志时，使用任务自己继承的 context；请求级汇总日志
  只包含请求生命周期内已完成的阶段，后台任务作为独立片段参与回放。
- 后台任务结果存储是进程内内存结构（有总量/单标识上限，最旧优先淘汰），
  重启后丢失；持久化查询以日志文件为准。
- 回放的 `rotated_away` 判定依赖进程内"**真正写入过日志**的标识"有界集合
  （默认 1 万个，LRU 淘汰）：标识对应的日志行很久以前写入、已被该集合淘汰时，
  会回退报 `unknown`；跨进程也无法判断，会报 `unknown`。回放本身只读日志，
  不依赖该集合。该集合只在事件成功落盘后登记，因此没发生轮转绝不会误报轮转。
- 进程内下游（默认）与上游共享日志文件以便演示；真实双进程各自写各自的日志，
  跨进程的完整时间线需要先汇集日志。
- 日志轮转参数在 handler 创建时固定，运行中修改需重建 logger
  （`app.logging_setup.reset_loggers()`，测试使用）；轮转会按 `backup_count`
  淘汰最旧文件，超出的历史内容会被删除（这是轮转的语义，不是丢失）。
- trace id 信任边界：客户端可伪造合法格式的 id，本服务只保证隔离与格式校验，
  不保证 id 的全局唯一性来源可信。
