#!/usr/bin/env python3
"""Download videos from direct URLs and yt-dlp supported platforms.

The direct download and platform-download behavior is adapted from
.agent/skills/z-web-pack/scripts/collect_web_pack.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

warnings.filterwarnings(
    "ignore",
    message=r"urllib3 .* doesn't match a supported version!",
)

import requests


def discover_project_root(script_path: Path, *, cwd: Path | None = None) -> Path:
    override = os.environ.get("ZHANGAI_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    resolved_script = script_path.resolve()
    for parent in resolved_script.parents:
        if parent.name == ".agent":
            return parent.parent
        if parent.name == "z-skills":
            return parent.parent
    current = Path(cwd or Path.cwd()).expanduser()
    for candidate in (current, *current.parents):
        if (candidate / ".agent" / "skills").is_dir() or (candidate / "z-skills").is_dir():
            return candidate
    return current


PROJECT_ROOT = discover_project_root(Path(__file__))
DEFAULT_OUT_ROOT = PROJECT_ROOT / "Video" / "Downloads"
_YTDLP_ON_PATH = shutil.which("yt-dlp")
YTDLP_CANDIDATES = [
    *([Path(_YTDLP_ON_PATH)] if _YTDLP_ON_PATH else []),
    Path("/Users/zz/miniconda3/bin/yt-dlp"),
]
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)

VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".flv", ".ogv"}
STREAM_EXTENSIONS = {".m3u8", ".mpd"}

PLATFORM_HOST_HINTS = {
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "bilibili.com": "Bilibili",
    "b23.tv": "Bilibili",
    "vimeo.com": "Vimeo",
    "x.com": "X",
    "twitter.com": "Twitter",
    "tiktok.com": "TikTok",
    "douyin.com": "Douyin",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "weixin.qq.com": "WeixinChannels",
}

# WeChat Channels (视频号) online parsing service
# Credits: https://github.com/ltaoo/wx_channels_download
# Default is the public demo; override via ~/.config/z-video-downloader/config.json:
#   {"api_url": "https://<your-worker>.<subdomain>.workers.dev/api/fetch_video_profile",
#    "token": "<access credential>"}
WX_CHANNELS_PARSE_API = "https://sph.litao.workers.dev/api/fetch_video_profile"


def wx_channels_config() -> dict:
    """Read self-hosted 视频号 parse service config (api_url + bearer token)."""
    raw = os.environ.get("WX_CHANNELS_CONFIG", "~/.config/z-video-downloader/config.json")
    try:
        data = json.loads(Path(raw).expanduser().read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("api_url"):
            return {"api_url": str(data["api_url"]).rstrip("/"), "token": str(data.get("token") or "")}
    except Exception:
        pass
    return {"api_url": WX_CHANNELS_PARSE_API, "token": ""}

INVIDIOUS_INSTANCES = (
    "https://inv.thepixora.com",
)
INVIDIOUS_FALLBACK_ITAG = "18"  # 360p progressive MP4 with audio.


def slugify(text: str, fallback: str = "video-download", max_len: int = 60) -> str:
    value = re.sub(r"[^\w.-]+", "-", text, flags=re.U).strip("-._")
    value = re.sub(r"-{2,}", "-", value)
    if not value:
        value = fallback
    return value[:max_len].strip("-._") or fallback


def safe_filename(text: str, fallback: str = "video") -> str:
    value = unquote(text or "").strip()
    value = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return value or fallback


def make_run_dir(out_root: Path, title: str) -> Path:
    date = dt.date.today().isoformat()
    name = slugify(title or "video-download")
    candidate = out_root / f"{date}-{name}"
    if not candidate.exists():
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    for index in range(2, 1000):
        with_suffix = out_root / f"{date}-{name}-{index:02d}"
        if not with_suffix.exists():
            with_suffix.mkdir(parents=True, exist_ok=True)
            return with_suffix
    raise RuntimeError(f"Cannot create unique output directory under {out_root}")


def find_ytdlp() -> Path | None:
    for candidate in YTDLP_CANDIDATES:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _clean_input_url(value: str) -> str:
    url = str(value or "").strip().strip("<>")
    if re.search(r"[\s\x00-\x1f]", url):
        raise ValueError("Unsafe video URL: whitespace or control characters are not allowed")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Unsupported video URL: {url or '<empty>'}")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Unsafe video URL: embedded credentials are not allowed")
    return url


def _urls_from_text_file(path: Path) -> list[str]:
    urls: list[str] = []
    for raw_line in path.expanduser().read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        urls.append(line)
    return urls


def _urls_from_inventory(path: Path) -> list[str]:
    lines = path.expanduser().read_text(encoding="utf-8").splitlines()
    source_index = -1
    urls: list[str] = []
    for line in lines:
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if "Source URL" in cells:
            source_index = cells.index("Source URL")
            continue
        if source_index < 0 or source_index >= len(cells):
            continue
        value = cells[source_index].strip().strip("<>").strip("`")
        if value.startswith(("http://", "https://")):
            urls.append(value)
    return urls


def collect_input_urls(
    positional_urls: list[str],
    *,
    url_files: list[Path],
    inventories: list[Path],
) -> list[str]:
    candidates = list(positional_urls)
    for path in url_files:
        candidates.extend(_urls_from_text_file(Path(path)))
    for path in inventories:
        candidates.extend(_urls_from_inventory(Path(path)))

    urls: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        if not str(value or "").strip():
            continue
        url = _clean_input_url(value)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        raise ValueError("No valid video URLs were provided")
    return urls


def is_wx_channels_url(url: str) -> bool:
    """Check if URL is a WeChat Channels (视频号) share link."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    return (host == "weixin.qq.com" or host.endswith(".weixin.qq.com")) and "/sph/" in parsed.path


