"""Platform mode: the Python half of JobRadar.

The Go orchestrator owns workflows and ingestion; this package owns everything that
reasons about a job. It runs as a Temporal worker on the `jobradar-agent` task queue:

    profiles    stored profile JSON -> config.Profile (personal or demo visitor)
    store       Postgres access for the agent activities
    search      Elasticsearch index, Gemini embeddings, hybrid BM25 + kNN with RRF
    graphs      LangGraph agents: screening (assess/validate/repair) and writing
                (draft/critique/revise)
    activities  the Temporal activities the Go RadarWorkflow calls by name
    worker      process entry point (`jobradar worker`)
"""
