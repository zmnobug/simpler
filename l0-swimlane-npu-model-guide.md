# L0 Swimlane NPU Model 架构说明

## 1. 一句话总结

`l0-swimlane-npu-model` 是在现有 Simpler L2 swimlane profiling 链路上，
补一层 AICore L0 视图。

它的目标不是替代 MindStudio Insight，也不是做一个独立的单 kernel replay
工具，而是让一次正常的 Simpler workload 运行额外产出一个 host 侧
`swimlane_converter` 可读的 L0 artifact：

```text
l2_perf_records.json
        +
l0-swimlane-npu-model.json
        +
tensor_dump/tensor_dump.json
        |
        v
merged_swimlane.json   # Perfetto / Chrome Trace JSON
```

当前实现可以把三类信息串起来：

- L2：task 是什么时候 dispatch、什么时候 finish 的。
- L0：这个 task 在 AICore 侧经历了哪些生命周期阶段。
- dump args：这个 task 当时看到的 tensor/scalar 参数是什么。

所以它解决的是“在真实 workload 时间线里定位问题 task”的问题。

## 2. 原始需求怎么理解

最初需求可以拆成三句话：

> 在 #2a 提供的 dump args 之上，实现核内事件采集、组织、落盘成
> host 侧 swimlane converter 可读的形式。参考 insight-trace skill，
> 但不是照搬实现。

这里有三个关键点：

1. **基于 dump args**
   - L0 数据要能和真实运行时的参数关联，而不是只靠手写或合成 replay 参数。

2. **host 侧 swimlane converter 可读**
   - 输出应该进入现有 `swimlane_converter`，最后落到 `merged_swimlane.json`。

3. **参考 Insight Trace，但不是照搬**
   - Insight Trace 可以作为“更深核内分析”的参考，但它的工作流和输出是
     MindStudio / msprof simulator 体系，不是 Perfetto swimlane 体系。

当前方案就是围绕这三点设计的：在正常 workload 运行链路里采集 L0 信息，
单独落盘成 sibling artifact，再由 converter 合并进 Perfetto。

## 3. 总体架构

整体分成四层：

```text
┌─────────────────────────────────────────────────────────────────┐
│ 用户 workload / SceneTest case                                  │
│   --enable-l2-swimlane [--dump-tensor]                           │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ Runtime 采集层                                                   │
│   AICore executor 记录 task lifecycle 时间戳                     │
│   AICPU collector 拷贝已提交 record                              │
│   Host L2PerfCollector 导出 L2 + L0 artifact                     │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ Host 侧原始产物                                                  │
│   l2_perf_records.json                                           │
│   l0-swimlane-npu-model.json                                     │
│   tensor_dump/tensor_dump.json                                   │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ swimlane_converter                                               │
│   合并 L2 timeline、L0 phases、dump args                         │
│   输出 merged_swimlane.json                                      │
└───────────────────────────────┬─────────────────────────────────┘
                                │
┌───────────────────────────────▼─────────────────────────────────┐
│ Perfetto UI                                                      │
│   AICore View                                                    │
│   AICPU View / Scheduler View                                    │
│   AICore L0 View                                                 │
└─────────────────────────────────────────────────────────────────┘
```

核心架构选择是：**L0 作为 L2 的 sibling artifact，而不是直接改
`l2_perf_records.json` 的 schema。**

这样做的原因：

- 不破坏现有 L2 工具链。
- 没有 L0 文件时，converter 仍然可以按原逻辑渲染 L2。
- L0 schema 后续可以独立演进。
- 未来要加更细粒度核内事件时，不需要把 L2 task record 变成混合大 schema。

## 4. 当前 L0 事件模型

当前实现采集的是 task 内部的粗粒度生命周期阶段：

```text
ack          : ack_time -> start_time
compute      : start_time -> compute_end_time
dump_barrier : compute_end_time -> barrier_end_time
```

这些不是 instruction-level 事件，也不是 TPUSH/TPOP/DMA 级别事件。
它们是 AICore executor 边界上的 task lifecycle marker。

为什么先做这一层：

- 打点位置稳定，不需要侵入每个 kernel 内部。
- 开销和风险较低。
- 可以和现有 `task_id` 对齐。
- 立刻能在 Perfetto 中看到 task 时间花在哪里：
  - ACK 相关开销
  - 主 compute 时间
  - dump barrier 等待时间

在 Perfetto 里，当前会长这样：

```text
AICore L0 View
  func_0_a(t0)              # L0 parent event
  func_0_a.ack(t0)          # phase event
  func_0_a.compute(t0)      # phase event
  func_0_a.dump_barrier(t0) # phase event
```

