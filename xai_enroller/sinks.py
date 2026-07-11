import hashlib
import hmac
from typing import Protocol
from urllib.parse import urlencode

import httpx

from .models import OAuthCredential, SinkReceipt


class SinkError(RuntimeError):
    pass


class CredentialSink(Protocol):
    async def store(self, credential: OAuthCredential) -> SinkReceipt: ...


class CPAAuthFileSink:
    def __init__(self, base_url, management_secret, client: httpx.AsyncClient, name_secret=None):
        self.base_url = base_url.rstrip("/")
        self.management_secret = management_secret
        self.client = client
        self.name_secret = name_secret or management_secret.encode()

    async def store(self, credential: OAuthCredential):
        subject = credential.subject or credential.refresh_token
        digest = hmac.new(self.name_secret, subject.encode(), hashlib.sha256).hexdigest()[:16]
        filename = f"xai-{digest}.json"
        document = {
            "type": "xai",
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "id_token": credential.id_token,
            "token_type": credential.token_type,
            "expires_in": credential.expires_in,
            "expired": credential.expires_at,
            "last_refresh": credential.last_refresh,
            "sub": credential.subject,
            "base_url": "https://api.x.ai/v1",
            "token_endpoint": credential.token_endpoint,
            "auth_kind": "oauth",
        }
        response = await self.client.post(
            f"{self.base_url}/v0/management/auth-files?{urlencode({'name': filename})}",
            headers={
                "Authorization": f"Bearer {self.management_secret}",
                "Content-Type": "application/json",
            },
            json=document,
            follow_redirects=False,
        )
        if response.status_code // 100 != 2:
            raise SinkError("CPA upload rejected")
        return SinkReceipt(filename.removesuffix(".json"))
