# Dump Args 开发过程与思路讲解

> 这份文档面向刚接触项目的同学，重点不是只给结论，而是把
> “为什么要做、怎么设计、代码大概在哪、怎么验证、遇到问题怎么
> 定位” 讲清楚。

## 1. 先用一句话理解这个功能

Dump Args 的作用是：

**任务在 NPU 设备侧运行时，把设备侧实际看到的 args 信息记录下来，
然后搬回 host 侧，写进文件，方便事后排查问题。**

这里的 args 可以理解成 “一个 task 调 kernel 时传进去的参数”。

比如一个任务可能调用了某个 kernel：

```text
func(a, b, out, scale, offset)
```

其中：

- `a` 是一个 tensor
- `b` 是一个 tensor
- `out` 是一个 output tensor
- `scale` 是一个 scalar
- `offset` 是一个 scalar

Dump Args 要记录的不是 “tensor 里面每个元素的值”，而是：

```text
这个 task 是谁？
它调用哪个 kernel？
它有几个 tensor 参数？
它有几个 scalar 参数？
每个 tensor 的地址、shape、dtype、offset 是什么？
每个 scalar 的值是什么？
```

这样当某个任务算错、卡住、或者和 swimlane / PMU 结果对不上时，
我们可以回头看：

```text
设备侧当时到底收到了什么参数？
```

## 2. 为什么需要 Dump Args

在调试 NPU runtime 问题时，常见情况是：

```text
结果错了
  ↓
不知道是哪个 task 错了
  ↓
知道 task 后，又不知道它当时拿到的参数是什么
  ↓
只能加日志、printf、复跑、猜测
```

已有的 tensor dump 能告诉我们：

```text
某个 tensor 在 kernel 前后是什么数据
```

但它不能完整回答：

```text
这个 task 当时有哪些参数？
这些参数在设备侧看到的 shape / dtype / offset 是什么？
scalar 参数是多少？
```

Dump Args 就是补齐这一块。

它的价值是：

- 让任务运行上下文可追溯。
- 能和 `task_id`、`func_id`、swimlane、PMU 结果关联。
- 未来其它 DFX 功能也可以复用。
- 不需要用户单独开一条新诊断链路。

## 3. 需求原文如何拆解

需求内容可以拆成四点：

### 3.1 新增一种 dump 数据项

不是新增一个完整系统，而是在 dump 能力里新增一种数据：

```text
原来：dump tensor
现在：dump tensor + dump args
```

### 3.2 把设备侧 args 搬到 host 侧

关键点是 “设备侧看到的 args”。

不是 Python 侧生成 args 时顺手记录一下，也不是 host 侧凭空重建一份。
因为真正运行时，设备侧看到的参数才是最可信的。

### 3.3 首选复用现有 dump tensor 通道

这个需求非常重要。

它的意思是：

```text
不要一上来就新建：
- 新开关
- 新共享内存
- 新队列
- 新 host collector
- 新输出目录
```

而是优先复用已有的：

```text
--dump-tensor
tensor dump shared memory
tensor dump ready queue
tensor dump meta buffer
tensor dump arena
tensor dump host collector
outputs/.../tensor_dump/
```

这样好处是：

- 改动小。
- 用户心智简单。
- 资源管理少一套。
- 和 tensor dump 天然能按 `task_id` 对齐。

### 3.4 只有独立启停需要时才新开通道

目前 Dump Args 和 Dump Tensor 一起打开即可。

未来如果 L0 swimlane 明确要求：

```text
我只要 args，不要 tensor
我需要单独触发 args dump
我需要和 tensor dump 不同生命周期
```

那时再考虑独立通道。

当前阶段不需要。

## 4. 项目里几个基本概念

刚接触项目时，容易被很多名词绕晕。这里先用通俗语言解释。

### 4.1 host 侧

host 侧就是 CPU 进程所在侧。

主要负责：

