# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.3.x   | :white_check_mark: |
| < 0.3   | :x:                |

## Reporting a Vulnerability

Please report vulnerabilities privately through GitHub's private vulnerability
reporting:

https://github.com/jayashankarvr/splicetrail/security/advisories/new

1. **Do NOT** open a public GitHub issue.
2. Describe the problem and the affected version.
3. Include steps to reproduce if you can.

We aim to respond within 48 hours and will work with you to understand and
address the issue.

## Running the API

`splicetrail serve` starts a REST API that indexes, matches and deletes images.
Before exposing it:

- **Authentication.** Set `SPLICETRAIL_API_KEY`. Every request then needs an
  `X-API-Key` header with that value, except the probes `/health/live` and
  `/health/ready`; anything else gets 401, including `/docs` and
  `/openapi.json`. Without the variable there is no authentication at all.
- **Bind address.** The server listens on `127.0.0.1` by default. Binding to
  any other address without `SPLICETRAIL_API_KEY` logs a warning. The Docker
  image listens on `0.0.0.0` inside the container, and `docker-compose.yml`
  publishes it on the host's loopback only.
- **Limits.** Uploads are capped at 50 MB (413). `/index` and `/match` refuse
  images over `SPLICETRAIL_MAX_IMAGE_PIXELS` pixels (default 500,000; 413);
  `/hash` allows up to 50 megapixels. At most `SPLICETRAIL_MAX_TARGETS` targets
  (default 1000) are kept, oldest evicted first. Index, match and delete take
  turns on the engine; a request that waits longer than
  `SPLICETRAIL_LOCK_TIMEOUT` seconds (default 30) gets 503 with `Retry-After`.
- **Requests can't be cancelled.** Once indexing or matching has started it
  runs to the end, even if the client disconnects. The pixel cap is what keeps
  one request bounded; lower it if requests take too long on your hardware.
- **PhotoDNA.** `POST /hash?algorithm=photodna` sends the uploaded image to
  Microsoft. It is refused (403) unless both `SPLICETRAIL_API_KEY` and
  `SPLICETRAIL_PHOTODNA_KEY` are set, so only key holders can spend the
  operator's PhotoDNA quota.
- **Errors.** Unexpected errors return a fixed message and a request id; the
  details go to the server log only.
- **No TLS, no rate limiting.** Put a reverse proxy (see `nginx.conf`) in front
  for HTTPS and rate limits.

## Index Files

A saved index is three files: `<prefix>.index` (FAISS), `<prefix>.meta.json`
and `<prefix>.engine.json` (a thorough index adds the same three under
`<prefix>.rotated.*`). The JSON files hold
metadata only and pickle is never used. Loading a FAISS file from an untrusted
source still runs FAISS's native parser on it, so only load indexes you made.
With `SPLICETRAIL_INDEX_DIR` set, the API keeps its index and a PNG copy of
every indexed image in that directory; protect it like the images themselves.

## Dependencies

We update dependencies to address known vulnerabilities. Run
`pip install --upgrade splicetrail` periodically.
