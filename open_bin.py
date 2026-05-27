import argparse
import json
from pathlib import Path

import numpy as np


DTYPE_MAP = {
    "FLOAT32": np.float32,
    "FLOAT16": np.float16,
    "FLOAT64": np.float64,
    "INT8": np.int8,
    "UINT8": np.uint8,
    "INT16": np.int16,
    "UINT16": np.uint16,
    "INT32": np.int32,
    "UINT32": np.uint32,
    "INT64": np.int64,
    "UINT64": np.uint64,
}


def read_tensor(bin_path: Path, tensor_meta: dict) -> np.ndarray:
    dtype = DTYPE_MAP[tensor_meta["dtype"]]
    with bin_path.open("rb") as f:
        f.seek(tensor_meta["bin_offset"])
        raw = f.read(tensor_meta["bin_size"])
    return np.frombuffer(raw, dtype=dtype).reshape(tensor_meta["shape"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect tensor_dump.bin using tensor_dump.json metadata.")
    parser.add_argument(
        "dump_dir",
        nargs="?",
        default="outputs/TestTensorDump_default_20260521_201006/tensor_dump",
        help="Directory containing tensor_dump.json and tensor_dump.bin.",
    )
    parser.add_argument("--index", type=int, default=None, help="Only print one tensor by index.")
    parser.add_argument("--limit", type=int, default=16, help="Number of flat elements to print.")
    args = parser.parse_args()

    dump_dir = Path(args.dump_dir)
    meta_path = dump_dir / "tensor_dump.json"
    meta = json.loads(meta_path.read_text())
    bin_path = dump_dir / meta["bin_file"]

    tensors = meta["tensors"]
    indices = [args.index] if args.index is not None else range(len(tensors))

    print(f"dump_dir: {dump_dir}")
    print(f"total_tensors: {meta['total_tensors']}")

    for i in indices:
        t = tensors[i]
        arr = read_tensor(bin_path, t)
        flat = arr.reshape(-1)
        print(
            f"\n[{i}] task={t['task_id']} stage={t['stage']} role={t['role']} "
            f"arg={t['arg_index']} func={t['func_id']}"
        )
        print("shape:", arr.shape, "dtype:", arr.dtype)
        print(f"first {args.limit}:", flat[: args.limit])
        print("min/max:", flat.min(), flat.max())


if __name__ == "__main__":
    main()
