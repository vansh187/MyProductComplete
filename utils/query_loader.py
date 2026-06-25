import yaml
import os

class QueryLoader:
    _cache = {}

    @classmethod
    def get(cls, filename: str, key: str) -> str:
        if filename not in cls._cache:
            queries_dir = os.path.join(os.path.dirname(__file__), '..', 'queries')
            path = os.path.join(queries_dir, filename)
            with open(path, 'r') as f:
                cls._cache[filename] = yaml.safe_load(f)
        query = cls._cache[filename].get(key)
        if query is None:
            raise KeyError(f"Query '{key}' not found in {filename}")
        return query.strip()
