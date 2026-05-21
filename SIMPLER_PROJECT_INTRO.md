# Simpler 项目介绍

本文是一份面向开发者的 Simpler 项目总览。它不替代各子系统的细节文档，而是把项目的定位、架构分层、运行链路、目录结构、测试方式和 DFX 能力串起来，帮助新成员快速建立完整认识。

## 1. 项目定位

Simpler 是一个面向 Ascend NPU 任务运行时的开发、仿真、测试和分析框架。项目围绕 PTO Runtime 构建，核心目标是让开发者能够用 Python 驱动测试和编排，用 C++/device 代码实现运行时和 kernel，在仿真环境或真实 NPU 板卡上验证任务图调度、AICPU/AICore 协作、依赖关系、性能采集和可视化分析。

从使用者视角看，Simpler 主要解决几类问题：

- 用 Python 编写 SceneTest 或示例脚本，快速构造输入、golden、kernel callable 和运行配置。
- 自动编译 Host、AICPU、AICore 三类程序，并把它们按平台组织成可运行的 runtime binaries。
- 在 `a2a3sim`、`a5sim` 这类线程仿真平台上进行快速功能验证，不依赖真实 NPU 硬件。
- 在 `a2a3`、`a5` 真实板卡平台上运行同一套 runtime 逻辑，验证硬件行为。
- 用 L2 swimlane、L0 swimlane、tensor dump、PMU、日志等 DFX 能力分析任务时序、调度开销和 kernel 行为。

从实现者视角看，Simpler 是一个分层运行时系统：

- Python 层负责用例描述、编译入口、运行入口和测试集成。
- Host runtime 负责设备上下文、内存、二进制加载、Host 到 Device 的执行控制。
- AICPU runtime 负责任务图调度、依赖释放、AICore 派发和完成回收。
- AICore runtime 负责实际计算 kernel 的执行、任务完成通知和低层 profiling 记录。
- DFX 工具链负责把运行时记录导出为 JSON，再转换成 Perfetto 等工具可读的 swimlane trace。

## 2. 一句话理解 Simpler

Simpler 可以理解为：

```text
Python 测试/编排入口
  -> RuntimeBuilder / KernelCompiler 编译 Host + AICPU + AICore
  -> ChipWorker / Worker 提交 Callable + TaskArgs + CallConfig
  -> Host Runtime 启动设备侧 runtime
  -> AICPU 构建和调度任务图
  -> AICore 执行具体 kernel
  -> Host 回收结果和 DFX 记录
  -> swimlane_converter 生成可视化 trace
```

它的价值不只是“跑一个 kernel”，而是把任务图、依赖关系、多核调度、仿真/上板一致性、调试数据和性能视图放进同一套开发流程里。

## 3. 支持的平台

项目目前按架构和执行环境划分平台：

| 平台 | 含义 | 典型用途 | 是否需要真实 NPU |
| --- | --- | --- | --- |
| `a2a3sim` | A2/A3 线程仿真平台 | 本地功能开发、单测、快速回归 | 否 |
| `a2a3` | A2/A3 真实板卡平台 | 上板功能验证、性能数据采集 | 是 |
| `a5sim` | A5 线程仿真平台 | A5 逻辑开发、快速回归 | 否 |
| `a5` | A5 真实板卡平台 | A5 上板验证 | 是 |

仿真平台的主要优势是启动快、依赖少、适合开发初期定位逻辑问题。真实板卡平台用于验证 CANN toolchain、device ABI、真实设备时序、硬件同步和性能数据。

共享 NPU 机器上运行 `a2a3` 或 `a5` 上板任务时，应通过任务队列提交，避免多人抢卡。仿真平台 `a2a3sim`、`a5sim` 不占用 NPU 卡，一般不需要走 NPU 队列。

## 4. Runtime 形态

`src/{arch}/runtime/` 下按架构维护不同 runtime。当前 README 中列出的主要 runtime 有两类：

| Runtime | 图构建位置 | 典型用途 |
| --- | --- | --- |
| `host_build_graph` | Host CPU | 开发、调试、较直观的 host 侧图构建 |
| `tensormap_and_ringbuffer` | AICPU / device 侧 | 更接近生产形态的任务依赖和调度执行 |

`host_build_graph` 更适合理解端到端流程和做早期功能验证。`tensormap_and_ringbuffer` 是当前很多重点例子和 DFX 功能验证使用的 runtime，因为它更完整地覆盖 AICPU 调度、TensorMap 依赖、RingBuffer slot 管理、AICore 派发等关键路径。

## 5. 三程序模型

Simpler 的 L2 单芯片 runtime 采用 Host、AICPU、AICore 三程序模型。三者分别编译、分别加载，通过明确的 ABI 和共享数据结构协作。

```text
Python Application / SceneTest
        |
        | RuntimeBuilder / KernelCompiler
        v
+-------------------+
| Host Runtime .so  |
+-------------------+
        |
        | launch / memory / binary registration
        v
+-------------------+       dispatch / handshake       +-------------------+
| AICPU Runtime .so | -------------------------------> | AICore Kernel .o |
+-------------------+                                  +-------------------+
        |                                                       |
        | scheduler / dependency / FIN                         | compute
        v                                                       v
   profiling records                                      task result
```