- Python 测试入口
- 构建 callable
- 准备输入输出 tensor
- 调用 runtime
- 分配 dump 共享内存
- 最后收集 dump 数据并写文件

### 4.2 device 侧

device 侧就是 NPU 上运行的部分。

这里又分：

- AICPU：更像调度控制核，负责调度 task、搬运控制信息。
- AICore/AIV/AIC：真正执行 kernel 计算的核。

Dump Args 主要发生在 AICPU 调度 task 的时候。

### 4.3 task

task 可以理解为 runtime 内部的一次 kernel 调用。

一个 task 通常有：

- `task_id`
- `func_id`
- tensor args
- scalar args
- 依赖关系
- 要跑在哪类 core 上

### 4.4 args

args 就是 task 调 kernel 时的参数。

在这个项目里，args 大致分两类：

- tensor 参数
- scalar 参数

tensor 参数除了地址外，还有：

- dtype
- shape
- raw_shape
- offsets
- buffer size
- owner task id

scalar 参数就是一些 64-bit 值。

### 4.5 tensor dump

已有的 tensor dump 功能用于记录 task 输入输出 tensor 的数据。

用户打开方式：

```bash
--dump-tensor
```

输出目录：

```text
outputs/<case>_<timestamp>/tensor_dump/
```

核心文件：

```text
tensor_dump.json
tensor_dump.bin
```

原来：

- JSON 记录 tensor 元信息。
- bin 文件记录 tensor 二进制内容。

现在：

- JSON 里新增 `args` 数组。
- args 本身只作为描述信息写 JSON。

## 5. 总体设计

这次设计的核心思路是：

```text
把 args 当成 tensor dump 通道里的另一种 record。
```

原来通道里只有一种 record：

```text
TensorDumpRecord
```

现在给 record 增加一个 kind：

```cpp
enum class DumpRecordKind : uint8_t {
    TENSOR = 0,
    ARGS = 1,
};
```

这样同一个 dump 通道里可以同时传两类东西：

```text
TENSOR record：表示这里是一条 tensor dump
ARGS record：表示这里是一条 args dump
```

host collector 收到 record 后，根据 kind 分发处理：

```text
如果 kind == TENSOR
    按原来的 tensor dump 逻辑处理

如果 kind == ARGS
    按 args payload schema 解析
    放入 collected_args_
    最后写到 tensor_dump.json 的 args 数组
```

## 6. 数据怎么从设备侧到 host 侧

可以把流程想象成一条传送带。

```text
设备侧 AICPU
  ↓ 写 record
DumpMetaBuffer
  ↓ 指向 payload
per-thread arena
  ↓ flush
ready queue
  ↓ host collector 读取
host 内存
  ↓ export
tensor_dump.json
```

### 6.1 metadata 和 payload

dump 系统一般分两部分：

```text
metadata：描述这条记录是什么
payload：真正的数据内容
```

对于 tensor dump：

```text
metadata 里有 task_id、func_id、shape、dtype、bin_offset 等
payload 是 tensor 数据本身
```

对于 args dump：

```text
metadata 里有 task_id、func_id、stage、payload_offset、payload_size
payload 是 args 结构化描述
```

### 6.2 args payload 格式

args payload 由三部分组成：

```text
ArgsDumpPayloadHeader
ArgsDumpTensorEntry[tensor_count]
uint64_t scalars[scalar_count]
```

也就是：

```text
先写一个 header，说明后面有几个 tensor、几个 scalar
再写 tensor 参数数组
最后写 scalar 参数数组
```

header 里有：

```cpp
struct ArgsDumpPayloadHeader {
    uint32_t version;
    uint32_t tensor_count;
    uint32_t scalar_count;
    uint32_t tensor_entry_size;
    uint32_t scalar_entry_size;
    uint32_t reserved;
};
```

`version` 的作用是方便以后升级格式。

## 7. 输出文件长什么样

开启 `--dump-tensor` 后，最终会得到：