def download_wx_channels_video(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    max_video_mb: float,
    timeout: int = 120,
) -> dict[str, Any]:
    """Download WeChat Channels video via online parsing service.

    Uses https://sph.litao.workers.dev/ to resolve the share link into
    actual video URLs (H.264 and H.265), then downloads directly.
    Credits: https://github.com/ltaoo/wx_channels_download
    """
    record = base_record(url, "wx-channels")
    record["platform"] = "WeixinChannels"
    try:
        # Step 1: Parse the share link via parsing service (with retries —
        # workers.dev endpoints can be flaky from some networks).
        cfg = wx_channels_config()
        parse_headers = {"Content-Type": "application/json", "User-Agent": DEFAULT_USER_AGENT}
        if cfg["token"]:
            parse_headers["Authorization"] = f"Bearer {cfg['token']}"
        parse_resp = None
        last_parse_err = ""
        for attempt in range(4):
            try:
                parse_resp = session.post(
                    cfg["api_url"],
                    json={"url": url},
                    headers=parse_headers,
                    timeout=min(timeout, 30),
                )
                break
            except requests.RequestException as exc:
                last_parse_err = str(exc)[:200]
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
        if parse_resp is None:
            record["note"] = f"wx-channels-parse-retry-exhausted: {last_parse_err}"
            return record
        if parse_resp.status_code != 200:
            record["note"] = f"wx-channels-parse-http-{parse_resp.status_code}: {parse_resp.text[:200]}"
            return record

        data = parse_resp.json()
        if data.get("errCode") and data.get("errCode") != 0:
            record["note"] = f"wx-channels-parse-error: {data.get('errMsg', 'unknown')}"
            return record
        if "error" in data:
            record["note"] = f"wx-channels-parse-error: {data['error'][:200]}"
            return record

        feed_info = (data.get("data") or {}).get("feedInfo") or {}
        author_info = (data.get("data") or {}).get("authorInfo") or {}

        # Extract video URLs: prefer H.264, fallback to H.265, then default
        h264_info = feed_info.get("h264VideoInfo") or {}
        h265_info = feed_info.get("h265VideoInfo") or {}
        h264_url = h264_info.get("videoUrl", "").strip()
        h265_url = h265_info.get("videoUrl", "").strip()
        default_url = feed_info.get("videoUrl", "").strip()

        video_urls: list[tuple[str, str]] = []  # (url, label)
        if h264_url:
            video_urls.append((h264_url, "H264"))
        if h265_url and h265_url != h264_url:
            video_urls.append((h265_url, "H265"))
        if not video_urls and default_url:
            video_urls.append((default_url, "default"))

        if not video_urls:
            record["note"] = "wx-channels-no-video-url-found"
            return record

        # Build filename from description and author
        description = feed_info.get("description", "").strip()
        nickname = author_info.get("nickname", "").strip()
        title_base = safe_filename(description[:80] or nickname or "wx-channels-video")

        # Step 2: Download the video(s)
        limit = int(max_video_mb * 1024 * 1024)
        downloaded_files: list[str] = []
        total_bytes = 0

        for video_url, label in video_urls:
            filename = f"{title_base}_{label}.mp4"
            target = unique_path(out_dir / filename)
            partial = Path(str(target) + ".part")

            try:
                resp = session.get(
                    video_url,
                    headers={"User-Agent": DEFAULT_USER_AGENT, "Referer": "https://weixin.qq.com/"},
                    timeout=timeout,
                    stream=True,
                )
                resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "").lower()
                if "text/html" in content_type:
                    # Skip this version, video URL may have expired
                    continue

                length = resp.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > limit:
                            record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                            return record
                    except ValueError:
                        pass

                size = 0
                with open(partial, "wb") as handle:
                    for chunk in resp.iter_content(1024 * 64):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > limit:
                            handle.close()
                            partial.unlink(missing_ok=True)
                            record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                            return record
                        handle.write(chunk)

                if size == 0:
                    partial.unlink(missing_ok=True)
                    continue

                partial.replace(target)
                downloaded_files.append(str(target))
                total_bytes += size
            except Exception as exc:  # noqa: BLE001
                if partial.exists():
                    partial.unlink(missing_ok=True)
                # If at least one version downloaded, continue
                if not downloaded_files:
                    record["note"] = f"wx-channels-download-failed: {str(exc)[:200]}"
                continue

        if downloaded_files:
            record["status"] = "ok"
            record["files"] = downloaded_files
            record["bytes"] = total_bytes
            versions = ", ".join(label for _, label in video_urls[:len(downloaded_files)])
            record["note"] = f"via {urlparse(cfg['api_url']).hostname} ({versions})"
        else:
            if not record["note"]:
                record["note"] = "wx-channels-all-versions-failed"
        return record
    except requests.exceptions.Timeout:
        record["note"] = "wx-channels-parse-timeout"
        return record
    except Exception as exc:  # noqa: BLE001
        record["note"] = f"wx-channels-error: {str(exc)[:250]}"
        return record


