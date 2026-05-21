/*
 * Copyright (c) PyPTO Contributors.
 */
#ifndef PLATFORM_AICORE_L0_KERNEL_EVENT_AICORE_H_
#define PLATFORM_AICORE_L0_KERNEL_EVENT_AICORE_H_

#include <cstdint>
#ifndef __CCE_AICORE__
#include <chrono>
#endif

#include "common/l2_perf_profiling.h"

#ifndef __gm__
#define __gm__
#endif

#ifndef __aicore__
#ifdef __CCE_AICORE__
#define __aicore__ [aicore]
#else
#define __aicore__
#endif
#endif

static constexpr uint32_t L0_KERNEL_EVENT_INVALID_TASK_ID = 0xFFFFFFFFu;

enum L0KernelEventId : uint16_t {
    L0_KERNEL_EVENT_TASK_ACK = 1,
    L0_KERNEL_EVENT_KERNEL_CALL_BEGIN = 2,
    L0_KERNEL_EVENT_KERNEL_CALL_END = 3,
    L0_KERNEL_EVENT_FINISH_SIGNAL = 4,
};

__aicore__ __attribute__((always_inline)) static inline void l0_kernel_event_reset(
    __gm__ L2PerfAicoreRing *ring, uint32_t task_id
) {
    if (ring == nullptr) {
        return;
    }
    __gm__ L2PerfRecord *record = &ring->dual_issue_slots[task_id % PLATFORM_L2_AICORE_RING_SIZE];
    record->kernel_event_count = 0;
    record->kernel_event_overflow = 0;
}

__aicore__ __attribute__((always_inline)) static inline uint64_t l0_kernel_event_now() {
#ifdef __CCE_AICORE__
    return get_sys_cnt();
#else
    uint64_t elapsed_ns = static_cast<uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::high_resolution_clock::now().time_since_epoch()
        )
            .count()
    );
    constexpr uint64_t kNsPerSec = std::nano::den;
    uint64_t seconds = elapsed_ns / kNsPerSec;
    uint64_t remaining_ns = elapsed_ns % kNsPerSec;
    return seconds * PLATFORM_PROF_SYS_CNT_FREQ + (remaining_ns * PLATFORM_PROF_SYS_CNT_FREQ) / kNsPerSec;
#endif
}

__aicore__ __attribute__((always_inline)) static inline void l0_kernel_event_mark_at(
    __gm__ L2PerfAicoreRing *ring, uint32_t task_id, uint16_t event_id, uint64_t timestamp
) {
    if (ring == nullptr) {
        return;
    }
    if (task_id == L0_KERNEL_EVENT_INVALID_TASK_ID) {
        return;
    }
    __gm__ L2PerfRecord *record = &ring->dual_issue_slots[task_id % PLATFORM_L2_AICORE_RING_SIZE];
    uint32_t idx = record->kernel_event_count;
    if (idx >= L2_KERNEL_EVENT_MAX) {
        record->kernel_event_overflow = 1;
        return;
    }
    record->kernel_events[idx].event_id = event_id;
    record->kernel_events[idx].flags = 0;
    record->kernel_events[idx].reserved = 0;
    record->kernel_events[idx].timestamp = timestamp;
    record->kernel_event_count = idx + 1;
}

__aicore__ __attribute__((always_inline)) static inline void l0_kernel_event_mark(
    __gm__ L2PerfAicoreRing *ring, uint32_t task_id, uint16_t event_id
) {
    l0_kernel_event_mark_at(ring, task_id, event_id, l0_kernel_event_now());
}

#endif  // PLATFORM_AICORE_L0_KERNEL_EVENT_AICORE_H_
