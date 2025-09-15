from datetime import datetime
from typing import Annotated, Any
import functools
import itertools
import io
import json
import logging
import uuid
from contextvars import ContextVar
from urllib.parse import quote, unquote_plus

from bs4 import BeautifulSoup, Comment
from fastapi import FastAPI, Depends, Form, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, HttpUrl
from pydantic_settings import BaseSettings
from PIL import Image, ImageDraw, ImageFont
import httpx

from kobeck.readeck import Readeck
from kobeck.logging_utils import sanitize_sensitive_data

logger = logging.getLogger(__name__)

# Context variable to store current request data for error logging
current_request: ContextVar[dict] = ContextVar("current_request", default=None)


def dump_on_error(func):
    """Decorator to capture full request/response context on errors."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            request_data = current_request.get()
            if request_data:
                error_dump = {
                    "error": True,
                    "exception_type": type(e).__name__,
                    "exception_message": str(e),
                    "request": sanitize_sensitive_data(request_data),
                    "endpoint": func.__name__,
                }
                logger.error("ERROR_DUMP: %s", json.dumps(error_dump, indent=2))
            raise

    return wrapper


class Settings(BaseSettings):
    readeck_url: str
    convert_to_jpeg: bool = True


settings = Settings()
app = FastAPI()


def create_placeholder_image(message: str = "Image unavailable") -> bytes:
    """Create a placeholder JPEG image with the given message."""
    width, height = 800, 600
    img = Image.new("RGB", (width, height), color="#ffffff")
    draw = ImageDraw.Draw(img)

    try:
        font = ImageFont.load_default(size=36)
    except Exception:
        try:
            # Fallback to default font
            font = ImageFont.load_default()
        except Exception:
            font = None

    # Calculate text position for centering
    if font:
        bbox = draw.textbbox((0, 0), message, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
    else:
        # Rough estimation for built-in font (scaled up)
        text_width = len(message) * 12
        text_height = 22

    x = (width - text_width) // 2
    y = (height - text_height) // 2

    draw.text((x, y), message, fill="#666666", font=font)

    # Convert to JPEG bytes
    img_buffer = io.BytesIO()
    img.save(img_buffer, format="JPEG", quality=85)
    return img_buffer.getvalue()


@app.on_event("startup")
def init_app():
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s(%(name)s):\t%(message)s"
    )


@app.middleware("http")
async def capture_request_context(request: Request, call_next: Any):
    """Simple middleware to capture request context for error dumping."""
    correlation_id = str(uuid.uuid4())
    body = await request.body()

    request_data = {
        "correlation_id": correlation_id,
        "method": request.method,
        "url": str(request.url),
        "headers": dict(request.headers),
        "body": body.decode("utf-8", errors="replace") if body else None,
        "timestamp": datetime.now().isoformat(),
    }

    # Store in context variable for decorator access
    current_request.set(request_data)

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
@dump_on_error
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
@dump_on_error
async def download(
    req: Annotated[DownloadRequest, Form()], readeck: ReadeckDep, request: Request
):
    """Download an article."""
    # Build the list of subdomains making up a bookmark URL
    sites_to_try = [req.url.host]
    parts = req.url.host.split(".")
    while len(parts) > 2:
        parts.pop(0)  # Remove leftmost subdomain
        candidate = ".".join(parts)
        sites_to_try.append(candidate)

    requested_url = unquote_plus(str(req.url))
    article = None
    bookmark_found = None

    logger.debug("Looking for bookmark URL %s", requested_url)
    for site in sites_to_try:
        try:
            logger.debug("Searching Readeck bookmarks for site %s", site)
            async for bookmark in readeck.bookmarks(site=site):
                bookmark_url = unquote_plus(str(bookmark.url))
                if bookmark_url == requested_url:
                    bookmark_found = bookmark
                    logger.debug("Match found with bookmark %s", bookmark)
                    break
                logger.debug("Ignoring mismatched bookmark URL: ", bookmark_url)
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
            src = original_src = img["src"]
            if (
                not src.endswith(".jpg")
                and not src.endswith(".jpeg")
                and settings.convert_to_jpeg
            ):
                # Build proper external URL using proxy headers
                scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
                host = request.headers.get("host", request.url.netloc)
                prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")

                src = f"{scheme}://{host}{prefix}/api/convert-image?url={quote(original_src, safe='')}"
                logger.info(
                    "Replacing image URL for conversion: %s -> %s", original_src, src
                )

            images[str(i)] = {"image_id": str(i), "item_id": str(i), "src": src}
            img.replace_with(Comment(f"IMG_{i}"))
        else:
            img.decompose()

    return {
        "images": images,
        "article": str(soup),
    }


@app.post("/api/kobo/send")
@dump_on_error
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


@app.get("/api/convert-image")
async def convert_image(url: str):
    """Convert an image to JPEG format.

    Returns placeholder on failure.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()

            # Open the image with Pillow
            img = Image.open(io.BytesIO(response.content))

            # Convert to RGB if necessary (for PNG with transparency, etc.)
            if img.mode != "RGB":
                img = img.convert("RGB")

            # Convert to JPEG
            img_buffer = io.BytesIO()
            img.save(img_buffer, format="JPEG", quality=85)
            jpeg_data = img_buffer.getvalue()

            return Response(
                content=jpeg_data,
                media_type="image/jpeg",
                headers={"Cache-Control": "public, max-age=3600"},
            )

    except Exception as e:
        logger.error("Failed to convert image %s: %s", url, str(e))
        # Return placeholder image on any failure
        placeholder_data = create_placeholder_image("Image conversion failed")
        return Response(
            content=placeholder_data,
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=300"},
        )
