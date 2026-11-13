"""FastAPI REST API for SpliceTrail forensics engine."""

import io
import base64
import hmac
import ipaddress
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Optional, List, Dict, Any, Tuple
import numpy as np
from PIL import Image

try:
    from fastapi import FastAPI, File, UploadFile, HTTPException, Query
    from fastapi.concurrency import run_in_threadpool
    from pydantic import BaseModel

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from splicetrail import ForensicsEngine, Config, __version__
from splicetrail.engine import _env_float, _env_int
from splicetrail.exceptions import DependencyError, TargetExistsError
from splicetrail.jpeg import sigma_from_tables
from splicetrail.report import region_offset

logger = logging.getLogger(__name__)

# Security limits
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB
# Room for the multipart framing around the file when checking the whole body.
MULTIPART_OVERHEAD = 1024 * 1024
# Upload pixel cap (SPLICETRAIL_MAX_IMAGE_PIXELS). Indexing and matching cost grows
# with the pixel count: on a 16-core machine a 0.5 MP image took up to ~32 s to
# index and ~13 s to match (1 MP: ~47 s and ~34 s, peak RSS up to 4.6 GB), so
# this default keeps one request well under a minute.
MAX_IMAGE_PIXELS = 500_000
# /hash only downsamples the image, so it keeps a looser decompression-bomb cap.
MAX_HASH_PIXELS = 50_000_000
DEFAULT_MAX_TARGETS = 1000  # SPLICETRAIL_MAX_TARGETS
DEFAULT_LOCK_TIMEOUT = 30.0  # seconds; SPLICETRAIL_LOCK_TIMEOUT
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "BMP", "TIFF", "WEBP"}
# Reachable without the API key so container health checks keep working.
PUBLIC_PATHS = {"/health/live", "/health/ready"}


class MatchCoord(BaseModel):
    """Coordinate pair."""

    x: int
    y: int


class MatchInfo(BaseModel):
    """Information about a single match."""

    source_coord: MatchCoord
    target_coord: MatchCoord
    patch_size: int
    fit_p_value: float  # per-patch goodness of fit, not a match probability
    image_id: str


class CoherentRegionInfo(BaseModel):
    """Information about a coherent region."""

    offset: Optional[MatchCoord] = (
        None  # null when mirrored or rescaled (see the bboxes)
    )
    image_id: str
    num_matches: int
    confidence: float
    log10_nfa: Optional[float] = None  # < 0: expected less than once by chance
    significant: bool
    source_bbox: Dict[str, int]
    target_bbox: Dict[str, int]
    mirrored: bool = False
    source_scale: float = 1.0
    rotation_degrees: float = 0.0


class MatchResponse(BaseModel):
    """Response from match endpoint."""

    success: bool
    detected: bool
    best_match: Optional[MatchInfo] = None
    total_matches: int
    coherent_regions: List[CoherentRegionInfo] = []
    stats: Dict[str, Any]
    heatmap_base64: Optional[str] = None


class IndexResponse(BaseModel):
    """Response from index endpoint."""

    success: bool
    image_id: str
    patches_indexed: int
    message: str


async def read_upload(file: "UploadFile", limit: Optional[int] = None) -> bytes:
    """
    Read an upload in chunks, giving up (413) once it passes ``limit`` bytes.

    Defaults to MAX_FILE_SIZE. Never reads more than ``limit + 1`` bytes.
    """
    limit = MAX_FILE_SIZE if limit is None else limit
    chunks = []
    total = 0
    while True:
        chunk = await file.read(min(1024 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise _too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large() -> "HTTPException":
    return HTTPException(
        status_code=413,
        detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)}MB",
    )


