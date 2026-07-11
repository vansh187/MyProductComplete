"""
Persistence for margin_config - the tunable formula parameters (notional/span
pct, expiry & moneyness & price-source multipliers) per contract_type and
optionally per underlying. Read-only from the engine's perspective, so these
methods open their own short-lived connection rather than requiring a
caller-owned cursor - config lookups aren't part of the money-moving
transaction, just an input to it.
"""

import logging

from database.PostgresConnectionFactory import PostgresConnectionFactory
from utils.query_loader import QueryLoader

logger = logging.getLogger(__name__)

_CONFIG_COLUMNS = [
    "contract_type", "underlying", "notional_pct", "span_pct",
    "near_expiry_multiplier", "far_expiry_multiplier", "near_expiry_days", "far_expiry_days",
    "moneyness_itm_multiplier", "moneyness_atm_multiplier", "moneyness_otm_multiplier",
    "session_gap_multiplier", "price_source_tier1_multiplier", "price_source_tier2_multiplier",
    "price_source_tier3_multiplier", "price_source_tier4_multiplier", "tier3_verification_band_pct",
]


class MarginConfigPersistence:
    """Reads margin_config rows, falling back from underlying-specific to the default row."""

    def __init__(self):
        self.logger = logger

    def get_config(self, contract_type: str, underlying: str | None) -> dict | None:
        """
        Returns the underlying-specific config row if one exists and is
        active, else the contract_type's default (underlying IS NULL) row,
        else None.

        Raises:
            ValueError: If parameters are invalid
            Exception: If the query fails
        """
        if contract_type not in ("OPTION", "FUTURES"):
            raise ValueError(f"Invalid contract_type: {contract_type}")

        conn = None
        cursor = None
        try:
            conn = PostgresConnectionFactory.create_connection()
            cursor = conn.cursor()

            if underlying:
                cursor.execute(
                    QueryLoader.get('margin.yaml', 'get_active_margin_config'),
                    (contract_type, underlying.upper())
                )
                row = cursor.fetchone()
                if row is not None:
                    return dict(zip(_CONFIG_COLUMNS, row))

            cursor.execute(
                QueryLoader.get('margin.yaml', 'get_default_margin_config'),
                (contract_type,)
            )
            row = cursor.fetchone()
            return dict(zip(_CONFIG_COLUMNS, row)) if row is not None else None
        except Exception as ex:
            self.logger.error(f"Error fetching margin_config for {contract_type}/{underlying}: {str(ex)}")
            raise Exception(f"Error fetching margin_config for {contract_type}/{underlying}: {str(ex)}") from ex
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
