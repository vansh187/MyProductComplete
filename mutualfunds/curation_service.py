import logging

from mutualfunds.collections_config import MFCollectionsCatalog
from mutualfunds.curation.base import MFCurationProvider
from mutualfunds.curation.curated_picks_repository import MFCuratedPicksRepository
from mutualfunds.returns_repository import MFReturnsRepository

logger = logging.getLogger("mutualfunds.curation")

_POPULAR_COLLECTION_KEY = "popular"
_POPULAR_LIMIT = 4


class MFCurationService:
    """
    Runs once daily (after MFDailyNavSyncService), producing mf_curated_picks
    for "popular" and every MFCollectionsCatalog tile. Tries providers in
    order (Gemini -> Groq -> plain ranked fallback) and always writes
    SOMETHING, so the explore page is never left without curated data after
    the first successful run.
    """

    def __init__(self, returns_repository: MFReturnsRepository, curated_picks_repository: MFCuratedPicksRepository,
                 collections_catalog: MFCollectionsCatalog, primary_provider: MFCurationProvider,
                 secondary_provider: MFCurationProvider, fallback_provider: MFCurationProvider):
        self._returns_repository = returns_repository
        self._curated_picks_repository = curated_picks_repository
        self._collections_catalog = collections_catalog
        self._primary_provider = primary_provider
        self._secondary_provider = secondary_provider
        self._fallback_provider = fallback_provider

    async def curate_all(self) -> None:
        await self._curate_one(_POPULAR_COLLECTION_KEY, "Popular Funds", categories=None,
                                sort_by="return_3y", limit=_POPULAR_LIMIT, pool_size=30)
        for definition in self._collections_catalog.all():
            await self._curate_one(definition.key, definition.title, definition.category_filter,
                                    definition.sort_by, limit=10, pool_size=definition.candidate_pool_size)

    async def _curate_one(self, collection_key: str, title: str, categories: list[str] | None,
                           sort_by: str, limit: int, pool_size: int) -> None:
        candidates = self._returns_repository.top_by_category(
            categories=categories, sort_by=sort_by, limit=pool_size, offset=0
        )
        if not candidates:
            logger.warning(f"[MFCurationService] no candidates for '{collection_key}' - skipping")
            return

        picks = await self._curate_with_fallback_chain(candidates, title, limit)
        self._curated_picks_repository.upsert_picks(collection_key, picks)

    async def _curate_with_fallback_chain(self, candidates: list[dict], title: str, limit: int) -> list:
        for provider in (self._primary_provider, self._secondary_provider):
            try:
                picks = await provider.curate(candidates, title, limit)
                if picks:
                    return picks
            except Exception as ex:
                logger.warning(f"[MFCurationService] provider {type(provider).__name__} failed for '{title}': {ex}")
        return await self._fallback_provider.curate(candidates, title, limit)
