"""consilium-health: a multi-agent clinical-information assistant.

Not medical advice.  This is an educational software project.  It does not diagnose, treat, or
provide clinical guidance, and it must not be used for real medical decisions.  No patient data of
any kind may be used with it.

Layering (a reviewer should be able to point at any module and name its layer):

``cli`` / ``api``            Interface
``router``                   Router      -- planner, blackboard, synthesizer
``agents`` / ``loop``        Agents      -- the three specialists and the ReAct engine they share
``skills``                   Skills      -- seven atomic, self-describing tools plus the registry
``retrieval`` / ``memory``   Substrate
``safety`` / ``llm``
``trace`` / ``config`` / ``log``
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