```text
outputs/TestXXX_default_YYYYMMDD_HHMMSS/tensor_dump/tensor_dump.json
```

里面大概是：

```json
{
  "total_tensors": 5,
  "total_args": 2,
  "tensors": [
    {
      "task_id": "0x0000000000000000",
      "subtask_id": 0,
      "func_id": 0,
      "role": "input",
      "stage": "before_dispatch",
      "dtype": "FLOAT32",
      "shape": [16384],
      "bin_offset": 0,
      "bin_size": 65536
    }
  ],
  "args": [
    {
      "task_id": "0x0000000000000000",
      "subtask_id": 0,
      "func_id": 0,
      "stage": "before_dispatch",
      "tensor_count": 3,
      "scalar_count": 0,
      "payload_size": 312,
      "overwritten": false,
      "tensors": [
        {
          "arg_index": 0,
          "buffer_addr": "0x...",
          "buffer_size": 65536,
          "owner_task_id": "0x...",
          "dtype": "FLOAT32",
          "shape": [16384],
          "raw_shape": [16384],
          "offsets": [0],
          "is_contiguous": true,
          "is_all_offset_zero": true
        }
      ],
      "scalars": []
    }
  ]
}
```

你可以理解为：

- `tensors` 看 tensor 数据 dump。
- `args` 看 task 当时的参数描述。

## 8. 用户怎么查看

已有工具 `dump_viewer.py` 增加了 `--args` 模式。

命令：

```bash
python -m simpler_setup.tools.dump_viewer --args
```

输出类似：

```text
Using latest dump directory: outputs/TestDumpTensorExample_default_.../tensor_dump
   idx             task_id  s            stage  func  tensors  scalars  overwritten
--------------------------------------------------------------------------------------------
     0  0x0000000000000000  0  before_dispatch     0        3        0        False
     1  0x0000000000000001  0  before_dispatch     1        1        0        False
```

每列含义：

- `idx`：第几条 args record。
- `task_id`：任务 id。
- `s`：subtask id。
- `stage`：记录发生阶段。
- `func`：kernel function id。
- `tensors`：tensor 参数数量。
- `scalars`：scalar 参数数量。
- `overwritten`：payload 是否被 arena 覆盖。

## 9. 代码改动按模块理解

下面不逐行讲代码，而是按职责讲。

### 9.1 公共结构定义

相关文件：

```text
src/a2a3/platform/include/common/tensor_dump.h
src/a5/platform/include/common/tensor_dump.h
```

这里定义的是 host 和 device 都要认的结构。

新增内容包括：

- `DumpRecordKind`
- `ArgsDumpPayloadHeader`
- `ArgsDumpTensorEntry`
- `ArgsDumpInfo`

这些结构必须保持两侧理解一致。

### 9.2 AICPU 侧写 args record

相关文件：

```text
src/a2a3/platform/include/aicpu/tensor_dump_aicpu.h
src/a5/platform/include/aicpu/tensor_dump_aicpu.h
src/a2a3/platform/src/aicpu/tensor_dump_aicpu.cpp
src/a5/platform/src/aicpu/tensor_dump_aicpu.cpp
```

这里负责：

- 从 payload 里提取 tensor/scalar args。
- 组织 `ArgsDumpPayloadHeader`。
- 把 tensor entries 和 scalar values 写入 arena。
- 往 `DumpMetaBuffer` 里追加一条 `kind=ARGS` 的 record。

核心函数可以理解为：

```text
dump_args_for_payload(...)
    ↓
dump_args_record(...)
```

`dump_args_for_payload` 更像 “把 runtime payload 转换成 args dump 结构”。

`dump_args_record` 更像 “把 args dump 结构写入 dump 通道”。

### 9.3 PTO2 runtime 接入点

相关文件：

```text
src/a2a3/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp
src/a5/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp
```

这里是在 task dispatch 前记录 args：

```text
调度器准备把 task 发给 core
  ↓
如果 --dump-tensor 开启
  ↓
先 dump args
  ↓
再按原逻辑 dump tensor before_dispatch
  ↓
真正 dispatch task
```

