"""ADK entrypoint: root_agent is the document ingest pipeline.

Local `adk web` / `adk run` only — do not expose this agent on a public interface.
Production ingest uses deepcatalog.ingest.ingest_document.
"""

from deepcatalog.adk_debug import build_pipeline_agent

root_agent = build_pipeline_agent()
