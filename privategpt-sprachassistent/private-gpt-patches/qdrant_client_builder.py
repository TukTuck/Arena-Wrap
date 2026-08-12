import contextlib
import logging
import os
from typing import Any

from qdrant_client import (  # type: ignore[import-not-found]
    AsyncQdrantClient,
    QdrantClient,
)

from private_gpt.settings.settings import Settings

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


class QdrantClients:
    client: QdrantClient
    aclient: AsyncQdrantClient

    def __init__(self, client: QdrantClient, aclient: AsyncQdrantClient) -> None:
        self.client = client
        self.aclient = aclient

    def close(self) -> None:
        """Close the sync and async clients."""
        if not self:
            return

        if self.client and hasattr(self.client, "close") and self.client:
            self.client.close()

        logger.debug("Closing Qdrant clients")
        if self.client:
            del self.client
        if self.aclient:
            del self.aclient

    async def aclose(self) -> None:
        """Close the async client."""
        if not self:
            return

        if hasattr(self.aclient, "close"):
            await self.aclient.close()

        logger.debug("Closing async Qdrant client")
        if self.aclient:
            del self.aclient

    def __del__(self) -> None:
        """Ensure the clients are closed when the instance is deleted."""
        with contextlib.suppress(Exception):
            self.close()
            del self


class QdrantClientBuilder:
    @staticmethod
    def build_clients(settings: Settings) -> QdrantClients:
        if settings.qdrant is None:
            client = QdrantClient()
            aclient = AsyncQdrantClient()

            return QdrantClients(
                client=client,
                aclient=aclient,
            )

        config = settings.qdrant.get_parameters(QdrantClient, exclude_none=True)

        if QdrantClientBuilder.is_local_path(config):
            from private_gpt.paths import resolve_data_path

            config = dict(config)
            config["path"] = str(resolve_data_path(config["path"]))
            # Local mode: PatchedQdrantVectorStore routes every operation through
            # the sync client when the underlying client is a QdrantLocal (see its
            # `isinstance(self._client._client, QdrantLocal)` checks). The async
            # client is therefore never used in local mode.
            #
            # Creating a second (async) client on the same storage folder would
            # fail on Windows: QdrantLocal holds an exclusive portalocker handle
            # on the `.lock` file, which cannot be removed/deleted on Windows.
            client = QdrantClient(**config)
            aclient = None

        else:
            client = QdrantClient(**config)
            aclient = AsyncQdrantClient(**config)

        return QdrantClients(
            client=client,
            aclient=aclient,
        )

    @staticmethod
    def is_local_path(config: dict[str, Any]) -> bool:
        return config.get("path") is not None

    @staticmethod
    def clean_lock(settings: Settings) -> None:
        db_dir = settings.qdrant.path
        if db_dir is None:
            return

        lock_file = os.path.join(db_dir, ".lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)
