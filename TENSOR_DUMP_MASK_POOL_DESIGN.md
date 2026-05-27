# Tensor Dump Mask Pool 设计说明

## 1. 背景

selective tensor dump 需要记录“某个 task 的哪些 tensor 参数需要 dump”。早期方案把选择信息放进 `PTO2TaskPayload`：

```cpp
uint16_t tensor_dump_arg_mask;
```

这个方案能工作，但 `PTO2TaskPayload` 是 runtime 调度和 dispatch 的热路径结构。dump 属于诊断功能，把冷字段放进 payload 会带来几个风险：

- 改变 `PTO2TaskPayload` 布局。
- 影响 `tensors[]`、`scalars[]` 等字段的 cache line 位置。
- 后续如果 mask 从 16 bit 扩到 64 bit 或更多，会继续扩大 payload。
- 即使用户不打开 selective dump，普通任务仍然承担 payload 布局变化带来的风险。

因此当前方案改为：**dump mask 不进入 `PTO2TaskPayload`，由 platform 侧维护旁路 mask table。**

## 2. 当前实现目标

当前实现遵循以下边界：

- 不新增 `PTO2TaskPayload` 字段。
- 不通过 `PTO2RuntimeOps` 增加 selective dump 开关套壳。
- selective dump 的开关状态、mask 存储、查询和过滤都在 platform 侧。
- runtime 只在 task submit 阶段把 `Arg::dump(...)` 生成的 mask 绑定到当前 task id。
- 未开启 selective mode 时保持历史行为：`--dump-tensor` dump 全量 tensor。
- 开启 selective mode 后，只 dump `Arg::dump(...)` 标记过的 task 参数。

这里的“不侵入 runtime”不是 runtime 代码零改动。`Arg` 和 task submit 本来属于 runtime 用户接口层，runtime 仍然需要传递用户选择；但 runtime 不拥有 dump 状态、不改变 payload、不改变调度数据结构。

## 3. 用户侧接口

用户侧接口保持为：

```cpp
enable_dump_tensor_selective();

Arg params;
params.add_input(g);
params.add_input(c);
params.add_output(ext_f);
params.dump(g, ext_f);
rt_submit_aiv_task(0, params);
```

语义：

- 不开 `--dump-tensor`：不产生 tensor dump。
- 开 `--dump-tensor`，不调用 `enable_dump_tensor_selective()`：保持默认全量 dump。
- 开 `--dump-tensor`，调用 `enable_dump_tensor_selective()`：只 dump `params.dump(...)` 指定的 tensor 参数。

`Arg::dump(...)` 不区分输入/输出，用户直接传已经加入当前 `Arg` 的 tensor 或 `TensorCreateInfo`。runtime 会根据参数在 `Arg` 中的位置生成 bit mask。

例如：

```cpp
params.add_input(g);       // tensor index 0
params.add_input(c);       // tensor index 1
params.add_output(ext_f);  // tensor index 2
params.dump(g, ext_f);
```

对应：

```text
bit 0 = 1
bit 2 = 1
mask = bit0 | bit2
```

## 4. 整体流程

当前链路如下：

```text
用户 orchestration
  enable_dump_tensor_selective()
        |
        v
runtime Arg 层
  记录当前用户已请求 selective dump
        |
        v
Arg params
  add_input/add_output/add_inout
  dump(...)
  生成 tensor_dump_arg_mask
        |
        v
runtime submit
  payload.init(...)
  #if PTO2_PROFILING
  set_dump_tensor_selective_mode(true)   // only when Arg captured selective request
  set_dump_tensor_task_mask(task_id, mask)
  #endif
  发布 task
        |
        v
platform AICPU dump
  get_dump_tensor_task_mask(task_id)
  should_dump_task(mask)
  should_dump_tensor_arg(mask, arg_index)
  写 tensor_dump 记录
```

关键时序是：

```text
写 payload
打开 selective mode
登记 task mask
发布 task
```

mask 必须在 task 对 AICPU/scheduler 可见前登记完成。

