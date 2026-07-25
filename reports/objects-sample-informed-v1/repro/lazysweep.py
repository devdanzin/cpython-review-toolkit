import re
import pathlib

FILES = (
    "tupleobject genericaliasobject unionobject templateobject descrobject "
    "odictobject funcobject weakrefobject structseq iterobject capsule "
    "interpolationobject cellobject lazyimportobject"
).split()

ROOT = pathlib.Path("/home/danzin/projects/cpython/Objects")

# "if (X->f == NULL)" / "if (!X->f)" / "if (X->f == NULL &&..." etc.
PAT = re.compile(
    r"if\s*\(\s*(?:!\s*)?\(?\s*([A-Za-z_][A-Za-z0-9_]*)\s*->\s*([A-Za-z_][A-Za-z0-9_]*)\s*"
    r"(?:==\s*NULL|==\s*0)?\s*\)?"
)


def main() -> None:
    for name in FILES:
        p = ROOT / f"{name}.c"
        lines = p.read_text().splitlines()
        for i, ln in enumerate(lines):
            m = PAT.search(ln)
            if not m:
                continue
            # require a NULL-ish test
            if "NULL" not in ln and "!" not in ln:
                continue
            var, fld = m.group(1), m.group(2)
            asn = re.compile(
                rf"{re.escape(var)}\s*->\s*{re.escape(fld)}\s*=[^=]"
            )
            for j in range(i + 1, min(i + 7, len(lines))):
                if asn.search(lines[j]):
                    print(f"{p.name}:{i + 1}  {var}->{fld}")
                    print(f"    IF   {ln.strip()}")
                    print(f"    SET  {lines[j].strip()}  (line {j + 1})")
                    break


if __name__ == "__main__":
    main()
