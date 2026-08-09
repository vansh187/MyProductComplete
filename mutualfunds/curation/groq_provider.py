from groq import AsyncGroq

from mutualfunds.curation.base import MFCurationProvider
from mutualfunds.curation.prompt_support import MFCurationPromptBuilder, MFCurationResponseParser
from productdto.mutualFundDto import CuratedPickDTO

_MODEL_NAME = "llama-3.3-70b-versatile"


class GroqCurationProvider(MFCurationProvider):
    """Secondary LLM curation provider - used only when Gemini fails/errors."""

    def __init__(self, api_key: str):
        self._client = AsyncGroq(api_key=api_key)
        self._prompt_builder = MFCurationPromptBuilder()
        self._response_parser = MFCurationResponseParser()

    async def curate(self, candidates: list[dict], collection_title: str, limit: int) -> list[CuratedPickDTO]:
        if not candidates:
            return []
        prompt = self._prompt_builder.build(candidates, collection_title, limit)
        response = await self._client.chat.completions.create(
            model=_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        valid_codes = {candidate["scheme_code"] for candidate in candidates}
        content = response.choices[0].message.content
        return self._response_parser.parse(content, valid_codes, "groq", limit)
