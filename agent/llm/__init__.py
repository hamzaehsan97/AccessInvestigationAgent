"""LLM provider abstraction, prompts, and Planner→Executor→Verifier runtime."""
from agent.llm.model_client import (
    ChatResult,
    ModelClient,
    OpenRouterClient,
    StubModelClient,
    default_client,
)
from agent.llm.prompts import executor_system, planner_system, verifier_system
from agent.llm.runtime import Agent

__all__ = [
    "ChatResult",
    "ModelClient",
    "OpenRouterClient",
    "StubModelClient",
    "default_client",
    "executor_system",
    "planner_system",
    "verifier_system",
    "Agent",
]
