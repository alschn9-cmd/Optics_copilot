import logging

import httpx
from sqlmodel import Session

from app.models import ContentItem, ContentStatus, PipelineMode, advance_status
from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)

POSTS_PATH = "/wp-json/wp/v2/posts"


class CMSError(RuntimeError):
    """WordPress rejected the request or returned an unexpected body."""


class CMSClient:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self._http = http_client
        self._base = self.settings.wp_url.rstrip("/")
        self._auth = httpx.BasicAuth(self.settings.wp_user, self.settings.wp_app_password)

    def create_or_update_post(
        self,
        item: ContentItem,
        *,
        mode: PipelineMode | str,
        actor: str = "pipeline",
    ) -> ContentItem:
        payload = {
            "title": item.title_ru,
            "content": item.body_ru,
            "status": "publish",
        }
        data = self._upsert(item, payload)
        post_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(post_id, int):
            raise CMSError("WordPress response did not include a post id")

        # The WordPress post already exists. Store its id before the status
        # change so a later failure updates this post instead of creating another.
        item.cms_post_id = post_id
        self.session.add(item)
        self.session.commit()
        if item.status != ContentStatus.PUBLISHED:
            advance_status(self.session, item, ContentStatus.PUBLISHED, mode, actor)
            self.session.commit()
        self.session.refresh(item)
        logger.info("published item %s as WordPress post %s", item.id, post_id)
        return item

    def _upsert(self, item: ContentItem, payload: dict) -> dict:
        if item.cms_post_id is None:
            return self._request("POST", POSTS_PATH, payload)
        return self._request("POST", f"{POSTS_PATH}/{item.cms_post_id}", payload)

    def unpublish(self, item: ContentItem) -> None:
        if item.cms_post_id is None:
            raise ValueError("item has no cms_post_id")
        self._request(
            "POST",
            f"{POSTS_PATH}/{item.cms_post_id}",
            {"status": "draft"},
        )
        logger.info("unpublished WordPress post %s for item %s", item.cms_post_id, item.id)

    def _request(self, method: str, path: str, payload: dict) -> dict:
        owns_client = self._http is None
        client = self._http or httpx.Client(timeout=60)
        try:
            response = client.request(
                method,
                f"{self._base}{path}",
                json=payload,
                auth=self._auth,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                raise CMSError(f"WordPress {method} {path} failed: {code}") from exc
            return response.json()
        finally:
            if owns_client:
                client.close()
