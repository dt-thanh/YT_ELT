"""Scripted fake of the OpenAI client, shared by tests. No network."""
import copy
from types import SimpleNamespace as NS

from youtube_intel.config import ModelPrice

PRICING = (ModelPrice("gpt-4o-mini", 0.15, 0.60),
           ModelPrice("text-embedding-3-small", 0.02, None))


class APITimeoutError(Exception):
    """Same class name as openai.APITimeoutError."""


def chat_reply(content="{}", *, prompt_tokens=100, completion_tokens=20,
               model="gpt-4o-mini-2024-07-18", tool_calls=None, refusal=None, usage=True):
    msg = NS(content=content, tool_calls=tool_calls, refusal=refusal)
    return NS(choices=[NS(message=msg)], model=model,
              usage=NS(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
              if usage else None)


class FakeOpenAI:
    """Returns (or raises) the scripted items in order; records the requests."""

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []
        self.chat = NS(completions=NS(create=self._next))
        self.embeddings = NS(create=self._next)

    def _next(self, **kwargs):
        # Deep copy: callers keep appending to the same messages list after the call,
        # but the real API serialises the request at call time.
        self.requests.append(copy.deepcopy(kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_call(name, args, call_id=None):
    """One tool call as the OpenAI SDK returns it (arguments are a JSON string)."""
    import json
    return NS(id=call_id or f"call_{name}", type="function",
              function=NS(name=name, arguments=args if isinstance(args, str) else json.dumps(args)))


def calls(*tool_calls):
    """A chat reply that only calls tools."""
    return chat_reply(None, tool_calls=list(tool_calls))
