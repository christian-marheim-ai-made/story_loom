
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"GGUF"

(UINT8, INT8, UINT16, INT16, UINT32, INT32, FLOAT32, BOOL, STRING, ARRAY,
 UINT64, INT64, FLOAT64) = range(13)

_FIXED = {
    UINT8: ("<B", 1), INT8: ("<b", 1), UINT16: ("<H", 2), INT16: ("<h", 2),
    UINT32: ("<I", 4), INT32: ("<i", 4), FLOAT32: ("<f", 4), BOOL: ("<?", 1),
    UINT64: ("<Q", 8), INT64: ("<q", 8), FLOAT64: ("<d", 8),
}

MAX_ARRAY_ITEMS = 64

INITIAL_WINDOW = 8 * 1024 * 1024
MAX_WINDOW = 128 * 1024 * 1024


class GGUFError(ValueError):
    pass


def _read(fh: BinaryIO, n: int) -> bytes:
    data = fh.read(n)
    if len(data) != n:
        raise GGUFError(f"header ends early: wanted {n} bytes, got {len(data)}")
    return data


def _u32(fh: BinaryIO) -> int:
    return struct.unpack("<I", _read(fh, 4))[0]


def _u64(fh: BinaryIO) -> int:
    return struct.unpack("<Q", _read(fh, 8))[0]


def _string(fh: BinaryIO) -> str:
    length = _u64(fh)
    if length > 64 * 1024 * 1024:
        raise GGUFError(f"implausible string length {length} — file is probably not a GGUF")
    return _read(fh, length).decode("utf-8", errors="replace")


def _skip_string(fh: BinaryIO) -> None:
    fh.seek(_u64(fh), 1)


def _value(fh: BinaryIO, vtype: int) -> Any:
    if vtype in _FIXED:
        fmt, size = _FIXED[vtype]
        return struct.unpack(fmt, _read(fh, size))[0]
    if vtype == STRING:
        return _string(fh)
    if vtype == ARRAY:
        elem_type = _u32(fh)
        count = _u64(fh)
        if elem_type in _FIXED:
            fmt, size = _FIXED[elem_type]
            keep = min(count, MAX_ARRAY_ITEMS)
            items = [struct.unpack(fmt, _read(fh, size))[0] for _ in range(keep)]
            fh.seek((count - keep) * size, 1)
        elif elem_type == STRING:
            items = []
            for i in range(count):
                if i < MAX_ARRAY_ITEMS:
                    items.append(_string(fh))
                else:
                    _skip_string(fh)
        else:
            raise GGUFError(f"unsupported array element type {elem_type}")
        return {"_array": True, "count": count, "elem_type": elem_type,
                "items": items, "truncated": count > len(items)}
    raise GGUFError(f"unsupported metadata value type {vtype}")


def read_header(path: Path, read_tensors: bool = True,
                initial_window: int = INITIAL_WINDOW) -> dict:
    path = Path(path)
    size = path.stat().st_size
    window = max(4096, min(initial_window, size))
    while True:
        with open(path, "rb") as raw:
            buffer = raw.read(window)
        try:
            return _parse(io.BytesIO(buffer), path, size, read_tensors)
        except GGUFError as exc:
            outgrew = "ends early" in str(exc) and window < min(size, MAX_WINDOW)
            if not outgrew:
                raise
            window = min(window * 2, size, MAX_WINDOW)


