<!--
SPDX-FileCopyrightText: 2026 RyanLin-InfEvo

SPDX-License-Identifier: AGPL-3.0-or-later
-->
# Task 0.10 — GitHub Pages `.json.gz` `Content-Type` / `Content-Encoding` Behavior

## Probe Commands Executed
All static sites hosted on `*.github.io` share GitHub Pages' global Fastly/Varnish CDN and file-serving configuration. We probed live GitHub Pages URLs for both `.json.gz` (`https://metacpan.github.io/metacpan-river/river.json.gz`, plus secondary verification on `Packages.gz` and `.js.gz`) and plain `.json` (`https://vega.github.io/vega-datasets/data/cars.json`) with and without `Accept-Encoding: gzip`:

```bash
curl -sS -D - -H "Accept-Encoding: gzip, deflate, br" https://metacpan.github.io/metacpan-river/river.json.gz -o /tmp/river_body.json.gz
curl -sS -D - https://metacpan.github.io/metacpan-river/river.json.gz -o /dev/null
curl -sS -D - -H "Accept-Encoding: gzip" https://vega.github.io/vega-datasets/data/cars.json -o /tmp/cars_gz.bin
curl -sS -D - https://vega.github.io/vega-datasets/data/cars.json -o /tmp/cars_raw.json
```

## Verbatim HTTP Responses

### 1. `.json.gz` With `Accept-Encoding: gzip, deflate, br`
```http
HTTP/2 200 
server: GitHub.com
content-type: application/gzip
last-modified: Sun, 15 Nov 2026 03:34:35 GMT
access-control-allow-origin: *
etag: "6af9284b-57011"
expires: Sun, 15 Nov 2026 05:50:47 GMT
cache-control: max-age=600
x-proxy-cache: MISS
x-github-request-id: FF30:148882:55C2222:56445C4:6AF945DF
x-github-edge-region: fra
accept-ranges: bytes
date: Sun, 15 Nov 2026 05:41:01 GMT
via: 1.1 varnish
age: 14
x-served-by: cache-sin-wsss1830021-SIN
x-cache: HIT
x-cache-hits: 1
x-timer: S1794721262.687429,VS0,VE3
vary: Accept-Encoding
x-fastly-request-id: 4a608c6cbf47ac85b8336adad23bdc1e2912718b
content-length: 356369
```
Body verification (`file` & `gunzip -c`):
```text
/tmp/river_body.json.gz: gzip compressed data, was "river.json", last modified: Sun Nov 15 03:34:22 2026, from Unix, original size modulo 2^32 3819881
356369 /tmp/river_body.json.gz
```
Magic bytes (`xxd`): `1f8b 0808 3e28 f96a 0003 7269 7665 722e` (raw gzip binary stream).

### 2. `.json.gz` Without `Accept-Encoding`
```http
HTTP/2 200 
server: GitHub.com
content-type: application/gzip
last-modified: Sun, 15 Nov 2026 03:34:35 GMT
access-control-allow-origin: *
etag: "6af9284b-57011"
expires: Sun, 15 Nov 2026 05:50:53 GMT
cache-control: max-age=600
...
content-length: 356369
```
Identical `content-type: application/gzip`, **no `content-encoding` header**, and identical `content-length: 356369`.

### 3. Plain `.json` With `Accept-Encoding: gzip`
```http
HTTP/2 200 
server: GitHub.com
content-type: application/json; charset=utf-8
last-modified: Sun, 25 Oct 2026 14:58:17 GMT
access-control-allow-origin: *
etag: W/"6ade1909-1888c"
expires: Sun, 15 Nov 2026 05:49:49 GMT
cache-control: max-age=600
content-encoding: gzip
...
content-length: 9311
```
Body verification: `9311` bytes on wire (vs `100492` bytes uncompressed without `Accept-Encoding`).

### 4. Plain `.json` Without `Accept-Encoding`
```http
HTTP/2 200 
server: GitHub.com
content-type: application/json; charset=utf-8
access-control-allow-origin: *
cache-control: max-age=600
content-length: 100492
```

## Conclusions for `SiteDataSource` (`§8.5`)
1. **`.json.gz` is served verbatim without `Content-Encoding: gzip`**: GitHub Pages returns `content-type: application/gzip` and omits `content-encoding`. Consequently, browsers (`fetch` / `package:http`) do **not** automatically gunzip `.json.gz` payloads — the response `response.bodyBytes` contains raw `1f 8b` gzip bytes. `SiteDataSource` MUST detect `.json.gz` and decompress client-side via `DecompressionStream('gzip')` in browsers (`dart:io` `gzip.decode` in VM unit tests).
2. **`.json` is transparently compressed over the wire**: When the browser requests `manifest.json`, `whitelist.json`, `latest.json`, or `days/*.json`, GitHub Pages automatically sends `content-encoding: gzip` and shrinks wire transfer by ~10× (`100,492 B → 9,311 B`), while the browser transparently passes decoded UTF-8 JSON to Dart.
3. **`Cache-Control: max-age=600` (10 minutes)**: Confirmed across all responses. Because `fast` updates every 10 minutes, cache-busting query params (`?t=<epochMs>` on `manifest.json` and `?v=<manifest.generatedAt>` / `?v=<manifest.dailyLastOk>` on data files, with **zero** custom request headers to preserve simple CORS without preflight) are required as specified in `§8.5`.
4. **CORS**: `access-control-allow-origin: *` is set by default on GitHub Pages responses.