如果有 func id 到算子名的映射，会显示得更像算子：

```text
add(t0)
add.compute(t0)
```

没有映射时，converter 使用稳定 fallback：

```text
func_0_a(t0)
func_1_b(r1t0)
```

## 5. 关联模型

这套架构依赖几个稳定 ID：

| 字段 | 作用 |
|---|---|
| `task_id` | L2、L0、dump args 的主关联 key |
| `subtask_id` | 区分 AIC/AIV 子任务槽位 |
| `func_id` | 关联 kernel/operator 显示名 |
| `core_id` | 决定事件放到哪条 L0 lane |
| `core_type` | 区分 AIC/AIV 展示 lane |

raw L0 artifact 里会带：

```json
"args_key": {
  "task_id": 0,
  "subtask_id": 1
}
```

Perfetto L0 父事件和 phase 子事件的 `args` 里也会带：

- `taskId`
- `funcId`
- `coreId`
- `subtaskId`

dump args 只挂在 L0 parent event 上，不重复挂在每个 phase 上。

这样语义更清楚：

- parent event 表示 task 上下文。
- phase event 表示 task 内时间拆分。

## 6. Raw Artifact 约定

当前 L0 原始文件是：

```text
outputs/<case>_<timestamp>/l0-swimlane-npu-model.json
```

schema version 当前为 `1`：

```json
{
  "version": 1,
  "time_unit": "us",
  "args_manifest": "tensor_dump/tensor_dump.json",
  "events": [
    {
      "name": "kernel",
      "task_id": 0,
      "func_id": 0,
      "core_id": 7,
      "core_type": "aiv",
      "subtask_id": 1,
      "start_time_us": 88.460,
      "end_time_us": 500.000,
      "duration_us": 411.540,
      "args_key": {
        "task_id": 0,
        "subtask_id": 1
      },
      "phases": [
        {
          "name": "ack",
          "start_time_us": 88.140,
          "end_time_us": 88.460,
          "duration_us": 0.320
        },
        {
          "name": "compute",
          "start_time_us": 88.460,
          "end_time_us": 499.900,
          "duration_us": 411.440
        },
        {
          "name": "dump_barrier",
          "start_time_us": 499.900,
          "end_time_us": 500.000,
          "duration_us": 0.100
        }
      ]
    }
  ]
}
```

兼容策略：

- 新代码只生成 `l0-swimlane-npu-model.json`。
- converter 仍兼容读取旧文件名 `l0_swimlane_records.json`。

## 7. Runtime 侧改动

### 7.1 扩展 L2PerfRecord

`L2PerfRecord` 新增 AICore lifecycle 时间戳：

```cpp
uint64_t ack_time;
uint64_t compute_end_time;
uint64_t barrier_end_time;
```

这个字段布局很关键：这些字段和 `task_id` 都保留在 AICore 写入并 flush、
AICPU invalidate 的首个 cache line 里，避免破坏现有
`SINGLE_CACHE_LINE` 同步语义。

相关文件：

- [src/a2a3/platform/include/common/l2_perf_profiling.h](/data/zhaomin/simpler/src/a2a3/platform/include/common/l2_perf_profiling.h)
- [src/a5/platform/include/common/l2_perf_profiling.h](/data/zhaomin/simpler/src/a5/platform/include/common/l2_perf_profiling.h)

### 7.2 AICore executor 打点

AICore executor 在稳定边界记录时间：

```text
ack_time          = ACK write 前
start_time        = ACK write 后
compute_end_time  = execute_task(...) 后
barrier_end_time  = optional dump barrier 后
end_time          = task 最终结束
```

相关文件：

- [src/a2a3/runtime/tensormap_and_ringbuffer/aicore/aicore_executor.cpp](/data/zhaomin/simpler/src/a2a3/runtime/tensormap_and_ringbuffer/aicore/aicore_executor.cpp)
- [src/a2a3/runtime/host_build_graph/aicore/aicore_executor.cpp](/data/zhaomin/simpler/src/a2a3/runtime/host_build_graph/aicore/aicore_executor.cpp)
- [src/a5/runtime/tensormap_and_ringbuffer/aicore/aicore_executor.cpp](/data/zhaomin/simpler/src/a5/runtime/tensormap_and_ringbuffer/aicore/aicore_executor.cpp)
- [src/a5/runtime/host_build_graph/aicore/aicore_executor.cpp](/data/zhaomin/simpler/src/a5/runtime/host_build_graph/aicore/aicore_executor.cpp)

### 7.3 AICPU collector 拷贝字段

AICPU 从 AICore staging record 中拷贝新增字段到最终 record stream。