### 5.1 Host Runtime

Host Runtime 位于 `src/{arch}/platform/*/host/`，主要职责包括：

- 创建设备上下文，绑定 device id。
- 管理 device memory，包括 tensor 分配、释放、Host 与 Device 拷贝。
- 加载 AICPU 和 AICore 二进制。
- 通过 C API 暴露给 Python binding 和 `ChipWorker`。
- 启动 runtime，等待设备执行完成，回收结果。
- 导出 L2/L0/tensor dump/PMU 等 DFX 产物。

典型入口包括 `DeviceRunner`、`MemoryAllocator` 和 `pto_runtime_c_api.h`。

### 5.2 AICPU Runtime

AICPU Runtime 位于 `src/{arch}/platform/*/aicpu/` 和 `src/{arch}/runtime/*/aicpu/`。它是设备侧调度器，主要职责包括：

- 初始化 AICPU 与 AICore 之间的握手机制。
- 维护任务图中的 fanin/fanout 关系。
- 判断哪些任务依赖已满足，可以派发。
- 将 ready task 派发给空闲 AICore。
- 观察 AICore FIN 完成信号。
- 释放下游任务依赖，推进整个 DAG。
- 记录调度阶段和任务完成相关 profiling 信息。

可以把 AICPU 理解为单芯片内负责“安排谁什么时候去哪个 AICore 跑”的控制核心。

### 5.3 AICore Runtime

AICore Runtime 位于 `src/{arch}/platform/*/aicore/` 和 `src/{arch}/runtime/*/aicore/`。它是执行计算 kernel 的设备侧工作单元，主要职责包括：

- 轮询或等待 AICPU 下发的任务。
- 解析 task args、kernel 地址和 function id。
- 调用具体 incore kernel。
- 记录任务开始、结束、executor 边界 marker 等事件。
- 向 AICPU 写回 FIN 信号。

可以把 AICore 理解为“真正干计算活”的核心，而 AICPU 是“派活和回收结果”的核心。

## 6. 核心数据模型

Simpler 的任务执行链路围绕三个概念展开：`Callable`、`TaskArgs`、`CallConfig`。

### 6.1 Callable

`Callable` 是一个不透明执行目标。它在不同层级含义不同：

- 在 L2 `ChipWorker` 中，通常对应一个已注册的 `ChipCallable` id。
- 在更高层 `Worker` 中，可能对应一个 orchestration function。
- 在 SUB task 中，可能对应一个 Python callable registry id。

对调度器来说，`Callable` 只是“要执行什么”的句柄。真正如何解释，由接收它的 worker 决定。

### 6.2 TaskArgs

`TaskArgs` 描述一次任务执行需要的 tensor 和 scalar 参数。它既承载参数，也承载依赖分析信息。

典型内容包括：

- tensor 列表。
- scalar 列表。
- 每个 tensor 的方向 tag，例如 `INPUT`、`OUTPUT`、`INOUT`、`OUTPUT_EXISTING`、`NO_DEP`。

方向 tag 的关键作用是帮助 Orchestrator / TensorMap 推导依赖。例如：

- `INPUT` 会查找当前 tensor 的生产者，建立 fanin 依赖。
- `OUTPUT` 会把当前任务登记为这个 tensor 的新生产者。
- `INOUT` 同时读旧值、写新值，因此既查找生产者，也更新生产者。
- `NO_DEP` 不参与依赖推导。

### 6.3 CallConfig

`CallConfig` 是一次调用的执行配置，典型字段包括：

- `block_dim`：AICore block 数。
- `aicpu_thread_num`：AICPU scheduler 线程数。
- `enable_l2_swimlane`：是否开启 L2 swimlane profiling，以及 profiling level。
- `enable_dump_tensor`：是否 dump tensor 参数。
- `enable_pmu`：是否开启 PMU 采集。
- `output_prefix`：输出目录前缀。

`CallConfig` 是轻量 POD，在不同层级按值传递。

## 7. 任务图和调度模型

Simpler 中的任务不是孤立执行，而是形成 DAG。DAG 的构建和执行主要由 Orchestrator、Scheduler、Worker/WorkerThread 协作完成。

### 7.1 Orchestrator

Orchestrator 是 DAG builder。用户的 orchestration function 通过它提交任务：

- `submit_next_level`
- `submit_next_level_group`
- `submit_sub`
- `submit_sub_group`
- `alloc`

Orchestrator 的核心工作是：

1. 从 Ring 中申请一个 slot。
2. 把 `Callable`、`TaskArgs`、`CallConfig` 写入 slot。
3. 根据 tensor tag 查询 TensorMap，推导生产者依赖。
4. 更新 TensorMap，把输出 tensor 关联到当前任务。
5. 将 fanin/fanout wiring 请求交给 Scheduler。
6. 返回 task slot 句柄。

Orchestrator 本身不执行任务，它负责把用户提交动作翻译成 DAG 节点和依赖关系。

### 7.2 TensorMap

