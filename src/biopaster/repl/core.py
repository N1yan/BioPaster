from pathlib import Path   
from ..providers import get_provider_class 
from ..agent.agent_loop import agent_loop, ToolEvent
from ..config import get_provider_config
from ..tool_system.context import ToolContext
from ..agent.conversation import Conversation
from ..tool_system.defaults import build_default_registry


def on_event(event: ToolEvent) -> None:
    if event.kind == "tool_use":
        print("tool_use_event")
    if event.kind == "tool_result":
        print("tool_result_event")
    if event.kind == "tool_error":
        msg = event.error or "Error"
        print(msg)
        
def on_text_chunk(chunk: str) -> None:
    if not chunk:
        return
    print(chunk, end="", flush=True)
  
class BioPasterREPL:  
    def __init__(self, provider_name="qwen"):  
        config = get_provider_config(provider_name)  
        provider_class = get_provider_class(provider_name)  
        self.conversation = Conversation()
        self.provider = provider_class(  
            api_key=config["api_key"],  
            base_url=config.get("base_url"),  
            model=config.get("default_model"),  
            context_window=config.get("context_window", 128000),  
            max_output_tokens=config.get("max_output_tokens")
        )  
        self.tool_registry = build_default_registry()  
        self.tool_context = ToolContext(
            # workspace_root=Path.cwd(),
            workspace_root=Path("/home/yan/test/BioPaster"),
            tools=self.tool_registry.list_tools(),
            notebook_path=Path("/home/yan/test/BioPaster/notebook.ipynb")
            )  
  
    def run(self):  
        while True:  
            try:  
                user_input = input("> ")  
            except EOFError:  
                break  
            if user_input.strip() in ("/exit", "/quit"):  
                break  
            self.conversation.add_user_message(user_input) 
            result = agent_loop(  
                conversation=self.conversation,  
                provider=self.provider,  
                tool_registry=self.tool_registry,  
                tool_context=self.tool_context,  
                max_turns=20,  
                stream=True,
                on_text_chunk=on_text_chunk,
                on_event=on_event
            )  
            