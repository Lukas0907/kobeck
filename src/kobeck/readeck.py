from collections.abc import AsyncIterator
from typing import Literal
from datetime import datetime, UTC
import re

import httpx
from pydantic import BaseModel, HttpUrl


class BookmarkSync(BaseModel):
    id: str
    time: str
    type: Literal["update"] | Literal["delete"]


class ResourceImage(BaseModel):
    src: HttpUrl
    width: int
    height: int


class ResourceLink(BaseModel):
    src: HttpUrl


class Resources(BaseModel):
    article: ResourceLink | None = None
    icon: ResourceImage | None = None
    image: ResourceImage | None = None
    log: ResourceLink
    props: ResourceLink
    thumbnail: ResourceImage | None = None


class Bookmark(BaseModel):
    authors: list[str]
    created: datetime
    description: str
    document_type: str
    has_article: bool
    href: HttpUrl
    id: str
    is_archived: bool
    is_deleted: bool
    is_marked: bool
    labels: list[str]
    lang: str
    loaded: bool
    read_progress: int
    resources: Resources
    site: str
    site_name: str
    state: int
    text_direction: str
    title: str
    type: str
    updated: datetime
    url: HttpUrl
    word_count: int


def parse_header_links(value):
    """Return a list of parsed link headers proxies.

    i.e. Link: <http:/.../front.jpeg>; rel=front; type="image/jpeg",<http://.../back.jpeg>; rel=back;type="image/jpeg"

    Copied from requests.utils.parse_header_links.

    :rtype: list
    """

    links = []

    replace_chars = " '\""

    value = value.strip(replace_chars)
    if not value:
        return links

    for val in re.split(", *<", value):
        try:
            url, params = val.split(";", 1)
        except ValueError:
            url, params = val, ""

        link = {"url": url.strip("<> '\"")}

        for param in params.split(";"):
            try:
                key, value = param.split("=")
            except ValueError:
                break

            link[key.strip(replace_chars)] = value.strip(replace_chars)

        links.append(link)

    return links


def get_next_header_link(headers: httpx.Headers) -> str | None:
    return next(
        (
            ln["url"]
            for ln in parse_header_links(headers["link"])
            if ln["rel"] == "next"
        ),
        None,
    )


class Readeck:
    def __init__(self, url: str, token: str):
        self.url = url
        self.token = token

    def get_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def get_client(self):
        return httpx.AsyncClient(headers=self.get_headers())

    async def bookmarks_sync(self, since: datetime | None = None) -> list[BookmarkSync]:
        async with self.get_client() as client:
            params = {}
            if since:
                since = since.astimezone(UTC).replace(tzinfo=None).isoformat()
                params["since"] = since
            r = await client.get(f"{self.url}/api/bookmarks/sync", params=params)
            r.raise_for_status()
            return [BookmarkSync(**entry) for entry in r.json()]

    async def bookmarks(self, site: str) -> AsyncIterator[BookmarkSync]:
        async with self.get_client() as client:
            next_url = f"{self.url}/api/bookmarks"
            while next_url:
                r = await client.get(next_url, params={"site": site})
                r.raise_for_status()
                for entry in r.json():
                    yield Bookmark(**entry)
                next_url = get_next_header_link(r.headers)

    async def bookmark_details(self, id: str) -> Bookmark:
        async with self.get_client() as client:
            r = await client.get(f"{self.url}/api/bookmarks/{id}")
            r.raise_for_status()
            return Bookmark(**r.json())

    async def bookmark_article(self, id: str) -> str:
        async with self.get_client() as client:
            r = await client.get(f"{self.url}/api/bookmarks/{id}/article")
            r.raise_for_status()
            return r.text

    async def bookmark_update(self, id: str, **kwargs) -> None:
        async with self.get_client() as client:
            r = await client.patch(f"{self.url}/api/bookmarks/{id}", json=kwargs)
            r.raise_for_status()

    async def bookmark_create(self, url: str) -> None:
        async with self.get_client() as client:
            r = await client.post(f"{self.url}/api/bookmarks", json={"url": url})
            r.raise_for_status()
