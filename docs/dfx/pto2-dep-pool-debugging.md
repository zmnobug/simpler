# PTO2 dep_pool 调试日志说明

本文说明本轮为 issue 860 / 959 增加的 PTO2 scheduler
诊断方法，以及关键打印字段的含义。目标是把日志从
“host 侧 507018” 进一步还原到 AICPU runtime 内部到底卡在哪个环节。

## 使用方式

建议在 onboard 环境下同时打开 device log 和较详细的 runner 日志：

```bash
export ASCEND_PROCESS_LOG_PATH="$(pwd)/log"

task-submit --device auto \
  --run "python <case>/test_<case>.py --device {} --platform a2a3 \
  --log-level v0 --dump-tensor"
```

对 issue 860 的 replay，可把 `<case>/test_<case>.py` 替换为：

```text
_jit_attention_csa_test_refresh_20260526_103922/test_attention_csa_test_refresh.py
```

运行后主要看两类日志：

```text
log/debug/device-<id>/device-<pid>_<timestamp>.log
log/debug/plog/plog-<pid>_<timestamp>.log
```

device log 里能看到 PTO2 内部诊断。plog 里通常只能看到
CANN/Runtime 的外层传播结果，例如 `507018 [aicpu exception]`。

## 新增诊断点

### `DEP_POOL_BLOCK`

打印位置：

```text
src/{a2a3,a5}/runtime/tensormap_and_ringbuffer/runtime/scheduler/pto_scheduler.h
```

触发条件在 `drain_wiring_queue()`：

```text
1. scheduler 从 wiring queue 取到一个待 wire 的 task。
2. 该 task 的 fanin 数量为 wfanin。
3. dep_pool 当前剩余空间 available_before < wfanin。
4. scheduler 调用 dep_pool.reclaim(...) 尝试回收旧 entry。
5. 回收后 available_after 仍然 < wfanin。
6. 当前 task 无法完成 wiring，打印 DEP_POOL_BLOCK。
```

首次卡住时打印 `WARN`。同一个 task 和同一个
`last_task_alive` 连续卡住时，`block_count` 会递增。达到
`PTO2_DEP_POOL_SPIN_LIMIT` 后打印 `ERROR`，之后按间隔继续打印。

示例：

```text
DEP_POOL_BLOCK ring=2 task_id=8589935572 block_count=100000
wfanin=130 available_before=14 available_after=14
top=16371 tail=1 used=16370 high_water=16370 capacity=16384
last_task_alive=24 current_task_index=1312 in_flight=1288
wiring_batch=29/30 wiring_queue=684 fanin_refcount=0/0
```

字段含义：

| 字段 | 含义 | 如何解读 |
| --- | --- | --- |
| `ring` | 当前 task 所属 ring。 | 可用于区分是哪一层 ring_depth 卡住。 |
| `task_id` | 当前正在尝试 wire 的 task id。 | 长时间相同表示卡在同一个 task。 |
| `block_count` | 连续命中同一卡点的次数。 | 持续增长表示不是瞬时资源紧张。 |
| `wfanin` | 当前 task 需要写入的 fanin entry 数。 | 必须小于等于可用 dep_pool entry 才能继续。 |
| `available_before` | reclaim 前的 dep_pool 可用 entry 数。 | 小于 `wfanin` 才进入 reclaim。 |
| `available_after` | reclaim 后的 dep_pool 可用 entry 数。 | 仍小于 `wfanin` 就无法 wire。 |
| `top` | dep_pool 分配前沿。 | 和 `tail` 一起表示池内占用范围。 |
| `tail` | dep_pool 回收边界。 | 不前进表示 reclaim 没释放新空间。 |
| `used` | 当前已占用 entry 数。 | 接近 `capacity` 表示 dep_pool 近乎满。 |
| `high_water` | 本轮运行观察到的最高占用。 | 用于评估容量压力峰值。 |
| `capacity` | 当前 dep_pool 容量。 | 默认或实验值来自 `PTO2_DEP_LIST_POOL_SIZE`。 |
| `last_task_alive` | scheduler 认为仍需保留依赖的最早 task index。 | 不前进会阻止 dep_pool 回收旧 entry。 |
| `current_task_index` | ring 当前提交进度。 | 和 `last_task_alive` 比较可看 in-flight 压力。 |
| `in_flight` | `current_task_index - last_task_alive`。 | 值大表示未释放窗口很宽。 |
| `wiring_batch` | 当前本地 wiring batch 的处理位置。 | `29/30` 表示 index=29、total=30。 |
| `wiring_queue` | 仍在等待 wiring 的队列长度。 | 值大表示后续大量 task 还未 wire。 |
| `fanin_refcount` | 当前已满足 fanin / 期望 fanin。 | `0/0` 通常说明 slot 还没完成 wiring。 |

