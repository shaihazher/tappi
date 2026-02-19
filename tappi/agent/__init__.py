"""tappi agent — LLM-powered browser automation.

The agent connects to any LLM provider (via LiteLLM) and uses tappi
tools to accomplish tasks autonomously.

    from tappi.agent import Agent

    agent = Agent()
    agent.chat("Go to github.com and find trending repos")
"""