def decode_image(
    contents: bytes, max_pixels: Optional[int] = None
) -> Tuple[np.ndarray, float]:
    """
    Decode an uploaded image after checking its format and pixel count.

    Image.open only reads the header, so the size check runs before any pixel
    data is decoded. PIL's own decompression bomb guard (its process-wide
    MAX_IMAGE_PIXELS, left untouched here) still applies on top.

    Returns:
        (image as numpy array, JPEG rounding-error std from its quantization tables; 0 if not JPEG)

    Raises:
        HTTPException: 400 for a bad or unsupported image, 413 for too many pixels
    """
    max_pixels = MAX_IMAGE_PIXELS if max_pixels is None else max_pixels
    try:
        image = Image.open(io.BytesIO(contents))

        if image.format not in ALLOWED_IMAGE_FORMATS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported format: {image.format}. Allowed: {sorted(ALLOWED_IMAGE_FORMATS)}",
            )

        width, height = image.size
        if width * height > max_pixels:
            raise HTTPException(
                status_code=413,
                detail=f"Image too large: {width}x{height}. Maximum {max_pixels} pixels",
            )

        jpeg_sigma = (
            (sigma_from_tables(getattr(image, "quantization", None)) or 0.0)
            if image.format == "JPEG"
            else 0.0
        )
        image_rgb = image.convert("RGB")
        return np.array(image_rgb), jpeg_sigma

    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        # The warning only arrives as an exception when warnings are set to error.
        raise HTTPException(
            status_code=413, detail=f"Image too large. Maximum {max_pixels} pixels"
        )
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid or unreadable image file")


async def validate_uploaded_image(
    file: "UploadFile", max_pixels: Optional[int] = None
) -> Tuple[np.ndarray, float]:
    """Read (size-capped) and decode an upload; decoding runs in a worker thread."""
    contents = await read_upload(file)
    return await run_in_threadpool(decode_image, contents, max_pixels)


class _Busy(Exception):
    """The engine lock wasn't free within the lock timeout."""


class _RequestGuard:
    """
    ASGI middleware in front of every route:

    - API key: when one is configured, every path but PUBLIC_PATHS needs a
      matching X-API-Key header (401 otherwise).
    - Body size: a Content-Length above ``body_limit`` gets 413 before the body
      is read; a body without one is counted as it streams in and cut off at
      the limit, so an oversized upload is never spooled in full.
    - Errors: an unhandled exception becomes a generic 500 with a request id;
      the traceback is logged with that id, never sent to the client.
    """

    def __init__(self, app, api_key: Optional[str], body_limit: int):
        self.app = app
        self.api_key = api_key.encode() if api_key else None
        self.body_limit = body_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        if self.api_key is not None and scope.get("path") not in PUBLIC_PATHS:
            given = headers.get(b"x-api-key", b"")
            if not hmac.compare_digest(given, self.api_key):
                await _send_json(
                    send,
                    401,
                    {"detail": "Missing or invalid API key (X-API-Key header)"},
                    [(b"www-authenticate", b"X-API-Key")],
                )
                return

        length = headers.get(b"content-length")
        if length is not None:
            try:
                too_big = int(length) > self.body_limit
            except ValueError:
                await _send_json(send, 400, {"detail": "Bad Content-Length"})
                return
            if too_big:
                await _send_json(send, 413, {"detail": _too_large().detail})
                return

        received = 0
        overflow = False
        started = False

        async def limited_receive():
            nonlocal received, overflow
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.body_limit:
                    overflow = True
                    raise _too_large()
            return message

        async def guarded_send(message):
            nonlocal started
            if overflow:
                return  # whatever the app made of the cut-off body; 413 goes out below
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if overflow:
                pass
            elif started:
                raise
            else:
                request_id = uuid.uuid4().hex[:16]
                logger.exception(
                    "request %s: %s %s failed",
                    request_id,
                    scope.get("method"),
                    scope.get("path"),
                )
                await _send_json(
                    send,
                    500,
                    {"detail": "Internal server error", "request_id": request_id},
                    [(b"x-request-id", request_id.encode())],
                )
                return
        if overflow and not started:
            await _send_json(send, 413, {"detail": _too_large().detail})


async def _send_json(send, status: int, body: Dict[str, Any], headers=()) -> None:
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode()),
                *headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