需要特别注意：`set_dump_tensor_task_mask()` 只在 mask 非 0 时登记。未标记 task 不写 table，后续 dump 时查询不到 mask，就会在 selective mode 下被整 task 跳过。

## 5. selective 开关传递

`enable_dump_tensor_selective()` 定义在 orchestration API 中，是用户可调用入口：

```cpp
static inline void enable_dump_tensor_selective() {
    set_tensor_dump_selective_requested(true);
}
```

这里不直接调用 platform，也不依赖跨 `.so` 的 weak 符号解析。它只在 runtime `Arg` 层记录“用户希望开启 selective dump”。

真正打开 platform selective mode 的动作放在 runtime submit 阶段，并且只在 profiling 构建中编译：

```cpp
#if PTO2_PROFILING
if (args.tensor_dump_selective_requested()) {
    set_dump_tensor_selective_mode(true);
}
#endif
```

这样可以保证 dump 的控制逻辑和 AICPU dump 消费侧使用同一个 `PTO2_PROFILING` 门控。`PTO2_PROFILING=0` 时 tensor dump 本身不存在，submit 热路径也不会保留 dump 分支。

这个接口没有走 `PTO2RuntimeOps`，也没有新增 runtime 侧的 `rt_tensor_dump_selective()`。原因是 selective dump 的状态属于 platform DFX 功能，runtime 只负责在真实 task submit 前把用户参数选择转交给 platform。

## 6. Arg 中的选择状态

`Arg` 内部维护两个信息：

```cpp
uint64_t tensor_dump_arg_mask_;
bool tensor_dump_selective_requested_;
```

其中：

- `tensor_dump_arg_mask_` 表示当前 task 的哪些 tensor 参数被 `dump(...)` 标记。
- `tensor_dump_selective_requested_` 表示创建或 reset 这个 `Arg` 时，用户是否已经调用过 `enable_dump_tensor_selective()`。

`dump(...)` 只负责生成当前 `Arg` 的 mask，不负责打开 platform 开关：

```cpp
params.dump(mi);
```

如果 `mi` 是当前 `Arg` 的第 0 个 tensor 参数，则 mask 为：

```text
0b...0001
```

当前 `dump(...)` 内部还会刷新一次 `tensor_dump_selective_requested_`：

```cpp
tensor_dump_selective_requested_ = is_tensor_dump_selective_requested();
```

这样可以覆盖下面这种用户写法：

```cpp
Arg params;
params.add_input(x);
enable_dump_tensor_selective();
params.dump(x);
rt_submit_aiv_task(0, params);
```

即使 `Arg` 先创建，`dump(...)` 也能在提交前捕获到 selective 请求。否则这个 task 会有 mask，但 submit 阶段不会打开 platform selective mode，结果会退化为全量 dump。

`dump(...)` 查找参数时按地址匹配已经加入当前 `Arg` 的对象：

- `Tensor` 参数匹配 `INPUT`、`INOUT`、`OUTPUT_EXISTING` 等保存为 `ptr` 的槽位。
- `TensorCreateInfo` 参数只匹配 `OUTPUT` 槽位保存的 `create_info`。

如果传入的对象不属于当前 `Arg`，`Arg` 会进入 error 状态：

```cpp
set_error("dump: tensor is not part of this Arg");
set_error("dump: TensorCreateInfo is not part of this Arg");
```

## 7. Platform Mask Table

当前实现没有把 mask 存成 payload 字段，而是在 platform AICPU 侧维护旁路 table：

```cpp
struct DumpTaskMaskEntry {
    uint64_t task_id;
    TensorDumpArgMask mask;
};
```

核心接口：

```cpp
void set_dump_tensor_selective_mode(bool enable);
bool is_dump_tensor_selective_mode();

void set_dump_tensor_ring_window_mask(uint32_t ring_id, uint32_t task_window_mask);
void set_dump_tensor_task_mask(uint64_t task_id, TensorDumpArgMask mask);
TensorDumpArgMask get_dump_tensor_task_mask(uint64_t task_id);
```

