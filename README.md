# kindle_to_foliate
```
python3 kindle_to_foliate.py <original_epub_file> <annotation file from kindle>
```

Notes:
1. The output file will be in the same directory as the original epub file, with the name `<original_epub_file>_annotated.epub`.
2. Somme annotations may not be correctly placed in the output file, especially if the original epub file has a complex structure. There may be some cases where the annotations can not be placed at all. In such cases, the program will print a warning message.
