"""
Standalone smoke test for server.py -- connects a real MCP client over
stdio (the same way Claude Desktop does) and checks the handshake, tool
list, and one read-only tool call all work. Doesn't write anything.

Usage:
  python3 test_server.py
"""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_PATH = str(Path(__file__).resolve().parent / "server.py")
EXPECTED_TOOLS = {
    "get_progress_summary", "log_session", "get_session_detail", "resolve_weak_area",
    "get_catalog", "suggest_next_problems", "add_custom_problem",
    "save_practice_doc", "get_practice_doc", "list_practice_docs",
    "scan_dsa_directory", "import_solved_dsa_problem", "import_solved_dsa_problems",
    "save_dsa_solution", "save_candidate_context", "get_candidate_context",
    "scan_lld_directory", "read_lld_solution", "import_solved_lld_problem",
    "import_solved_lld_problems", "save_lld_solution",
    "start_mock_attempt", "list_mock_attempts", "save_mock_evaluation",
    "save_ideal_solution", "save_simple_solution", "get_lld_feedback",
    "log_lld_drill", "get_lld_drill_log",
}


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[SERVER_PATH])
    print(f"Spawning: {sys.executable} {SERVER_PATH}")
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Handshake OK.")

            tools_result = await session.list_tools()
            found = {t.name for t in tools_result.tools}
            print(f"{len(found)} tool(s) exposed.")
            missing = EXPECTED_TOOLS - found
            if missing:
                print(f"FAIL: missing expected tools: {sorted(missing)}")
                return 1

            result = await session.call_tool("get_progress_summary", {})
            text = "".join(c.text for c in result.content if hasattr(c, "text"))
            print("--- get_progress_summary() ---")
            print(text)
            print("--- end ---")

    print("\nAll checks passed: server starts, speaks MCP, and responds to a real tool call.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