当前 table 采用按需分配：

- selective dump 没有登记非零 mask 时，不分配 table。
- 第一次登记非零 mask 时分配 `DumpTaskMaskEntry` 数组。
- `set_dump_tensor_enabled()` 会清理已有 table 内容，并关闭 selective mode。
- task submit 阶段遇到 selective `Arg` 时打开 selective mode，并按 task id 登记非零 mask。

查询 key 是完整 `task_id`。实现内部会从 `task_id` 拆出 `ring_id` 和 slot：

```text
ring_id = task_id >> 32
slot = low32(task_id) & ring_window_mask[ring_id]
```

再以 `(ring_id, slot)` 计算初始 hash 位置，并用完整 `task_id` 做冲突校验。这样可以避免 ring slot 复用时误命中旧 task。

当前 table 容量是固定的 `32768` 个 entry：

```cpp
static constexpr uint32_t DUMP_TASK_MASK_TABLE_CAPACITY = 32768;
```

索引计算依赖容量为 2 的幂：

```cpp
idx = (ring_id * TENSOR_DUMP_MASK_POOL_MAX_SLOTS + slot) &
      (DUMP_TASK_MASK_TABLE_CAPACITY - 1);
```

冲突使用线性探测。entry 中保存完整 `task_id`，所以即使 slot 复用，新 task 查询到旧 slot 的 entry 也不会误匹配。

## 8. 生命周期和清理

selective dump 有两个层次的开关：

```cpp
static bool g_enable_dump_tensor = false;
static bool g_dump_tensor_selective_mode = false;
```

`g_enable_dump_tensor` 是 `--dump-tensor` 对应的总开关。`g_dump_tensor_selective_mode` 表示是否按 mask 过滤。

每次 tensor dump 初始化时，host/platform 会调用：

```cpp
set_dump_tensor_enabled(enable);
```

当前实现中它会做三件事：

```cpp
g_enable_dump_tensor = enable;
g_dump_tensor_selective_mode = false;
clear_dump_mask_table();
```

这里清理 mask table 是必要的。selective mode 的开启发生在 submit 阶段：

```cpp
#if PTO2_PROFILING
if (args.tensor_dump_selective_requested()) {
    set_dump_tensor_selective_mode(true);
}
#endif
```

如果只在用户显式调用入口时清 table，那么同一进程连续运行多个 selective dump case 时，上一轮的 mask table 可能残留。把清理放进 `set_dump_tensor_enabled()` 后，每轮 dump 初始化都会清掉旧状态，并把 selective mode 复位为 false。

## 9. AICPU Dump 过滤逻辑

AICPU dump 入口不再从 payload 读取 mask，而是按当前 task id 查询 platform table：

```cpp
TensorDumpArgMask dump_arg_mask = TENSOR_DUMP_ARG_MASK_NONE;
if (is_dump_tensor_selective_mode()) {
    dump_arg_mask = get_dump_tensor_task_mask(slot_state.task->task_id.raw);
}
if (!should_dump_task(dump_arg_mask)) {
    return;
}
```

task 级过滤：

```cpp
bool should_dump_task(TensorDumpArgMask arg_mask) {
    if (!is_dump_tensor_selective_mode()) {
        return true;
    }
    return arg_mask != TENSOR_DUMP_ARG_MASK_NONE;
}
```

参数级过滤：

```cpp
bool should_dump_tensor_arg(TensorDumpArgMask arg_mask, int32_t arg_index) {
    if (!is_dump_tensor_selective_mode()) {
        return true;
    }
    if (arg_index < 0 || arg_index >= static_cast<int32_t>(TENSOR_DUMP_ARG_MASK_BITS)) {
        return false;
    }
    return (arg_mask & (TensorDumpArgMask{1} << arg_index)) != 0;
}
```

也就是说：

- selective mode 关闭：所有 task、所有 tensor 参数都允许 dump。
- selective mode 打开且 mask 为 0：整个 task 不 dump。
- selective mode 打开且 mask 非 0：只 dump mask 中 bit 为 1 的 tensor 参数。

