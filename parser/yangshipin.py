# File: parse-video-py/parser/yangshipin.py
# Purpose: 央视频 解析
import json
import re
import time
from typing import Any, Dict

import httpx
from Crypto.Cipher import AES

from .base import BaseParser, VideoAuthor, VideoInfo


class YangShiPin(BaseParser):
    """
    央视频
    """

    GUID = "mft9t9id_9etjjlqngy"
    PLATFORM = "4330701"

    async def parse_share_url(self, share_url: str) -> VideoInfo:
        real_url = share_url
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(real_url, headers=self.get_default_headers())
            response.raise_for_status()

            if "vid" not in real_url:
                match = re.search(r"URL='(.*?)'", response.text)
                if match:
                    real_url = match.group(1)
                    response = await client.get(real_url, headers=self.get_default_headers())
                    response.raise_for_status()

        match = re.search(r"__STATE_video__=(.*?)</script>", response.text, flags=re.DOTALL)
        if not match:
            raise ValueError("parse video json info from html fail")

        json_data = match.group(1)
        data = json.loads(json_data)

        title = self._get_by_path(data, ["payloads", "sharevideo", "title"]) or ""
        cover = self._get_by_path(data, ["payloads", "sharevideo", "cover_pic"]) or ""
        vid = self._get_by_path(data, ["payloads", "sharevideo", "vid"])
        if not vid:
            raise ValueError("parse video id from html fail")

        video_url, width, height = await self._fetch_video_info(vid)
        return VideoInfo(
            video_url=video_url or "",
            cover_url=cover or "",
            title=title or "",
            author=VideoAuthor(),
        )

    async def parse_video_id(self, video_id: str) -> VideoInfo:
        raise NotImplementedError("央视频暂不支持直接解析视频ID")

    async def _fetch_video_info(self, vid: str) -> tuple[str, int | None, int | None]:
        ckey = self._get_ckey(vid, self.GUID)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                "https://playvv.yangshipin.cn/playvinfo",
                params={
                    "guid": self.GUID,
                    "vid": vid,
                    "otype": "json",
                    "appVer": "1.43.0",
                    "encryptVer": "8.1",
                    "platform": self.PLATFORM,
                    "cKey": ckey,
                },
                headers={"Referer": "https://www.yangshipin.cn/"},
            )
            response.raise_for_status()

        text = response.text
        if text.startswith("(") and text.endswith(")"):
            text = text[1:-1]
        data = json.loads(text)
        if not data or data.get("exem") != 2:
            raise ValueError("get video info fail")

        base_url = self._get_by_path(data, ["vl", "vi", 0, "ul", "ui", 0, "url"])
        path = self._get_by_path(data, ["vl", "vi", 0, "fn"])
        vkey = self._get_by_path(data, ["vl", "vi", 0, "fvkey"])
        width = self._get_by_path(data, ["vl", "vi", 0, "vw"])
        height = self._get_by_path(data, ["vl", "vi", 0, "vh"])

        if not base_url or not path or not vkey:
            return "", None, None

        video_url = (
            f"{base_url}{path}"
            f"?sdtfrom={self.PLATFORM}&guid={self.GUID}&vkey={vkey}&platform=2"
        )
        return video_url, width, height

    def _get_ckey(self, vid: str, guid: str) -> str:
        time_str = str(int(time.time()))[:10]
        data = (
            f"|{vid}|{time_str}|mg3c3b04ba|1.43.0|{guid}|{self.PLATFORM}|"
            "https://w.yangshipin.cn/|mozilla/5.0 (windows nt |https://m.yangshipin.cn/|Mozilla|Netscape|Win32|"
        )
        o = 0
        for ch in data:
            o = ((o << 5) - o + ord(ch)) & 0xFFFFFFFF
        if o & 0x80000000:
            o = -((~o & 0xFFFFFFFF) + 1)
        qn = o
        encrypt_content = f"|{qn}{data}"

        key = bytes.fromhex("4E2918885FD98109869D14E0231A0BF4")
        iv = bytes.fromhex("16B17E519DDD0CE5B79D7A63A4DD801C")
        encrypted = self._aes_cbc_hex(encrypt_content.encode("utf-8"), key, iv)
        return "--01" + encrypted.upper()

    def _aes_cbc_hex(self, data: bytes, key: bytes, iv: bytes) -> str:
        pad_len = 16 - (len(data) % 16)
        data += bytes([pad_len]) * pad_len
        cipher = AES.new(key, AES.MODE_CBC, iv)
        return cipher.encrypt(data).hex()

    def _get_by_path(self, data: Dict[str, Any], path: list) -> Any:
        cur = data
        for key in path:
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            elif isinstance(cur, list) and isinstance(key, int) and len(cur) > key:
                cur = cur[key]
            else:
                return None
        return cur
