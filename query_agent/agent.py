"""ADK entrypoint: root_agent answers questions over archived documents via RAG.

Local `adk web` / `adk run` only — do not expose this agent on a public interface.
Production Ask uses deepcatalog.ask.ask_archive (no ADK tool loop).
"""

from deepcatalog.adk_debug import build_query_agent

root_agent = build_query_agent()