def classify_url(url: str) -> str:
    parsed = urlparse(url)
    suffix = Path(parsed.path).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "direct"
    if suffix in STREAM_EXTENSIONS:
        return "stream"
    if is_wx_channels_url(url):
        return "wx-channels"
    return "platform"


def platform_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for hint, name in PLATFORM_HOST_HINTS.items():
        if host == hint or host.endswith("." + hint):
            return name
    if classify_url(url) == "direct":
        return "Direct"
    if classify_url(url) == "stream":
        return "Stream"
    return "yt-dlp"


def youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.strip("/")
    candidate = ""
    if host == "youtu.be" or host.endswith(".youtu.be"):
        candidate = path.split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif path.startswith(("shorts/", "embed/", "live/")):
            candidate = path.split("/", 1)[1].split("/", 1)[0]
    if re.fullmatch(r"[\w-]{11}", candidate or ""):
        return candidate
    return ""


def is_youtube_url(url: str) -> bool:
    return bool(youtube_video_id(url))


def fetch_invidious_title(
    session: requests.Session,
    instance: str,
    video_id: str,
    *,
    timeout: int,
) -> str:
    try:
        response = session.get(
            f"{instance.rstrip('/')}/api/v1/videos/{video_id}",
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        return safe_filename(data.get("title") or "")
    except Exception:  # noqa: BLE001
        return ""


def invidious_redirect_location(
    session: requests.Session,
    url: str,
    *,
    timeout: int,
) -> str:
    response = session.get(
        url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Range": "bytes=0-2047"},
        timeout=timeout,
        allow_redirects=False,
    )
    if response.is_redirect:
        location = response.headers.get("Location")
        if not location:
            raise RuntimeError("invidious-redirect-without-location")
        return urljoin(url, location)
    response.raise_for_status()
    return url


def resolve_invidious_proxy_url(
    session: requests.Session,
    instance: str,
    video_id: str,
    *,
    itag: str = INVIDIOUS_FALLBACK_ITAG,
    timeout: int,
) -> str:
    base = instance.rstrip("/")
    latest = f"{base}/latest_version?id={video_id}&itag={itag}&local=true"
    first = invidious_redirect_location(session, latest, timeout=timeout)
    return invidious_redirect_location(session, first, timeout=timeout)


def parse_content_range_total(value: str) -> int:
    match = re.search(r"/(\d+)$", value or "")
    if not match:
        return 0
    return int(match.group(1))


def fetch_proxy_range(
    session: requests.Session,
    proxy_url: str,
    start: int,
    end: int,
    *,
    timeout: int,
) -> tuple[requests.Response, bytes]:
    response = session.get(
        proxy_url,
        headers={"User-Agent": DEFAULT_USER_AGENT, "Range": f"bytes={start}-{end}"},
        timeout=timeout,
    )
    data = response.content
    return response, data


def download_youtube_invidious_fallback(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    max_video_mb: float,
    timeout: int,
    instances: tuple[str, ...] | list[str] | None = None,
    chunk_size: int = 1024 * 1024,
) -> dict[str, Any]:
    record = base_record(url, "platform-fallback")
    record["platform"] = "YouTube / Invidious proxy"
    video_id = youtube_video_id(url)
    if not video_id:
        record["note"] = "youtube-video-id-not-found"
        return record

    last_error = ""
    for instance in instances or INVIDIOUS_INSTANCES:
        instance = instance.rstrip("/")
        try:
            title = fetch_invidious_title(session, instance, video_id, timeout=min(timeout, 30))
            filename = f"{title or 'youtube-video'} [{video_id}]-360p.mp4"
            target = unique_path(out_dir / safe_filename(filename))
            proxy_url = resolve_invidious_proxy_url(
                session,
                instance,
                video_id,
                timeout=min(timeout, 45),
            )

            response, first_bytes = fetch_proxy_range(session, proxy_url, 0, 2047, timeout=min(timeout, 45))
            if response.status_code != 206 or not first_bytes.startswith(b"\x00\x00\x00"):
                raise RuntimeError(f"invidious-probe-failed:{response.status_code}")
            total = parse_content_range_total(response.headers.get("Content-Range", ""))
            if total <= 0:
                raise RuntimeError("content-range-total-missing")
            limit = int(max_video_mb * 1024 * 1024)
            if total and total > limit:
                record["note"] = f"invidious-video-larger-than-{max_video_mb:g}MB"
                return record

            partial = Path(str(target) + ".part")
            with open(partial, "wb") as handle:
                handle.write(first_bytes)
            written = len(first_bytes)
            start = written
            consecutive_failures = 0
            while start < total:
                end = min(start + chunk_size - 1, total - 1)
                chunk = b""
                for attempt in range(1, 8):
                    try:
                        response, chunk = fetch_proxy_range(
                            session,
                            proxy_url,
                            start,
                            end,
                            timeout=min(timeout, 45),
                        )
                        expected = end - start + 1
                        if response.status_code != 206:
                            raise RuntimeError(f"status {response.status_code}")
                        if len(chunk) != expected:
                            raise RuntimeError(f"got {len(chunk)} expected {expected}")
                        consecutive_failures = 0
                        break
                    except Exception as exc:  # noqa: BLE001
                        consecutive_failures += 1
                        last_error = str(exc)[:160]
                        if consecutive_failures >= 6:
                            proxy_url = resolve_invidious_proxy_url(
                                session,
                                instance,
                                video_id,
                                timeout=min(timeout, 45),
                            )
                            consecutive_failures = 0
                        if attempt == 7:
                            raise
                        time.sleep(min(10, attempt * 1.5))
                with open(partial, "ab") as handle:
                    handle.write(chunk)
                written += len(chunk)
                if written > limit:
                    partial.unlink(missing_ok=True)
                    record["note"] = f"invidious-video-larger-than-{max_video_mb:g}MB"
                    return record
                start = end + 1

            if partial.stat().st_size != total:
                raise RuntimeError(
                    f"invidious-size-mismatch:{partial.stat().st_size}!={total}"
                )
            partial.replace(target)

            record["status"] = "ok"
            record["files"] = [str(target)]
            record["bytes"] = target.stat().st_size
            record["note"] = f"yt-dlp failed; downloaded via {instance} local=true itag={INVIDIOUS_FALLBACK_ITAG}"
            return record
        except Exception as exc:  # noqa: BLE001
            last_error = f"{instance}: {str(exc)[:220]}"
            continue

    record["note"] = f"invidious-fallback-failed: {last_error}"
    return record


def filename_from_headers(headers: requests.structures.CaseInsensitiveDict[str]) -> str:
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.I)
    if match:
        return safe_filename(match.group(1))
    match = re.search(r'filename="?([^";]+)"?', disposition, re.I)
    if match:
        return safe_filename(match.group(1))
    return ""


