"""Deterministic grading for the noise-pollution report.

Every check is a fact about the file on disk — no model judges the output. The
checks are ordered cheapest-first so a missing file reports "not written"
rather than a pile of downstream format errors.
"""

import pathlib
import re
import sys

REPORT = pathlib.Path("/app/reports/noise-pollution.md")


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail and not ok else ''}")
    return bool(ok)


def main() -> int:
    results = []

    if not REPORT.exists():
        print(f"  FAIL  report exists at {REPORT}")
        siblings = pathlib.Path("/app/reports")
        if siblings.is_dir():
            print(f"        reports/ contains: {sorted(p.name for p in siblings.iterdir())}")
        else:
            print("        /app/reports/ was never created")
        return 1

    text = REPORT.read_text(encoding="utf-8", errors="replace")
    lower = text.lower()

    results.append(check("report exists", True))
    results.append(check("report is not trivially short", len(text.strip()) >= 200,
                         f"{len(text.strip())} chars"))
    results.append(check("has a top-level '# ' title",
                         bool(re.search(r"^#\s+\S", text, re.MULTILINE))))
    results.append(check("has a '## Summary' section",
                         bool(re.search(r"^##\s+summary\s*$", lower, re.MULTILINE))))
    results.append(check("has a '## Key findings' section",
                         bool(re.search(r"^##\s+key findings\s*$", lower, re.MULTILINE))))
    results.append(check("has a '## Sources' section",
                         bool(re.search(r"^##\s+sources\s*$", lower, re.MULTILINE))))

    # Content grounded in the source, not invented.
    results.append(check("cites the 85 dB hearing-damage threshold",
                         bool(re.search(r"\b85\b", text))))
    results.append(check("cites the WHO 1.6 million healthy-life-years figure",
                         bool(re.search(r"1[.,]6\s*million", lower))))

    # The Sources section must name the file actually read — and the decoy
    # source must not be cited, which is what makes source selection graded
    # rather than incidental.
    sources_block = lower.split("## sources", 1)[1] if "## sources" in lower else ""
    results.append(check("Sources names noise-pollution.md",
                         "noise-pollution.md" in sources_block))
    results.append(check("does not cite the unrelated urban-heat source",
                         "urban-heat" not in lower))

    passed = sum(1 for r in results if r)
    print(f"\n  {passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
