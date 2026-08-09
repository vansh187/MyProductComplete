from google import genai

from mutualfunds.curation.base import MFCurationProvider
from mutualfunds.curation.prompt_support import MFCurationPromptBuilder, MFCurationResponseParser
from productdto.mutualFundDto import CuratedPickDTO

_MODEL_NAME = "gemini-flash-latest"  # alias - avoids hardcoding a dated model
                                      # version that Google can deprecate for
                                      # new API keys without notice (as
                                      # happened with the pinned 2.5-flash)


class GeminiCurationProvider(MFCurationProvider):
    """Primary LLM curation provider. Any failure (network, quota, bad JSON)
    propagates as an exception - MFCurationService catches it and falls
    through to Groq, never lets it break the explore page."""

    def __init__(self, api_key: str):
        self._client = genai.Client(api_key=api_key)
        self._prompt_builder = MFCurationPromptBuilder()
        self._response_parser = MFCurationResponseParser()

    async def curate(self, candidates: list[dict], collection_title: str, limit: int) -> list[CuratedPickDTO]:
        if not candidates:
            return []
        prompt = self._prompt_builder.build(candidates, collection_title, limit)
        response = await self._client.aio.models.generate_content(
            model=_MODEL_NAME,
            contents=prompt,
        )
        valid_codes = {candidate["scheme_code"] for candidate in candidates}
        return self._response_parser.parse(response.text, valid_codes, "gemini", limit)