def size_text(size: int | None) -> str:
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def base_record(url: str, kind: str) -> dict[str, Any]:
    return {
        "url": url,
        "kind": kind,
        "platform": platform_name(url),
        "status": "failed",
        "files": [],
        "bytes": 0,
        "note": "",
    }


def download_direct_video(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    max_video_mb: float,
    referer: str = "",
    timeout: int = 30,
) -> dict[str, Any]:
    record = base_record(url, "direct")
    partial: Path | None = None
    try:
        headers = {"User-Agent": DEFAULT_USER_AGENT}
        if referer:
            headers["Referer"] = referer
        response = session.get(url, timeout=timeout, stream=True, headers=headers)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type or "application/json" in content_type:
            record["note"] = f"unexpected-content-type:{content_type.split(';')[0]}"
            return record

        limit = int(max_video_mb * 1024 * 1024)
        length = response.headers.get("Content-Length")
        if length:
            try:
                if int(length) > limit:
                    record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                    return record
            except ValueError:
                pass

        parsed = urlparse(url)
        ext = Path(parsed.path).suffix.lower()
        if ext not in VIDEO_EXTENSIONS:
            ext = ".mp4"
        header_name = filename_from_headers(response.headers)
        fallback_stem = safe_filename(Path(parsed.path).stem or "video")
        filename = header_name or f"{fallback_stem}{ext}"
        if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
            filename = f"{Path(filename).stem}{ext}"
        target = unique_path(out_dir / filename)
        partial = Path(str(target) + ".part")

        resume_from = partial.stat().st_size if partial.exists() else 0
        mode = "wb"
        expected_total = 0
        if resume_from:
            response.close()
            range_headers = dict(headers)
            range_headers["Range"] = f"bytes={resume_from}-"
            response = session.get(
                url,
                timeout=timeout,
                stream=True,
                headers=range_headers,
            )
            response.raise_for_status()
            if response.status_code == 206:
                content_range = response.headers.get("Content-Range", "")
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                if not match or int(match.group(1)) != resume_from:
                    raise RuntimeError("invalid-content-range-for-resume")
                expected_total = int(match.group(3))
                mode = "ab"
            else:
                resume_from = 0

        length = response.headers.get("Content-Length")
        if not expected_total and length:
            try:
                expected_total = resume_from + int(length)
            except ValueError:
                expected_total = 0
        if expected_total and expected_total > limit:
            record["note"] = f"video-larger-than-{max_video_mb:g}MB"
            return record

        size = resume_from
        with open(partial, mode) as handle:
            for chunk in response.iter_content(1024 * 16):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    handle.close()
                    partial.unlink(missing_ok=True)
                    record["note"] = f"video-larger-than-{max_video_mb:g}MB"
                    return record
                handle.write(chunk)

        if expected_total and size != expected_total:
            record["note"] = f"incomplete-download:{size}!={expected_total} | partial={partial}"
            return record
        partial.replace(target)

        record["status"] = "ok"
        record["files"] = [str(target)]
        record["bytes"] = size
        return record
    except Exception as exc:  # noqa: BLE001
        partial_note = f" | partial={partial}" if partial is not None and partial.exists() else ""
        record["note"] = (str(exc) + partial_note)[:300]
        return record


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index:02d}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Cannot create unique filename for {path}")


