import sys, os
# Ensure the current working directory is on sys.path so that the local
# `agentra` package shadows any externally installed copy.
# Prepend the repository root to give it priority.
repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
# Ensure root comes before any external editable installations
for idx, p in enumerate(sys.path):
    if 'editable' in p or 'agentra-0.1.0' in p:
        sys.path.insert(idx, repo_root)
        break
else:
    sys.path.insert(0, repo_root)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
# Also make sure our local `agentra/` module directory is visible.
agentra_path = os.path.join(repo_root, 'agentra')
if agentra_path not in sys.path and os.path.isdir(agentra_path):
    sys.path.insert(0, agentra_path)
# This file is automatically imported by Python when the interpreter starts.
