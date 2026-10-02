"""Smoke test: the app package imports and the scaffold is wired up."""


def test_app_package_imports() -> None:
    import app

    assert app is not None


def test_engine_subpackage_imports() -> None:
    from app import engine

    assert engine.__doc__ is not None