def format_selector(quality: str) -> str:
    if quality == "best":
        return "bv*+ba/b"
    try:
        height = int(quality)
    except ValueError as exc:
        raise ValueError("--quality must be an integer height like 1080 or 'best'") from exc
    if height <= 0:
        raise ValueError("--quality height must be positive")
    return f"bv*[height<={height}]+ba/b[height<={height}]/b"


def build_ytdlp_cmd(
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path,
    quality: str,
    max_video_mb: float,
    cookies_file: str = "",
    browser_cookies: str = "",
    playlist: bool = False,
    write_info_json: bool = True,
    trim_filenames: int = 150,
    subtitles: bool = False,
    subtitle_langs: str = "zh.*,en.*",
    embed_thumbnail: bool = False,
    download_archive: str = "",
    concurrent_fragments: int = 4,
) -> list[str]:
    template = str(out_dir / "%(title).150B [%(id)s].%(ext)s")
    cmd = [
        str(ytdlp),
        "--ignore-config",
        "--continue",
        "--part",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--retry-sleep",
        "fragment:exp=1:20",
        "--concurrent-fragments",
        str(concurrent_fragments),
        "--no-progress",
        "--print",
        "after_move:filepath",
        "--merge-output-format",
        "mp4",
        "--embed-metadata",
        "--trim-filenames",
        str(trim_filenames),
        "--max-filesize",
        str(int(max_video_mb * 1024 * 1024)),
        "-f",
        format_selector(quality),
        "-o",
        template,
        url,
    ]
    if write_info_json:
        cmd.insert(1, "--write-info-json")
    if subtitles:
        cmd[1:1] = [
            "--write-subs",
            "--write-auto-subs",
            "--sub-langs",
            subtitle_langs,
            "--convert-subs",
            "srt",
        ]
    if embed_thumbnail:
        cmd.insert(1, "--embed-thumbnail")
    if download_archive:
        cmd[1:1] = ["--download-archive", str(Path(download_archive).expanduser())]
    if playlist:
        cmd.insert(1, "--yes-playlist")
    else:
        cmd.insert(1, "--no-playlist")
    if cookies_file:
        cmd[1:1] = ["--cookies", str(Path(cookies_file).expanduser())]
    elif browser_cookies:
        cmd[1:1] = ["--cookies-from-browser", browser_cookies]
    return cmd


