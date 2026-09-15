"""Repair the known Hermes API cache-usage omission without rewriting other code."""
import ast
import os
from pathlib import Path
import sys
import tempfile


class UnsupportedSource(ValueError):
    pass


def same(left, right):
    return ast.dump(left, include_attributes=False) == ast.dump(right, include_attributes=False)


def expr(source):
    return ast.parse(source, mode="eval").body


LEGACY_USAGE = '''{
    "input_tokens": getattr(agent, "session_prompt_tokens", 0) or 0,
    "output_tokens": getattr(agent, "session_completion_tokens", 0) or 0,
    "total_tokens": getattr(agent, "session_total_tokens", 0) or 0,
}'''
FIXED_USAGE = LEGACY_USAGE.rstrip()[:-1] + '''
    "cache_read_tokens": getattr(agent, "session_cache_read_tokens", 0) or 0,
    "cache_write_tokens": getattr(agent, "session_cache_write_tokens", 0) or 0,
}'''
LEGACY_RESPONSE = '{key: usage.get(key, 0) for key in _USAGE_TOKEN_KEYS}'
FIXED_RESPONSE = '''{
    **{key: usage.get(key, 0) for key in _USAGE_TOKEN_KEYS},
    "input_tokens_details": {
        "cached_tokens": usage.get("cache_read_tokens", 0),
        "cache_write_tokens": usage.get("cache_write_tokens", 0),
    },
}'''


def patched_source(source):
    tree = ast.parse(source)
    edits = []
    targets = (
        ("_finish_turn_result", LEGACY_USAGE, FIXED_USAGE),
        ("_responses_usage_payload", LEGACY_RESPONSE, FIXED_RESPONSE),
    )
    lines = source.splitlines(keepends=True)
    # AST columns count UTF-8 bytes, not Unicode characters.
    raw = source.encode("utf-8")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
    for name, legacy, fixed in targets:
        functions = [node for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
        if len(functions) != 1:
            raise UnsupportedSource(f"expected one {name}")
        if name == "_finish_turn_result":
            nodes = [node.value for node in functions[0].body if isinstance(node, ast.Assign)
                     and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                     and node.targets[0].id == "usage"]
        else:
            nodes = [node.value for node in functions[0].body if isinstance(node, ast.Return)]
        if len(nodes) != 1:
            raise UnsupportedSource(f"unsupported {name} control flow")
        old = [node for node in nodes if same(node, expr(legacy))]
        new = [node for node in nodes if same(node, expr(fixed))]
        if len(new) == 1 and not old:
            continue
        if len(old) != 1 or new:
            raise UnsupportedSource(f"unsupported {name} usage shape")
        node = old[0]
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        indent = " " * node.col_offset
        replacement = fixed.replace("\n", "\n" + indent).encode("utf-8")
        edits.append((start, end, replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        raw = raw[:start] + replacement + raw[end:]
    updated = raw.decode("utf-8")
    compile(updated, "api_server.py", "exec")
    return updated


def patch_file(path):
    path = Path(path)
    if not path.exists():
        return False
    if path.is_symlink():
        raise UnsupportedSource("API source must not be a symlink")
    original = path.read_bytes()
    updated = patched_source(original.decode("utf-8")).encode("utf-8")
    if updated == original:
        return False
    stat = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=".cache-usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), stat.st_mode & 0o7777)
            if (os.fstat(stream.fileno()).st_uid, os.fstat(stream.fileno()).st_gid) != (stat.st_uid, stat.st_gid):
                os.fchown(stream.fileno(), stat.st_uid, stat.st_gid)
        if path.read_bytes() != original:
            raise UnsupportedSource("API source changed while patching")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


if __name__ == "__main__":
    try:
        print("changed" if patch_file(sys.argv[1]) else "unchanged")
    except (OSError, SyntaxError, UnsupportedSource) as error:
        print(f"Hermes cache usage patch refused: {error}", file=sys.stderr)
        sys.exit(1)
