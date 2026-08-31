import sys
from pathlib import Path
SRC = Path(__file__).resolve().parents[1]  # .../BioPaster/src
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


from dotenv import load_dotenv
import os
from anthropic import Anthropic
from datetime import datetime
from pathlib import Path
from ..tool_system.defaults import build_default_registry
from ..tool_system.persist import Persist
from ..tool_system.protocol import ToolResult
from ..tool_system.map_result import map_tool_result, map_tool_result_gemma
import json


WORKDIR = Path("/home/yan/test/BioPaster")
os.chdir(WORKDIR)
load_dotenv(WORKDIR / ".env", override=True)

PROVIDER = os.getenv("PROVIDER")
BASE_URL = os.getenv(f"{PROVIDER}_BASE_URL")
API_KEY = os.getenv(f"{PROVIDER}_API_KEY")
MODEL = os.getenv(f"{PROVIDER}_MODEL_1")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8192"))

client = Anthropic(base_url=BASE_URL, api_key=API_KEY)

# ── Tool Result Persist ──
TOOL_RESULT_DIR = WORKDIR / ".tool_results"
TOOL_RESULT_DIR.mkdir(exist_ok=True)
persist = Persist(TOOL_RESULT_DIR, max_content_length=20000)

def process_tool_result(tool_result: ToolResult, tool_use_id: str) -> str:
    tool_result = persist.persist(tool_result, tool_use_id)
    if "gemma" in MODEL:
        return map_tool_result_gemma(tool_result, tool_use_id)
    else:
        return map_tool_result(tool_result, tool_use_id)

# ── MCP Clients ──
mcp_clients = {}

# ── Tools ──
tool_registry = build_default_registry()
# Convert tools to schemas (Anthropic format)
tool_schemas = []
for spec in tool_registry.list_specs():
    tool_schemas.append({
        "name": spec.name,
        "description": spec.description,
        "input_schema": spec.input_schema,
    })


# ── System Prompt ──
PROMPT_SECTIONS = {
    "identity": ("You are BioPaster, a helpful assistant that can help researchers do biology research, "
                 "including but not limited to literature review, data analysis, and experimental design."
                 "[NOTE] Important: since we are currently in the development stage, in addition to your regular responses,"
                 "you are required to report any errors, warnings, or difficulties you encounter while performing"
                 "tasks. Whenever an error or warning occurs, you must provide the exact error/warning message verbatim"),
    "tools": "Available tools:",
    "workspace": f"Working directory: {WORKDIR}",
    "language": "You should use the language of the user's question to respond.",
    "memory": "Relevant memories are injected below when available."
}

def assemble_system_prompt(context: dict) -> str:
    sections = [PROMPT_SECTIONS["identity"],
                PROMPT_SECTIONS["tools"],
                PROMPT_SECTIONS["language"],
                PROMPT_SECTIONS["workspace"]]
    # sections.append(f"Current time: {datetime.now().isoformat(timespec='seconds')}")
    sections.append("Skills catalog:\n" + "")
    if context.get("memories"):
        sections.append(f"Relevant memories:\n{context['memories']}")
    mcp_names = list(mcp_clients.keys())
    if mcp_names:
        sections.append(f"Connected MCP servers: {', '.join(mcp_names)}")
    return "\n\n".join(sections)

def call_llm(messages: list, context: dict, tools: list, max_tokens: int) -> str:
    system = assemble_system_prompt(context)
    return client.messages.create(
        model=MODEL,
        system=system,
        messages=messages,
        tools=tools,
        max_tokens=max_tokens,
        extra_body={"reasoning": {"enabled": True, "exclude": False}}
    )

# ── Agent Loop ──
def agent_loop(messages: list, context: dict):
    while True:
        try:
            response = call_llm(messages, context, tool_schemas, MAX_TOKENS)
            content = response.content
            if content is None:
                content = [{"type": "text", "text": ""}]
                
            # Print the reasoning
            for block in content:
                btype = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
                if btype == "thinking":
                    text = getattr(block, "thinking", None) or (block.get("thinking") if isinstance(block, dict) else "")
                    print("\033[90m[thinking]\n" + (text or "") + "\033[0m")
                elif btype == "redacted_thinking":
                    print("\033[90m[thinking redacted]\033[0m")
                    
            messages.append({"role": "assistant", "content": content})
        except Exception as e:
            messages.append({"role": "assistant", "content": [
                {"type": "text", "text": f"[Error] {type(e).__name__}: {e}"}]})
            return
        
        if response.stop_reason != "tool_use":
            return
        
        results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_result = tool_registry.dispatch(block)
                tool_block = process_tool_result(tool_result, block.id)
                
                if "gemma" in MODEL:
                    results.extend(tool_block)
                else:
                    results.append(tool_block)
        
        messages.append({"role": "user", "content": results})
        

if __name__ == "__main__":
    print("Enter a question, press Enter to send. Type q to quit.\n")
    history = []
    context = {}
    while True:
        try:
            user_input = input("\033[36m >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if user_input.strip().lower() in ("q", "exit"):
            break
        history.append({"role": "user", "content": user_input})
        agent_loop(history, context)
        response_content = history[-1]["content"]
        if isinstance(response_content, list):
            for block in response_content:
                if isinstance(block, dict) and block.get("type", None) == "text":
                    print(block["text"])
                elif getattr(block, "type", None) == "text":
                    print(block.text)
        print()