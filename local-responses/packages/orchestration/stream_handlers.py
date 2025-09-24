class ToolCallAssembler:
    def __init__(self):
        self.buf = ""

    def feed(self, delta: str) -> list[dict]:
        from packages.utils.tools import is_valid_tool_json
        self.buf += delta
        results = []
        while True:
            s = self.buf.lstrip()
            if not s:
                break
            # Try to find a valid JSON object at the start
            if s[0] != '{':
                # skip until next possible JSON
                idx = s.find('{')
                if idx == -1:
                    self.buf = ""
                    break
                s = s[idx:]
                self.buf = s
            # Try to parse
            for i in range(1, len(s)+1):
                try:
                    chunk = s[:i]
                    valid, data = is_valid_tool_json(chunk)
                    if valid:
                        results.append(data)
                        self.buf = s[i:]
                        break
                except Exception:
                    continue
            else:
                break
        return results

    def finalize(self):
        self.buf = ""
