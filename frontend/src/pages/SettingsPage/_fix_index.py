"""Remove orphaned Python tab JSX from index.tsx (lines 1997-2268)."""
import pathlib

target = pathlib.Path(__file__).parent / "index.tsx"
lines = target.read_text(encoding="utf-8").splitlines(keepends=True)

# Keep lines 1-1996 (index 0-1995) and lines 2269+ (index 2268+)
fixed = lines[:1996] + lines[2268:]
target.write_text("".join(fixed), encoding="utf-8")
print(f"Done: removed {len(lines) - len(fixed)} lines, new total = {len(fixed)}")
