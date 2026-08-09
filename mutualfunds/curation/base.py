from abc import ABC, abstractmethod

from productdto.mutualFundDto import CuratedPickDTO


class MFCurationProvider(ABC):
    """
    A curation provider ranks/selects/blurbs a Popular Funds or Collection
    tile's contents from a candidate pool of REAL, already-computed fund
    data - it must never invent a scheme_code outside that pool. Concrete
    LLM providers (Gemini, Groq) enforce this via prompt instructions;
    MFCurationService independently re-validates every response against the
    candidate set regardless, since prompt instructions alone are not a
    reliability guarantee for a fintech product.
    """

    @abstractmethod
    async def curate(self, candidates: list[dict], collection_title: str, limit: int) -> list[CuratedPickDTO]:
        """
        candidates: real rows from MFReturnsRepository.top_by_category(),
        each a dict with at least scheme_code, scheme_name, scheme_category,
        return_3y/return_1y/etc.
        Returns up to `limit` CuratedPickDTOs, ranked, each curated_by set
        to this provider's identifier.
        """
        raise NotImplementedError