当前实现只在 AICPU dump 采集阶段使用 mask。mask 不写入 `TensorDumpRecord`，也不进入 host 导出的 `tensor_dump.json`。原因是 mask 是过滤控制信息，不是 dump 结果本身；结果侧通过 `task_id`、`func_id`、`arg_index`、`role`、`stage` 就能表达实际 dump 了哪些 tensor。

早期曾考虑把 mask 低 16 位写进 record，但这会带来两个问题：

- 当前 `TensorDumpArgMask` 是 `uint64_t`，写低 16 位会造成语义截断。
- 改 `TensorDumpRecord` 会影响 binary metadata layout，属于不必要的格式变更。

所以最终方案是不把 mask 写入 dump record。

## 10. 为什么 task mask 为 0 不登记

当前 `set_dump_tensor_task_mask()` 对 0 mask 直接返回：

```cpp
if (mask == TENSOR_DUMP_ARG_MASK_NONE) {
    return;
}
```

这是可行的，因为 selective mode 下：

- 未标记 task 查询不到 mask，返回 `TENSOR_DUMP_ARG_MASK_NONE`。
- `should_dump_task(0)` 直接返回 false。

同时 table entry 保存完整 `task_id`，slot 复用时旧 entry 不会匹配新 task id，因此不会因为不清 0 mask 而继承旧 task 的选择。

## 11. 对 Runtime 的影响

runtime 侧当前只承担三件事：

1. `Arg::dump(...)` 把用户选择转换为 `tensor_dump_arg_mask_`。
2. `Arg` 捕获 `enable_dump_tensor_selective()` 后的 selective 请求状态。
3. submit 阶段在任务发布前调用 platform API：

```cpp
#if PTO2_PROFILING
if (args.tensor_dump_selective_requested()) {
    set_dump_tensor_selective_mode(true);
}
if (args.tensor_dump_arg_mask() != 0) {
    set_dump_tensor_task_mask(task_id.raw, args.tensor_dump_arg_mask());
}
#endif
```

runtime 不做的事情：

- 不在 `PTO2RuntimeOps` 中新增 dump 开关。
- 不定义 runtime 侧 `rt_tensor_dump_selective` 之类的接口。
- 不定义 weak 的 `platform_enable_dump_tensor_selective` 跨 `.so` 套壳。
- 不在 `PTO2TaskPayload` 中保存 mask。
- 不在 hidden `alloc_tensors` task 上登记 mask。
- 不参与 AICPU dump 过滤决策。

这部分 runtime 修改确实存在，但它不改变调度热结构：

- `Arg` 是用户侧参数构造对象，不是 AICPU scheduler 消费的 payload。
- submit 阶段新增的 platform API 调用发生在任务发布前，且只在 `PTO2_PROFILING` 构建中存在。
- `PTO2TaskPayload` 的字段、大小、cache line 布局保持不变。

`alloc_tensors` 路径不登记 mask。它产生的是 hidden alloc task，通常被编排器直接置为完成状态，不走 worker dispatch 和 completion dump 点；在这里登记 mask 没有消费者，反而会误导读代码的人以为 alloc task 可以被 dump。

## 12. Sim 加载方式

sim 平台的 AICPU SO 仍使用局部加载：

```cpp
dlopen(aicpu_so_path_.c_str(), RTLD_NOW | RTLD_LOCAL);
```

不要为了让 orchestration SO 解析 platform dump 符号而改成 `RTLD_GLOBAL | RTLD_NODELETE`。Linux CI 中 pytest-xdist 的 worker 会在同一个 Python 进程里串行运行多个 scene case；如果 AICPU SO 用 `RTLD_GLOBAL | RTLD_NODELETE` 加载，旧 SO 的全局符号和静态状态会留在进程中，后续 case 可能解析到错误符号或旧状态，导致随机 segfault。

当前方案不依赖 sim 全局符号解析。orchestration 入口只设置 `Arg` 请求状态，submit 阶段通过 runtime 已链接的 platform API 打开 selective mode 并登记 mask。