class IndexStore:
    """
    Engine state kept under one directory (SPLICETRAIL_INDEX_DIR).

    Layout: ``snap-<id>/engine.*`` bundles written by ForensicsEngine.save,
    ``CURRENT`` naming the live snapshot, and ``images/`` holding a lossless
    PNG of every indexed target (a loaded bundle re-reads patches from there).

    A save writes a whole new snapshot, then swaps ``CURRENT`` with os.replace,
    then deletes older snapshots and unreferenced images. A crash at any point
    leaves ``CURRENT`` naming a complete snapshot. Files aren't fsynced, so a
    power loss (as opposed to a process crash) can still lose the last save.
    """

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.images = os.path.join(self.root, "images")
        os.makedirs(self.images, exist_ok=True)
        self._pointer = os.path.join(self.root, "CURRENT")

    def _current(self) -> Optional[str]:
        try:
            with open(self._pointer) as f:
                name = f.read().strip()
        except FileNotFoundError:
            return None
        return os.path.join(self.root, name, "engine")

    def load(self, config: Config) -> Optional[ForensicsEngine]:
        """The saved engine, or None if nothing was saved yet."""
        path = self._current()
        if path is None:
            return None
        return ForensicsEngine.load(path, **config.to_dict())

    def new_image_path(self) -> str:
        return os.path.join(self.images, f"{uuid.uuid4().hex}.png")

    def save(self, engine: ForensicsEngine) -> None:
        from splicetrail.search.index import _atomic_write

        name = f"snap-{uuid.uuid4().hex}"
        directory = os.path.join(self.root, name)
        os.mkdir(directory)
        try:
            engine.save(os.path.join(directory, "engine"))

            def write_pointer(tmp):
                with open(tmp, "w") as f:
                    f.write(name)

            _atomic_write(self._pointer, write_pointer)
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        self._prune(name, engine)

    def _prune(self, keep: str, engine: ForensicsEngine) -> None:
        for entry in os.listdir(self.root):
            if entry.startswith("snap-") and entry != keep:
                shutil.rmtree(os.path.join(self.root, entry), ignore_errors=True)
        live = {engine.get_target_path(i) for i in engine.target_ids}
        for entry in os.listdir(self.images):
            path = os.path.join(self.images, entry)
            if path not in live:
                try:
                    os.remove(path)
                except OSError:
                    pass