TensorMap 是依赖推导的关键结构。它记录“某个 tensor 当前最新的生产者是谁”。当后续任务读取这个 tensor 时，就能找到需要等待的前置任务。

例如：

```text
Task A: output X
Task B: input X
```

Task A 提交时，TensorMap 记录 `X -> A`。Task B 提交时，发现自己读取 X，于是建立 `A -> B` 的依赖边。

### 7.3 Ring / Slot

Ring 是任务 slot 池。每个 slot 保存一个待执行或正在执行的 DAG 节点状态，例如：

- worker type。
- callable。
- task args。
- config。
- fanin count。
- fanout consumers。
- group size。
- 当前 state。

Ring 同时提供背压能力。当 in-flight slot 用完时，提交侧会等待，避免无限制堆积任务。

### 7.4 Scheduler

Scheduler 是 DAG executor。它维护几个核心队列：

- wiring queue：Orchestrator 生产，Scheduler 消费，用于补 fanout 边。
- ready queue：依赖已满足、可以派发的任务。
- completion queue：WorkerThread 执行完成后回报的任务。

Scheduler 循环大致分三步：

1. 处理 wiring queue，完成 fanout 边连接。
2. 从 ready queue 取任务，分配给空闲 WorkerThread。
3. 处理 completion queue，释放下游依赖，把新 ready 的任务放入 ready queue。

Scheduler 不关心 kernel 参数内容，也不做 tensor 计算。它只移动 slot id、维护依赖计数、触发派发。

### 7.5 Worker / WorkerThread

Worker 是对一组执行资源的抽象。WorkerThread 是实际执行任务的线程或进程单元。根据 worker type，任务可能被派发到：

- NEXT_LEVEL worker：进入下一级 runtime，例如 L2 `ChipWorker`。
- SUB worker：执行 Python callable。

在多层级 runtime 中，高层 Worker 负责组合，真正执行 kernel 的叶子仍然是 L2 `ChipWorker`。

## 8. L2 执行链路

一个典型 L2 用例从 Python 到 AICore 的路径如下：

```text
SceneTest / python example
  |
  | 构造输入 tensor、golden、CALLABLE、RUNTIME_CONFIG
  v
RuntimeBuilder.get_binaries(runtime)
  |
  | 编译或复用 host.so / aicpu.so / aicore.o
  v
ChipWorker.init(device_id, bins)
  |
  | dlopen host runtime，创建 DeviceContext
  v
worker.register(ChipCallable)
  |
  | 得到 callable id
  v
worker.run(cid, TaskArgs, CallConfig)
  |
  | Host 分配 device memory，上传 callable 和参数
  v
AICPU scheduler kernel
  |
  | 构建 ready 队列，派发任务
  v
AICore kernel
  |
  | 执行 add / matmul / paged attention 等 incore kernel
  v
AICPU 回收 FIN，推进后继任务
  |
  v
Host 同步 stream，拷回输出，导出 DFX
  |
  v
SceneTest 比对 golden，生成 output 目录
```

这条链路里，Python 负责描述“要跑什么”，Host 负责“把运行材料送到设备并启动”，AICPU 负责“调度任务图”，AICore 负责“执行 kernel”，DFX 负责“把过程记录下来”。

## 9. 目录结构说明

下面是根目录下常见目录和文件的作用。

### 9.1 根目录

| 路径 | 作用 |
| --- | --- |
| `README.md` | 项目快速介绍、平台、runtime、测试命令和文档入口 |
| `CMakeLists.txt` | C++/runtime 构建入口 |
| `pyproject.toml` | Python 包构建、依赖和 editable install 配置 |
| `LICENSE` | 项目许可证 |
| `docs/` | 架构、开发、测试、DFX 文档 |
| `src/` | C++ runtime、platform、common 代码 |
| `python/` | Python package 和 nanobind bindings |
| `simpler_setup/` | SceneTest、RuntimeBuilder、KernelCompiler、工具链 |
| `examples/` | 可直接运行的示例和场景用例 |
| `tests/` | 单测、系统测试、lint 测试 |
| `tools/` | 辅助脚本 |
| `outputs/` | 运行用例后生成的输出目录 |
| `build/` | 本地构建产物，通常不作为源码阅读入口 |

### 9.2 `src/`

`src/` 是 runtime 的主体代码。

| 路径 | 作用 |
| --- | --- |
| `src/a2a3/` | A2/A3 架构相关 platform 和 runtime |
| `src/a5/` | A5 架构相关 platform 和 runtime |
| `src/common/` | 跨架构共享组件 |
| `src/common/task_interface/` | `Callable`、`TaskArgs`、`CallConfig`、tensor arg 等任务接口定义 |
| `src/common/worker/` | Worker、ChipWorker、C API 等执行入口相关代码 |
| `src/common/hierarchical/` | 多层级 runtime、Orchestrator、Scheduler、WorkerManager 等 |
| `src/common/log/` | Host/device 日志能力 |
| `src/common/sim_context/` | 仿真上下文和线程仿真相关支持 |
| `src/common/platform_comm/` | 平台通信和公共设备侧协议 |
| `src/common/pto_runtime2/` | PTO Runtime v2 相关公共代码 |