关键判断：

```text
available_after < wfanin
used 接近 capacity
block_count 持续增长
last_task_alive 长时间不变
wiring_queue 仍然很大
```

这组信号同时出现时，说明 scheduler 被卡在 dep_pool 空间不足和
reclaim 无法前进的闭环上。

### `state=UNWIRED`

打印位置：

```text
src/{a2a3,a5}/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_cold_path.cpp
```

过去 stall snapshot 里 `fanin_count=0` 容易被误解为
“零依赖 task 已经 READY”。现在显式分类为：

```text
state=UNWIRED fanin_refcount=0/0
```

含义是：

```text
orchestrator 已经把 task 提交到 ring，
但 scheduler 还没有从 wiring queue 把它接入依赖图。
```

这类 task 不是可调度 task。它没有进入 ready queue，也不会被派发到
AICore/AIV。它只能等待 scheduler wiring 继续推进。

示例：

```text
[STALL thread=0 idle_iterations=1000000000]
TASK ring=2 task_id=8589935572 state=UNWIRED
fanin_refcount=0/0 kernels=[aic:-1 aiv0:72 aiv1:-1]
```

字段含义：

| 字段 | 含义 |
| --- | --- |
| `thread` | 打印 stall 诊断的 scheduler thread。 |
| `idle_iterations` | 当前 thread 已连续 idle 的循环次数。 |
| `ring` | task 所属 ring。 |
| `task_id` | task id。 |
| `state=UNWIRED` | task 已提交但还没有完成 dependency wiring。 |
| `fanin_refcount=0/0` | 还没 wire 出有效 fanin 计数。 |
| `kernels` | task 关联的 aic / aiv kernel id。`-1` 表示无该类 kernel。 |

和其他状态的区别：

| 状态 | 含义 |
| --- | --- |
| `UNWIRED` | task 尚未接入依赖图，不能调度。 |
| `READY` | fanin 已满足，理论上可以进入 ready queue 或被派发。 |
| `WAIT` | 已 wire，但依赖尚未满足。 |
| `RUNNING` | 已派发，正在某个 core 上执行。 |

### `SUMMARY`

stall snapshot 末尾会打印整体扫描摘要：

```text
[STALL thread=0 idle_iterations=1000000000]
SUMMARY completed=1249/1934 last_progress_iteration=1226
scan_unwired=685 scan_ready=0 scan_waiting=0 scan_running=0
```

字段含义：

| 字段 | 含义 | 如何解读 |
| --- | --- | --- |
| `completed` | 已完成 task 数 / 总 task 数。 | 小于总数表示 runtime 未正常完成。 |
| `last_progress_iteration` | 最近一次有进度时的 completed 数。 | 长时间不变表示无新 task 完成。 |
| `scan_unwired` | 扫描到的 UNWIRED task 数。 | 高值说明大量 task 卡在 wiring 前。 |
| `scan_ready` | 扫描到的 READY task 数。 | 高值说明 ready queue 或 dispatch 路径可能有问题。 |
| `scan_waiting` | 扫描到的 WAIT task 数。 | 高值说明依赖未满足，可能是依赖链或 FIN 问题。 |
| `scan_running` | 扫描到的 RUNNING task 数。 | 高值说明还有 kernel 在跑或 FIN 未回收。 |

