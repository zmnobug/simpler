# Dump Args 功能验证总结

## 1. 需求概述

本需求是为 L0 swimlane 的子前置任务新增一类通用 DFX 能力：
在任务运行时，把设备侧看到的 args 信息搬到 host 侧，便于后续
swimlane、PMU、依赖分析等 DFX 能力关联任务运行上下文。

需求要求优先复用现有 dump tensor 通道，不新增独立 dump 通道。
只有在 L0 swimlane 后续需要独立启停或触发能力时，才考虑另开
独立通道。

## 2. 实现概览

当前实现选择复用现有 tensor dump 通道：

- 继续使用 `--dump-tensor` 作为启停开关。
- 继续使用 tensor dump 的共享内存、ready queue、meta buffer、
  per-thread arena、host collector 和输出目录。
- 在原有 `TensorDumpRecord` 上增加逻辑 record 类型：
  `DumpRecordKind::TENSOR` 和 `DumpRecordKind::ARGS`。
- args payload 写入现有 arena，metadata 仍进入现有
  `DumpMetaBuffer`。
- host collector 读取 ARGS record 后，把结果写入
  `tensor_dump.json` 的 `args` 数组。

输出仍在：

```text
outputs/<case>_<timestamp>/tensor_dump/
├── tensor_dump.json
└── tensor_dump.bin
```

其中 tensor 数据继续使用 `tensors` 数组和 `tensor_dump.bin`；
args 描述信息写入 JSON manifest 的 `args` 数组，不写入 bin 文件。

## 3. Dump Args 内容

每条 args record 目前包含：

- `task_id`
- `subtask_id`
- `func_id`
- `stage`，当前为 `before_dispatch`
- `tensor_count`
- `scalar_count`
- `payload_size`
- `overwritten`
- `tensors`
- `scalars`

每个 tensor arg 记录：

- `arg_index`
- `buffer_addr`
- `buffer_size`
- `owner_task_id`
- `dtype`
- `shape`
- `raw_shape`
- `offsets`
- `is_contiguous`
- `is_all_offset_zero`

scalar arg 以十六进制字符串形式写入 `scalars`。

## 4. 本次发现并修复的问题

验证过程中发现 PTO2 路径下 args record 能正常落盘，但
`func_id` 被写成 `4294967295`。这是 `INVALID_KERNEL_ID=-1` 被按
unsigned JSON 输出后的结果。

根因：

PTO2 的 AIV-only task 中，`kernel_id[0]` 对应 AIC slot，值为
`INVALID_KERNEL_ID`。原实现生成 args record 时固定取
`slot_state.task->kernel_id[0]`，导致 AIV-only task 的 `func_id`
错误。

修复：

在 a2a3 和 a5 的 PTO2 scheduler dispatch 路径中，增加
`first_active_subtask_slot()`，从 active subtask 中选择第一个有效
slot，并使用该 slot 的 `subtask_id` 和 `kernel_id` 生成 args
record。

修改文件：

- `src/a2a3/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp`
- `src/a5/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp`

修复后 PTO2 vector/tensor dump 用例输出：

```text
total_args = 5
func_ids   = [0, 1, 2]
subtask_ids = [1]
```

## 5. 测试环境和约束

使用环境：

```bash
source ~/.codex-proxy-env
source /data/miniconda3/etc/profile.d/conda.sh
conda activate zm_pypto
```

本地使用：

```bash
CCACHE_DIR=/tmp/simpler-ccache
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa
```

说明：

- 本地 `PTO_ISA_ROOT` 不是 CI 中 pin 的
  `50d9c806c3e351d5039c9f0f02a267590420b4d9`，因为本机已有 checkout
  中没有该 commit。
- 硬件测试严格通过 `task-submit` 队列执行，没有直接抢卡。
- `task-submit` 自动分配设备，命令中不手写物理卡号。

## 6. 测试流程

### 6.1 a2a3sim PTO2 dump args

命令：

```bash
source ~/.codex-proxy-env 2>/dev/null || true
source /data/miniconda3/etc/profile.d/conda.sh
conda activate zm_pypto
env CCACHE_DIR=/tmp/simpler-ccache \
    PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
    python -m pytest \
    tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py \
    --platform a2a3sim \
    --device 0-3 \
    -p no:xdist \
    --pto-session-timeout 600 \
    --dump-tensor \
    --build \
    -v
```

