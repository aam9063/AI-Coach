"""Service layer: integrations between stored data and the pure engine.

Modules here may read the DB and call ``app/engine`` functions, but the
engine stays pure: ``app/engine`` must never import ``app.db`` or
``app.services`` (brief §6 purity rule).
"""
