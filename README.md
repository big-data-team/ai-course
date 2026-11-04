# About

The repository is used during the following courses:
1. Practical AI Course: Agentic Coding & AI Agents (AI Agents Engineer Specialization)

- AI agents: tools and MCP
- agentic coding and agentic environment
- LLMs, Ralph-loop and evals
- context & memory, agent harness
- capstone project
- https://bigdatateam.org/ru/artificial-intelligence-course

# Environment Configuration

Solutions are graded on Python 3.10 with the packages from [requirements.txt](requirements.txt).

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/)
2. Download [requirements.txt](requirements.txt)
3. Create the environment:
```bash
# uv downloads Python 3.10 itself if there is none on the machine
uv venv --python 3.10
source .venv/bin/activate
uv pip install -r requirements.txt
```

For more information about Python virtual environments see:
* https://docs.astral.sh/uv/pip/environments/
* https://packaging.python.org/en/latest/guides/installing-using-pip-and-virtual-environments/
* https://docs.python.org/3/library/venv.html

See the Python version and the packages of the environment with the help of:
```bash
uv run python --version
uv pip list
```

If you need to remove the environment, delete its folder:
```bash
rm -rf .venv
```

# Programming Assignments

## HW01: AI Tool for Datetime Parsing (`ai.tool_datetime_parser`)

Starter files: [hw01_tool_datetime_parser](hw01_tool_datetime_parser)

| File | What it is |
|---|---|
| `task_Surname_Name_tool_datetime_parser.py` | the function template: rename it to `task_<Surname>_<Name>_tool_datetime_parser.py` |
| `tool.json` | the MCP Tool description: write your texts instead of `<...>`, keep `outputSchema` as is |
| `open_cases.json` | the open test cases of the Grader |
| `check_open_cases.py` | a self-check on the open cases |

How to run the self-check:
```bash
cd hw01_tool_datetime_parser
uv run python check_open_cases.py task_Dral_Alexey_tool_datetime_parser.py
```

# Contributors

<a href = "https://github.com/big-data-team/ai-course/graphs/contributors">
  <img src = "https://contrib.rocks/image?repo=big-data-team/ai-course"/>
</a>

<!-- made with [contributors-img](https://contrib.rocks) -->
