"""Gateway service entry point — run via `python -m antigona.gateway`.

Канонический entrypoint: создаёт только канонический Gateway
(``gateway.api.create_gateway_app``). Легаси ``antigona.main`` (v0.2.0)
не используется — единая точка входа (Step 2 манифеста).
"""

from antigona.gateway.main import main

main()
