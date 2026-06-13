"""
grid_agent_project — Autonomous Grid Self-Healing Agent

An LLM-in-the-loop system that reads power grid states from pandapower,
evaluates N-1 contingency violations, and autonomously generates optimal
JSON redispatch/curtailment commands to restore grid security.

Modules:
    grid_environment  — Physics engine (pandapower wrapper)
    grid_agent        — LLM brain (DeepSeek via OpenAI SDK)
    main              — Autonomous control loop entry point
"""

__version__ = "1.0.0"