def ytdlp_env() -> dict[str, str]:
    env = os.environ.copy()
    path_parts = [
        "/Users/zz/miniconda3/bin",
        "/opt/homebrew/bin",
        env.get("PATH", ""),
    ]
    env["PATH"] = os.pathsep.join(part for part in path_parts if part)
    return env


def likely_cookie_fix(note: str) -> bool:
    lowered = note.lower()
    markers = [
        "sign in",
        "login",
        "cookies",
        "captcha",
        "bot",
        "precondition",
        "http error 412",
        "http error 403",
        "forbidden",
        "confirm",
    ]
    return any(marker in lowered for marker in markers)


def video_files_from_paths(paths: list[str]) -> list[str]:
    result: list[str] = []
    for raw in paths:
        path = Path(raw.strip())
        if path.exists() and path.suffix.lower() in VIDEO_EXTENSIONS:
            result.append(str(path))
    return result


def download_with_ytdlp(
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path,
    quality: str,
    max_video_mb: float,
    cookies_file: str = "",
    browser_cookies: str = "",
    playlist: bool = False,
    timeout: int = 3600,
    subtitles: bool = False,
    subtitle_langs: str = "zh.*,en.*",
    embed_thumbnail: bool = False,
    download_archive: str = "",
) -> dict[str, Any]:
    kind = "stream" if classify_url(url) == "stream" else "platform"
    record = base_record(url, kind)
    cmd = build_ytdlp_cmd(
        url,
        out_dir,
        ytdlp=ytdlp,
        quality=quality,
        max_video_mb=max_video_mb,
        cookies_file=cookies_file,
        browser_cookies=browser_cookies,
        playlist=playlist,
        subtitles=subtitles,
        subtitle_langs=subtitle_langs,
        embed_thumbnail=embed_thumbnail,
        download_archive=download_archive,
    )
    try:
        before = {str(path) for path in out_dir.iterdir()}
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=ytdlp_env(),
        )
        printed_files = video_files_from_paths((proc.stdout or "").splitlines())
        after_paths = [
            str(path)
            for path in out_dir.iterdir()
            if str(path) not in before and path.suffix.lower() in VIDEO_EXTENSIONS
        ]
        files = sorted(set(printed_files + after_paths))
        if proc.returncode == 0 and files:
            record["status"] = "ok"
            record["files"] = files
            record["bytes"] = sum(Path(path).stat().st_size for path in files)
            return record

        diagnostic = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        archive_markers = (
            "has already been recorded in the archive",
            "has already been downloaded",
        )
        if proc.returncode == 0 and any(marker in diagnostic.lower() for marker in archive_markers):
            record["status"] = "skipped"
            record["note"] = "already present in download archive"
            return record
        if proc.returncode == 0 and not files and download_archive:
            record["status"] = "skipped"
            record["note"] = "no new file; treated as already present in download archive"
            return record

        tail = diagnostic.splitlines()
        record["note"] = (tail[-1] if tail else f"yt-dlp-exit-{proc.returncode}")[:300]
        if likely_cookie_fix(record["note"]) and not (cookies_file or browser_cookies):
            record["note"] += " | 可用 --cookies-file cookies.txt 重试"
        return record
    except subprocess.TimeoutExpired:
        record["note"] = f"yt-dlp-timeout-{timeout}s"
        return record
    except Exception as exc:  # noqa: BLE001
        record["note"] = str(exc)[:300]
        return record


