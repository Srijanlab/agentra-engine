# Claude Guidelines & Codebase Rules

When interacting with this codebase, please adhere to the following architectural, formatting, and behavioral guidelines.

## 1. Comment & Documentation Style
- **Concise Docstrings:** Do not write excessively long, multi-paragraph docstrings. Module and function docstrings should be limited to a single, clear summary sentence.
- **No Dead Code:** Do not leave large blocks of commented-out code. If code is no longer needed, delete it completely.
- **Avoid Over-Commentation:** Rely on readable, self-documenting code. Do not write large paragraphs of inline `#` comments unless absolutely critical for explaining an obscure bug or workaround.

## 2. Design Patterns & Architecture
- **Single Responsibility Principle (SRP):** Every class, function, and module should have one, and only one, reason to change. Do not create "god objects" or massive utility files.
- **Sub-folder Organization:** Organize code logically into domain-specific subfolders. Avoid dumping everything into the root of a package. Use subfolders to encapsulate specific features and their internal logic.

## 3. File Size Constraints
- **500 Lines Limit:** No file in this codebase should exceed 500 lines. If a file is approaching or exceeding 500 lines, it is a clear sign that SRP is being violated. You must refactor and split the file into smaller, focused modules inside an appropriate subfolder.
