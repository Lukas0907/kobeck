from datetime import datetime
from typing import Annotated, Any
import itertools
import logging

from bs4 import BeautifulSoup, Comment
from fastapi import FastAPI, Depends, Form, HTTPException, Request
from pydantic import BaseModel, HttpUrl
from pydantic_settings import BaseSettings

from kobeck.readeck import Readeck

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    readeck_url: str


settings = Settings()
app = FastAPI()


@app.on_event("startup")
def init_app():
    logging.basicConfig(level=logging.INFO)


@app.middleware("http")
async def log_request_body(request: Request, call_next: Any):
    body = await request.body()
    logger.info(body)
    return await call_next(request)


class AuthenticatedRequest(BaseModel):
    access_token: str
    consumer_key: str


class GetRequest(AuthenticatedRequest):
    contentType: str
    count: int
    detailType: str
    offset: int
    state: str
    total: str
    since: datetime | None = None


class ExistingItemAction(BaseModel):
    action: str
    item_id: str


class NewItemAction(BaseModel):
    action: str
    url: HttpUrl


class SendRequest(AuthenticatedRequest):
    actions: list[ExistingItemAction | NewItemAction]


class DownloadRequest(AuthenticatedRequest):
    images: int
    refresh: int
    output: str
    url: HttpUrl


def get_readeck(req: AuthenticatedRequest):
    return Readeck(
        url=settings.readeck_url,
        token=req.access_token,
    )


ReadeckDep = Annotated[Readeck, Depends(get_readeck)]


@app.post("/api/kobo/get")
async def get(req: GetRequest, readeck: ReadeckDep):
    """Get updated and deleted articles since a given timestamp."""
    bsyncs = await readeck.bookmarks_sync(since=req.since)
    result = {"status": 1, "list": {}, "total": len(bsyncs)}

    for bsync in itertools.islice(bsyncs, req.offset, req.offset + req.count):
        if bsync.type == "delete":
            result["list"][bsync.id] = {
                "item_id": bsync.id,
                "status": "2",
            }
        else:
            bookmark = await readeck.bookmark_details(bsync.id)

            optional = {}
            if bookmark.resources.image:
                has_image = "1"
                image = {"src": bookmark.resources.image.src}
                images = {
                    "1": {
                        "image_id": "1",
                        "item_id": "1",
                        "src": bookmark.resources.image.src,
                    }
                }
                optional["top_image_url"] = bookmark.resources.image.src
            else:
                has_image = "0"
                image = {}
                images = {}

            result["list"][bsync.id] = {
                "authors": {a: {"author_id": a, "name": a} for a in bookmark.authors},
                "excerpt": bookmark.description,
                "favorite": "0",
                "given_title": bookmark.title,
                "given_url": bookmark.url,
                "has_image": has_image,
                "has_video": "0",
                "image": image,
                "images": images,
                "is_article": "1",
                "item_id": bookmark.id,
                "resolved_id": bookmark.id,
                "resolved_title": bookmark.title,
                "resolved_url": bookmark.url,
                "status": "0",
                "tags": {
                    lb: {"item_id": bsync.id, "tag": lb} for lb in bookmark.labels
                },
                "time_added": int(bookmark.created.timestamp()),
                "time_read": 0,  # Seems to be always 0?
                "time_updated": int(bookmark.updated.timestamp()),
                "videos": [],
                "word_count": bookmark.word_count,
                **optional,
            }

    return result


@app.post("/api/kobo/download")
async def download(req: Annotated[DownloadRequest, Form()], readeck: ReadeckDep):
    """Download an article."""
    # Build the list of subdomains making up a bookmark URL
    sites_to_try = [req.url.host]
    parts = req.url.host.split(".")
    while len(parts) > 2:
        parts.pop(0)  # Remove leftmost subdomain
        candidate = ".".join(parts)
        sites_to_try.append(candidate)

    article = None
    bookmark_found = None

    for site in sites_to_try:
        try:
            logger.debug("Searching Readeck bookmarks for site %s", site)
            async for bookmark in readeck.bookmarks(site=site):
                if bookmark.url == req.url:
                    bookmark_found = bookmark
                    logger.debug("Match found with bookmark %s", bookmark)
                    break
            if bookmark_found:
                break
        except Exception:
            logger.error("Error searching Readeck bookmarks for site %s", site)
            continue

    if bookmark_found:
        article = await readeck.bookmark_article(bookmark_found.id)
    else:
        raise HTTPException(status_code=404, detail="Article not found")

    # Images need to be extracted and referenced by a comment.
    soup = BeautifulSoup(article, features="html.parser")
    images = {}
    for i, img in enumerate(soup.find_all("img")):
        if img.has_attr("src"):
            images[str(i)] = {"image_id": str(i), "item_id": str(i), "src": img["src"]}
            img.replace_with(Comment(f"IMG_{i}"))
        else:
            img.decompose()

    return {
        "images": images,
        "article": str(soup),
    }


@app.post("/api/kobo/send")
async def send(req: SendRequest, readeck: ReadeckDep):
    """Modify article state."""
    action_results = []
    for action in req.actions:
        match action.action:
            case "archive":
                await readeck.bookmark_update(action.item_id, is_archived=True)
                action_results.append(True)
            case "readd":
                await readeck.bookmark_update(action.item_id, is_archived=False)
                action_results.append(True)
            case "favorite":
                await readeck.bookmark_update(action.item_id, is_marked=True)
                action_results.append(True)
            case "unfavorite":
                await readeck.bookmark_update(action.item_id, is_marked=False)
                action_results.append(True)
            case "delete":
                await readeck.bookmark_update(action.item_id, is_deleted=True)
                action_results.append(True)
            case "add":
                await readeck.bookmark_create(str(action.url))
                action_results.append(True)
            case _:
                action_results.append(False)

    return {"status": all(action_results), "action_results": action_results}