## 13. Payload Cache Line 影响

把 mask 放进 payload 的问题不只是“多几个字节”。AICPU 调度读取 payload 是热路径，CPU cache line 通常按 64B 粒度搬运。

如果新增字段导致 payload 跨过新的 cache line 边界，就可能带来：

- 每个 task 多一次 cache line 填充。
- dispatch 自旋循环中读取更多冷字段。
- `tensors[]` 或 `scalars[]` 起始 offset 被推后，影响后续访问局部性。
- task 数量和 AICPU 并发上来后，额外 cache miss 被放大。

mask table 把 dump 选择信息移到 platform 冷路径，普通调度不为 dump 字段长期付成本。

## 14. 当前限制

当前 `TensorDumpArgMask` 是 `uint64_t`，因此最多表达 64 个 tensor 参数：

```cpp
static_assert(MAX_TENSOR_ARGS <= 64, "tensor dump arg mask assumes at most 64 tensor arguments");
```

这已经比早期 16 bit mask 更宽，但仍不是无限制。如果未来需要支持超过 64 个 tensor 参数，可以把 `TensorDumpArgMask` 扩展为多 word，例如：

```cpp
constexpr uint32_t TENSOR_DUMP_ARG_MASK_BITS_PER_WORD = 64;
constexpr uint32_t TENSOR_DUMP_ARG_MASK_MAX_ARGS = 512;
constexpr uint32_t TENSOR_DUMP_ARG_MASK_WORDS =
    (TENSOR_DUMP_ARG_MASK_MAX_ARGS + TENSOR_DUMP_ARG_MASK_BITS_PER_WORD - 1) /
    TENSOR_DUMP_ARG_MASK_BITS_PER_WORD;
```

因为 mask 已经移出 `PTO2TaskPayload`，扩展 mask 宽度主要影响 platform mask table 和 `Arg` 内部选择表示，不会改变 task payload 布局。

## 15. 验证结果

当前已验证：

- `cmake --build build/cp310-cp310-linux_aarch64` 通过。
- `TestTensorDumpPartial::test_run --platform a2a3sim --dump-tensor` 通过。
- `examples/a2a3/tensormap_and_ringbuffer/paged_attention` 的 `TestPagedAttention::Case1` onboard 不带 dump 通过。
- 同一 Case 打开 `--dump-tensor` 且在 orchestration 中使用 `params_up.dump(mi);` 后通过。
- submit 侧 selective hook 已纳入 `#if PTO2_PROFILING`，hidden `alloc_tensors` hook 已删除。
- `platform_enable_dump_tensor_selective` weak 套壳已删除，sim 继续使用 `RTLD_LOCAL`。

最新 dump 结果只包含被标记的参数：

```text
outputs/TestPagedAttention_Case1_20260525_211928/tensor_dump/tensor_dump.json
records 16384
(3, 0, 'input', 'before_dispatch') 16384
unique func_ids [3]
unique arg_indices [0]
```

这说明 selective mode 已经生效：没有继续 dump 全量 task，只 dump 了 `FUNC_ONLINE_UPDATE` 中 `params_up.dump(mi)` 对应的 tensor 参数。

另外，曾经出现过 Linux sim CI worker segfault，定位为 `RTLD_GLOBAL | RTLD_NODELETE` 造成的全局符号/静态状态污染。改回 `RTLD_LOCAL` 后验证：

- `TestL2Swimlane --platform a2a3sim` 通过。
- `TestOrchSoCache --platform a5sim` 通过。
- `prepared_callable + l2_swimlane` 子集通过。
- `prepared_callable + orch_so_cache` 子集通过。

## 16. 后续可优化点

后续如果要继续增强，可以考虑：

- 将 `TensorDumpArgMask` 扩展为多 word，支持超过 64 个 tensor 参数。
- 增加 slot 复用压力测试，验证旧 task mask 不会误命中新 task。
- 增加多 ring 场景 selective dump 测试。
- 对 mask table 容量和冲突策略补充统计日志，方便排查极端任务量场景。