issue 860 关键模式是：

```text
completed=1249/1934
scan_unwired=685
scan_ready=0
scan_waiting=0
scan_running=0
```

这表示剩余 task 不是在运行，也不是在等待依赖完成，而是还没 wire。
如果同时看到所有 cluster 都 idle，就可以把方向收敛到
scheduler wiring / dep_pool reclaim。

### `CLUSTER`

stall snapshot 还会打印每个 scheduler thread 负责的 core 状态：

```text
CLUSTER cluster_id=2 aic=core2(idle)
aiv0=core28(idle) aiv1=core29(idle)
```

如果所有 cluster 都是 `idle`，说明没有 AICore/AIV kernel 正在跑。
这能排除“某个 kernel 计算太慢”这一类解释。

### `TIMEOUT_EXIT`

打印位置：

```text
src/{a2a3,a5}/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_cold_path.cpp
```

触发位置：

```text
src/{a2a3,a5}/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp
```

当前 scheduler 有两类退出保护：

```text
iteration_budget_elapsed = idle_iterations >= MAX_IDLE_ITERATIONS
wall_clock_budget_elapsed = now_ts - last_progress_ts > SCHEDULER_TIMEOUT_CYCLES
```

触发后会打印：

```text
TIMEOUT_EXIT after_idle_iterations=1000000000
```

含义是 Simpler PTO2 scheduler 自己判断已经长期无进度，主动进入
emergency shutdown。它不是 CANN op timeout 的日志。

错误传播链路是：

```text
TIMEOUT_EXIT
-> latch PTO2_ERROR_SCHEDULER_TIMEOUT
-> aicpu_execute returns rc=-100
-> simpler_aicpu_exec failed
-> CANN/Runtime reports 507018 [aicpu exception]
```

因此 host 侧看到 `507018` 时，要回 device log 看 `TIMEOUT_EXIT`
之前的 PTO2 诊断，不能只按 CANN timeout 理解。

### `SHUTDOWN_SNAPSHOT`

当某个 scheduler thread 触发 timeout exit 时，会先打印：

```text
SHUTDOWN_SNAPSHOT trigger_thread=2
reason=scheduler_timeout idle_iterations=1000000000
```

随后 dump 所有 scheduler thread 的 task 状态和 cluster 状态。
这份 snapshot 是定位根因的主要依据。

## 如何判断 dep_pool deadlock

可以按下面顺序读日志。

### 先找 `DEP_POOL_BLOCK`

用 `rg` 搜索：

```bash
rg -n "DEP_POOL_BLOCK" log/debug/device-*/device-*.log
```

如果看到同一个 `task_id` 的 `block_count` 持续增长，并且：

```text
available_after < wfanin
used 接近 capacity
tail / last_task_alive 长时间不变
```

说明 dep_pool 空间不足不是瞬时现象。

### 再看 stall summary

搜索：

```bash
rg -n "TIMEOUT_EXIT|SUMMARY|UNWIRED|CLUSTER" \
  log/debug/device-*/device-*.log
```

如果最后是：

```text
scan_unwired > 0
scan_ready = 0
scan_waiting = 0
scan_running = 0
所有 CLUSTER idle
```

说明系统不是被正在运行的 kernel 卡住，而是没有可运行 task，
剩余 task 都停在 wiring 前。

### 最后对照 plog

plog 里通常会看到：

```text
rtStreamSynchronizeWithTimeout: ErrCode=507018, desc=[aicpu exception]
Aicpu kernel execute failed ... funcName=simpler_aicpu_exec
```

这是 AICPU runtime 返回失败后的外层传播结果。对于 dep_pool 问题，
plog 不是根因证据，device log 才是。

## issue 860 的典型日志解读

