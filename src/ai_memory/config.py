from dataclasses import dataclass
import os
from pathlib import Path

def env_secret(name, default=''):
    path = os.getenv(name + '_FILE')
    return Path(path).read_text().strip() if path else os.getenv(name, default)

@dataclass(frozen=True)
class Settings:
    database_url: str = 'sqlite:///./ai-memory.db'
    instance_id: str = 'home'
    auth_file: str = ''
    api_token: str = ''
    api_token_scopes: tuple = ('read',)
    qdrant_url: str = 'http://ai-memory-qdrant:6333'
    qdrant_api_key: str = ''
    qdrant_collection: str = 'home-minilm-v1'
    inference_url: str = 'http://ai-memory-inference:8081'
    inference_token: str = ''
    oauth_issuer: str = ''
    oauth_jwks_url: str = ''
    oauth_resource: str = ''
    oauth_subjects: tuple = ()
    mcp_hosts: tuple = ('localhost:*', '127.0.0.1:*', 'ai-memory-api:*', 'testserver')
    mcp_origins: tuple = ('http://localhost:*', 'http://127.0.0.1:*')

    @classmethod
    def from_env(cls):
        return cls(database_url=env_secret('DATABASE_URL', cls.database_url),
            auth_file=os.getenv('AI_MEMORY_AUTH_FILE', ''), api_token=env_secret('AI_MEMORY_API_TOKEN'),
            qdrant_url=os.getenv('QDRANT_URL', cls.qdrant_url), qdrant_api_key=env_secret('QDRANT_API_KEY'),
            qdrant_collection=os.getenv('QDRANT_COLLECTION', cls.qdrant_collection),
            inference_url=os.getenv('AI_MEMORY_INFERENCE_URL', cls.inference_url),
            inference_token=env_secret('AI_MEMORY_INFERENCE_TOKEN'),
            oauth_issuer=os.getenv('AI_MEMORY_OAUTH_ISSUER', ''),
            oauth_jwks_url=os.getenv('AI_MEMORY_OAUTH_JWKS_URL', ''),
            oauth_resource=os.getenv('AI_MEMORY_OAUTH_RESOURCE', ''),
            oauth_subjects=tuple(filter(None, os.getenv('AI_MEMORY_OAUTH_SUBJECTS', '').split(','))),
            mcp_hosts=tuple(filter(None, os.getenv('AI_MEMORY_MCP_HOSTS', ','.join(cls.mcp_hosts)).split(','))),
            mcp_origins=tuple(filter(None, os.getenv('AI_MEMORY_MCP_ORIGINS', ','.join(cls.mcp_origins)).split(','))))
