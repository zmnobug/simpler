# Tensor Dump 支持按 Task 参数选择 Dump 开发流程

## 1. 需求分析

原有 `--dump-tensor` 是全量 dump：只要打开开关，运行时会采集所有 task 的 tensor input/output。这个行为适合全局排查，但在大图、长链路或大 tensor 场景下会产生大量无关数据，也会带来额外的 AICPU 采集和内存拷贝开销。

本次需求是允许用户在 orchestration 中只标记关心的 task tensor。例如：

```cpp
enable_dump_tensor_selective();

Arg params_t4;
params_t4.add_input(g);
params_t4.add_input(c);
params_t4.add_output(ext_f);
params_t4.dump(g, c, ext_f);
rt_submit_aiv_task(0, params_t4);
```

最终语义：

- `--dump-tensor` 仍然是总开关；不打开时不会产生 tensor dump。
- 不调用 `enable_dump_tensor_selective()`：保持历史兼容行为，dump 全部 task tensor。
- 调用 `enable_dump_tensor_selective()`：进入 selective mode，只采集带 `Arg::dump(...)` 标记的 task。
- `Arg::dump(...)` 不区分 input/output，只选择当前 `Arg` 中的 tensor 参数；输入输出方向由 `add_input()`、`add_output()`、`add_inout()` 决定。
- 过滤发生在 AICPU 采集入口，而不是 host 导出前裁剪，因此未选中的 task/tensor 不会被采集和拷贝。

## 2. 用户接口

用户侧接口分两步：

```cpp
enable_dump_tensor_selective();  // 开启按 Arg::dump(...) 选择采集

Arg args;
args.add_input(x);
args.add_input(y);
args.add_output(z);
args.dump(x, z);            // 只 dump 当前 task 的 x 和 z
rt_submit_aiv_task(func_id, args);
```

`Arg::dump(...)` 的约束：

- 参数必须是已经添加到当前 `Arg` 的 `Tensor` 或 `TensorCreateInfo`。
- 不允许传临时对象。
- 如果传入的 tensor 不属于当前 `Arg`，`Arg` 会进入 error 状态，后续 submit 会走已有的参数错误处理。
- 如果不调用 `enable_dump_tensor_selective()`，`dump(...)` 标记不会改变旧的全量 dump 行为。

## 3. 开发方案

整体链路分为 4 层：

```text
用户 orchestration
  enable_dump_tensor_selective()
  Arg::dump(...)
        |
        v
runtime submit
  Arg::tensor_dump_arg_mask()
  platform dump mask pool
        |
        v
AICPU dump collection
  should_dump_task(mask)
  should_dump_tensor_arg(mask, arg_index)
        |
        v
host collector/export
  保存 dump_arg_mask 供诊断和测试校验
```

### 3.1 用户接口层

修改文件：

```text
src/a2a3/runtime/tensormap_and_ringbuffer/orchestration/pto_orchestration_api.h
src/a5/runtime/tensormap_and_ringbuffer/orchestration/pto_orchestration_api.h
src/a2a3/runtime/tensormap_and_ringbuffer/runtime/pto_types.h
src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_types.h
```

`pto_orchestration_api.h` 只声明 platform 导出的用户侧开关，不经过 runtime ops table：

```cpp
__attribute__((visibility("default"))) void enable_dump_tensor_selective(void);
```

`Arg` 增加参数选择接口：

```cpp
template <typename... Args>
void dump(Args &&...args);

uint16_t tensor_dump_arg_mask() const;
```

`dump(...)` 将当前 `Arg` 中被选中的 tensor 参数位置写入 bitmask：

```text
bit N 对应 PTO2TaskPayload::tensors[N]
```

例如：

```cpp
params.add_input(g);      // tensor arg 0
params.add_input(c);      // tensor arg 1
params.add_output(ext_f); // tensor arg 2
params.dump(g, ext_f);    // mask = bit0 | bit2
```

### 3.2 Runtime 提交层

修改文件：

```text
src/a2a3/runtime/tensormap_and_ringbuffer/runtime/pto_runtime2_types.h
src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_runtime2_types.h
```