def download_one(
    session: requests.Session,
    url: str,
    out_dir: Path,
    *,
    ytdlp: Path | None,
    quality: str,
    max_video_mb: float,
    cookies_file: str,
    browser_cookies: str,
    playlist: bool,
    prefer_ytdlp: bool,
    invidious_fallback: bool,
    timeout: int,
    subtitles: bool = False,
    subtitle_langs: str = "zh.*,en.*",
    embed_thumbnail: bool = False,
    download_archive: str = "",
) -> dict[str, Any]:
    kind = classify_url(url)
    # WeChat Channels (视频号) uses dedicated online parsing
    if kind == "wx-channels":
        return download_wx_channels_video(
            session,
            url,
            out_dir,
            max_video_mb=max_video_mb,
            timeout=min(timeout, 120),
        )
    if kind == "direct" and not prefer_ytdlp:
        record = download_direct_video(
            session,
            url,
            out_dir,
            max_video_mb=max_video_mb,
            referer=url,
            timeout=min(timeout, 120),
        )
        if record["status"] == "ok" or ytdlp is None:
            return record
    if ytdlp is None:
        record = base_record(url, "platform")
        record["note"] = "yt-dlp-not-found"
        return record
    record = download_with_ytdlp(
        url,
        out_dir,
        ytdlp=ytdlp,
        quality=quality,
        max_video_mb=max_video_mb,
        cookies_file=cookies_file,
        browser_cookies=browser_cookies,
        playlist=playlist,
        timeout=timeout,
        subtitles=subtitles,
        subtitle_langs=subtitle_langs,
        embed_thumbnail=embed_thumbnail,
        download_archive=download_archive,
    )
    if (
        record["status"] != "ok"
        and invidious_fallback
        and is_youtube_url(url)
        and likely_cookie_fix(record.get("note", ""))
        and not (cookies_file or browser_cookies)
    ):
        fallback_record = download_youtube_invidious_fallback(
            session,
            url,
            out_dir,
            max_video_mb=max_video_mb,
            timeout=timeout,
        )
        if fallback_record["status"] == "ok":
            fallback_record["note"] = (
                f"yt-dlp note: {record.get('note', '')} | {fallback_record.get('note', '')}"
            )[:300]
            return fallback_record
        record["note"] = (
            f"{record.get('note', '')} | {fallback_record.get('note', '')}"
        )[:300]
    return record


