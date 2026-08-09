import json

from productdto.mutualFundDto import CuratedPickDTO

_SYSTEM_INSTRUCTION = (
    "You are curating a list for a mutual fund investing app's explore page. "
    "You will be given a JSON array of REAL candidate mutual fund schemes with "
    "their real computed returns. Choose and rank the best schemes for the given "
    "collection title, and optionally write a short blurb (max 15 words) for each. "
    "You MUST choose scheme_code values ONLY from the candidates provided - never "
    "invent, guess, or reference a scheme_code that is not in the candidate list. "
    "Respond with ONLY strict JSON in this exact shape, no prose, no markdown fences: "
    '{"picks": [{"scheme_code": <int>, "rank": <int starting at 1>, "blurb": "<short text or null>"}]}'
)


class MFCurationPromptBuilder:
    """Builds the shared prompt text handed to every LLM curation provider."""

    def build(self, candidates: list[dict], collection_title: str, limit: int) -> str:
        candidate_payload = [
            {
                "scheme_code": candidate["scheme_code"],
                "scheme_name": candidate.get("scheme_name"),
                "scheme_category": candidate.get("scheme_category"),
                "fund_house": candidate.get("fund_house"),
                "return_1y": self._to_number(candidate.get("return_1y")),
                "return_3y": self._to_number(candidate.get("return_3y")),
                "return_5y": self._to_number(candidate.get("return_5y")),
            }
            for candidate in candidates
        ]
        return (
            f"{_SYSTEM_INSTRUCTION}\n\n"
            f"Collection title: {collection_title}\n"
            f"Pick at most {limit} schemes.\n"
            f"Candidates:\n{json.dumps(candidate_payload)}"
        )

    def _to_number(self, value) -> float | None:
        """psycopg2 returns NUMERIC columns as Decimal, which json.dumps cannot
        serialize - left unconverted, this raised a TypeError on every real
        call (Decimal is never JSON-native), silently forcing every curation
        run down to the ranked fallback regardless of Gemini/Groq working."""
        return float(value) if value is not None else None


class MFCurationResponseParser:
    """
    Parses an LLM's JSON response into validated CuratedPickDTOs, dropping
    any pick whose scheme_code is not a member of the candidate set - the
    hard guarantee against hallucinated funds, independent of how well the
    provider followed the prompt.
    """

    def parse(self, raw_text: str, valid_codes: set[int], curated_by: str, limit: int) -> list[CuratedPickDTO]:
        cleaned = raw_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            cleaned = cleaned[cleaned.find("{"):]
        payload = json.loads(cleaned)

        picks: list[CuratedPickDTO] = []
        for entry in payload.get("picks", []):
            try:
                scheme_code = int(entry["scheme_code"])
            except (KeyError, TypeError, ValueError):
                continue
            if scheme_code not in valid_codes:
                continue
            picks.append(CuratedPickDTO(
                scheme_code=scheme_code,
                rank=len(picks) + 1,
                blurb=entry.get("blurb"),
                curated_by=curated_by,
            ))
            if len(picks) >= limit:
                break
        return picks