### 9.3 `src/a2a3/` 与 `src/a5/`

这两个目录的结构大体相似：

```text
src/a2a3/
  platform/
    ... host / aicpu / aicore 平台实现
  runtime/
    host_build_graph/
    tensormap_and_ringbuffer/
  docs/

src/a5/
  platform/
  runtime/
  docs/
```

其中：

- `platform/` 更偏平台适配，包括 host runner、device kernel wrapper、sim/onboard 差异处理。
- `runtime/` 更偏运行时算法，包括任务图构建、调度、AICPU/AICore executor。
- `docs/` 存放对应架构自己的平台和 runtime 说明。

### 9.4 `simpler_setup/`

`simpler_setup/` 是 Python 侧开发体验的核心目录。

| 路径 | 作用 |
| --- | --- |
| `simpler_setup/scene_test.py` | SceneTest 测试框架，负责用例发现、参数化、输出目录、golden 比对等 |
| `simpler_setup/runtime_builder.py` | 编译或定位 runtime binaries |
| `simpler_setup/runtime_compiler.py` | 编译 Host/AICPU/AICore 组件 |
| `simpler_setup/incore/` | incore kernel 编译相关支持 |
| `simpler_setup/goldens/` | golden 生成和比较工具 |
| `simpler_setup/tools/swimlane_converter.py` | swimlane trace 转换工具 |
| `simpler_setup/tools/` | DFX 和辅助工具 |

通常新增例子、跑测试、生成 trace，都会经过这里的代码。

### 9.5 `python/`

`python/` 包含用户可 import 的 Python 包和 bindings。

| 路径 | 作用 |
| --- | --- |
| `python/simpler/` | Python package 入口 |
| `python/simpler/task_interface.py` | Python 侧 task interface 封装 |
| `python/bindings/task_interface.cpp` | nanobind C++ binding |

`ChipWorker`、`TaskArgs`、`CallConfig` 等 Python 可见类型主要通过这里暴露。

### 9.6 `examples/`

`examples/` 是理解项目最好的入口之一。它包含按架构和 runtime 组织的示例：

```text
examples/a2a3/tensormap_and_ringbuffer/
examples/a5/tensormap_and_ringbuffer/
examples/workers/l2/
examples/workers/l3/
```

常见示例包括：

- vector example：适合入门，任务图和 kernel 简单。
- paged attention：更接近真实 workload，适合验证多 kernel、多任务和 DFX。
- worker examples：适合理解 L2/L3 Worker、Orchestrator、Scheduler 的组合方式。

### 9.7 `tests/`

`tests/` 包含不同层次的测试：

| 路径 | 作用 |
| --- | --- |
| `tests/ut/py/` | Python 单元测试，例如 runtime builder、scene test、swimlane converter |
| `tests/ut/cpp/` | C++ 单元测试 |
| `tests/st/` | 系统测试和场景测试 |
| `tests/lint/` | lint 相关测试 |

开发新功能时，一般至少需要补对应 Python unit test 或 SceneTest smoke case。涉及 C++ runtime 公共逻辑时，需要考虑 C++ unit test 或平台仿真回归。

### 9.8 `docs/`

`docs/` 是理解架构的主要入口：

| 文档 | 内容 |
| --- | --- |
| `docs/getting-started.md` | 环境、安装、运行示例、构建流程 |
| `docs/chip-level-arch.md` | L2 单芯片三程序模型 |
| `docs/task-flow.md` | `Callable` / `TaskArgs` / `CallConfig` 数据流 |
| `docs/orchestrator.md` | DAG submit、TensorMap、Ring、Scope |
| `docs/scheduler.md` | DAG dispatch、ready queue、completion queue |
| `docs/worker-manager.md` | WorkerThread、线程/进程模式、mailbox |
| `docs/hierarchical_level_runtime.md` | 多层级 runtime 模型 |
| `docs/dfx/l2-swimlane-profiling.md` | L2 swimlane profiling |
| `docs/dfx/l0-swimlane-npu-model.md` | L0 swimlane NPU model |
| `docs/dfx/tensor-dump.md` | tensor dump |
| `docs/dfx/pmu-profiling.md` | PMU profiling |

## 10. 编译与安装

