from .adapters import HTTPExtractor, HTTPEmbedder, QdrantIndex, SwitchingExtractor
from .config import Settings
from .db import initialize_schema, make_engine, make_session_factory
from .service import MemoryService


def build_extractor(settings: Settings):
    qwen = HTTPExtractor(settings.inference_url, settings.inference_token)
    if settings.extractor_socket or settings.extractor_url:
        primary = HTTPExtractor(
            settings.extractor_url or 'http://localhost',
            settings.inference_token,
            unix_socket=settings.extractor_socket or None,
        )
        return SwitchingExtractor(
            primary,
            qwen,
            mode_file=settings.extractor_mode_file,
            default_mode=settings.extractor_mode or 'agy',
        )
    return qwen


def build_service(settings=None, *, extractor=None, embedder=None, index=None):
    settings = settings or Settings.from_env()
    engine = make_engine(settings.database_url)
    initialize_schema(engine)
    return MemoryService(make_session_factory(engine),
        extractor or build_extractor(settings),
        embedder or HTTPEmbedder(settings.inference_url, settings.inference_token),
        index or QdrantIndex(settings.qdrant_url, settings.qdrant_collection, settings.qdrant_api_key)), engine
