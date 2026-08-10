A prose file is at `/data/sample.txt`.

Build word frequencies from the text using these rules:

- Split into words on non-alphanumeric characters
- Compare words case-insensitively (store lowercase)
- Ignore empty tokens

Write `/data/top_words.json` with exactly:

```json
{"words": [["word", count], ...]}
```

Include the **top 5** words by count (highest first). Break ties by alphabetical
word order. Each `word` must be lowercase.
