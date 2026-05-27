# 选择性 Tensor Dump — 收尾改动记录

基线：分支已合入 commit `6ca6e6e9`（按 tensor 参数选择性 dump 的功能实现）。
该实现可工作，但有三处需收尾。以下改动在 a5 与 a2a3 两套对称进行。当前工作区已按本文完成代码调整，后续以验证结果为准。

## 1. submit 绑定 hook 加 `#if PTO2_PROFILING`

**做什么**：把 `submit_task_common` 内「读 `Arg` 的选择标记 / 写 task→mask 表」
那段 hook 用 `#if PTO2_PROFILING ... #endif` 包裹。（`alloc_tensors` 内的同款
hook 不是包宏而是整段删除，见 §3。）

**为什么**：tensor dump 的消费侧（scheduler 抓取 input/output）本就全部在
`#if PTO2_PROFILING` 内，唯独提交侧这段 hook 是无条件编译的。perf/release 变体
（`PTO2_PROFILING=0`）下 dump 整体不存在，这段 hook 却仍在每次 submit 的热路径里
留一个分支。包宏后关闭构建完全不编译该段，提交热路径零开销，与消费侧门控一致。

**怎么做**：[pto_orchestrator.cpp](src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_orchestrator.cpp)
的 `submit_task_common`，a2a3 同名函数同位置。

**注意**：只能包这段 hook 代码，**不能**把 `Arg` 的 `tensor_dump_arg_mask_` 等
数据成员包进宏。`Arg` 跨独立编译的编排 kernel 与 runtime 传递，字段包宏会因
`PTO2_PROFILING` 在两边取值不一致而导致结构体布局错位、跨边界读写踩踏
（`Arg` 所在的 [pto_types.h](src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_types.h)
并不引入该宏，会按 0 处理，风险真实）。字段保持无条件，代价仅约 9 字节栈对象。

## 2. 删除冗余的启用入口 `platform_enable_dump_tensor_selective`

**做什么**：删掉 `platform_enable_dump_tensor_selective`（平台定义、两处头声明、
以及 `enable_dump_tensor_selective()` 内对它的调用）；`enable_dump_tensor_selective()`
只保留 `set_tensor_dump_selective_requested(true)`。

**为什么**：它本意是在编排入口直接通知平台「进入 selective 模式 + 清空旧 mask
表」，但三件事都已被覆盖或失效：

- 开启 selective：提交路径每次 submit 都会 `set_dump_tensor_selective_mode(true)`，
  且 dump 只发生在 submit 之后，提前到入口开启无收益；
- 清空 mask 表：`set_dump_tensor_enabled(true)` 每 run 在编排前调用，本身就清表
  + 复位（onboard [kernel.cpp:112](src/a5/platform/onboard/aicpu/kernel.cpp#L112)、
  sim [device_runner.cpp:517](src/a5/platform/sim/host/device_runner.cpp#L517)）；
- 跨 `.so` 直连：它是 weak 符号，需平台 `.so` 全局可见才能被编排 kernel 解析；
  sim 以 `RTLD_LOCAL` 加载 AICPU `.so`，该符号实为 null，在 sim 上是死代码。

删除后行为不变（默认构建下 selective 仍由提交路径可靠开启、清表仍由
`set_dump_tensor_enabled` 兜底），并去掉脆弱的跨 `.so` 依赖。

**怎么做**：

- [tensor_dump_aicpu.cpp](src/a5/platform/src/aicpu/tensor_dump_aicpu.cpp)：删函数定义；
- [tensor_dump_aicpu.h](src/a5/platform/include/aicpu/tensor_dump_aicpu.h)：删声明；
- [pto_orchestration_api.h](src/a5/runtime/tensormap_and_ringbuffer/orchestration/pto_orchestration_api.h)：
  删 weak 声明，`enable_dump_tensor_selective()` 瘦身为单行；
- a2a3 镜像同样四处。

**保留**：`Arg::tensor_dump_selective_requested_` 字段不动——它承载「对未标记的
task 也进入 selective（从而跳过）」的语义，删它会让混排时未标记 task 被误全量 dump。

## 3. 删除 `alloc_tensors` 内的 dump hook（死代码）

**做什么**：删掉 `alloc_tensors` 里那段读 `Arg` 选择标记 / 写 task→mask 表的
hook（即合入版本中与 `submit_task_common` 对称的那段），整段移除而非包宏。

**为什么**：`alloc_tensors` 产生的是 hidden alloc task，它在编排器内被直接
inline 置为 `PTO2_TASK_COMPLETED`（[pto_orchestrator.cpp:841](src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_orchestrator.cpp#L841)），
既不走 worker dispatch 也跳过完成回调。而 `dump_tensors_for_task` 在本 runtime
只在 `scheduler_dispatch`（BEFORE_DISPATCH）和 `scheduler_completion`
（AFTER_COMPLETION）两处被调用，alloc task 两条都不经过。因此这里写的 mask
**永不被读**，是死代码。更本质地：alloc task 无 kernel、buffer 尚未被写，此刻
dump 也无意义；该张量要等真实任务消费/产出时，才由那个真实任务的 `dump(...)`
正常 dump（§1 的 `submit_task_common` 路径）。

> 我们真正需要 dump 的只有编排中提交的真实任务；hidden alloc task 不在 dump
> 路径上，故该 hook 应删除，避免「能对 alloc 张量选择性 dump」的误导性假能力。

**怎么做**：[pto_orchestrator.cpp](src/a5/runtime/tensormap_and_ringbuffer/runtime/pto_orchestrator.cpp)
的 `alloc_tensors`，删除其中 `set_dump_tensor_selective_mode` /
`set_dump_tensor_task_mask` 那段；a2a3 同名函数同位置。

## 验证

- 默认构建（`PTO2_PROFILING=1`）跑 `TestTensorDumpPartial`，确认筛选行为不变
  （3 条记录、2 input/1 output、单一 task_id）。
- perf 变体（`PTO2_PROFILING=0`）构建，确认编译通过、提交热路径无 dump 相关指令。

## 当前实现状态

- `submit_task_common` 中的 selective hook 已加 `#if PTO2_PROFILING`。
- `platform_enable_dump_tensor_selective` 的定义、头文件声明和 orchestration weak 调用已删除。
- `alloc_tensors` 中的 selective hook 已删除。
- `enable_dump_tensor_selective()` 现在只记录 `set_tensor_dump_selective_requested(true)`。
