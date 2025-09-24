from packages.utils.tools import args_hash, canon_args, is_valid_tool_json
from packages.storage import repo

class ToolRuntime:
    def __init__(self, thread_id: str, attempt_id: str, now: int, settings):
        self.thread_id = thread_id
        self.attempt_id = attempt_id
        self.now = now
        self.settings = settings

    def try_execute(self, tool_name: str, args: dict) -> dict:
        h = args_hash(args, self.settings.TOOL_ARGS_HASH_ALGO)
        cached = repo.get_tool_run(self.thread_id, tool_name, h)
        if cached:
            return {"cached": True, "text": cached.result_text}
        text = self._dispatch(tool_name, args)
        repo.insert_tool_run(self.thread_id, self.attempt_id, tool_name, canon_args(args), h, text, "done", self.now)
        return {"cached": False, "text": text}

    def _dispatch(self, tool_name, args):
        # TODO: implement actual tool dispatch logic
        return f"[tool:{tool_name}] called with args: {args}"
