# File: parse-video-py/parser/wechatmp.py
# Purpose: 微信公众号 解析
import re
from urllib.parse import urlencode

import httpx

from .base import BaseParser, ImgInfo, VideoAuthor, VideoInfo


class WeChatMP(BaseParser):
    """
    微信公众号
    """

    IMAGE_LIST_PATTERN = re.compile(
        r"width: '(.*?)'[\\s\\S]*?height: '(.*?)'[\\s\\S]*?cdn_url: '(.*?)'",
        re.DOTALL,
    )
    VIDEO_LIST_PATTERN = re.compile(
        r"format_id: '(.*?)'[\\s\\S]*?height: '(.*?)'[\\s\\S]*?url: \\?\\?'(.*?)'[\\s\\S]*?width: '(.*?)'",
        re.DOTALL,
    )

    async def parse_share_url(self, share_url: str) -> VideoInfo:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(share_url, headers=self.get_default_headers())
            response.raise_for_status()

        if "var title = " not in response.text:
            raise ValueError("parse media info from html fail")

        title = self._extract_field(response.text, r"var title = '(.*?)'")
        author = VideoAuthor(
            uid="",
            name=self._extract_field(response.text, r'var author = \"(.*?)\"') or "",
            avatar=self._extract_field(response.text, r'var hd_head_img = \"(.*?)\"') or "",
        )
        cover = self._extract_field(response.text, r'var cdn_url_235_1 = \"(.*?)\";') or ""
        video_cover = self._extract_field(response.text, r"window.__mpVideoCoverUrl = '(.*?)';")
        if video_cover:
            cover = video_cover

        images = self._extract_images(response.text)
        video_url = self._extract_video_url(response.text)

        return VideoInfo(
            video_url=video_url or "",
            cover_url=cover or "",
            title=title or "",
            images=images,
            author=author,
        )

    async def parse_video_id(self, video_id: str) -> VideoInfo:
        raise NotImplementedError("微信公众号暂不支持直接解析视频ID")

    def _extract_field(self, text: str, pattern: str) -> str:
        match = re.search(pattern, text)
        return match.group(1) if match else ""

    def _extract_images(self, text: str) -> list[ImgInfo]:
        images = []
        for match in self.IMAGE_LIST_PATTERN.finditer(text):
            url = match.group(3)
            if url and url != "0":
                images.append(ImgInfo(url=url))
        return images

    def _extract_video_url(self, text: str) -> str:
        vid = self._extract_field(text, r"video_id.DATA'\\) : '(.*?)'")
        if not vid:
            return ""
        data = self._extract_field(text, r"window.__mpVideoTransInfo = ([\\s\\S]*?)window.__mpVideoTransInfo")
        if not data:
            return ""
        for match in self.VIDEO_LIST_PATTERN.finditer(data):
            format_id = match.group(1)
            url = match.group(3)
            if not url:
                continue
            url = url.replace("\\x26amp;", "&")
            query = urlencode(
                {
                    "vid": vid,
                    "format_id": format_id,
                    "support_redirect": "0",
                    "mmversion": "false",
                }
            )
            return f"{url}?{query}"
        return ""
