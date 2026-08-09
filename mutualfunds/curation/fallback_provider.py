from mutualfunds.curation.base import MFCurationProvider
from productdto.mutualFundDto import CuratedPickDTO


class RankedFallbackCurationProvider(MFCurationProvider):
    """
    No LLM call at all - candidates already arrive sorted by the repository
    query (return_3y DESC etc.), so this just takes the top N as-is. Final
    link in the fallback chain: if both Gemini and Groq fail, the explore
    page still gets a correct, real-data-backed list instead of erroring.
    """

    async def curate(self, candidates: list[dict], collection_title: str, limit: int) -> list[CuratedPickDTO]:
        return [
            CuratedPickDTO(scheme_code=candidate["scheme_code"], rank=index + 1,
                            blurb=None, curated_by="fallback_ranked")
            for index, candidate in enumerate(candidates[:limit])
        ]
