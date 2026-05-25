"""
Global publish lock — serialises all publishing operations across all platforms.
Prevents concurrent Chrome / ffmpeg instances (Facebook + YouTube running at once)
which caused 99% CPU spikes on low-resource VPS.
"""
import asyncio

# One at a time: Facebook, YouTube, Instagram — no concurrent publishing.
PUBLISH_LOCK = asyncio.Lock()