runtime 不再提供 `tensor_dump_selective_impl` 这类 platform 接口套壳，也不在 `PTO2RuntimeOps` 中增加 selective dump 函数指针。用户侧开关 `enable_dump_tensor_selective()` 由 platform 直接导出。

提交 task 时，runtime 将 `Arg` 上的选择 mask 写入 payload：

```cpp
uint16_t tensor_dump_arg_mask{0};

void init(..., const Arg &args, ...) {
    tensor_dump_arg_mask = args.tensor_dump_arg_mask();
}
```

这里默认值直接使用 `0`，避免 runtime 头文件依赖 `common/tensor_dump.h`。这样可以避免 `common/tensor_dump.h -> platform_config.h` 把 `cycles_to_us()` 等 platform helper 泄漏到 orchestration 编译单元，导致已有 paged attention case 出现重定义。

### 3.3 Platform AICPU Dump 控制和采集层

修改文件：

```text
src/a2a3/platform/include/aicpu/tensor_dump_aicpu.h
src/a5/platform/include/aicpu/tensor_dump_aicpu.h
src/a2a3/platform/src/aicpu/tensor_dump_aicpu.cpp
src/a5/platform/src/aicpu/tensor_dump_aicpu.cpp
```

`tensor_dump_aicpu.h` 同时承载 C 控制接口和 C++ AICPU 采集 helper，不使用自定义宏做 include 隔离，也不新增单独 control header。

头文件结构为：

```cpp
#include <stdint.h>

#ifndef __cplusplus
#include <stdbool.h>
#endif

#ifdef __cplusplus
#include <cinttypes>
#endif

#include "common/memory_barrier.h"
#include "common/tensor_dump.h"
#include "data_type.h"

#ifdef __cplusplus
#include "callable.h"
#include "common/unified_log.h"
#endif

#ifdef __cplusplus
extern "C" {
#endif

void set_platform_dump_base(uint64_t dump_data_base);
uint64_t get_platform_dump_base();
void set_dump_tensor_enabled(bool enable);
bool is_dump_tensor_enabled();
void set_dump_tensor_selective_mode(bool enable);
bool is_dump_tensor_selective_mode();
__attribute__((visibility("default"))) void enable_dump_tensor_selective();

#ifdef __cplusplus
}
#endif

#ifdef __cplusplus
// C++ only: CoreCallable / TensorDumpInfo helpers and dump_tensors_for_task(...)
#endif
```

platform 实现维护 selective mode 状态：

```cpp
static bool g_dump_tensor_selective_mode = false;

extern "C" void set_dump_tensor_selective_mode(bool enable) {
    g_dump_tensor_selective_mode = enable;
}

extern "C" bool is_dump_tensor_selective_mode() {
    return g_dump_tensor_selective_mode;
}

extern "C" __attribute__((visibility("default"))) void enable_dump_tensor_selective() {
    set_dump_tensor_selective_mode(true);
}
```

打开/关闭总 dump 开关时重置 selective mode：

```cpp
extern "C" void set_dump_tensor_enabled(bool enable) {
    g_enable_dump_tensor = enable;
    g_dump_tensor_selective_mode = false;
}
```

AICPU 采集入口过滤：

```cpp
bool should_dump_task(uint16_t arg_mask) {
    if (!is_dump_tensor_selective_mode()) {
        return true;
    }
    return arg_mask != TENSOR_DUMP_ARG_MASK_NONE;
}

bool should_dump_tensor_arg(uint16_t arg_mask, int32_t arg_index) {
    if (!is_dump_tensor_selective_mode()) {
        return true;
    }
    if (arg_index < 0 || arg_index >= 16) return false;
    return (arg_mask & static_cast<uint16_t>(1u << arg_index)) != 0;
}
```

`dump_tensors_for_task(...)` 在遍历 tensor 前先跳过未标记 task，在记录每个 tensor 前再判断该 tensor argument 是否被选中：

```cpp
const auto &pl = *slot_state.payload;
if (!should_dump_task(pl.tensor_dump_arg_mask)) {
    return;
}

...

if (get_tensor_dump_role_from_direction(dir, &role) &&
    should_dump_tensor_at_stage(role, stage) &&
    should_dump_tensor_arg(pl.tensor_dump_arg_mask, payload_index)) {
    ...
}
```

