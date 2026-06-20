"""One-shot script: wrap all bare CREATE TYPE statements in migrations with a
DO/EXCEPTION block so they are idempotent.  Run with any Python 3.x."""

import re, pathlib

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations" / "versions"

# Match  op.execute(text(  ... "CREATE TYPE <name> AS ENUM"  ...  ))
# across potentially multiple string-literal lines inside the text() call.
# Strategy: find the whole op.execute(text(...)) block that contains CREATE TYPE.

BLOCK_RE = re.compile(
    r'op\.execute\(text\(\s*'          # op.execute(text(
    r'((?:"[^"]*"\s*)+)'               # one or more "..." string literals
    r'\s*\)\)',                        # ))
    re.DOTALL,
)

def wrap_create_type(match: re.Match) -> str:
    inner = match.group(1)          # the concatenated string literals
    if "CREATE TYPE" not in inner:
        return match.group(0)       # leave unrelated op.execute calls alone

    # Collect the individual quoted strings, strip their quotes
    parts = re.findall(r'"([^"]*)"', inner)
    sql = "".join(parts).strip()

    # Already wrapped?
    if "DO $$" in sql or "EXCEPTION" in sql:
        return match.group(0)

    # Rebuild as DO block
    new_sql = (
        f'op.execute(text(\n'
        f'    "DO $$ BEGIN "\n'
        f'    "{sql}; "\n'
        f'    "EXCEPTION WHEN duplicate_object THEN null; END $$"\n'
        f'))'
    )
    return new_sql


fixed_files = []
for path in sorted(MIGRATIONS_DIR.glob("*.py")):
    if path.name.startswith("__"):
        continue
    original = path.read_text(encoding="utf-8")
    patched = BLOCK_RE.sub(wrap_create_type, original)
    if patched != original:
        path.write_text(patched, encoding="utf-8")
        fixed_files.append(path.name)

if fixed_files:
    print("Patched:", ", ".join(fixed_files))
else:
    print("No changes needed.")
