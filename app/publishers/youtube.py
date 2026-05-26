import os
import json
import logging
import subprocess

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

from app.publishers.lock import PUBLISH_LOCK

logger = logging.getLogger(__name__)

TOKEN_FILE = os.environ.get("YOUTUBE_TOKEN_FILE", "/app/data/sessions/youtube_oauth.json")


def _load_credentials() -> Credentials:
    with open(TOKEN_FILE) as f:
        data = json.load(f)
    creds = Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(Request())
    return creds


def _transcode_for_youtube(src: str) -> str:
    """Re-encode to H.264/AAC so YouTube can process it reliably."""
    if src.endswith("_yt.mp4"):
        return src
    dst = src.rsplit(".", 1)[0] + "_yt.mp4"
    if os.path.exists(dst):
        return dst
    result = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-threads", "2",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.0",
        "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-movflags", "+faststart",
        dst,
    ], capture_output=True, text=True, timeout=300)
    if result.returncode == 0 and os.path.exists(dst):
        logger.info(f"YouTube: transcoded to {dst}")
        return dst
    logger.warning(f"YouTube: ffmpeg failed (rc={result.returncode}), using original. stderr: {result.stderr[-300:]}")
    return src


async def post_video(video_path: str, title: str, description: str) -> bool:
    if not os.path.exists(TOKEN_FILE):
        logger.error(f"YouTube: token file not found: {TOKEN_FILE}")
        return False

    async with PUBLISH_LOCK:
        video_path = _transcode_for_youtube(video_path)

    try:
        creds = _load_credentials()
    except Exception as e:
        logger.error(f"YouTube: failed to load credentials: {e}")
        return False

    youtube = build("youtube", "v3", credentials=creds, cache_discovery=False)

    body = {
        "snippet": {
            "title": title[:100],
            "description": description or "",
            "categoryId": "22",
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=4 * 1024 * 1024,
    )

    try:
        request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info(f"YouTube: uploading... {pct}%")

        video_id = response.get("id", "?")
        logger.info(f"YouTube: uploaded successfully - https://youtu.be/{video_id}")
        return True

    except HttpError as e:
        logger.error(f"YouTube API error: {e.status_code} {e.reason}")
        return False
    except Exception as e:
        logger.error(f"YouTube: upload failed: {e}")
        return False


async def login_youtube(username: str, password: str) -> dict:
    return {"ok": False, "error": "YouTube login via browser is disabled. Use OAuth token file."}
