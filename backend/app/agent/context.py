"""Per-turn athlete context block for the system prompt (WA-8, §9.2).

The system prompt carries a COMPACT block describing who the athlete is
and which thresholds/units are configured, read from the database and
the owner configuration AT TURN TIME — not a hardcoded blob, not a
snapshot. Our parts:

- **Threshold resolution mirrors the tools** (``get_zones``): the
  ``athlete_profile`` row is the value of record and reports its own
  ``*_source`` provenance; owner configuration (settings) is the
  fallback, named ``owner_configuration`` exactly as the tools name it.
- **A threshold configured nowhere is reported AS MISSING** — every
  configurable threshold is either listed with its value or named in the
  "NO configurado" line with the same honesty instruction the
  insufficient-data rule (WA-7, §9.3) imposes: never defaulted, never
  guessed, never a silent zero.
- **SHORT by design**: the block is context, not a data dump. It carries
  identity, configured thresholds and units only; the numbers a reply
  needs come from the tools at reply time (the "engine decides, model
  explains" principle, §3).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.settings import Settings
from app.db.models import AthleteProfileRow

__all__ = ["build_athlete_context"]

SessionFactory = async_sessionmaker[AsyncSession]

# The block opens with a header the tests (and the model) can anchor on;
# "leído ahora" states the read-at-turn-time contract.
CONTEXT_HEADER = "Contexto del atleta (configuración y base de datos, leído ahora):"

# The missing-threshold honesty rule, stated once for the whole list.
MISSING_RULE = (
    "no lo inventes, no lo estimes y no lo sustituyas por un cero: si lo "
    "necesitas, di que falta y cómo obtenerlo"
)

OWNER_CONFIGURATION = "owner_configuration"


def _fmt(value: float) -> str:
    """Compact, deterministic number formatting (no trailing zeros)."""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


async def build_athlete_context(
    session_factory: SessionFactory, settings: Settings
) -> str:
    """Build the compact athlete context block for THIS turn.

    Args:
        session_factory: Async session factory on the application
            database (the ``athlete_profile`` row is read here, per turn).
        settings: Application settings (owner configuration and identity).

    Returns:
        A short Spanish block appended to the system prompt: identity,
        timezone/units, the configured thresholds (each with its
        provenance source) and the missing ones named as missing.
    """
    async with session_factory() as session:
        profile = (
            await session.execute(
                select(AthleteProfileRow).where(AthleteProfileRow.athlete_id == 1)
            )
        ).scalar_one_or_none()

    configured: list[str] = []
    missing: list[str] = []

    def add(
        label: str, value: float | None, unit: str, source: str | None
    ) -> None:
        """Record one threshold as configured (with provenance) or missing."""
        if value is None:
            missing.append(f"{label} ({unit})")
            return
        provenance = f", fuente: {source}" if source else ""
        configured.append(f"{label} {_fmt(value)} {unit}{provenance}")

    # Threshold resolution mirrors the tools (get_zones): the profile row
    # is the value of record with its own source; owner configuration is
    # the fallback; configured nowhere -> missing.
    profile_ftp = profile.ftp_watts if profile is not None else None
    if profile_ftp is not None:
        add("FTP", profile_ftp, "W", profile.ftp_source if profile else None)
    else:
        add("FTP", settings.athlete_ftp_w, "W",
            OWNER_CONFIGURATION if settings.athlete_ftp_w is not None else None)

    profile_css = profile.css_mps if profile is not None else None
    if profile_css is not None:
        add("CSS", profile_css, "m/s", profile.css_source if profile else None)
    else:
        add("CSS", settings.athlete_css_speed_mps, "m/s",
            OWNER_CONFIGURATION if settings.athlete_css_speed_mps is not None else None)

    # Settings-only thresholds (not stored in athlete_profile).
    add("LTHR", settings.athlete_lthr_bpm, "ppm", None)
    add("FC máx", settings.athlete_hr_max_bpm, "ppm", None)
    add("FC reposo", settings.athlete_hr_rest_bpm, "ppm", None)
    add("Umbral de velocidad de carrera",
        settings.athlete_threshold_run_speed_mps, "m/s", None)
    add("Peso", settings.athlete_weight_kg, "kg", None)

    lines: list[str] = [CONTEXT_HEADER]
    if settings.athlete_name:
        lines.append(f"- Atleta: {settings.athlete_name}.")
    lines.append(
        f"- Zona horaria: {settings.timezone}; unidades métricas "
        "(vatios, ppm, km, horas, m/s; TSS/CTL/ATL/TSB en puntos)."
    )
    if configured:
        lines.append(f"- Umbrales configurados: {', '.join(configured)}.")
    if missing:
        lines.append(
            f"- NO configurado: {', '.join(missing)} — {MISSING_RULE}."
        )
    # The sexed TRIMP coefficient set the load engine selects (owner
    # choice; the engine rejects unknown values, so report it as read).
    lines.append(
        f"- Coeficientes TRIMP (carga por FC) configurados para: "
        f"{settings.engine_trimp_sex}."
    )
    return "\n".join(lines)