### 3.4 公共 Dump Record 和 Host Collector

修改文件：

```text
src/a2a3/platform/include/common/tensor_dump.h
src/a5/platform/include/common/tensor_dump.h
src/a2a3/platform/include/host/tensor_dump_collector.h
src/a5/platform/include/host/tensor_dump_collector.h
src/a2a3/platform/src/host/tensor_dump_collector.cpp
src/a5/platform/src/host/tensor_dump_collector.cpp
```

公共定义新增：

```cpp
constexpr uint16_t TENSOR_DUMP_ARG_MASK_NONE = 0;
```

`TensorDumpRecord` / `TensorDumpInfo` / host `DumpedTensor` 增加：

```cpp
uint16_t dump_arg_mask;
```

Host collector 只记录这个字段用于诊断和测试校验，不做导出前过滤。partial dump 的核心过滤发生在 AICPU 采集入口。

## 4. 测试实现

新增 partial dump 测试 orchestration：

```text
tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/kernels/orchestration/partial_dump_orch.cpp
```

新增测试类：

```text
tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py
```

测试复用 vector example 的计算图：

```text
t0: c = a + b
t1: d = c + 1
t2: e = c + 2
t3: g = d * e
t4: f = g + c
```

在 orchestration 入口开启 selective mode：

```cpp
enable_dump_tensor_selective();
```

只在最后一个 task `t4` 上标记：

```cpp
params_t4.dump(g, c, ext_f);
```

预期 manifest 只包含 `t4` 的 3 条记录：

```text
before_dispatch input  arg0
before_dispatch input  arg1
after_completion output arg2
```

测试断言：

```text
total_tensors == 3
before_dispatch == 2
after_completion == 1
task_id 只包含 0x0000000100000003
role 顺序为 input, input, output
```

## 5. 测试流程

### 5.1 环境变量

```bash
cd /data/zhaomin/simpler

export TORCH_DEVICE_BACKEND_AUTOLOAD=0
export PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa
export PYTHONPATH=build/cp310-cp310-linux_aarch64/python/bindings:.:python
export LD_LIBRARY_PATH=build/lib
```

### 5.2 构建全部 runtime

```bash
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
cmake --build build/cp310-cp310-linux_aarch64
```

该命令会增量构建：

```text
a2a3sim
a5sim
a2a3 onboard
a5 onboard
```

### 5.3 全量 Dump 基准

```bash
TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
PYTHONPATH=build/cp310-cp310-linux_aarch64/python/bindings:.:python \
LD_LIBRARY_PATH=build/lib \
conda run -n zm_pypto python \
tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py \
  -p a2a3sim \
  --dump-tensor \
  --case TestTensorDump::default \
  --log-level info
```

预期：

```text
TestTensorDump::default ... PASSED
```

### 5.4 Partial Dump 测试

```bash
TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
PYTHONPATH=build/cp310-cp310-linux_aarch64/python/bindings:.:python \
LD_LIBRARY_PATH=build/lib \
conda run -n zm_pypto python \
tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py \
  -p a2a3sim \
  --dump-tensor \
  --case TestTensorDumpPartial::default \
  --log-level info
```

预期：

```text
TestTensorDumpPartial::default ... PASSED
```

### 5.5 Paged Attention 回归

该回归用于确认 runtime 头文件不再通过 `common/tensor_dump.h` 泄漏 `platform_config.h` 中的 `cycles_to_us()`，避免和已有 paged attention helper 重定义：

```bash
TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
PYTHONPATH=build/cp310-cp310-linux_aarch64/python/bindings:.:python \
LD_LIBRARY_PATH=build/lib \
conda run -n zm_pypto python \
examples/a2a3/tensormap_and_ringbuffer/paged_attention/test_paged_attention.py \
  -p a2a3sim \
  --case TestPagedAttention::CaseSmall1 \
  --log-level info
```

预期：

```text
TestPagedAttention::CaseSmall1 ... PASSED
```

## 6. 验证结果

本地已验证：

