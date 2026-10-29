"""Q&A assistant over the tracked YouTube data.

The LLM may only call the read-only tools in tools.py and submit an answer;
grounding.py checks every number and video id in that answer against the tool
results of the same turn before it is shown. agent.py runs the loop.
"""
