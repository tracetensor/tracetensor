from pathlib import Path

path = Path("/data/greeting.txt")
assert path.exists(), "greeting.txt was not created"
content = path.read_text(encoding="utf-8")
assert content == "Hello, TraceTensor!\n" or content == "Hello, TraceTensor!", (
    f"unexpected content: {content!r}"
)
print("greeting ok")
