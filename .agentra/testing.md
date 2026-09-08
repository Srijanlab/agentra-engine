<!-- owner: agent:testing -->
<!-- source-sha: cae3ec8fb32537e13541040df2da15c9cbdbc663 -->

# Testing

## Local test recipe
- test: `pytest tests`
- test: `find agentra -name '*.py' -print0 | xargs -0 -n1 python -m py_compile`
- build: `pip install -e .[dev]`
- build: `pip install -r requirements.txt`
- build: `docker build -t agentra-engine .`

## Last run
(none yet)