结果：

```text
1 passed
```

产物验证：

```bash
python -m simpler_setup.tools.dump_viewer --args
```

结果摘要：

```text
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 6.2 a5sim PTO2 dump args

命令：

```bash
source ~/.codex-proxy-env 2>/dev/null || true
source /data/miniconda3/etc/profile.d/conda.sh
conda activate zm_pypto
env CCACHE_DIR=/tmp/simpler-ccache \
    PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
    python -m pytest \
    examples/a5/tensormap_and_ringbuffer/vector_example \
    --platform a5sim \
    -p no:xdist \
    --pto-session-timeout 600 \
    --dump-tensor \
    --build \
    -v
```

结果：

```text
1 passed
```

产物验证：

```text
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 6.3 a2a3 硬件 PTO2 dump args

硬件测试通过队列提交：

```bash
task-submit --device auto --max-time 0 --timeout 0 --run \
"source ~/.codex-proxy-env 2>/dev/null || true; \
 source /data/miniconda3/etc/profile.d/conda.sh && \
 conda activate zm_pypto && \
 env CCACHE_DIR=/tmp/simpler-ccache \
     PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
     python -m pytest \
     tests/st/a2a3/tensormap_and_ringbuffer/dfx/tensor_dump/test_tensor_dump.py \
     --platform a2a3 \
     -p no:xdist \
     --pto-session-timeout 600 \
     --dump-tensor \
     --build \
     -v"
```

结果：

```text
1 passed
```

产物验证：

```text
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 6.4 a2a3 硬件 HBG dump args

先跑了不带 `--dump-tensor` 的基线，确认 host_build_graph 硬件用例本身
可运行：

```text
1 passed
```

随后通过队列跑带 dump 的 HBG 用例：

```bash
task-submit --device auto --max-time 0 --timeout 0 --run \
"source ~/.codex-proxy-env 2>/dev/null || true; \
 source /data/miniconda3/etc/profile.d/conda.sh && \
 conda activate zm_pypto && \
 env CCACHE_DIR=/tmp/simpler-ccache \
     PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa \
     python -m pytest \
     tests/st/a2a3/host_build_graph/dump_tensor/test_dump_tensor_example.py \
     --platform a2a3 \
     -p no:xdist \
     --pto-session-timeout 600 \
     --dump-tensor \
     --build \
     -v"
```

结果：

```text
1 passed
```

产物验证：

```text
total_tensors = 5
total_args = 2
func_ids = [0, 1]
subtask_ids = [0]
scalar_counts = [0, 0]
```

### 6.5 格式检查

命令：

```bash
git diff --check
```

结果：

```text
passed
```

## 7. 测试中遇到的环境问题

硬件队列测试中遇到过设备相关失败：

```text
halMemCtl failed with rc=13
aclrtSynchronizeStreamWithTimeout failed: 507018
```

该问题在部分设备上出现，重试到其他队列分配设备后同一用例通过。
因此判断为共享硬件环境或设备权限波动，不是 dump args 逻辑的稳定
失败。

已按仓库规则记录到本地 `KNOWN_ISSUES.md`。

## 8. 最终结论

Dump Args 功能当前满足需求目标：

- 复用了现有 dump tensor 通道，没有新增独立通道。
- args 信息能够从设备侧写入 dump arena，并由 host collector 搬回。
- host 侧 manifest 中新增 `args` 数组，能通过 `dump_viewer --args`
  查看。
- a2a3/a5 PTO2 路径均通过 sim 验证。
- a2a3 PTO2 硬件路径通过队列验证。
- a2a3 host_build_graph 硬件路径通过队列验证。
- 已修复 PTO2 AIV-only task 的 args `func_id` 错误。

当前建议：

- 可以进入代码评审。
- CI 环境仍建议使用官方 pin 的 PTO-ISA commit 再跑完整流水。
- 若后续 L0 swimlane 需要独立启停或触发，再考虑是否拆出独立 args
  dump 通道。
