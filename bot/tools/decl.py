"""Print a Cpp2IL diffable-cs file as bare declarations (attributes stripped).

Usage: decl.py <Class> [<Class> ...]   (searched under decompiled/DiffableCs/App)
"""
import pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parents[2] / "decompiled" / "DiffableCs"
ATTR = re.compile(r"^\s*\[[A-Za-z]+(\(.*\))?\]\s*$")
RVA = re.compile(r'\[Address\(RVA = "(0x[0-9A-F]+)"')


def show(path):
    print(f"// {path.relative_to(ROOT)}")
    rva = None
    skip_depth = None  # brace depth of a compiler-generated nested class being skipped
    depth = 0
    for line in path.read_text().splitlines():
        if skip_depth is None and re.search(r"(class|struct) <", line):
            skip_depth = depth
        depth += line.count("{") - line.count("}")
        if skip_depth is not None:
            if depth <= skip_depth and "}" in line:
                skip_depth = None
            continue
        m = RVA.search(line)
        if m:
            rva = m.group(1)
            continue
        if ATTR.match(line) or not line.strip():
            continue
        if rva and "(" in line:
            line = f"{line}  // {rva}"
        rva = None
        print(line)


for name in sys.argv[1:]:
    hits = sorted(ROOT.glob(f"**/{name}.cs"))
    if not hits:
        print(f"// {name}: not found")
    for p in hits:
        show(p)