```text
PTO_ISA_ROOT=... cmake --build build/cp310-cp310-linux_aarch64: passed
TestTensorDumpPartial::default --dump-tensor a2a3sim: passed
TestPagedAttention::CaseSmall1 a2a3sim: passed
git diff --check: passed
```

历史验证覆盖：

```text
a2a3sim build: passed
a5sim build: passed
a2a3 onboard build: passed
a5 onboard build: passed
TestTensorDump::default --dump-tensor: passed
TestTensorDumpPartial::default --dump-tensor: passed
```

## 7. 注意事项

1. `--dump-tensor` 仍然必须打开，否则 `enable_dump_tensor_selective()` 和 `Arg::dump(...)` 不会产生 dump 文件。
2. `enable_dump_tensor_selective()` 建议放在 orchestration 入口；如果放在中途，之前已执行或已采集的 task 不会按新模式回溯处理。
3. 如果不调用 `enable_dump_tensor_selective()`，即使某个 `Arg` 调了 `dump(...)`，也保持旧的全量 dump 行为。
4. `Arg::dump(...)` 只接受当前 `Arg` 中已添加的 tensor 参数；方向由 `add_input()`、`add_output()`、`add_inout()` 决定。
5. runtime 不直接拥有 selective dump 状态；状态和采集实现位于 platform AICPU dump 模块。
6. `tensor_dump_aicpu.h` 不使用自定义宏进行内容隔离，也不拆出单独 control header；只用 `__cplusplus` 区分 C 控制声明和 C++ helper/template。
7. Host collector 不做导出前裁剪，partial dump 的过滤发生在 AICPU 采集入口。