推荐日常开发使用项目本地虚拟环境，并采用 editable install：

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install --no-build-isolation scikit-build-core nanobind cmake pytest torch
pip install --no-build-isolation -e .
```

如果需要真实 Ascend toolchain，需要先设置 CANN 环境：

```bash
source /usr/local/Ascend/ascend-toolkit/latest/bin/setenv.bash
export ASCEND_HOME_PATH=/usr/local/Ascend/ascend-toolkit/latest
```

仿真平台通常不需要真实 NPU 硬件，但某些 kernel 如果使用 PTO ISA 头文件，仍可能需要 `PTO_ISA_ROOT`。项目测试框架会尝试自动在 `build/pto-isa` 下准备该依赖。

常见构建命令：

```bash
cmake --build build/cp310-cp310-linux_aarch64 --target build_runtimes -j 8
```

实际 build 目录名可能随 Python ABI、平台和本地构建方式变化。

## 11. 运行示例

### 11.1 运行 A2/A3 仿真示例

```bash
python examples/a2a3/tensormap_and_ringbuffer/vector_example/test_vector_example.py -p a2a3sim
```

### 11.2 运行 A5 仿真示例

```bash
python examples/a5/tensormap_and_ringbuffer/paged_attention/test_paged_attention.py -p a5sim
```

### 11.3 运行 pytest 批量测试

```bash
pytest examples tests/st --platform a2a3sim
```

### 11.4 运行 Python 单测

```bash
pytest tests/ut/py -q
```

### 11.5 运行 C++ 单测

```bash
cmake -B tests/ut/cpp/build -S tests/ut/cpp
cmake --build tests/ut/cpp/build
ctest --test-dir tests/ut/cpp/build --output-on-failure
```

### 11.6 运行上板任务

上板平台需要指定真实平台和 device，例如：

```bash
python examples/a2a3/tensormap_and_ringbuffer/vector_example/test_vector_example.py -p a2a3 -d 0
```

在共享 NPU 机器上，应通过 `task-submit` 分配卡：

```bash
task-submit --device auto --run "python examples/a2a3/tensormap_and_ringbuffer/vector_example/test_vector_example.py -p a2a3 -d {}"
```

长时间任务需要设置更长的 `--max-time`，否则默认超时可能终止任务。

## 12. SceneTest 用例模型

SceneTest 是 Simpler 的主要测试组织方式。一个典型 SceneTest 会包含：

- `CALLABLE`：描述 orchestration source、incore kernels、function id、core type、参数签名和可选显示名称。
- `RUNTIME_CONFIG`：描述 runtime、block_dim、aicpu thread 数等。
- case 定义：描述不同输入 shape、dtype、参数规模。
- golden 生成：用 Python 或 torch 生成期望输出。
- 比对逻辑：运行后比较输出 tensor 和 golden。

SceneTest 的优势是把编译、运行、输出目录、DFX 开关、golden 比对和 pytest 参数化整合到同一个框架里。

运行后通常会在 `outputs/` 下生成类似目录：

```text
outputs/<TestClass>_<CaseName>_<YYYYMMDD_HHMMSS>/
```

其中可能包含：

- 原始输出数据。
- `l2_perf_records.json`。
- `l0-swimlane-npu-model.json`。
- `merged_swimlane.json`。
- `tensor_dump/`。
- `name_map_<case>.json`。
- device log 或其他 profiling 文件。

## 13. DFX 和性能分析能力

Simpler 的 DFX 能力是项目的重要组成部分。它让开发者不仅能知道“结果对不对”，还可以分析“为什么慢”“任务在哪里等待”“哪个阶段调度开销大”“kernel 参数和事件是否能对应上”。

### 13.1 L2 Swimlane Profiling

L2 swimlane profiling 关注单芯片任务级时序和 AICPU 调度阶段。开启方式：

```bash
python tests/st/<case>/test_<name>.py -p a2a3 --enable-l2-swimlane
```

它主要采集：

- AICore task start/end/duration。
- AICPU dispatch/finish timestamp。
- task fanout，用于在 Perfetto 中画依赖箭头。
- AICPU scheduler phase，例如 complete、dispatch、scan、idle wait。
- Orchestrator phase summary。

输出文件：

```text
l2_perf_records.json
merged_swimlane.json
```

`merged_swimlane.json` 可直接拖入 Perfetto 查看。

### 13.2 L0 Swimlane NPU Model

L0 swimlane NPU model 是在 L2 swimlane 基础上增加的 AICore L0 视图。当前设计目标是把 kernel span 和更细粒度 kernel 内事件组织成 host 侧 converter 可读的 sibling artifact。

典型输出：

```text
l0-swimlane-npu-model.json
merged_swimlane.json
```

当前 L0 视图可以和 L2 task 通过 `task_id` 关联，也可以和 dump args 通过 `task_id` 关联。对于 paged attention 这类多 kernel workload，配合 name map 后可以在 Perfetto 中看到 `QK`、`SF`、`PV`、`UP` 等 operator/kernel 名称，而不只是 `func_0` 这类 fallback label。

当前无侵入 L0 marker 来自 AICore executor，而不是业务算子源码：

- `task_ack`：AICore 看到 task dispatch 并向 AICPU ACK。
- `kernel_call_begin`：executor 准备调用 `kernel_entry(args)`。
- `kernel_call_end`：`kernel_entry(args)` 返回 executor。
- `finish_signal`：executor 完成可选 barrier，准备结束 task。

真正更细粒度的核内事件，例如 pipe wait、TPUSH/TPOP、指令级或 simulator event，应该来自 simulator hook、PTO intrinsic wrapper 或 Insight-style replay，而不是在每个业务算子实现里手工插 marker。

L0 与 L2 的关系可以概括为：

- L2 回答“哪个 task 在哪个 AICore 上从什么时候跑到什么时候，以及调度链路如何”。
- L0 回答“这个 task 对应的 kernel 内部还发生了哪些阶段或事件”。
- 两者应该通过 `task_id`、`func_id`、`core_id`、`subtask_id` 等字段建立关联。

### 13.3 Tensor Dump

Tensor dump 用于记录 task 参数和 tensor 信息。它可以帮助回答：

- 某个 task 执行时拿到的输入输出 tensor 是什么。
- task_id 和参数记录是否能对应。
- kernel 内事件是否能反查到当时的参数上下文。

典型输出：

```text
tensor_dump/
  tensor_dump.json
  tensor_dump.bin