def write_reports(out_dir: Path, urls: list[str], records: list[dict[str, Any]]) -> None:
    payload = {
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "urls": urls,
        "records": records,
    }
    (out_dir / "download-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    ok_count = sum(1 for record in records if record["status"] == "ok")
    skipped_count = sum(1 for record in records if record["status"] == "skipped")
    failed_count = len(records) - ok_count - skipped_count
    lines = [
        "# 视频下载报告",
        "",
        f"- 输出目录：`{out_dir}`",
        f"- 链接数量：{len(records)}",
        f"- 成功：{ok_count}",
        f"- 已跳过：{skipped_count}",
        f"- 失败：{failed_count}",
        "",
        "| 状态 | 类型 | 平台 | 文件 | 大小 | 链接 | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        files = "<br>".join(f"`{Path(path).name}`" for path in record.get("files", []))
        lines.append(
            "| "
            + " | ".join(
                [
                    escape_table(record.get("status", "")),
                    escape_table(record.get("kind", "")),
                    escape_table(record.get("platform", "")),
                    files or "",
                    escape_table(size_text(record.get("bytes") or None)),
                    f"<{record.get('url', '')}>",
                    escape_table(record.get("note", "")),
                ]
            )
            + " |"
        )
    (out_dir / "download-report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def escape_table(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download videos from direct URLs and yt-dlp platforms.")
    parser.add_argument("urls", nargs="*", help="Video URLs")
    parser.add_argument(
        "--url-file",
        action="append",
        default=[],
        help="Text file containing one video URL per line; repeatable",
    )
    parser.add_argument(
        "--inventory",
        action="append",
        default=[],
        help="04-media-inventory.md produced by z-web-pack; repeatable",
    )
    parser.add_argument("--out-root", default=str(DEFAULT_OUT_ROOT), help="Output root directory")
    parser.add_argument("--run-dir", default="", help="Reuse an existing run directory, including .part files")
    parser.add_argument("--title", default="", help="Run title used in output folder name")
    parser.add_argument("--quality", default="1080", help="Max video height, e.g. 720/1080, or best")
    parser.add_argument("--max-video-mb", type=float, default=2000.0, help="Per video size limit")
    parser.add_argument("--cookies-file", default="", help="Read cookies from a Netscape cookies.txt file")
    parser.add_argument("--browser-cookies", default="", help="Read cookies from browser: chrome/safari/edge/firefox")
    parser.add_argument("--playlist", action="store_true", help="Allow playlist downloads")
    parser.add_argument("--prefer-ytdlp", action="store_true", help="Use yt-dlp even for direct video URLs")
    parser.add_argument("--no-invidious-fallback", action="store_true", help="Disable YouTube Invidious proxy fallback")
    parser.add_argument("--subtitles", action="store_true", help="Download manual and automatic subtitles")
    parser.add_argument("--sub-langs", default="zh.*,en.*", help="Subtitle languages passed to yt-dlp")
    parser.add_argument("--embed-thumbnail", action="store_true", help="Embed the video thumbnail when supported")
    parser.add_argument("--download-archive", default="", help="yt-dlp archive file for skipping prior downloads")
    parser.add_argument("--timeout", type=int, default=3600, help="yt-dlp timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        urls = collect_input_urls(
            args.urls,
            url_files=[Path(path).expanduser() for path in args.url_file],
            inventories=[Path(path).expanduser() for path in args.inventory],
        )
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.max_video_mb <= 0 or args.timeout <= 0:
        print("error: --max-video-mb and --timeout must be positive", file=sys.stderr)
        return 1
    try:
        format_selector(args.quality)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.cookies_file:
        cookie_path = Path(args.cookies_file).expanduser()
        if not cookie_path.is_file():
            print(f"error: cookies file not found: {cookie_path}", file=sys.stderr)
            return 1

    out_root = Path(args.out_root).expanduser()
    title = args.title or platform_name(urls[0]).lower()
    if args.run_dir:
        out_dir = Path(args.run_dir).expanduser()
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = make_run_dir(out_root, title)

    ytdlp = find_ytdlp()
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    records: list[dict[str, Any]] = []
    for index, url in enumerate(urls, start=1):
        print(f"[{index}/{len(urls)}] {url}", flush=True)
        record = download_one(
            session,
            url,
            out_dir,
            ytdlp=ytdlp,
            quality=args.quality,
            max_video_mb=args.max_video_mb,
            cookies_file=args.cookies_file,
            browser_cookies=args.browser_cookies,
            playlist=args.playlist,
            prefer_ytdlp=args.prefer_ytdlp,
            invidious_fallback=not args.no_invidious_fallback,
            timeout=args.timeout,
            subtitles=args.subtitles,
            subtitle_langs=args.sub_langs,
            embed_thumbnail=args.embed_thumbnail,
            download_archive=args.download_archive,
        )
        records.append(record)
        status = record["status"]
        note = f" ({record['note']})" if record.get("note") else ""
        print(f"  -> {status}{note}", flush=True)

    write_reports(out_dir, urls, records)
    ok_count = sum(1 for record in records if record["status"] == "ok")
    skipped_count = sum(1 for record in records if record["status"] == "skipped")
    failed_count = len(records) - ok_count - skipped_count
    print(out_dir)
    print(f"videos_ok={ok_count}")
    print(f"videos_skipped={skipped_count}")
    print(f"videos_failed={failed_count}")
    if ok_count + skipped_count == len(records):
        return 0
    if ok_count + skipped_count == 0:
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
