# HTML Partial Extractor

This tool is designed to **automatically extract and refactor HTML partials**. It scans a directory of HTML files,
identifies common structural patterns, headers, footers, and repeated components, and extracts them into separate
partial files. It then replaces the original code with `@@include` statements, streamlining the maintenance of static
HTML projects.

## Concepts

This project utilizes several algorithms and concepts often used in data mining and document clustering to identify "
near-duplicate" HTML structures, rather than relying on strict exact string matching:

* **Locality Sensitive Hashing (LSH):** Uses `MinHash` (via `datasketch`) to rapidly fingerprint HTML fragments. This
  allows the tool to quickly find candidate pairs of similar HTML structures across many files without comparing every
  pair to every other pair.
* **SimHash:** Calculates a fingerprint for HTML tags based on their descendants. The Hamming distance between these
  fingerprints is used to determine how structurally similar two potential candidates are.
* **Structural Canonicalization:** Before comparison, HTML nodes are "normalized" (removing specific classes, IDs, and
  whitespace) to ensure that minor content differences (like "active" classes on a nav bar) don't prevent the detection
  of the underlying structural pattern.
* **Tree/Node Metrics:** Uses node counts and structural path shingling to assess the size and complexity of DOM
  subtrees.

## Installation

This project uses `virtualenv` for dependency management.

1. **Create a virtual environment** (if you haven't already):
   ```bash
   python3 -m venv .venv
   ```

2. **Activate the environment**:
    * On macOS/Linux:
      ```bash
      source .venv/bin/activate
      ```
    * On Windows:
      ```bash
      .venv\Scripts\activate
      ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

To run the refactorer, execute the `main.py` script providing the source directory (containing your original HTML files)
and a destination directory (where the refactored code will be saved).

```bash 
  python main.py <src_directory> <dest_directory>
```