```

在 L0/L2 分析中，dump args 的价值是提供“事件背后的参数上下文”。单独看 timeline 只能知道某段时间发生了什么，结合 dump args 才更容易定位它处理的是哪组 tensor、哪组 scalar、哪个 logical case。

### 13.4 PMU Profiling

PMU profiling 用于采集硬件性能计数。它更偏向性能瓶颈定位，例如计算、访存、流水线或硬件事件统计。PMU 信息一般和 task timeline 结合使用，timeline 负责定位时间段，PMU 负责解释硬件侧行为。

### 13.5 Swimlane Converter

`simpler_setup/tools/swimlane_converter.py` 将原始 runtime JSON 转换为 Perfetto 可读的 Chrome Trace JSON。

常见用法：

```bash
python -m simpler_setup.tools.swimlane_converter
```

或指定输入：

```bash
python -m simpler_setup.tools.swimlane_converter outputs/<case>/l2_perf_records.json
```

converter 的职责包括：

- 解析 L2 task records。
- 自动发现同目录下的 L0 sibling file。
- 读取 name map，把 `func_id` 转成更可读的 operator 名称。
- 合并 dump args 关联信息。
- 生成 `merged_swimlane.json`。

## 14. Perfetto 与 MindStudio Insight 的关系

Perfetto 和 MindStudio Insight 是两类可视化工具，不应该被理解成底层需求本身。

更准确的分层是：

```text
底层需求：
  采集任务、kernel、kernel 内事件、参数和性能计数
  |
中间表示：
  l2_perf_records.json
  l0-swimlane-npu-model.json
  tensor_dump.json
  PMU records
  |
转换器：
  swimlane_converter
  insight_trace workspace generator
  |
可视化工具：
  Perfetto
  MindStudio Insight
```

因此，真正需要稳定的是底层采集点、事件模型、关联键和文件 schema。Perfetto 或 Insight 只是不同消费端。一个好的设计应该尽量让底层事件可复用，而不是把实现强绑定到某个 UI。

## 15. 仿真与上板的区别

仿真和上板使用尽量相同的上层 API 和测试入口，但底层实现存在差异。

### 15.1 仿真环境

仿真平台如 `a2a3sim`、`a5sim` 通常：

- 不占用真实 NPU 卡。
- 使用 host thread 模拟 AICPU/AICore 行为。
- 更容易调试、加日志、快速迭代。
- 时序和真实硬件不完全等价。
- 适合功能正确性、schema、converter、任务图和大部分 DFX 逻辑验证。

### 15.2 上板环境

真实平台如 `a2a3`、`a5` 通常：

- 需要 CANN toolchain 和真实 NPU。
- 需要通过 device id 选择设备。
- 需要注意共享机器的任务队列。
- 能验证真实设备 ABI、硬件同步、CANN runtime、真实性能和实际 profiling 行为。
- 调试成本更高，失败定位更依赖日志和 DFX。

### 15.3 开发建议

一般开发流程建议：

1. 先在 `a2a3sim` 或 `a5sim` 跑最小 case。
2. 补 Python unit test 验证工具和 schema。
3. 跑典型 workload，例如 vector example、paged attention。
4. 打开 L2/L0/tensor dump 验证 output 是否符合预期。
5. 再用任务队列提交上板 smoke test。
6. 最后补文档和 commit message。

## 16. 典型开发流程

以新增一个 DFX 能力或修改 runtime 行为为例，推荐流程如下。

### 16.1 读现有路径

先确认功能属于哪一层：

- Python 测试或 CLI：看 `simpler_setup/`、`python/`。
- task interface：看 `src/common/task_interface/`。
- 高层 DAG 调度：看 `src/common/hierarchical/`。
- L2 runtime：看 `src/{arch}/runtime/`。
- 平台适配：看 `src/{arch}/platform/`。
- DFX 转换：看 `simpler_setup/tools/` 和 `docs/dfx/`。

### 16.2 做最小闭环

优先在一个小 case 上形成闭环：

```bash
python examples/a2a3/tensormap_and_ringbuffer/vector_example/test_vector_example.py \
  -p a2a3sim \
  --enable-l2-swimlane \
  --dump-tensor
```

确认：

- 用例 PASS。
- output 目录生成。
- 原始 JSON 非空。
- converter 成功。
- `merged_swimlane.json` 在 Perfetto 中可打开。

### 16.3 扩到典型 workload

再跑更复杂的 workload，例如 paged attention：

```bash
python examples/a2a3/tensormap_and_ringbuffer/paged_attention/test_paged_attention.py \
  -p a2a3sim \
  --enable-l2-swimlane \
  --dump-tensor
