from .adapters import HTTPExtractor, HTTPEmbedder, QdrantIndex
from .config import Settings
from .db import initialize_schema, make_engine, make_session_factory
from .service import MemoryService

def build_service(settings=None, *, extractor=None, embedder=None, index=None):
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    initialize_schema(engine)
    return MemoryService(make_session_factory(engine),
        extractor or HTTPExtractor(settings.inference_url, settings.inference_token),
        embedder or HTTPEmbedder(settings.inference_url, settings.inference_token),
        index or QdrantIndex(settings.qdrant_url, settings.qdrant_collection, settings.qdrant_api_key)), engine
