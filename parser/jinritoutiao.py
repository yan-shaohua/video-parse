# File: parse-video-py/parser/jinritoutiao.py
# Purpose: 今日头条 解析
import json
import re
import urllib.parse
from typing import Any, Dict

import httpx

from .base import BaseParser, VideoAuthor, VideoInfo


class JinRiTouTiao(BaseParser):
    """
    今日头条
    """

    async def parse_share_url(self, share_url: str) -> VideoInfo:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # 先访问首页获取必要的cookie
            await client.get("https://www.toutiao.com/", headers=self.get_default_headers())
            response = await client.get(share_url, headers=self.get_default_headers())
            response.raise_for_status()

        render_data = self._extract_render_data(response.text)
        if not render_data:
            raise ValueError("parse video json info from html fail")

        initial_video = render_data.get("data", {}).get("initialVideo", {})
        if not initial_video:
            raise ValueError("parse video info from html fail")

        title = initial_video.get("title", "")
        author = VideoAuthor(
            uid=str(initial_video.get("userInfo", {}).get("userId", "")),
            name=initial_video.get("userInfo", {}).get("name", "") or "",
            avatar=initial_video.get("userInfo", {}).get("avatarUrl", "") or "",
        )
        cover_url = initial_video.get("coverUrl", "") or ""
        video_url = self._extract_video_url(initial_video)

        return VideoInfo(
            video_url=video_url or "",
            cover_url=cover_url or "",
            title=title or "",
            author=author,
        )

    async def parse_video_id(self, video_id: str) -> VideoInfo:
        raise NotImplementedError("今日头条暂不支持直接解析视频ID")

    def _extract_render_data(self, html_text: str) -> Dict[str, Any]:
        match = re.search(
            r'id="RENDER_DATA" type="application/json">(.*?)</script>',
            html_text,
            flags=re.DOTALL,
        )
        if not match:
            return {}
        data = match.group(1)
        data = urllib.parse.unquote(data)
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return {}

    def _extract_video_url(self, initial_video: Dict[str, Any]) -> str:
        video_list = (
            initial_video.get("videoPlayInfo", {})
            .get("video_list", {})
        )
        if isinstance(video_list, dict):
            for value in video_list.values():
                if isinstance(value, dict):
                    return value.get("main_url", "") or ""
        return ""
