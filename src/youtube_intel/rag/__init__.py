"""Retrieval over video titles and descriptions (pgvector + Postgres full-text).

Only public metadata is indexed - no transcripts. Descriptions are untrusted
text and are passed to the LLM as data.
"""
