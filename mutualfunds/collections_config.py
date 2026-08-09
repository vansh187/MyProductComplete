from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionDefinition:
    key: str
    title: str
    icon_hint: str
    category_filter: list[str] | None   # None = ranks across every category
    sort_by: str = "return_3y"
    candidate_pool_size: int = 30


class MFCollectionsCatalog:
    """
    Static tile metadata for the Explore page's "Collections" row (title,
    icon, which mf_schemes.scheme_category values it maps to, how it's
    sorted). Never holds fund data itself - MutualFundService always
    resolves a tile's actual fund list live from MFReturnsRepository /
    MFCuratedPicksRepository. Edited directly to add/rename a tile.
    """

    def __init__(self):
        self._definitions: dict[str, CollectionDefinition] = {
            definition.key: definition for definition in self._build_definitions()
        }

    def _build_definitions(self) -> list[CollectionDefinition]:
        return [
            CollectionDefinition(
                key="high-return", title="High return", icon_hint="trending-up",
                category_filter=None, sort_by="return_3y",
            ),
            CollectionDefinition(
                key="best-sip-funds", title="Best SIP funds", icon_hint="wallet",
                category_filter=["Equity Scheme - Large Cap Fund", "Equity Scheme - Flexi Cap Fund",
                                  "Equity Scheme - ELSS"], sort_by="return_3y",
            ),
            # Verified against live mfapi.in data: "Other Scheme - Gold ETF" is
            # the only category cleanly specific to gold. Silver ETFs and both
            # metals' fund-of-funds variants (the SIP-friendly, non-demat way
            # most retail investors actually buy gold/silver) are lumped by
            # AMFI/mfapi.in into generic buckets ("Other Scheme - Other  ETFs"
            # [sic, double space in the source data] / "Other Scheme - FoF
            # Domestic") shared with unrelated fund types - filtering on those
            # would incorrectly pull in non-precious-metal funds, so this tile
            # deliberately stays gold-ETF-only until a name-based match
            # ("%gold%"/"%silver%") is added alongside the category filter.
            CollectionDefinition(
                key="gold-silver", title="Gold & Silver", icon_hint="ingot",
                category_filter=["Other Scheme - Gold ETF"],
                sort_by="return_1y",
            ),
            CollectionDefinition(
                key="large-cap", title="Large Cap", icon_hint="building",
                category_filter=["Equity Scheme - Large Cap Fund"], sort_by="return_3y",
            ),
            CollectionDefinition(
                key="mid-cap", title="Mid Cap", icon_hint="building-2",
                category_filter=["Equity Scheme - Mid Cap Fund"], sort_by="return_3y",
            ),
            CollectionDefinition(
                key="small-cap", title="Small Cap", icon_hint="storefront",
                category_filter=["Equity Scheme - Small Cap Fund"], sort_by="return_3y",
            ),
        ]

    def get(self, key: str) -> CollectionDefinition | None:
        return self._definitions.get(key)

    def all(self) -> list[CollectionDefinition]:
        return list(self._definitions.values())
