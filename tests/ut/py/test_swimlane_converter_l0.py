import json

from simpler_setup.tools.swimlane_converter import generate_chrome_trace_json, load_dump_args_json, load_l0_swimlane_json


def test_load_l0_swimlane_json_next_to_perf_records(tmp_path):
    perf = tmp_path / "l2_perf_records.json"
    perf.write_text('{"version": 1, "tasks": []}\n')
    l0 = tmp_path / "l0-swimlane-npu-model.json"
    l0.write_text(
        json.dumps(
            {
                "version": 1,
                "events": [
                    {
                        "name": "kernel",
                        "task_id": 7,
                        "func_id": 3,
                        "core_id": 1,
                        "core_type": "aiv",
                        "start_time_us": 1.0,
                        "duration_us": 2.0,
                    }
                ],
            }
        )
    )

    events = load_l0_swimlane_json(perf)

    assert events is not None
    assert events[0]["task_id"] == 7


def test_load_l0_swimlane_json_supports_legacy_filename(tmp_path):
    perf = tmp_path / "l2_perf_records.json"
    perf.write_text('{"version": 1, "tasks": []}\n')
    l0 = tmp_path / "l0_swimlane_records.json"
    l0.write_text(json.dumps({"version": 1, "events": [{"task_id": 8}]}))

    events = load_l0_swimlane_json(perf)

    assert events is not None
    assert events[0]["task_id"] == 8


def test_generate_chrome_trace_json_includes_l0_view(tmp_path):
    output = tmp_path / "merged_swimlane.json"
    tasks = [
        {
            "task_id": 7,
            "func_id": 3,
            "core_id": 1,
            "core_type": "aiv",
            "start_time_us": 1.0,
            "end_time_us": 3.0,
            "duration_us": 2.0,
            "dispatch_time_us": 0.5,
            "finish_time_us": 3.5,
            "fanout": [],
            "fanout_count": 0,
        }
    ]
    l0_events = [
        {
            "name": "kernel",
            "task_id": 7,
            "func_id": 3,
            "core_id": 1,
            "core_type": "aiv",
            "subtask_id": 2,
            "start_time_us": 1.0,
            "duration_us": 2.0,
            "args_key": {"task_id": 7, "subtask_id": 2},
            "phases": [
                {"name": "compute", "start_time_us": 1.1, "end_time_us": 2.8, "duration_us": 1.7},
            ],
            "kernel_events": [
                {"event_id": 1, "name": "task_ack", "timestamp_us": 1.05},
                {"event_id": 2, "name": "kernel_call_begin", "timestamp_us": 1.15},
            ],
        }
    ]

    generate_chrome_trace_json(tasks, output, func_id_to_name={"3": "add"}, l0_events=l0_events)

    trace = json.loads(output.read_text())
    events = trace["traceEvents"]
    assert any(e.get("pid") == 5 and e.get("args", {}).get("name") == "AICore L0 View" for e in events)
    assert any(
        e.get("pid") == 5
        and e.get("cat") == "l0"
        and e.get("name") == "add(t7)"
        and e.get("args", {}).get("eventType") == "kernel"
        and e.get("args", {}).get("subtaskId") == 2
        for e in events
    )
    assert any(
        e.get("pid") == 5
        and e.get("cat") == "l0_kernel_event"
        and e.get("name") == "add.task_ack(t7)"
        and e.get("args", {}).get("eventId") == 1
        for e in events
    )
    assert any(
        e.get("pid") == 5
        and e.get("cat") == "l0_kernel_span"
        and e.get("name") == "add.task_ack->kernel_call_begin(t7)"
        and abs(e.get("dur", 0.0) - 0.1) < 1e-9
        for e in events
    )
    assert any(
        e.get("pid") == 5
        and e.get("cat") == "l0_phase"
        and e.get("name") == "add.compute(t7)"
        and e.get("args", {}).get("phase") == "compute"
        and e.get("args", {}).get("subtaskId") == 2
        for e in events
    )


def test_load_dump_args_json_next_to_perf_records(tmp_path):
    perf = tmp_path / "l2_perf_records.json"
    perf.write_text('{"version": 1, "tasks": []}\n')
    dump_dir = tmp_path / "tensor_dump"
    dump_dir.mkdir()
    (dump_dir / "tensor_dump.json").write_text(
        json.dumps(
            {
                "args": [
                    {
                        "task_id": "0x0000000200000007",
                        "subtask_id": 1,
                        "func_id": 3,
                        "stage": "before_dispatch",
                        "tensor_count": 1,
                        "scalar_count": 1,
                        "tensors": [{"arg_index": 0, "dtype": "float32", "shape": [2, 4]}],
                        "scalars": ["0x000000000000002a"],
                    }
                ]
            }
        )
    )

    records = load_dump_args_json(perf)

    assert records is not None
    assert 0x0000000200000007 in records
    assert records[0x0000000200000007][0]["subtask_id"] == 1


def test_generate_chrome_trace_json_attaches_dump_args_to_l0_event(tmp_path):
    output = tmp_path / "merged_swimlane.json"
    tasks = [
        {
            "task_id": 7,
            "func_id": 3,
            "core_id": 1,
            "core_type": "aiv",
            "start_time_us": 1.0,
            "end_time_us": 3.0,
            "duration_us": 2.0,
            "dispatch_time_us": 0.5,
            "finish_time_us": 3.5,
            "fanout": [],
            "fanout_count": 0,
        }
    ]
    l0_events = [
        {
            "name": "kernel",
            "task_id": 7,
            "func_id": 3,
            "core_id": 1,
            "core_type": "aiv",
            "subtask_id": 1,
            "start_time_us": 1.0,
            "duration_us": 2.0,
            "args_key": {"task_id": 7},
        }
    ]
    dump_args_by_task = {
        7: [
            {
                "task_id": "0x0000000000000007",
                "subtask_id": 1,
                "func_id": 3,
                "stage": "before_dispatch",
                "tensor_count": 1,
                "scalar_count": 1,
                "payload_size": 160,
                "overwritten": False,
                "tensors": [{"arg_index": 0, "dtype": "float32", "shape": [2, 4], "buffer_size": 32}],
                "scalars": ["0x000000000000002a"],
            }
        ]
    }

    generate_chrome_trace_json(
        tasks,
        output,
        func_id_to_name={"3": "add"},
        l0_events=l0_events,
        dump_args_by_task=dump_args_by_task,
    )

    trace = json.loads(output.read_text())
    l0 = next(e for e in trace["traceEvents"] if e.get("pid") == 5 and e.get("cat") == "l0")
    assert l0["args"]["dumpArgs"]["tensorCount"] == 1
    assert l0["args"]["dumpArgs"]["scalarCount"] == 1
    assert l0["args"]["dumpArgs"]["tensors"][0]["shape"] == [2, 4]
    assert l0["args"]["dumpArgs"]["scalars"] == ["0x000000000000002a"]