相关文件：

- [src/a2a3/platform/src/aicpu/l2_perf_collector_aicpu.cpp](/data/zhaomin/simpler/src/a2a3/platform/src/aicpu/l2_perf_collector_aicpu.cpp)
- [src/a5/platform/src/aicpu/l2_perf_collector_aicpu.cpp](/data/zhaomin/simpler/src/a5/platform/src/aicpu/l2_perf_collector_aicpu.cpp)

### 7.4 Host 导出 L0 artifact

Host 侧新增导出接口：

```cpp
int L2PerfCollector::export_l0_swimlane_json();
```

调用顺序是：

```text
export_swimlane_json()
export_l0_swimlane_json()
```

也就是说，当前 L0 跟随 `--enable-l2-swimlane`，还没有独立的
`--enable-l0-swimlane` 开关。

相关文件：

- [src/a2a3/platform/include/host/l2_perf_collector.h](/data/zhaomin/simpler/src/a2a3/platform/include/host/l2_perf_collector.h)
- [src/a2a3/platform/src/host/l2_perf_collector.cpp](/data/zhaomin/simpler/src/a2a3/platform/src/host/l2_perf_collector.cpp)
- [src/a5/platform/include/host/l2_perf_collector.h](/data/zhaomin/simpler/src/a5/platform/include/host/l2_perf_collector.h)
- [src/a5/platform/src/host/l2_perf_collector.cpp](/data/zhaomin/simpler/src/a5/platform/src/host/l2_perf_collector.cpp)

## 8. Converter 侧改动

converter 是 raw artifacts 到 Perfetto 的边界。

相关文件：

- [simpler_setup/tools/swimlane_converter.py](/data/zhaomin/simpler/simpler_setup/tools/swimlane_converter.py)
- [tests/ut/py/test_swimlane_converter_l0.py](/data/zhaomin/simpler/tests/ut/py/test_swimlane_converter_l0.py)

它做四件事：

1. 加载 L0：
   - 优先读 `l0-swimlane-npu-model.json`
   - 兼容读旧的 `l0_swimlane_records.json`

2. 加载 dump args：
   - 读取 `tensor_dump/tensor_dump.json`
   - 支持整数和 hex string 形式的 task id
   - 按 `task_id` 分组

3. 匹配 L0 和 dump args：
   - 先按 `task_id`
   - 再按 `subtask_id`
   - 优先选 `before_dispatch`

4. 生成 Perfetto event：
   - L0 parent event：`cat: "l0"`
   - L0 phase event：`cat: "l0_phase"`
   - 新 process：`AICore L0 View`
   - dump args 摘要挂到 parent event 的 `args.dumpArgs`

## 9. dump args 在这里怎么用

dump args 现在已经进入最终 Perfetto JSON，不再只是旁路文件。

L0 parent event 的 `args.dumpArgs` 包含摘要：

```text
stage
subtaskId
funcId
tensorCount
scalarCount
payloadSize
overwritten
tensors[]
scalars[]
```

tensor 摘要里会包含：

- arg index
- dtype
- shape/raw shape
- offsets
- buffer size
- contiguous 相关标志

这样用户在 Perfetto 里点开一个 L0 task，就能同时看到：

- 这个 task 的时间位置
- task 内部 phase 拆分
- 这个 task 当时的输入参数摘要

## 10. 和 PR #821 / Insight Trace 的关系

PR #821 实现的是另一条互补链路。

它新增了 `simpler_setup/insight_trace`，用于生成 MindStudio Insight /
`msprof op simulator` 的单 kernel replay workspace。

两者区别如下：

| 维度 | `l0-swimlane-npu-model` | PR #821 Insight Trace |
|---|---|---|
| 核心问题 | 真实 workload 里哪个 task 慢/异常 | 单个 kernel 内部发生了什么 |
| 执行方式 | 正常 Simpler workload | 独立 replay workspace |
| 输出目标 | Perfetto / `swimlane_converter` | MindStudio Insight |
| 主要产物 | `l0-swimlane-npu-model.json` | `trace.json`、`visualize_data.bin`、`instr_exe*.csv` |
| args 来源 | 实际运行的 `tensor_dump.json` | recipe 或 `--arg-spec` |
| 覆盖范围 | 整个 task graph | 一个选中的 kernel |
| 粒度 | 当前是 task lifecycle phase | simulator instruction-level export |
| 是否改 runtime | 是 | 否 |

所以它们不应该互相替代。

更合理的调试链路是：

