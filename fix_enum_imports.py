"""
Replace sa.Enum(..., create_type=False) with postgresql.ENUM(..., create_type=False)
in migration files. sa.Enum silently ignores create_type; only postgresql.ENUM honours it.
"""
import re, pathlib

MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations" / "versions"

# We do two things per file:
# 1. Add ENUM to the existing postgresql import line (or add the import if absent)
# 2. Replace every `sa.Enum(` that is part of a create_type=False block with `ENUM(`

def fix_file(path: pathlib.Path) -> bool:
    original = path.read_text(encoding="utf-8")
    text = original

    # Step 1 – fix imports ------------------------------------------------
    # If there's already a "from sqlalchemy.dialects.postgresql import ..."  line,
    # add ENUM to it (if not already there).
    pg_import_re = re.compile(
        r"from sqlalchemy\.dialects\.postgresql import ([^\n]+)", re.MULTILINE
    )
    m = pg_import_re.search(text)
    if m:
        imports = m.group(1)
        if "ENUM" not in imports:
            new_imports = imports.rstrip() + ", ENUM"
            text = text[:m.start(1)] + new_imports + text[m.end(1):]
    else:
        # No postgresql import yet – add one after the last "from sqlalchemy" import
        sa_import_re = re.compile(r"(from sqlalchemy[^\n]*\n)")
        last = None
        for mm in sa_import_re.finditer(text):
            last = mm
        if last:
            insert_pos = last.end()
            text = text[:insert_pos] + "from sqlalchemy.dialects.postgresql import ENUM\n" + text[insert_pos:]

    # Step 2 – replace sa.Enum( with ENUM( --------------------------------
    # We only replace occurrences that contain create_type=False somewhere inside
    # the call.  Strategy: find balanced-paren blocks starting with "sa.Enum("
    # and check if they contain create_type=False.
    result = []
    i = 0
    while i < len(text):
        # Look for next sa.Enum(
        idx = text.find("sa.Enum(", i)
        if idx == -1:
            result.append(text[i:])
            break
        result.append(text[i:idx])

        # Collect the whole call by counting parens
        start = idx + len("sa.Enum(")  # position just after the opening (
        depth = 1
        j = start
        while j < len(text) and depth > 0:
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        # text[idx:j] is the full `sa.Enum(...)`
        full_call = text[idx:j]

        if "create_type=False" in full_call:
            # Replace sa.Enum( → ENUM(
            full_call = "ENUM(" + full_call[len("sa.Enum("):]

        result.append(full_call)
        i = j

    text = "".join(result)

    if text != original:
        path.write_text(text, encoding="utf-8")
        return True
    return False


fixed = []
for path in sorted(MIGRATIONS_DIR.glob("*.py")):
    if path.name.startswith("__"):
        continue
    if fix_file(path):
        fixed.append(path.name)

if fixed:
    print("Fixed:", ", ".join(fixed))
else:
    print("No changes needed.")