def _parse(fh: BinaryIO, path: Path, size: int, read_tensors: bool) -> dict:
    if _read(fh, 4) != MAGIC:
        raise GGUFError(f"{path.name} does not start with the GGUF magic bytes")
    version = _u32(fh)
    if version not in (2, 3):
        raise GGUFError(f"unsupported GGUF version {version} (this reads v2 and v3)")
    tensor_count = _u64(fh)
    kv_count = _u64(fh)
    if kv_count > 100_000 or tensor_count > 1_000_000:
        raise GGUFError("header counts are implausible; the file may be truncated")

    meta: dict[str, Any] = {}
    for _ in range(kv_count):
        key = _string(fh)
        meta[key] = _value(fh, _u32(fh))

    tensors: list[dict] = []
    type_counts: dict[int, int] = {}
    parameters = 0
    if read_tensors:
        for _ in range(tensor_count):
            name = _string(fh)
            n_dims = _u32(fh)
            if n_dims > 8:
                raise GGUFError(f"tensor {name!r} claims {n_dims} dimensions")
            dims = [_u64(fh) for _ in range(n_dims)]
            ttype = _u32(fh)
            _u64(fh)
            count = 1
            for d in dims:
                count *= d
            parameters += count
            type_counts[ttype] = type_counts.get(ttype, 0) + 1
            if len(tensors) < 8:
                tensors.append({"name": name, "dims": dims, "type": ttype})

    arch = meta.get("general.architecture", "unknown")

    def arch_key(name: str, default=None):
        return meta.get(f"{arch}.{name}", default)

    head_count = arch_key("attention.head_count")
    head_count_kv = arch_key("attention.head_count_kv", head_count)
    embedding_length = arch_key("embedding_length")
    key_length = arch_key("attention.key_length")
    if key_length is None and head_count and embedding_length:
        key_length = embedding_length // head_count
    value_length = arch_key("attention.value_length", key_length)

    info = {
        "path": str(path),
        "file_size": size,
        "gguf_version": version,
        "tensor_count": tensor_count,
        "metadata_count": kv_count,
        "architecture": arch,
        "name": meta.get("general.name"),
        "basename": meta.get("general.basename"),
        "file_type_raw": meta.get("general.file_type"),
        "quantization_version": meta.get("general.quantization_version"),
        "block_count": arch_key("block_count"),
        "context_length": arch_key("context_length"),
        "embedding_length": embedding_length,
        "head_count": head_count,
        "head_count_kv": head_count_kv,
        "key_length": key_length,
        "value_length": value_length,
        "rope_freq_base": arch_key("rope.freq_base"),
        "sliding_window": arch_key("attention.sliding_window"),
        "sliding_window_pattern": arch_key("attention.sliding_window_pattern"),
        "has_chat_template": "tokenizer.chat_template" in meta,
        "vocab_size": (meta.get("tokenizer.ggml.tokens") or {}).get("count"),
        "parameters": parameters,
        "tensor_type_counts": type_counts,
        "first_tensors": tensors,
    }
    info["bits_per_weight"] = (size * 8 / parameters) if parameters else None
    info["chat_template_head"] = (meta.get("tokenizer.chat_template") or "")[:400] \
        if info["has_chat_template"] else ""
    return info


def kv_cache_bytes(info: dict, ctx: int, bytes_per_element: float = 2.0) -> int | None:
    layers, kv_heads = info.get("block_count"), info.get("head_count_kv")
    head_dim = info.get("key_length")
    if not all(isinstance(v, int) and v > 0 for v in (layers, kv_heads, head_dim, ctx)):
        return None
    return int(2 * layers * kv_heads * head_dim * ctx * bytes_per_element)


def check_against_spec(info: dict, spec) -> list[str]:
    problems: list[str] = []
    if info["architecture"] == "unknown":
        problems.append("the file declares no architecture")
    ctx_max = info.get("context_length")
    if isinstance(ctx_max, int) and spec.ctx > ctx_max:
        problems.append(
            f"config asks for ctx {spec.ctx:,} but the model was trained for "
            f"{ctx_max:,} — llama-server may refuse or produce nonsense past that"
        )
    if not info["has_chat_template"]:
        problems.append(
            "no chat template embedded in the GGUF. story-studio relies on the "
            "template inside the file; without one llama-server has to guess"
        )
    bpw = info.get("bits_per_weight")
    if bpw and not (2.0 < bpw < 9.0):
        problems.append(f"bits-per-weight is {bpw:.2f}, which is not a plausible quant — "
                        f"the file may be truncated")
    return problems
