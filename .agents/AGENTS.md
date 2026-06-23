# Antigravity Rules for Sophee Codebase

These rules apply to the Antigravity agent (the AI coding assistant) when writing, editing, or refactoring code for this workspace:

- **DO NOT USE GEMINI INTERACTIONS API**: Do not write code that uses the Gemini Interactions API (`client.aio.interactions.create` or `client.interactions.create`).
- **WHY**: The Interactions API currently does not support automatic function calling in Python, which is a core feature that ADK (Agent Development Kit) relies on for executing tools. Additionally, ADK handles its own session state, history compilation, and database persistence (`DatabaseSessionService`) on the client side, making the Interactions API redundant and prone to turn-alternation conflicts (e.g. `400 INVALID_ARGUMENT` errors).
- **STANDARD ALTERNATIVE**: Always use standard stateless content generation (`client.aio.models.generate_content` or `client.models.generate_content`) for model invocations inside helper tools and generation pipelines. If multi-turn behavior or history is needed, build a list of `types.Content` objects manually and pass them to the `contents` parameter of the generate_content call.

