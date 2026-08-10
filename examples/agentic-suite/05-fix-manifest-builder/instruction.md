# Fix manifest builder — deploy bundle lists wrong paths

## PR description

`/app/manifest.py` function `list_files(root)` should walk a project directory
and return **sorted relative POSIX paths** (use `/`) for every **file** (not
directories), relative to `root`.

Project sample lives at `/data/project/`.

Fix `/app/manifest.py`. Do not change `/data/project` or tests.

Expected list:
- `README.md`
- `src/app.py`
- `src/util.py`