核心链路可以按“用户标记 → runtime 提交 → platform 记录 → AICPU 过滤”理解。

  1. 用户接口
  simpler/src/a2a3/runtime/tensormap_and_ringbuffer/runtime/pto_types.h
  simpler/src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_types.h

  Arg 里新增了两类状态：

  uint64_t tensor_dump_arg_mask_;
  bool tensor_dump_selective_requested_;

  用户写：

  enable_dump_tensor_selective();

  Arg params;
  params.add_input(g);
  params.add_input(c);
  params.add_output(ext_f);
  params.dump(g, c, ext_f);
  rt_submit_aiv_task(0, params);

  params.dump(...) 会在当前 Arg 已添加的 tensor 参数里查找传入对象，然后把对应参数位置置 bit。

  例如：

  arg 0 = g      -> bit 0
  arg 1 = c      -> bit 1
  arg 2 = ext_f  -> bit 2

  最后得到：

  tensor_dump_arg_mask = 0b111

  2. selective 开关
  simpler/src/a2a3/runtime/tensormap_and_ringbuffer/orchestration/pto_orchestration_api.h
  simpler/src/a5/runtime/tensormap_and_ringbuffer/orchestration/pto_orchestration_api.h

  新增用户入口：

  static inline void enable_dump_tensor_selective() { set_tensor_dump_selective_requested(true); }

  它不直接调用 platform，不打开 dump，也不写 mask table。
  它只是告诉后续创建/提交的 Arg：

  用户希望 selective dump 生效

  真正的 dump 总开关仍然是命令行：

  --dump-tensor

  3. 提交 task 时绑定 mask
  simpler/src/a2a3/runtime/tensormap_and_ringbuffer/runtime/pto_orchestrator.cpp
  simpler/src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_orchestrator.cpp

  在真实 task submit 路径 submit_task_common(...) 里：

  payload.init(args, result, prepared.alloc_result, layout);

  #if PTO2_PROFILING
  if (args.tensor_dump_selective_requested()) {
      set_dump_tensor_selective_mode(true);
  }
  if (args.tensor_dump_arg_mask() != 0) {
      set_dump_tensor_task_mask(task_id.raw, args.tensor_dump_arg_mask());
  }
  #endif

  这里做两件事：

  - 如果这个 Arg 捕获到 selective 请求，就打开 platform selective mode。
  - 如果这个 task 有 dump(...) 标记，就把 task_id -> arg_mask 写到 platform mask table。

  这段放在 #if PTO2_PROFILING 下，因为 tensor dump 只在 profiling 构建里存在。

  另外，alloc_tensors(...) 里的同类登记逻辑已经删掉。原因是 alloc task 是 hidden task，不走正常 dispatch/completion dump 点，登记 mask 没有消费者。

  4. platform 侧 mask table
  simpler/src/a2a3/platform/src/aicpu/tensor_dump_aicpu.cpp
  simpler/src/a5/platform/src/aicpu/tensor_dump_aicpu.cpp

  platform 侧维护一张旁路表：

  struct DumpTaskMaskEntry {
      uint64_t task_id;
      TensorDumpArgMask mask;
  };

  核心接口：

  void set_dump_tensor_selective_mode(bool enable);
  void set_dump_tensor_task_mask(uint64_t task_id, TensorDumpArgMask mask);
  TensorDumpArgMask get_dump_tensor_task_mask(uint64_t task_id);

  它的作用是记录：

  某个 task_id 需要 dump 哪些 tensor 参数

  mask 没有放进 PTO2TaskPayload，所以不会改变调度热路径的数据结构。

  5. mask pool 容量
  simpler/src/a2a3/platform/include/common/tensor_dump.h
  simpler/src/a5/platform/include/common/tensor_dump.h

  定义：

  using TensorDumpArgMask = uint64_t;

  constexpr TensorDumpArgMask TENSOR_DUMP_ARG_MASK_NONE = 0;
  constexpr uint32_t TENSOR_DUMP_ARG_MASK_BITS = 64;
  constexpr uint32_t TENSOR_DUMP_MASK_POOL_MAX_RINGS = PTO2_MAX_RING_DEPTH;
  constexpr uint32_t TENSOR_DUMP_MASK_POOL_MAX_SLOTS = PTO2_TASK_WINDOW_SIZE;

  这里的含义是：

  - 最多支持 64 个 tensor 参数选择。
  - mask pool 支持的 ring 数跟 runtime 的 PTO2_MAX_RING_DEPTH 一致。
  - 每个 ring 的 slot 数跟 runtime 的 PTO2_TASK_WINDOW_SIZE 一致。

  这两个 PTO2 宏被移到轻量头：

  simpler/src/a2a3/runtime/tensormap_and_ringbuffer/runtime/pto_constants.h
  simpler/src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_constants.h

  避免 tensor_dump.h 重复写死 4 和 16384。

  6. AICPU dump 过滤
  仍然在 tensor_dump_aicpu.cpp。

  采集某个 task 的 tensor 前，会先查 mask：

  TensorDumpArgMask dump_arg_mask = TENSOR_DUMP_ARG_MASK_NONE;
  if (is_dump_tensor_selective_mode()) {
      dump_arg_mask = get_dump_tensor_task_mask(slot_state.task->task_id.raw);
  }
  if (!should_dump_task(dump_arg_mask)) {
      return;
  }

  task 级逻辑：

  bool should_dump_task(TensorDumpArgMask arg_mask) {
      if (!is_dump_tensor_selective_mode()) {
          return true;
      }
      return arg_mask != TENSOR_DUMP_ARG_MASK_NONE;
  }

  参数级逻辑：

  bool should_dump_tensor_arg(TensorDumpArgMask arg_mask, int32_t arg_index) {
      if (!is_dump_tensor_selective_mode()) {
          return true;
      }
      return (arg_mask & (TensorDumpArgMask{1} << arg_index)) != 0;
  }

  所以最终行为是：

  不加 --dump-tensor：
      不 dump

  加 --dump-tensor，但不调用 enable_dump_tensor_selective()：
      默认全量 dump

  加 --dump-tensor，并调用 enable_dump_tensor_selective()：
      只 dump 调用 Arg::dump(...) 标记过的 task 参数

  7. 测试覆盖
  新增测试 orchestration：

  simpler/tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/kernels/orchestration/partial_dump_orch.cpp

  新增/扩展测试：

  simpler/tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py

  验证 selective dump 后只产生 3 条记录：

  2 个 input
  1 个 output
  同一个 task_id

  这说明未标记 task 被跳过，标记 task 里也只 dump 指定参数。