def create_app(
    config: Optional[Config] = None, hash_list_path: Optional[str] = None
) -> "FastAPI":
    """
    Create FastAPI application with SpliceTrail engine.

    Settings read from the environment here:
        SPLICETRAIL_API_KEY: require this X-API-Key on every endpoint except
            /health/live and /health/ready (unset: no auth)
        SPLICETRAIL_INDEX_DIR: load and save the index there (unset: in memory)
        SPLICETRAIL_MAX_TARGETS: target cap, oldest evicted first (default 1000)
        SPLICETRAIL_MAX_IMAGE_PIXELS: upload pixel cap (default MAX_IMAGE_PIXELS)
        SPLICETRAIL_LOCK_TIMEOUT: seconds to wait for the engine before 503 (default 30)

    Args:
        config: Optional engine configuration; a max_targets of None gets the
            default cap, the API never runs unbounded
        hash_list_path: CSV of known hashes that /hash matches against; defaults
            to the SPLICETRAIL_HASH_LIST environment variable. Loaded once, here.

    Raises:
        ValueError: if the hash list or the saved index can't be read

    Returns:
        FastAPI application instance
    """
    if not FASTAPI_AVAILABLE:
        raise DependencyError("FastAPI required: pip install splicetrail[api]")

    api_key = os.environ.get("SPLICETRAIL_API_KEY") or None
    max_pixels = _env_int("SPLICETRAIL_MAX_IMAGE_PIXELS", MAX_IMAGE_PIXELS, min_val=1)
    lock_timeout = _env_float(
        "SPLICETRAIL_LOCK_TIMEOUT", DEFAULT_LOCK_TIMEOUT, min_val=0.0
    )
    index_dir = os.environ.get("SPLICETRAIL_INDEX_DIR") or None

    @asynccontextmanager
    async def lifespan(app):
        # Record service start time for the uptime in /health.
        app.state.start_time = time.time()
        yield

    app = FastAPI(
        title="SpliceTrail Forensics API",
        description="REST API: find where image regions were copied from",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        _RequestGuard,
        api_key=api_key,
        body_limit=MAX_FILE_SIZE + MULTIPART_OVERHEAD,
    )

    # Initialize engine
    config = config or Config()
    if config.max_targets is None:
        config = replace(config, max_targets=DEFAULT_MAX_TARGETS)
    store = IndexStore(index_dir) if index_dir else None
    engine = None
    if store is not None:
        try:
            engine = store.load(config)
        except Exception as e:
            raise ValueError(f"Can't load the saved index in {index_dir}: {e}") from e
    if engine is None:
        engine = ForensicsEngine(config)

    # Known hashes for /hash, read once so a bad file stops the server at startup.
    hash_list = None
    hash_list_path = hash_list_path or os.environ.get("SPLICETRAIL_HASH_LIST") or None
    if hash_list_path:
        from splicetrail.hashing import load_hash_list

        try:
            hash_list = load_hash_list(hash_list_path)
        except (OSError, ValueError) as e:
            raise ValueError(f"Can't read hash list {hash_list_path}: {e}") from e

    # Index, delete and match all touch the engine's target table, so they take
    # turns. The work itself runs in a worker thread to keep the event loop free.
    # A request that can't get the lock within lock_timeout gets 503. Python
    # threads can't be killed: work already running finishes even if its
    # client has gone.
    engine_lock = threading.Lock()

    def locked(fn, *args, **kwargs):
        if not engine_lock.acquire(timeout=lock_timeout):
            raise _Busy()
        try:
            return fn(*args, **kwargs)
        finally:
            engine_lock.release()

    async def run_locked(fn, *args, **kwargs):
        try:
            return await run_in_threadpool(locked, fn, *args, **kwargs)
        except _Busy:
            raise HTTPException(
                status_code=503,
                detail="Server busy: another request is using the index; retry later",
                headers={"Retry-After": str(max(1, math.ceil(lock_timeout)))},
            )

    def index_and_save(image_array, image_id, jpeg_sigma):
        path = None
        if store is not None:
            path = store.new_image_path()
            Image.fromarray(image_array).save(path, format="PNG", compress_level=1)
        try:
            count = engine.index_target(
                image_array, image_id, image_path=path, jpeg_sigma=jpeg_sigma
            )
        except BaseException:
            if path is not None and os.path.exists(path):
                os.remove(path)
            raise
        if store is not None:
            try:
                store.save(engine)
            except BaseException:
                # Keep memory in step with disk so a retry with this id works.
                engine.remove_target(image_id)
                raise
        return count

    def remove_and_save(image_id):
        removed = engine.remove_target(image_id)
        if removed and store is not None:
            store.save(engine)
        return removed

    @app.get("/")
    async def root():
        """API health check."""
        return {
            "name": "SpliceTrail Forensics API",
            "version": __version__,
            "status": "healthy",
            "indexed_targets": engine.target_count,
        }

    @app.get("/health/live")
    async def liveness_check():
        """
        Liveness probe for Kubernetes/container orchestration.

        Returns 200 if service is running (even if not ready to serve).
        This should only fail if the process is completely stuck.
        """
        return {"status": "alive", "timestamp": __import__("time").time()}

    @app.get("/health/ready")
    async def readiness_check():
        """
        Readiness probe for Kubernetes/container orchestration.

        Returns 200 if service is ready to accept requests.
        Checks that the engine is initialized and index is available.
        """
        try:
            index_size = engine.index.size if hasattr(engine, "index") else 0
            target_count = engine.target_count
        except Exception:
            logger.exception("readiness check failed")
            raise HTTPException(status_code=503, detail="Service not ready")

        body: Dict[str, Any] = {
            "status": "ready",
            "index_size": index_size,
            "target_count": target_count,
            "timestamp": __import__("time").time(),
        }
        # With persistence on, saves need a writable index directory; without
        # it nothing is written, so there's no disk to check.
        if store is not None:
            if not os.access(store.root, os.W_OK) or not os.access(
                store.images, os.W_OK
            ):
                raise HTTPException(
                    status_code=503, detail="Service not ready: index dir not writable"
                )
            body["storage_free_mb"] = shutil.disk_usage(store.root).free // (
                1024 * 1024
            )
        return body

    @app.get("/health")
    async def health_check():
        """
        Comprehensive health check endpoint.

        Returns detailed health information including:
        - Service status
        - Index statistics
        - Memory usage (if available)
        - Configuration
        """
        health_info = {
            "status": "healthy",
            "version": __version__,
            "timestamp": time.time(),
            "uptime_seconds": (
                time.time() - app.state.start_time
                if hasattr(app.state, "start_time")
                else None
            ),
            "index": {
                "size": engine.index.size if hasattr(engine, "index") else 0,
                "target_count": engine.target_count,
                "max_targets": engine.config.max_targets,
                "persistent": store is not None,
            },
            "config": {
                "patch_size": engine.config.patch_size,
                "pyramid_enabled": engine.config.pyramid_enabled,
                "coherence_enabled": engine.config.coherence_enabled,
            },
        }

        # Add memory info if available
        try:
            import psutil

            process = psutil.Process()
            memory_info = process.memory_info()
            health_info["memory"] = {
                "rss_mb": memory_info.rss / (1024 * 1024),
                "vms_mb": memory_info.vms / (1024 * 1024),
            }
        except ImportError:
            health_info["memory"] = "psutil not available"

        return health_info

    @app.get("/config")
    async def get_config():
        """Get current engine configuration."""
        return {
            "patch_size": engine.config.patch_size,
            "stride": engine.config.stride,
            "entropy_threshold": engine.config.entropy_threshold,
            "k_neighbors": engine.config.k_neighbors,
            "distance_threshold": engine.config.distance_threshold,
            "min_probability": engine.config.min_probability,
            "max_nfa": engine.config.max_nfa,
            "pyramid_enabled": engine.config.pyramid_enabled,
            "coherence_enabled": engine.config.coherence_enabled,
            "flip_invariant": engine.config.flip_invariant,
            "query_scales": engine.config.query_scales,
            "rotation_invariant": engine.config.rotation_invariant,
            "thorough": engine.config.thorough,
        }

    @app.post("/index", response_model=IndexResponse)
    async def index_image(
        file: UploadFile = File(...),
        image_id: Optional[str] = Query(
            None, description="Unique image identifier", max_length=255
        ),
    ):
        """
        Index a target image for later matching.

        Upload an image file to add it to the search index.
        Maximum file size: 50MB. Supported formats: JPEG, PNG, BMP, TIFF, WEBP.
        """
        image_array, jpeg_sigma = await validate_uploaded_image(file, max_pixels)

        # Sanitize and validate image_id
        if image_id is not None:
            # Remove potentially dangerous characters
            image_id = "".join(c for c in image_id if c.isalnum() or c in "._-")
            if not image_id:
                raise HTTPException(
                    status_code=400, detail="Invalid image_id after sanitization"
                )
        else:
            # Random rather than counter-based: a count reuses ids after deletes.
            image_id = f"img_{uuid.uuid4().hex[:12]}"

        try:
            count = await run_locked(index_and_save, image_array, image_id, jpeg_sigma)
        except TargetExistsError:
            raise HTTPException(
                status_code=409,
                detail=f"Image ID '{image_id}' already exists. Use a different ID or delete the existing one.",
            )

        return IndexResponse(
            success=True,
            image_id=image_id,
            patches_indexed=count,
            message=f"Indexed {count} patches from {file.filename}",
        )

    @app.post("/match", response_model=MatchResponse)
    async def match_image(
        file: UploadFile = File(...),
        target_ids: Optional[str] = Query(
            None, description="Comma-separated target IDs", max_length=1000
        ),
        return_heatmap: bool = Query(False, description="Include base64 heatmap"),
    ):
        """
        Match a source image against indexed targets.

        Upload an image to search for it in the indexed collection.
        Maximum file size: 50MB. Supported formats: JPEG, PNG, BMP, TIFF, WEBP.
        """
        image_array, jpeg_sigma = await validate_uploaded_image(file, max_pixels)

        try:
            # Parse target IDs if provided
            target_id_list = None
            if target_ids:
                target_id_list = [t.strip() for t in target_ids.split(",")]

            result = await run_locked(
                engine.match,
                image_array,
                jpeg_sigma=jpeg_sigma,
                target_ids=target_id_list,
                return_heatmap=return_heatmap,
            )

            # Build response
            best_match_info = None
            if result.best_match:
                best_match_info = MatchInfo(
                    source_coord=MatchCoord(
                        x=result.best_match.source_coord[0],
                        y=result.best_match.source_coord[1],
                    ),
                    target_coord=MatchCoord(
                        x=result.best_match.target_coord[0],
                        y=result.best_match.target_coord[1],
                    ),
                    patch_size=result.best_match.patch_size,
                    fit_p_value=result.best_match.probability,
                    image_id=result.best_match.image_id,
                )

            # Build coherent regions info
            regions_info = []
            for region in result.coherent_regions:
                offset = region_offset(region)
                regions_info.append(
                    CoherentRegionInfo(
                        offset=(
                            None
                            if offset is None
                            else MatchCoord(x=offset[0], y=offset[1])
                        ),
                        mirrored=region.mirrored,
                        source_scale=region.source_scale,
                        rotation_degrees=round(float(np.degrees(region.rotation)), 2),
                        image_id=region.image_id,
                        num_matches=len(region.matches),
                        confidence=region.confidence,
                        log10_nfa=region.log10_nfa,
                        significant=region.significant,
                        source_bbox={
                            "x": region.source_bbox[0],
                            "y": region.source_bbox[1],
                            "width": region.source_bbox[2],
                            "height": region.source_bbox[3],
                        },
                        target_bbox={
                            "x": region.target_bbox[0],
                            "y": region.target_bbox[1],
                            "width": region.target_bbox[2],
                            "height": region.target_bbox[3],
                        },
                    )
                )

            # Encode heatmap if requested
            heatmap_b64 = None
            if return_heatmap and result.heatmap is not None:
                heatmap_img = Image.fromarray((result.heatmap * 255).astype(np.uint8))
                buffer = io.BytesIO()
                heatmap_img.save(buffer, format="PNG")
                heatmap_b64 = base64.b64encode(buffer.getvalue()).decode()

            return MatchResponse(
                success=True,
                detected=bool(result.significant_regions),
                best_match=best_match_info,
                total_matches=len(result.matches),
                coherent_regions=regions_info,
                stats=result.stats,
                heatmap_base64=heatmap_b64,
            )

        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/hash")
    async def hash_endpoint(
        file: UploadFile = File(...),
        algorithm: Optional[List[str]] = Query(
            None,
            description="Algorithm to use; repeat for several. Default: every local one",
        ),
    ):
        """
        Perceptual hashes of an uploaded image.

        With a server hash list (--hash-list), the response also has the list
        entries the image matches. photodna sends the image to Microsoft's
        PhotoDNA Cloud Service and only works when the operator has set
        SPLICETRAIL_PHOTODNA_KEY on the server.
        """
        from splicetrail.hashing import (
            LOCAL_ALGORITHMS,
            available_algorithms,
            hash_image,
            match_hashes,
        )

        names = []
        for name in algorithm or []:
            name = name.lower()
            if name not in names:
                names.append(name)
        use_photodna = "photodna" in names
        names = [n for n in names if n != "photodna"]
        unknown = [n for n in names if n not in LOCAL_ALGORITHMS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown algorithm '{unknown[0]}'; choose from: pdq, phash, phash-lib, photodna",
            )
        if not algorithm:
            names = available_algorithms()
            if not names:
                raise HTTPException(
                    status_code=501,
                    detail="No hash algorithm is installed on the server: "
                    "pip install splicetrail[hash]",
                )

        client = None
        if use_photodna:
            from splicetrail import photodna

            # Unauthenticated, anyone reaching the server could spend the
            # operator's PhotoDNA quota and send images to Microsoft.
            if api_key is None:
                raise HTTPException(
                    status_code=403,
                    detail="photodna is disabled: it needs SPLICETRAIL_API_KEY set on the "
                    "server, so that only key holders can send images to PhotoDNA",
                )
            if not os.environ.get("SPLICETRAIL_PHOTODNA_KEY"):
                raise HTTPException(
                    status_code=403,
                    detail="photodna is disabled: the server operator has to set "
                    "SPLICETRAIL_PHOTODNA_KEY (a Microsoft PhotoDNA Cloud Service key) on the server",
                )
            try:
                client = photodna.PhotoDnaClient()
            except photodna.PhotoDnaError as e:
                raise HTTPException(status_code=501, detail=str(e))

        contents = await read_upload(file)
        image_array, _ = await run_in_threadpool(
            decode_image, contents, max(max_pixels, MAX_HASH_PIXELS)
        )

        try:
            hashes = await run_in_threadpool(
                hash_image, image_array, names, dihedral=config.thorough
            )
        except DependencyError as e:
            raise HTTPException(status_code=501, detail=str(e))
        except RuntimeError as e:
            raise HTTPException(
                status_code=502, detail=f"Hashing with {', '.join(names)} failed: {e}"
            )

        body: Dict[str, Any] = {
            "hashes": [
                {
                    "algorithm": h.algorithm,
                    "hash": h.hex,
                    "quality": h.quality,
                    "variant": h.variant,
                }
                for h in hashes
            ]
        }

        if hash_list is not None:
            report = await run_in_threadpool(match_hashes, hashes, hash_list)
            body["matches"] = [
                {
                    "id": m.id,
                    "algorithm": m.algorithm,
                    "hash": m.hex,
                    "distance": m.distance,
                    "variant": m.variant,
                }
                for m in report.matches
            ]

        if client is not None:
            try:
                body["photodna"] = await run_in_threadpool(client.match, contents)
            except photodna.PhotoDnaError as e:
                raise HTTPException(status_code=502, detail=str(e))

        return body

    @app.get("/targets")
    async def list_targets():
        """List all indexed target images (ids and sizes; never server paths)."""
        targets = []
        for image_id in engine.target_ids:
            shape = engine.get_target_shape(image_id)
            targets.append(
                {
                    "image_id": image_id,
                    "shape": {"height": shape[0], "width": shape[1]} if shape else None,
                }
            )
        return {"targets": targets, "count": len(targets)}

    @app.delete("/targets/{image_id}")
    async def delete_target(image_id: str):
        """
        Remove a target and its descriptors from the index.
        """
        if not await run_locked(remove_and_save, image_id):
            raise HTTPException(status_code=404, detail=f"Target {image_id} not found")
        return {"success": True, "message": f"Removed target {image_id}"}

    return app


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False  # a hostname: assume it's reachable from outside


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    config: Optional[Config] = None,
    hash_list_path: Optional[str] = None,
):
    """
    Run the API server.

    Args:
        host: Host to bind to
        port: Port to listen on
        config: Engine configuration
        hash_list_path: CSV of known hashes for /hash
    """
    try:
        import uvicorn
    except ImportError:
        raise DependencyError("uvicorn required: pip install splicetrail[api]")

    # The package logger only has a NullHandler; a server should show its
    # warnings and logged 500s, unless the host program configured logging.
    package_logger = logging.getLogger("splicetrail")
    if not logging.getLogger().handlers and all(
        isinstance(h, logging.NullHandler) for h in package_logger.handlers
    ):
        from splicetrail.logging_config import setup_logging

        setup_logging("WARNING")

    app = create_app(config, hash_list_path=hash_list_path)
    if not _is_loopback(host) and not os.environ.get("SPLICETRAIL_API_KEY"):
        logger.warning(
            "WARNING: serving on %s without SPLICETRAIL_API_KEY: anyone who can reach "
            "this port can index, match and delete images. Set SPLICETRAIL_API_KEY or "
            "bind to 127.0.0.1.",
            host,
        )
    uvicorn.run(app, host=host, port=port)


def __getattr__(name):
    # `uvicorn splicetrail.api:app` asks for this attribute. Build the default app on
    # first use rather than at import, so importing the module has no side
    # effects (it would read SPLICETRAIL_HASH_LIST and fail on a bad file).
    if name == "app":
        global app
        app = create_app() if FASTAPI_AVAILABLE else None
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
