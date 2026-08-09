"""Shared foundation for the v2 pipeline components (collectors, evaluator,
pool builder, analysis runner, orchestrator).

Components communicate only through the files named in ``specs/*-contract.md``;
this package holds the contract models, validators, and the small file/session
utilities every component needs. It deliberately does not import from
``tradingagents`` — collectors and the evaluator must run without the heavy
pipeline dependencies.
"""