为什么选择 before dispatch？

因为这个时刻最能代表：

```text
设备侧即将执行这个 task 时看到的参数
```

### 9.4 host_build_graph runtime 接入点

相关文件：

```text
src/a2a3/runtime/host_build_graph/aicpu/aicpu_executor.cpp
src/a5/runtime/host_build_graph/aicpu/aicpu_executor.cpp
```

host_build_graph 和 PTO2 的 task payload 结构不同，所以需要单独构造
args dump 信息。

这里主要做：

- 根据 callable signature 判断哪些参数是 tensor，哪些是 scalar。
- 从 task args 里收集 scalar 值。
- 从 tensor info 里收集 shape、dtype、offset 等。
- 在 dispatch 前调用 `dump_args_record`。

### 9.5 Host collector 解析并导出 JSON

相关文件：

```text
src/a2a3/platform/include/host/tensor_dump_collector.h
src/a5/platform/include/host/tensor_dump_collector.h
src/a2a3/platform/src/host/tensor_dump_collector.cpp
src/a5/platform/src/host/tensor_dump_collector.cpp
```

host collector 原来只处理 tensor record。

现在逻辑变成：

```text
读取 DumpMetaBuffer 中的每条 TensorDumpRecord
  ↓
看 rec.kind
  ↓
TENSOR：走原来的 tensor 处理逻辑
ARGS：读取 arena 里的 args payload，放到 collected_args_
```

最后导出 JSON 时：

```text
写 total_args
写 args 数组
```

### 9.6 dump_viewer 支持 args

相关文件：

```text
simpler_setup/tools/dump_viewer.py
```

新增：

```bash
--args
```

这个模式不导出 tensor 数据，只列出 manifest 里的 args record。

## 10. 一个关键 bug 的发现与修复

这次验证中发现一个很典型的问题。

### 10.1 问题现象

最开始跑完 PTO2 dump 后，`dump_viewer --args` 输出类似：

```text
func  tensors  scalars
4294967295  3  0
4294967295  2  2
4294967295  2  2
```

`4294967295` 非常可疑。

它其实是：

```text
uint32_t(-1)
```

也就是 C++ 里的 `-1` 被当成无符号数输出。

### 10.2 根因

PTO2 task 有三个 subtask slot：

```text
slot 0: AIC
slot 1: AIV0
slot 2: AIV1
```

很多 vector example 是 AIV-only，也就是只用 AIV slot。

这时：

```text
kernel_id[0] = INVALID_KERNEL_ID = -1
kernel_id[1] = 真正的 func_id
```

原实现写 args record 时固定用：

```cpp
slot_state.task->kernel_id[0]
```

所以 AIV-only task 就写错了。

### 10.3 修复思路

不能固定拿 slot 0。

应该从当前 task 的 active subtask 里找一个有效 slot：

```cpp
int32_t first_active_subtask_slot(const PTO2TaskSlotState &slot_state) {
    for (int32_t slot = 0; slot < PTO2_SUBTASK_SLOT_COUNT; slot++) {
        if (slot_state.active_mask.subtask_active(static_cast<PTO2SubtaskSlot>(slot)) &&
            slot_state.task->kernel_id[slot] != INVALID_KERNEL_ID) {
            return slot;
        }
    }
    return -1;
}
```

然后用这个 slot：

```cpp
dump_args_for_payload(
    thread_idx,
    task_id,
    args_slot,
    kernel_id[args_slot],
    payload,
    BEFORE_DISPATCH
);
```

### 10.4 修复结果

修复后输出：

