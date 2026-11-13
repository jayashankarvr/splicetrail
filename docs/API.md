# SpliceTrail REST API Documentation

> FastAPI-based REST interface for the SpliceTrail forensics engine

**Base URL**: `http://localhost:8000`

The API reports its version in `GET /` and `GET /health`.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Authentication](#authentication)
- [Server Settings](#server-settings)
- [Endpoints](#endpoints)
  - [Health Checks](#health-checks)
  - [Configuration](#configuration)
  - [Index Management](#index-management)
  - [Matching](#matching)
  - [Image Hashes](#image-hashes)
  - [Target Management](#target-management)
- [Error Handling](#error-handling)
- [Rate Limiting](#rate-limiting)
- [Examples](#examples)

---

## Quick Start

### Start the Server

```bash
# Using python module (listens on 127.0.0.1:8000)
python -m splicetrail serve --port 8000

# Reachable from other machines: set a key first
export SPLICETRAIL_API_KEY="$(openssl rand -hex 32)"
python -m splicetrail serve --host 0.0.0.0 --port 8000

# Using Docker (published on the host's 127.0.0.1:8000)
docker compose up

# Using Makefile
make serve
```

### Basic Usage

```bash
# Check API health
curl http://localhost:8000/health

# Index a target image
curl -X POST "http://localhost:8000/index?image_id=target_001" \
  -F "file=@target.jpg"

# Match a source image
curl -X POST "http://localhost:8000/match?return_heatmap=true" \
  -F "file=@source.jpg"
```

With `SPLICETRAIL_API_KEY` set, add the key to every request:

```bash
curl -H "X-API-Key: $SPLICETRAIL_API_KEY" http://localhost:8000/health
curl -X POST "http://localhost:8000/index?image_id=target_001" \
  -H "X-API-Key: $SPLICETRAIL_API_KEY" -F "file=@target.jpg"
```

The examples below leave the header out; add it whenever a key is set.

---

## Authentication

Without `SPLICETRAIL_API_KEY` there is no authentication: anyone who can reach
the port can index, match and delete images. That is why the server binds to
`127.0.0.1` by default, and logs a warning when bound anywhere else without a
key.

With `SPLICETRAIL_API_KEY` set, every request needs the header
`X-API-Key: <key>`. The only exceptions are the probes `GET /health/live` and
`GET /health/ready`, so container health checks keep working. Everything else,
including `/`, `/health`, `/docs`, `/redoc` and `/openapi.json`, answers a
missing or wrong key with:

```json
{"detail": "Missing or invalid API key (X-API-Key header)"}
```

**Status Codes:**
- `401 Unauthorized`: Missing or wrong `X-API-Key`

The key travels in clear text over plain HTTP; put a TLS-terminating reverse
proxy in front (see `nginx.conf`) when the API leaves the machine.

---

## Server Settings

Read from the environment when the server starts:

| Variable | Default | Effect |
|---|---|---|
| `SPLICETRAIL_API_KEY` | unset (no authentication) | Require `X-API-Key` on every endpoint except `/health/live` and `/health/ready` |
| `SPLICETRAIL_INDEX_DIR` | unset (index kept in memory only) | Load the index from this directory at startup and save it after every index and delete |
| `SPLICETRAIL_MAX_TARGETS` | 1000 | Most targets kept; past it, the oldest indexed target is evicted |
| `SPLICETRAIL_MAX_IMAGE_PIXELS` | 500000 | Pixel cap for `/index` and `/match` uploads (413 above it) |
| `SPLICETRAIL_LOCK_TIMEOUT` | 30 | Seconds a request waits for the engine before 503 |
| `SPLICETRAIL_HASH_LIST` | unset | CSV of known hashes for `/hash` (same as `serve --hash-list`) |
| `SPLICETRAIL_PHOTODNA_KEY` | unset | PhotoDNA Cloud Service key; `photodna` on `/hash` also needs `SPLICETRAIL_API_KEY` |

The engine settings (`SPLICETRAIL_PATCH_SIZE`, `SPLICETRAIL_THOROUGH` and the
others) are listed in the README under Environment Variables.

**Limits that aren't configurable:** uploads are capped at 50 MB, and `/hash`
accepts images up to 50 megapixels (or the pixel cap, if that is higher).

**Persistence.** With `SPLICETRAIL_INDEX_DIR`, each index or delete writes a
complete new snapshot of the index into that directory and then switches to
it atomically, so a crash leaves the last complete save. Indexed images are
kept there as lossless PNGs, because a reloaded index re-reads patches from
them. A save rewrites the whole index, so it gets slower as the index grows;
files aren't fsynced, so a power cut (unlike a process crash) can lose the
last save. A saved index keeps the patch size and other index-shaping settings
it was built with, whatever the server is started with. If the directory
holds an index that can't be read, the server refuses to start. Without the
variable, the index lives in memory and is lost on restart.

**Concurrency.** Index, match and delete take turns on the engine and run in a
worker thread, so health checks keep answering. A request that can't get the
engine within `SPLICETRAIL_LOCK_TIMEOUT` seconds gets 503 with a `Retry-After`
header. A request that has started can't be cancelled: it finishes even if the
client disconnects.

---

## Endpoints

### Health Checks

#### `GET /`

Root endpoint - basic health check.

**Response:**
```json
{
  "name": "SpliceTrail Forensics API",
  "version": "0.3.0",
  "status": "healthy",
  "indexed_targets": 42
}
```

**Status Codes:**
- `200 OK`: Service is running
- `401 Unauthorized`: API key required and missing or wrong

---

#### `GET /health/live`

Kubernetes liveness probe - checks if service is alive. Never needs the API key.

**Response:**
```json
{
  "status": "alive",
  "timestamp": 1704652800.0
}
```

**Status Codes:**
- `200 OK`: Process is running (no answer at all means it is hung)

**Use Case**: Kubernetes will restart pod if this fails

---

#### `GET /health/ready`

Kubernetes readiness probe - checks if service is ready to accept requests.
Never needs the API key. With `SPLICETRAIL_INDEX_DIR` set it also checks that
the directory is writable and reports its free space.

**Response:**
```json
{
  "status": "ready",
  "index_size": 1500000,
  "target_count": 42,
  "timestamp": 1704652800.0,
  "storage_free_mb": 51200
}
```

`storage_free_mb` is present only with `SPLICETRAIL_INDEX_DIR`.

**Status Codes:**
- `200 OK`: Service is ready
- `503 Service Unavailable`: The engine can't be read, or the index directory isn't writable

**Use Case**: Kubernetes will not send traffic to pod if this fails

---

#### `GET /health`

Comprehensive health check with detailed metrics.

**Response:**
```json
{
  "status": "healthy",
  "version": "0.3.0",
  "timestamp": 1704652800.0,
  "uptime_seconds": 3600.5,
  "index": {
    "size": 1500000,
    "target_count": 42,
    "max_targets": 1000,
    "persistent": true
  },
  "config": {
    "patch_size": 5,
    "pyramid_enabled": true,
    "coherence_enabled": true
  },
  "memory": {
    "rss_mb": 2048.5,
    "vms_mb": 4096.2
  }
}
```

`index.persistent` is true when `SPLICETRAIL_INDEX_DIR` is set. Target ids are
no longer listed here (they were before 0.3.0); use `GET /targets`.

**Status Codes:**
- `200 OK`: Service is healthy
- `401 Unauthorized`: API key required and missing or wrong

**Note**: Memory stats require the `psutil` package; without it `memory` is
the string `"psutil not available"`.

---

### Configuration

#### `GET /config`

Get current engine configuration.

**Response:**
```json
{
  "patch_size": 5,
  "stride": 1,
  "entropy_threshold": null,
  "k_neighbors": 8,
  "distance_threshold": 0.5,
  "min_probability": 0.01,
  "max_nfa": 1.0,
  "pyramid_enabled": true,
  "coherence_enabled": true,
  "flip_invariant": false,
  "query_scales": [1.0],
  "rotation_invariant": false,
  "thorough": false
}
```

**Status Codes:**
- `200 OK`: Configuration returned

---

### Index Management

#### `POST /index`

Index a target image for later matching.

**Request:**
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Parameters**:
  - `file` (required, form field): Image file (JPEG, PNG, BMP, TIFF, WEBP)
  - `image_id` (optional, query parameter): Unique identifier (alphanumeric + `._-`).
    If omitted a random `img_<hex>` id is generated.

**Limits:**
- Max file size: 50MB
- Max image pixels: `SPLICETRAIL_MAX_IMAGE_PIXELS` (default 500,000)
- `image_id` max length: 255 characters
- Past `SPLICETRAIL_MAX_TARGETS` targets (default 1000), the oldest is evicted

**Example:**
```bash
curl -X POST "http://localhost:8000/index?image_id=suspect_photo_001" \
  -F "file=@target.jpg"
```

**Response:**
```json
{
  "success": true,
  "image_id": "suspect_photo_001",
  "patches_indexed": 456789,
  "message": "Indexed 456789 patches from target.jpg"
}
```

**Status Codes:**
- `200 OK`: Image indexed successfully
- `400 Bad Request`: Invalid image or parameters
- `401 Unauthorized`: API key required and missing or wrong
- `409 Conflict`: Image ID already exists
- `413 Payload Too Large`: File over 50 MB or image over the pixel cap
- `422 Unprocessable Entity`: No `file` field, or `image_id` over 255 characters
- `500 Internal Server Error`: Indexing failed (see [Error Handling](#error-handling))
- `503 Service Unavailable`: Engine busy past the lock timeout; retry after `Retry-After` seconds

**Error Example:**
```json
{
  "detail": "Image ID 'suspect_photo_001' already exists. Use a different ID or delete the existing one."
}
```

---

#### `GET /targets`

List all indexed target images.

Each entry holds only the target's id and its size; server file paths are never returned.

**Response:**
```json
{
  "targets": [
    {
      "image_id": "suspect_photo_001",
      "shape": {"height": 1024, "width": 768}
    },
    {
      "image_id": "suspect_photo_002",
      "shape": {"height": 2048, "width": 1536}
    }
  ],
  "count": 2
}
```

**Status Codes:**
- `200 OK`: Targets listed
- `401 Unauthorized`: API key required and missing or wrong

---

#### `DELETE /targets/{image_id}`

Remove a target and its descriptors from the index.

**Parameters:**
- `image_id` (path): Target identifier

**Example:**
```bash
curl -X DELETE http://localhost:8000/targets/suspect_photo_001
```

**Response:**
```json
{
  "success": true,
  "message": "Removed target suspect_photo_001"
}
```

**Status Codes:**
- `200 OK`: Target removed
- `401 Unauthorized`: API key required and missing or wrong
- `404 Not Found`: Target does not exist
- `503 Service Unavailable`: Engine busy past the lock timeout

---

### Matching

#### `POST /match`

Match a source image against indexed targets.

**Request:**
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Parameters**:
  - `file` (required, form field): Source image file
  - `target_ids` (optional, query parameter): Comma-separated target IDs to search
  - `return_heatmap` (optional, query parameter): Include base64-encoded heatmap (default: false)

**Example:**
```bash
curl -X POST "http://localhost:8000/match?target_ids=suspect_photo_001,suspect_photo_002&return_heatmap=true" \
  -F "file=@fragment.jpg"
```

**Response:**
```json
{
  "success": true,
  "detected": true,
  "best_match": {
    "source_coord": {"x": 100, "y": 200},
    "target_coord": {"x": 450, "y": 680},
    "patch_size": 5,
    "fit_p_value": 0.83,
    "image_id": "suspect_photo_001"
  },
  "total_matches": 2458,
  "coherent_regions": [
    {
      "offset": {"x": 350, "y": 480},
      "image_id": "suspect_photo_001",
      "num_matches": 156,
      "confidence": 0.92,
      "log10_nfa": -14.2,
      "significant": true,
      "source_bbox": {"x": 0, "y": 0, "width": 200, "height": 300},
      "target_bbox": {"x": 350, "y": 480, "width": 200, "height": 300}
    }
  ],
  "stats": {
    "source_patches": 123456,
    "total_matches": 2458,
    "coherent_regions": 1,
    "significant_regions": 1
  },
  "heatmap_base64": "iVBORw0KGgoAAAANSUhEUgAA..."
}
```

**Response Fields:**
- `detected`: true if any region is significant (NFA < `max_nfa`)
- `best_match`: Best patch match of the most significant region (null if no matches)
  - `source_coord`: (x, y) in source image
  - `target_coord`: (x, y) in target image
  - `patch_size`: Size of matched patch
  - `fit_p_value`: Per-patch chi-square goodness of fit. Not a match
    probability: small patches fit each other by chance all the time
  - `image_id`: Target image identifier
- `total_matches`: Number of per-patch matches that passed the pre-filter
- `coherent_regions`: Clusters of matches sharing one offset, most significant first
  - `offset`: (dx, dy) from source to target
  - `image_id`: Target image the region was copied from
  - `num_matches`: Patches consistent with the region's color transform
  - `log10_nfa`: log10 of the expected number of regions this good with no
    copy present. Negative = less than one expected by chance
  - `significant`: `log10_nfa` below `log10(max_nfa)`
  - `confidence`: Ranking heuristic (mean fit p-value x log2 size)
  - `source_bbox`/`target_bbox`: Bounding boxes (x, y, width, height)
- `stats`: Search statistics
- `heatmap_base64`: Base64-encoded PNG heatmap (if requested)

**Status Codes:**
- `200 OK`: Matching completed
- `400 Bad Request`: Invalid image or parameters
- `401 Unauthorized`: API key required and missing or wrong
- `413 Payload Too Large`: File over 50 MB or image over the pixel cap
- `500 Internal Server Error`: Matching failed
- `503 Service Unavailable`: Engine busy past the lock timeout

**Heatmap Decoding:**
```python
import base64
from PIL import Image
import io

heatmap_bytes = base64.b64decode(response['heatmap_base64'])
heatmap_image = Image.open(io.BytesIO(heatmap_bytes))
heatmap_image.save('heatmap.png')
```

---

### Image Hashes

#### `POST /hash`

Perceptual hashes of an uploaded image, and the entries of the server's hash
list it matches. See "Image hashing" in the README for what the algorithms are
and how the match distances were chosen.

**Request:**
- **Method**: `POST`
- **Content-Type**: `multipart/form-data`
- **Parameters**:
  - `file` (required, form field): Image file
  - `algorithm` (optional, query parameter, repeatable): `pdq`, `phash`,
    `phash-lib` or `photodna`. Default: every local algorithm installed on the
    server (`photodna` is never included unless named)

Rotations and flips are hashed too when the server runs with `SPLICETRAIL_THOROUGH=true`.

**Example:**
```bash
curl -X POST "http://localhost:8000/hash?algorithm=pdq&algorithm=phash" \
  -F "file=@photo.jpg"
```

**Response:**
```json
{
  "hashes": [
    {"algorithm": "pdq", "hash": "d8f8f0cee0f4a84f0637022a078f67f0b36e2ed596621e1d33e6339c4e9c9b22",
     "quality": 100, "variant": "original"},
    {"algorithm": "phash", "hash": "aca29c1c33dc23d7", "quality": null, "variant": "original"}
  ],
  "matches": [
    {"id": "bridge", "algorithm": "pdq",
     "hash": "d8f8f0cee0f4a84f0637022a078f67f0b36e2ed596621e1d33e6339c4e9c9b22",
     "distance": 0, "variant": "original"}
  ]
}
```

**Response Fields:**
- `hashes`: One entry per algorithm, or eight per algorithm (one per rotation
  and flip) in thorough mode
  - `hash`: Lowercase hex (64 digits for `pdq`, 16 for `phash` and `phash-lib`)
  - `quality`: PDQ's 0-100 quality score; `null` for pHash. PDQ hashes below
    50 are not matched
  - `variant`: `original`, or the rotation/flip that was hashed (`rot90`,
    `rot180`, `rot270`, `flip_x`, `flip_y`, `flip_diag`, `flip_antidiag`)
- `matches`: Present only when the server was started with a hash list
  (`splicetrail serve --hash-list known.csv` or `SPLICETRAIL_HASH_LIST`). List entries
  within the algorithm's distance (31 bits for `pdq`, 10 for `phash` and
  `phash-lib`), nearest first. The list itself stays on the server
- `photodna`: Present only when `photodna` was asked for: the PhotoDNA Cloud
  Service's answer, passed through unchanged

**PhotoDNA:** `algorithm=photodna` sends the uploaded image to Microsoft. It is
refused unless the server operator has a PhotoDNA licence and has set both
`SPLICETRAIL_PHOTODNA_KEY` and `SPLICETRAIL_API_KEY` on the server (so only key
holders can send images to Microsoft).

**Limits:** 50 MB per file; images up to 50 megapixels (or
`SPLICETRAIL_MAX_IMAGE_PIXELS`, if higher). `/hash` doesn't use the engine, so
it doesn't wait for the lock.

**Status Codes:**
- `200 OK`: Hashed
- `400 Bad Request`: Unknown algorithm or unreadable image
- `401 Unauthorized`: API key required and missing or wrong
- `403 Forbidden`: `photodna` asked for but not enabled on this server (no
  `SPLICETRAIL_API_KEY` or no `SPLICETRAIL_PHOTODNA_KEY`)
- `413 Payload Too Large`: File exceeds limits
- `501 Not Implemented`: The algorithm's library isn't installed on the server
  (the message says how to install it), or the server's PhotoDNA settings are unusable
- `502 Bad Gateway`: libpHash or the PhotoDNA service failed. The message is fixed
  text ending in a request id, which is also sent in the `X-Request-ID` header;
  operators can find the underlying error by searching the server log for that id

---

## Error Handling

### Standard Error Response

```json
{
  "detail": "Error message describing what went wrong"
}
```

Unexpected server errors return a fixed message and an id, also sent in the
`X-Request-ID` header; the traceback is in the server log under that id:

```json
{"detail": "Internal server error", "request_id": "3f9c2a1b7d04e5a6"}
```

### Common Error Codes

| Status Code | Meaning | Example |
|-------------|---------|---------|
| `400 Bad Request` | Invalid input | Unsupported image format, invalid parameters |
| `401 Unauthorized` | Missing or wrong API key | `X-API-Key` absent while `SPLICETRAIL_API_KEY` is set |
| `403 Forbidden` | Feature disabled | `photodna` without the needed keys |
| `404 Not Found` | Resource not found | Target ID does not exist |
| `409 Conflict` | Resource conflict | Duplicate image ID |
| `413 Payload Too Large` | Too large | File over 50 MB, or image over the pixel cap |
| `422 Unprocessable Entity` | Malformed request | Missing `file` field |
| `500 Internal Server Error` | Server error | Indexing or matching failed |
| `503 Service Unavailable` | Busy or not ready | Engine busy past the lock timeout (`Retry-After` set); readiness check failed |

### Error Examples

**Invalid Image Format:**
```json
{
  "detail": "Unsupported format: GIF. Allowed: ['BMP', 'JPEG', 'PNG', 'TIFF', 'WEBP']"
}
```

**File Too Large:**
```json
{
  "detail": "File too large. Maximum size is 50MB"
}
```

**Too Many Pixels:**
```json
{
  "detail": "Image too large: 1024x768. Maximum 500000 pixels"
}
```

**Engine Busy** (with a `Retry-After` header):
```json
{
  "detail": "Server busy: another request is using the index; retry later"
}
```

---

## Rate Limiting

**Current Version**: No rate limiting by default

**Production Deployment**: Add rate limiting via Nginx:

```nginx
http {
    limit_req_zone $binary_remote_addr zone=api_limit:10m rate=10r/s;

    server {
        location / {
            limit_req zone=api_limit burst=10 nodelay;
            proxy_pass http://splicetrail_backend;
        }
    }
}
```

This limits each IP to 10 requests/second with burst up to 20.

---

## Examples

### Python Client

```python
import os

import requests

API_BASE = "http://localhost:8000"
# Only needed when the server has SPLICETRAIL_API_KEY set
session = requests.Session()
session.headers["X-API-Key"] = os.environ.get("SPLICETRAIL_API_KEY", "")

# Index a target
with open("target.jpg", "rb") as f:
    response = session.post(
        f"{API_BASE}/index",
        params={"image_id": "target_001"},
        files={"file": f},
    )
    print(response.json())

# Match a source
with open("source.jpg", "rb") as f:
    response = session.post(
        f"{API_BASE}/match",
        params={"target_ids": "target_001", "return_heatmap": "true"},
        files={"file": f},
    )
    result = response.json()

    for region in result["coherent_regions"]:
        if region["significant"]:
            print(f"Copied from {region['image_id']}")
            print(f"  source bbox: {region['source_bbox']}")
            print(f"  target bbox: {region['target_bbox']}")
            print(f"  log10 NFA: {region['log10_nfa']:.1f}")

    # Save heatmap
    if result["heatmap_base64"]:
        import base64
        from PIL import Image
        import io

        heatmap_bytes = base64.b64decode(result["heatmap_base64"])
        heatmap = Image.open(io.BytesIO(heatmap_bytes))
        heatmap.save("heatmap.png")

# List all targets
response = session.get(f"{API_BASE}/targets")
targets = response.json()
print(f"Indexed targets: {targets['count']}")

# Delete a target
response = session.delete(f"{API_BASE}/targets/target_001")
print(response.json())
```

### cURL Examples

**Batch Index Multiple Targets:**
```bash
for img in targets/*.jpg; do
  id=$(basename "$img" .jpg)
  curl -X POST "http://localhost:8000/index?image_id=$id" \
    -H "X-API-Key: $SPLICETRAIL_API_KEY" -F "file=@$img"
done
```

**Match with Specific Targets:**
```bash
curl -X POST "http://localhost:8000/match?target_ids=img_001,img_002,img_003" \
  -F "file=@source.jpg"
```

**Health Check Loop:**
```bash
while true; do
  curl -s http://localhost:8000/health/ready | jq '.status'
  sleep 5
done
```

### JavaScript (Fetch API)

```javascript
// Index a target
async function indexImage(file, imageId) {
  const formData = new FormData();
  formData.append('file', file);

  // headers: { 'X-API-Key': key } when the server has SPLICETRAIL_API_KEY set
  const url = `http://localhost:8000/index?image_id=${encodeURIComponent(imageId)}`;
  const response = await fetch(url, {
    method: 'POST',
    body: formData
  });

  return await response.json();
}

// Match a source
async function matchImage(file, targetIds = null, returnHeatmap = false) {
  const formData = new FormData();
  formData.append('file', file);
  const params = new URLSearchParams({ return_heatmap: returnHeatmap.toString() });
  if (targetIds) {
    params.set('target_ids', targetIds.join(','));
  }

  const response = await fetch(`http://localhost:8000/match?${params}`, {
    method: 'POST',
    body: formData
  });

  return await response.json();
}

// Usage
const fileInput = document.getElementById('file-input');
const file = fileInput.files[0];

const result = await matchImage(file, ['target_001'], true);
console.log('Detected:', result.detected, result.coherent_regions);

// Display heatmap
if (result.heatmap_base64) {
  const img = document.createElement('img');
  img.src = `data:image/png;base64,${result.heatmap_base64}`;
  document.body.appendChild(img);
}
```

---

## Performance Tips

### Optimize Index Size

1. **Increase stride**: Fewer patches, faster indexing
   ```bash
   export SPLICETRAIL_STRIDE=2  # 4x fewer patches
   ```

2. **Set an entropy threshold**: drop weakly textured patches (bits; must be
   below log2(8) = 3 for the 8-bin histogram, or nothing passes)
   ```bash
   export SPLICETRAIL_ENTROPY_THRESHOLD=2.0
   ```

### Concurrent Requests

Index, match and delete run in a worker thread, so the server stays responsive
(health checks keep answering). They take turns on the engine, so throughput
doesn't grow with client concurrency, and requests queued longer than
`SPLICETRAIL_LOCK_TIMEOUT` get 503. Each server process has its own index, so
more replicas don't share targets (a shared index isn't supported). Client-side
concurrency still works:

```python
import concurrent.futures
import requests

files = ["source1.jpg", "source2.jpg", "source3.jpg"]

def match_image(filename):
    with open(filename, "rb") as f:
        response = requests.post(
            "http://localhost:8000/match",
            files={"file": f}
        )
        return response.json()

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
    results = list(executor.map(match_image, files))
```

### Memory Management

Set maximum target limit to prevent memory exhaustion:

```bash
export SPLICETRAIL_MAX_TARGETS=100
```

When the limit is reached, the least recently indexed targets are evicted and
their descriptors removed from the index. The API uses 1000 when the variable
isn't set.

---

## Deployment Checklist

- [ ] Set `SPLICETRAIL_API_KEY` (and send `X-API-Key` from clients)
- [ ] Keep the default `127.0.0.1` bind, or publish only through a TLS reverse proxy
- [ ] Set `SPLICETRAIL_MAX_TARGETS` and `SPLICETRAIL_MAX_IMAGE_PIXELS` for your memory and time budget
- [ ] Set `SPLICETRAIL_INDEX_DIR` on persistent storage if the index must survive restarts
- [ ] Configure `/health/live` and `/health/ready` in the orchestrator
- [ ] Enable rate limiting in the reverse proxy
- [ ] Back up the index directory

---

## OpenAPI Documentation

Interactive API documentation (needs the API key when one is set) is available at:
- **Swagger UI**: `http://localhost:8000/docs`
- **ReDoc**: `http://localhost:8000/redoc`
- **OpenAPI JSON**: `http://localhost:8000/openapi.json`