```

如果功能涉及 A5，也需要跑：

```bash
python examples/a5/tensormap_and_ringbuffer/paged_attention/test_paged_attention.py \
  -p a5sim \
  --enable-l2-swimlane \
  --dump-tensor
```

### 16.4 补单测

工具类和 schema 类改动应补 Python unit test，例如：

```bash
pytest tests/ut/py/test_swimlane_converter_l0.py -q
```

runtime C++ 公共逻辑改动应考虑 C++ unit test 或至少完整仿真回归。

### 16.5 文档同步

涉及对外使用方式、输出文件、schema、平台支持范围的改动，需要同步：

- `README.md` 中的入口信息。
- `docs/dfx/*.md` 中的 DFX 说明。
- 示例或 guide 文档。
- commit message 中的测试记录。

## 17. 如何阅读这个项目

如果是新加入项目，建议按以下顺序阅读：

1. `README.md`：了解项目定位、平台、runtime 和测试命令。
2. `docs/getting-started.md`：搭环境，跑第一个例子。
3. `examples/a2a3/tensormap_and_ringbuffer/vector_example/`：看最小可运行 workload。
4. `simpler_setup/scene_test.py`：理解 SceneTest 如何组织编译、运行和输出。
5. `simpler_setup/runtime_builder.py`：理解 runtime binaries 如何准备。
6. `docs/chip-level-arch.md`：理解 Host/AICPU/AICore 三程序模型。
7. `docs/task-flow.md`：理解 `Callable`、`TaskArgs`、`CallConfig`。
8. `docs/orchestrator.md`：理解 DAG 是如何提交出来的。
9. `docs/scheduler.md`：理解 DAG 是如何被执行的。
10. `src/{arch}/runtime/tensormap_and_ringbuffer/`：看当前核心 runtime 实现。
11. `docs/dfx/l2-swimlane-profiling.md`：理解 L2 profiling。
12. `docs/dfx/l0-swimlane-npu-model.md`：理解 L0 swimlane NPU model。

不要一开始就从所有 C++ 文件横向扫一遍。更有效的方法是选一个例子，从 Python 入口向下追调用链，再回到文档补齐概念。

## 18. 新增 kernel 或 workload 的基本步骤

新增一个 workload 通常需要：

1. 在 `examples/{arch}/{runtime}/` 下创建目录。
2. 编写 incore kernel 源码。
3. 编写 orchestration 源码，提交任务图。
4. 在 Python SceneTest 中描述 `CALLABLE`。
5. 准备 input case 和 golden。
6. 设置 `RUNTIME_CONFIG`。
7. 在 sim 平台跑通。
8. 视需要增加 DFX 开关并检查 output。
9. 再决定是否上板验证。

`CALLABLE` 中建议给每个 incore kernel 配置可读 `name`，这样 DFX 视图里能显示 `add`、`matmul`、`QK`、`PV` 这类名称，而不是 fallback 的 `func_0`。

## 19. 新增 DFX 事件的基本原则

新增 DFX 能力时，应优先考虑几个问题：

- 事件的语义是什么，是 task 级、kernel 级、kernel 内阶段，还是硬件计数？
- 事件时间戳来自哪里，和 L2/L0 其他事件是否同一时间基？
- 事件如何关联到 `task_id`、`func_id`、`core_id`、`subtask_id`？
- 是否需要关联 dump args？
- 原始 JSON schema 是否稳定，是否向后兼容？
- converter 缺少该文件时是否能降级运行？
- sim 和 onboard 的支持范围是否一致，不一致时文档是否说清楚？
- 单测是否覆盖缺字段、空文件、旧 schema、新 schema？

好的 DFX 设计应该先保证底层事件模型清晰，再考虑 Perfetto 或 Insight 具体怎么展示。

## 20. 常见输出文件

| 文件 | 说明 |
| --- | --- |
| `l2_perf_records.json` | L2 task timing、fanout、scheduler/orchestrator phase 原始记录 |
| `l0-swimlane-npu-model.json` | L0 kernel span 和 kernel 内事件记录 |
| `merged_swimlane.json` | converter 生成的 Perfetto trace |
| `name_map_<case>.json` | `func_id` 到可读 kernel/operator 名称的映射 |
| `tensor_dump/tensor_dump.json` | tensor dump manifest，包含 task args 元数据 |
| `tensor_dump/tensor_dump.bin` | tensor dump 二进制数据 |
| device log | 设备侧日志，用于进一步定位调度或 kernel 问题 |

判断一次 DFX 运行是否健康，可以看：

- 用例最终是否 PASS。
- output 目录是否是本次新生成。
- 原始 JSON 是否非空。
- `task_id` 是否能在 L2、L0、dump args 间对上。
- `merged_swimlane.json` 是否能被 Perfetto 打开。
- 视图中的 lane、event 名称、持续时间是否符合 workload 预期。

## 21. 常见问题

### 21.1 仿真用例是否会使用 NPU？

`a2a3sim` 和 `a5sim` 是仿真平台，通常不会占用真实 NPU 卡。上板平台 `a2a3`、`a5` 才会使用真实设备。

### 21.2 为什么 Perfetto 打开 JSON 是空的？

常见原因包括：

- 原始 profiling 文件为空。
- converter 没有成功生成 trace event。
- 时间戳字段异常，事件 duration 为 0 或不可见。
- schema 版本不匹配，converter 跳过了某类事件。
- 打开的不是 `merged_swimlane.json`，而是某个原始中间文件。

排查时应先检查 JSON 中是否有 `traceEvents`，再检查 process/thread metadata 和 duration event。

### 21.3 L0 view 和 AICore view 有什么区别？

AICore view 来自 L2 task records，关注 task 在 AICore 上的整体执行 span。L0 view 应该在同一个 task 内继续下钻，展示 kernel span 或 kernel 内阶段事件。

如果 L0 当前只复用 L2 task span，那么它和 AICore view 会很像。加入 executor 边界 marker 后，可以稳定看到 kernel 调用边界；只有继续接入 pipe event、simulator event 或 instruction-like event 后，才会明显体现更细粒度核内分析价值。

### 21.4 dump args 有什么用？

dump args 负责提供事件对应的参数上下文。timeline 告诉你“什么时候发生”，dump args 告诉你“这个事件处理的是哪些 tensor/scalar”。它对 kernel 内事件分析尤其重要，因为同一个 kernel 名称可能在不同 task、不同输入块上重复出现。

### 21.5 `func_0_a(t0)` 和 `add` 哪个更好？

面向人查看 trace 时，`add`、`matmul`、`QK` 这类业务或算子名称更好。`func_0_a(t0)` 适合作为 fallback，因为它稳定、可自动生成，但可读性较差。

更推荐的做法是：

- 原始事件保留 `func_id`、`core_type`、`task_id` 等稳定字段。
- 展示名优先使用 name map 中的 operator/kernel name。
- 缺少 name map 时再 fallback 到 `func_<id>_<core>(t<task>)`。

## 22. 术语表

| 术语 | 含义 |
| --- | --- |
| AICPU | 设备侧控制核心，负责调度、依赖处理、任务派发 |
| AICore | 设备侧计算核心，负责执行具体 kernel |
| AIC | AI Cube core，偏矩阵/立方计算 |
| AIV | AI Vector core，偏向量计算 |
| PTO Runtime | 项目核心任务运行时 |
| SceneTest | Simpler 的 Python 场景测试框架 |
| Callable | 不透明执行目标句柄 |
| ChipCallable | L2 单芯片可执行 callable 描述 |
| TaskArgs | 一次任务执行的 tensor/scalar 参数 |
| CallConfig | 一次任务执行的配置 |
| Orchestrator | DAG 构建器 |
| Scheduler | DAG 执行调度器 |
| Worker | 执行资源抽象 |
| WorkerThread | 实际执行任务的线程或进程单元 |
| TensorMap | tensor 到最新生产者 task 的映射 |
| Ring | task slot 池和背压结构 |
| fanin | 当前任务依赖的前置任务数量 |
| fanout | 当前任务完成后会释放的后继任务 |
| L2 swimlane | 单芯片 task 级时序和调度 profiling |
| L0 swimlane | AICore kernel 级或 kernel 内事件视图 |
| dump args | task 参数 dump，用于把事件关联到 tensor/scalar 上下文 |
| PMU | 硬件性能计数 |
| Perfetto | 可视化 trace 的 Web 工具 |
| MindStudio Insight | 昇腾生态中的性能分析和可视化工具 |
| sim | 线程仿真环境，不使用真实 NPU |
| onboard | 真实 NPU 板卡环境 |

## 23. 当前项目的扩展方向

结合现有代码和 DFX 演进，后续比较自然的扩展方向包括：

- 继续增强 L0 kernel 内事件，让 L0 view 不止复用 L2 task span。
- 把 dump args、L0 event、L2 task record 的关联键做得更稳定。
- 让 sim 和 onboard 平台的 DFX 输出能力尽量对齐。
- 增强 converter 的 schema 校验和错误提示，减少“打开 trace 是空的”这类排查成本。
- 将 Perfetto trace 和 MindStudio Insight workspace 共享同一套底层事件模型。
- 为复杂 workload 增加更完整的 name map 和典型 regression case。
- 对 A5 平台补齐与 A2/A3 相同级别的 L2/L0 profiling 能力。

## 24. 总结

Simpler 的核心不是单个工具或单个示例，而是一整套 NPU runtime 开发闭环：

```text
写 workload
  -> 编译三程序 runtime
  -> 在 sim 或 onboard 运行
  -> 校验功能正确性
  -> 采集任务和 kernel 事件
  -> 转换成可视化 trace
  -> 根据结果改 runtime / kernel / 调度策略
```

理解这个闭环之后，再去看 `src/` 中的具体 runtime、`simpler_setup/` 中的测试工具、`docs/dfx/` 中的 profiling 文档，就会更容易判断一个改动属于哪一层、应该补哪些测试、输出文件应该长什么样，以及如何判断功能是否真的正确。