1. 正常跑 workload，打开 L2 swimlane、L0 NPU model、dump args。
2. 在 Perfetto 里用 L2/L0 找到异常 task。
3. 点 L0 parent event，看 `dumpArgs`，拿到真实参数上下文。
4. 把选中的 kernel 和参数转成 Insight Trace replay 输入。
5. 用 MindStudio Insight 做 instruction-level 深挖。

也就是说：

- `l0-swimlane-npu-model` 是 wide view，负责定位。
- Insight Trace 是 deep view，负责深入单 kernel。

## 11. 为什么当前方案更贴近原始需求

相比纯 Insight Trace 路线，这个方案更贴近原需求，原因是：

- 它产出 host-side `swimlane_converter` 可读的 L0 artifact。
- 它进入现有 Perfetto swimlane 工作流。
- 它使用真实 workload 的 `task_id` 和 dump args。
- 它不要求用户先手动选一个 kernel 才能采集。
- 它不破坏已有 L2 输出格式。

当前短板是粒度还不够深。

现在已经有真实 task 内 phase，但还没有 instruction-level、TPUSH/TPOP、
DMA 或 kernel pipeline stage 级别事件。

可以把当前状态理解成：

```text
Phase 1: 建立 L0 artifact + Perfetto 集成 + dump args 关联     # 已完成
Phase 2: 补更多 task-level metadata                            # 下一步
Phase 3: 接入更低层核内事件源                                  # 后续
Phase 4: 从 L0 event + dumpArgs 导出 Insight Trace replay 输入  # 后续
```

## 12. 当前验证快照

unit test：

```bash
conda run -n zm_pypto env \
  PYTHONPATH=/data/zhaomin/simpler/build/cp310-cp310-linux_aarch64/python/bindings:/data/zhaomin/simpler/python:/data/zhaomin/simpler \
  pytest tests/ut/py/test_swimlane_converter_l0.py -q
```

结果：

```text
5 passed
```

runtime build：

```bash
CCACHE_DISABLE=1 \
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
cmake --build build/cp310-cp310-linux_aarch64 --target build_runtimes -j 8
```

覆盖：

- a2a3sim / host_build_graph
- a2a3sim / tensormap_and_ringbuffer
- a5sim / host_build_graph
- a5sim / tensormap_and_ringbuffer
- a2a3 / host_build_graph
- a2a3 / tensormap_and_ringbuffer
- a5 / host_build_graph
- a5 / tensormap_and_ringbuffer

a2a3sim vector 验证结果：

```text
outputs/TestVectorExample_default_20260520_142247
l0 events: 5
phase counts: [3]
merged l0: 5
merged l0_phase: 15
merged l0 with dumpArgs: 5
```

a5sim vector 也验证过同类结果：

```text
l0 events: 5
phase counts: [3]
merged l0: 5
merged l0_phase: 15
merged l0 with dumpArgs: 5
```

## 13. 建议和同事讨论的问题

建议重点讨论这些架构问题：

1. L0 是否继续跟随 `--enable-l2-swimlane`，还是新增独立
   `--enable-l0-swimlane`？

2. schema v1 是否保留 `phases[]`，还是 v2 直接改成通用 nested event 模型？

3. 下一步最有价值的 L0 event source 是什么？
   - executor lifecycle
   - kernel wrapper stage
   - 显式 kernel instrumentation
   - TPUSH/TPOP 或 instruction-level source

4. dump args 需要嵌入 Perfetto 到什么程度？
   - 只放 summary
   - 放完整 tensor metadata
   - 只放外部文件引用

5. 是否需要把选中的 L0 event 导出成 Insight Trace replay spec？

6. 算子显示名怎么标准化？
   - 继续用 func id fallback
   - 要求显式 kernel name
   - 从 case metadata 自动生成映射

## 14. 推荐下一步

短期建议：

1. 保持 schema v1 和当前 converter 集成方式。
2. 在正式 DFX 文档里补一段架构说明，明确它和 Insight Trace 的边界。
3. 补充 dump args 匹配相关 unit test：
   - 只按 task id
   - task id + subtask id
   - hex string task id
   - dump args 缺失 fallback
4. 合并前决定是否需要独立 `--enable-l0-swimlane`。

中期建议：

1. 把 L0 artifact 从 `phases[]` 演进到通用 nested events。
2. 定义低开销 kernel instrumentation 点。
3. 加一个可选 exporter，把 L0 event + dumpArgs 转成 Insight Trace
   `--arg-spec`。
4. 用 Perfetto wide view 定位异常 task，再用 Insight Trace deep view
   分析单 kernel。

最重要的架构原则是：**wide workload timeline 和 deep single-kernel
simulator workflow 要打通，但不要混成一个系统。**
