"""
Sandbox Tools - Sandbox execution interface for code + files + images

Replaces the old execute_python, execute_bash, execute_javascript, and
session management tools with a cleaner interface:

- bash_tool: Execute any command (python3, node, shell commands)
- python_interpreter: Execute code against a persistent, kept-alive Python
  interpreter (variables survive across calls, unlike bash_tool)
- file_create: Create/overwrite files in sandbox workspace
- view: View file contents with line numbers
- str_replace: Replace text in files
- present_files: Generate download URLs for users
- ls: List direct children of a directory
- glob: Find files by name pattern
- grep: Search file contents by regex
- start_process / get_process_output / send_process_input / stop_process /
  list_processes: track a long-running background process across multiple
  tool calls
- (opt-in, see enable_git_tools) git_clone/git_status/git_add/git_commit/
  git_push/git_pull/git_branch/git_checkout
- (opt-in, see enable_run_tests) run_tests: run a test command and parse
  its output into a structured schema instead of raw stdout

All tools use preset packages only (no pip/npm install).
Files persist per work_item across sessions.

This package is framework-agnostic by default: `create_sandbox_tool_specs()`
returns a list of `ToolSpec` (see ./types.py) — a name, description, JSON-schema
parameters, and a plain async callable per tool, with zero LangChain
dependency. Nothing in this module (or bash_tool.py, file_tools.py,
present_files.py, search_tools.py, process_tools.py, git_tools.py,
python_interpreter_tool.py) imports langchain_core at module level.

`create_sandbox_tools()` / `create_sandbox_tools_with_manager()` remain for
backward compatibility: they build the same specs and adapt them into
LangChain tools (see ./adapters.py's `to_langchain_tools`), which requires
the optional `langchain` extra (`pip install boxkite-sandbox[langchain]`).
For a framework-agnostic OpenAI-style function-calling schema instead, use
`boxkite.tools.adapters.to_openai_functions`.
"""

from .types import ToolImageResult, ToolSpec
from .bash_tool import create_bash_tool, create_bash_tool_spec
from .file_tools import (
    create_file_create_tool,
    create_file_create_tool_spec,
    create_view_tool,
    create_view_tool_spec,
    create_str_replace_tool,
    create_str_replace_tool_spec,
)
from .http_request_tool import create_http_request_tool, create_http_request_tool_spec
from .present_files import create_present_files_tool, create_present_files_tool_spec
from .process_tools import (
    create_start_process_tool,
    create_start_process_tool_spec,
    create_get_process_output_tool,
    create_get_process_output_tool_spec,
    create_send_process_input_tool,
    create_send_process_input_tool_spec,
    create_stop_process_tool,
    create_stop_process_tool_spec,
    create_list_processes_tool,
    create_list_processes_tool_spec,
)
from .python_interpreter_tool import (
    create_python_interpreter_tool,
    create_python_interpreter_tool_spec,
)
from .run_tests_tool import (
    create_run_tests_tool,
    create_run_tests_tool_spec,
)
from .git_tools import (
    create_git_clone_tool,
    create_git_clone_tool_spec,
    create_git_status_tool,
    create_git_status_tool_spec,
    create_git_add_tool,
    create_git_add_tool_spec,
    create_git_commit_tool,
    create_git_commit_tool_spec,
    create_git_push_tool,
    create_git_push_tool_spec,
    create_git_pull_tool,
    create_git_pull_tool_spec,
    create_git_branch_tool,
    create_git_branch_tool_spec,
    create_git_checkout_tool,
    create_git_checkout_tool_spec,
    create_git_tools,
    create_git_tool_specs,
)
from .search_tools import (
    create_ls_tool,
    create_ls_tool_spec,
    create_glob_tool,
    create_glob_tool_spec,
    create_grep_tool,
    create_grep_tool_spec,
)
from .factory import (
    create_sandbox_tools,
    create_sandbox_tools_with_manager,
    create_sandbox_tool_specs,
)

__all__ = [
    "ToolSpec",
    "ToolImageResult",
    "create_bash_tool",
    "create_bash_tool_spec",
    "create_file_create_tool",
    "create_file_create_tool_spec",
    "create_view_tool",
    "create_view_tool_spec",
    "create_str_replace_tool",
    "create_str_replace_tool_spec",
    "create_http_request_tool",
    "create_http_request_tool_spec",
    "create_present_files_tool",
    "create_present_files_tool_spec",
    "create_start_process_tool",
    "create_start_process_tool_spec",
    "create_get_process_output_tool",
    "create_get_process_output_tool_spec",
    "create_send_process_input_tool",
    "create_send_process_input_tool_spec",
    "create_stop_process_tool",
    "create_stop_process_tool_spec",
    "create_list_processes_tool",
    "create_list_processes_tool_spec",
    "create_python_interpreter_tool",
    "create_python_interpreter_tool_spec",
    "create_run_tests_tool",
    "create_run_tests_tool_spec",
    "create_git_clone_tool",
    "create_git_clone_tool_spec",
    "create_git_status_tool",
    "create_git_status_tool_spec",
    "create_git_add_tool",
    "create_git_add_tool_spec",
    "create_git_commit_tool",
    "create_git_commit_tool_spec",
    "create_git_push_tool",
    "create_git_push_tool_spec",
    "create_git_pull_tool",
    "create_git_pull_tool_spec",
    "create_git_branch_tool",
    "create_git_branch_tool_spec",
    "create_git_checkout_tool",
    "create_git_checkout_tool_spec",
    "create_git_tools",
    "create_git_tool_specs",
    "create_ls_tool",
    "create_ls_tool_spec",
    "create_glob_tool",
    "create_glob_tool_spec",
    "create_grep_tool",
    "create_grep_tool_spec",
    "create_sandbox_tools",
    "create_sandbox_tools_with_manager",
    "create_sandbox_tool_specs",
]
