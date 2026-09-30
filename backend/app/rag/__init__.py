"""Retrieval-augmented answers for coverage questions (intent=question only).

Kept apart from the rule engine on purpose. RAG explains what her documents
and the regulations say; it never writes to ``Facts``, never feeds a verdict,
and never promises that a claim will be paid. Anything it cannot support from
a retrieved source becomes NO_SOURCE and is handed to the N5 router.

    ingest.py    sources.yaml -> pages -> clause chunks -> Chroma (data/index)
    retrieve.py  her insurer + product, or regulation; top 6
    answer.py    answer only from sources, cited [insurer, doc_type, p.X]
    eval.py      golden questions -> accuracy, citation rate, correct refusals
"""
