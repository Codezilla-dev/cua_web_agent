"""The discovery agent: the loop, the planner, and the verifier.

`verify.py` is deterministic and model-free. `planner.py` is the only module that
talks to an LLM. `loop.py` sequences the stages and owns the budgets.
"""