```text
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

这就符合预期了：

- `func_id` 不再是 `4294967295`
- AIV-only task 的 `subtask_id` 是 `1`

## 11. 测试怎么设计

测试不是只看 “pytest pass”。

这类 DFX 功能要验证三层：

```text
1. 程序能不能跑过
2. dump 文件有没有生成
3. 文件里的字段是不是对的
```

### 11.1 为什么要测 sim

sim 测试好处：

- 不抢硬件卡。
- 迭代快。
- 适合发现编译错误、schema 错误、基础逻辑错误。

这次跑了：

```text
a2a3sim PTO2
a5sim PTO2
```

### 11.2 为什么要测硬件

dump 通道涉及：

- 设备侧 AICPU 写数据
- host/device 共享内存
- arena
- ready queue
- flush
- host collect

这些在硬件上可能和 sim 有差异。

所以必须至少跑一遍硬件。

这次跑了：

```text
a2a3 hardware PTO2
a2a3 hardware host_build_graph
```

### 11.3 为什么要测 PTO2 和 HBG

项目里有两套 runtime：

```text
tensormap_and_ringbuffer，也就是 PTO2
host_build_graph，也就是 HBG
```

它们的 task/args 组织方式不同。

如果只测 PTO2，不能说明 HBG 接入正确。

如果只测 HBG，不能说明 PTO2 scheduler 接入正确。

所以两边都要测。

## 12. 运行测试的注意事项

### 12.1 激活环境

```bash
source ~/.codex-proxy-env 2>/dev/null || true
source /data/miniconda3/etc/profile.d/conda.sh
conda activate zm_pypto
```

### 12.2 设置 ccache 目录

默认 ccache 可能写到只读路径。

所以使用：

```bash
CCACHE_DIR=/tmp/simpler-ccache
```

### 12.3 PTO-ISA

本地使用已有 checkout：

```bash
PTO_ISA_ROOT=/data/zhaomin/pypto/build_output/_deps/pto-isa
```

注意：

这不是 CI pin 的 commit，只是本地可用验证环境。

### 12.4 硬件测试必须走 task-submit

共享机器不能直接抢卡。

硬件测试要用：

```bash
task-submit --device auto --max-time 0 --timeout 0 --run "..."
```

不要手写物理卡号。

daemon 会自动分配可用卡。

## 13. 实际验证结果

### 13.1 a2a3sim PTO2

命令核心：

```bash
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
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 13.2 a5sim PTO2

命令核心：

```bash
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
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 13.3 a2a3 hardware PTO2

命令通过队列提交：

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
total_args = 5
func_ids = [0, 1, 2]
subtask_ids = [1]
```

### 13.4 a2a3 hardware HBG

先跑 baseline：

```text
不带 --dump-tensor
1 passed
```

再跑 dump：

```text
带 --dump-tensor
1 passed
```

产物：

```text
total_tensors = 5
total_args = 2
func_ids = [0, 1]
subtask_ids = [0]
scalar_counts = [0, 0]
```

### 13.5 格式检查

```bash
git diff --check
```

结果：

```text
passed
```

## 14. 遇到的环境问题

硬件测试中出现过：

```text
halMemCtl failed with rc=13
aclrtSynchronizeStreamWithTimeout failed: 507018
```

同一用例换卡重试后通过。

所以判断是共享硬件设备或权限状态波动，不是 Dump Args 逻辑稳定失败。

这个问题已经按仓库规则记录到本地：

```text
KNOWN_ISSUES.md
```

## 15. 如何向同事讲这次开发

可以按这个顺序讲。

### 15.1 先讲背景

以前我们有 tensor dump，可以看 tensor 内容。

但是排查调度和 DFX 问题时，还需要知道：

```text
task 运行时实际拿到了哪些 args？
```

所以新增 Dump Args。

### 15.2 再讲设计选择

需求明确说优先复用 dump tensor 通道。

所以这次没有新增：

- 新开关
- 新通道
- 新队列
- 新输出目录

而是把 args 作为 tensor dump 通道里的另一种 record。

### 15.3 再讲数据流