典型 `DEP_POOL_BLOCK`：

```text
DEP_POOL_BLOCK ring=2 task_id=8589935572 block_count=100000
wfanin=130 available_before=14 available_after=14
top=16371 tail=1 used=16370 high_water=16370 capacity=16384
last_task_alive=24 current_task_index=1312 in_flight=1288
wiring_batch=29/30 wiring_queue=684 fanin_refcount=0/0
```

逐句解释：

```text
当前 task 需要 130 个 dep_pool entry。
dep_pool 只剩 14 个 entry。
调用 reclaim 后仍然只有 14 个 entry。
dep_pool 已使用 16370 / 16384，几乎满。
last_task_alive 停在 24，tail 没有推进。
后面还有 684 个 task 等着 wiring。
当前 slot 的 fanin_refcount=0/0，说明还没 wire 成有效 task。
```

最终 snapshot：

```text
TIMEOUT_EXIT after_idle_iterations=1000000000
SUMMARY completed=1249/1934
scan_unwired=685 scan_ready=0 scan_waiting=0 scan_running=0
```

结论：

```text
full tensor dump 放大了 scheduler 热路径上的耗时和 in-flight 压力。
默认 dep_pool 容量下，wiring 需要更多 dependency entry，
但 reclaim 无法释放足够 entry。
大量 task 保持 UNWIRED，scheduler 没有可运行 task。
最后由 Simpler scheduler timeout 退出，host 侧表现为 507018。
```

## 参数与日志的关系

这些日志是诊断手段，不等价于修复。常见参数如下：

| 参数 | 作用 | 对日志的影响 |
| --- | --- | --- |
| `PLATFORM_MAX_IDLE_ITERATIONS` | scheduler idle-iteration 上限。 | 决定何时打印 `TIMEOUT_EXIT after_idle_iterations=...`。 |
| `SCHEDULER_TIMEOUT_MS` | scheduler wall-clock stall 上限。 | wall-clock 超过时也会进入 `TIMEOUT_EXIT`。 |
| `PTO2_DEP_LIST_POOL_SIZE` | 每个 ring 的 dep_pool 容量。 | 影响 `capacity`、`used/capacity` 和是否触发 `DEP_POOL_BLOCK`。 |
| `PTO2_DEP_POOL_SPIN_LIMIT` | dep_pool block 诊断升级阈值。 | 达到该值后 `DEP_POOL_BLOCK` 从 WARN 升为 ERROR。 |

本轮实验中，原始 `PTO2_DEP_LIST_POOL_SIZE` 为 `16384`。
当前工作区的临时实验值为 `32768`。此前验证过 `65536` 可以绕过
issue 860 replay 的该卡点，但这属于 workaround，不是根因修复。

判断修复是否真正解决问题，应看：

```text
是否不再出现持续增长的 DEP_POOL_BLOCK。
是否不再有大量 UNWIRED task 留在 stall snapshot 中。
是否 completed 达到 total。
是否 tensor_dump.json 和 tensor_dump.bin 正常生成。
```

如果只是把容量调大后通过，只能说明容量放大绕过了当前 replay 的压力点；
仍需要检查 `drain_wiring_queue()`、`dep_pool.reclaim()`、
`last_task_alive` 和 `dep_pool_mark` 的闭环。

## 和 CANN timeout 的区别

CANN op timeout 日志形如：

```text
Config OP execute timeout success, enable[1] timeout[3000s]
```

或 host/plog 里的：

```text
aclrtSetOpExecuteTimeOutV2 ... actual timeout = ...
```

这些表示 CANN 外层 op 执行保护。PTO2 的：

```text
TIMEOUT_EXIT after_idle_iterations=...
DEP_POOL_BLOCK ...
SUMMARY ...
```

是 Simpler AICPU runtime 内部诊断。issue 860 当前定位里，关键证据是
PTO2 内部的 dep_pool / UNWIRED 日志；CANN `507018` 是失败传播结果。