```text
AICPU dispatch task 前
  ↓
收集 payload 里的 tensor/scalar args
  ↓
写入 dump arena
  ↓
追加一条 kind=ARGS 的 meta record
  ↓
flush 到 ready queue
  ↓
host collector 收集
  ↓
写入 tensor_dump.json 的 args 数组
```

### 15.4 然后讲代码分层

```text
common/tensor_dump.h
    定义 args dump 数据结构

aicpu/tensor_dump_aicpu.*
    设备侧写 args record

runtime/.../scheduler_dispatch.cpp
    PTO2 dispatch 前接入

runtime/host_build_graph/.../aicpu_executor.cpp
    HBG dispatch 前接入

host/tensor_dump_collector.*
    host 侧解析并导出 JSON

dump_viewer.py
    增加 --args 查看能力
```

### 15.5 最后讲问题和修复

验证时发现 PTO2 AIV-only task 的 `func_id` 错成 `4294967295`。

原因是固定取了 AIC slot 的 `kernel_id[0]`。

修复成：

```text
从 active subtask 中找有效 kernel_id
```

修复后 sim 和硬件都通过。

## 16. 新同学读代码建议

如果你是第一次看这个功能，建议按下面顺序读。

### 第一步：看输出

先跑或打开一个已有的：

```text
outputs/.../tensor_dump/tensor_dump.json
```

重点看：

```json
"args": [...]
```

先知道最终产物长什么样。

### 第二步：看 viewer

看：

```text
simpler_setup/tools/dump_viewer.py
```

搜索：

```text
--args
list_args
```

这里最接近用户视角。

### 第三步：看 host collector

看：

```text
src/a2a3/platform/src/host/tensor_dump_collector.cpp
```

搜索：

```text
DumpRecordKind::ARGS
collected_args_
```

这里能理解 host 怎么把 record 写进 JSON。

### 第四步：看 AICPU dump 写入

看：

```text
src/a2a3/platform/src/aicpu/tensor_dump_aicpu.cpp
```

搜索：

```text
dump_args_record
```

这里能理解设备侧怎么写 payload。

### 第五步：看 runtime 接入

PTO2：

```text
src/a2a3/runtime/tensormap_and_ringbuffer/runtime/scheduler/scheduler_dispatch.cpp
```

HBG：

```text
src/a2a3/runtime/host_build_graph/aicpu/aicpu_executor.cpp
```

这里能理解什么时候触发 dump args。

## 17. 这次开发的几个经验

### 17.1 DFX 功能要先复用已有链路

如果已有通道能满足需求，就不要一开始新建一套系统。

这次复用 tensor dump 通道，减少了很多资源管理和生命周期问题。

### 17.2 不要只看 pass，要看产物

DFX 功能经常会出现：

```text
测试 pass
文件也生成了
但字段是错的
```

这次 `func_id=4294967295` 就是靠看 manifest 发现的。

### 17.3 sim 和硬件都要测

sim 能发现大部分编译和逻辑问题。

硬件能发现真实共享内存、flush、collector、runtime 行为问题。

两者都需要。

### 17.4 taskqueue 环境要区分代码问题和设备问题

硬件测试失败不一定是代码错。

这次遇到的 `halMemCtl rc=13 / 507018`，换设备后同一用例通过。

这种要记录下来，但不要误判成代码逻辑 bug。

## 18. 最终结论

Dump Args 当前实现满足需求：

- 复用现有 dump tensor 通道。
- 不新增独立开关和独立通道。
- 设备侧在 dispatch 前记录 args。
- host 侧收集并写入 `tensor_dump.json`。
- `dump_viewer --args` 可以查看。
- PTO2 和 HBG 路径都完成验证。
- a2a3sim、a5sim、a2a3 硬件测试通过。
- 修复了 PTO2 AIV-only task 的 `func_id` 错误。

可以把它理解成：

```text
tensor dump 让我们知道 task 的 tensor 数据是什么；
dump args 让我们知道 task 当时拿到的参数描述是什么。
```

两者结合后，后续定位 runtime、调度、依赖、性能问题会更方便。
