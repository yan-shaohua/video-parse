# File: parse-video-py/parser/douyin.py
# Purpose: 解析处理模块
import json
import os
import re
import random
import secrets
import string
import pprint
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .base import BaseParser, ImgInfo, VideoAuthor, VideoInfo
from .douyin_xbogus import XBogus


class DouYin(BaseParser):
    """
    抖音 / 抖音火山版
    """

    async def parse_share_url(self, share_url: str) -> VideoInfo:
        # 解析URL获取域名
        parsed_url = urlparse(share_url)
        host = parsed_url.netloc
        is_note_hint = False
        redirect_url = ""

        if host in ["www.iesdouyin.com", "www.douyin.com"]:
            # 支持电脑网页端链接
            video_id = self._parse_video_id_from_path(share_url)
            if not video_id:
                raise ValueError("Failed to parse video ID from PC share URL")
            is_note_hint = self._is_slides_or_note_url(share_url)
            share_url = self._get_request_url_by_video_id(video_id)
        elif host == "v.douyin.com":
            # 支持app分享链接 https://v.douyin.com/xxxxxx
            video_id, redirect_url = await self._parse_app_share_url(share_url)
            if not video_id:
                raise ValueError("Failed to parse video ID from app share URL")
            is_note_hint = self._is_slides_or_note_url(redirect_url)
            share_url = redirect_url or self._get_request_url_by_video_id(video_id)
        else:
            raise ValueError(f"Douyin not support this host: {host}")

        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(share_url, headers=self._get_douyin_headers())
            response.raise_for_status()

        # 检查是否是图集内容
        is_note = is_note_hint or self._is_note_content(response.text, share_url)
        should_try_slides = is_note or self._should_try_slides_info(
            share_url, redirect_url, response.text
        )

        json_data = None
        detail_data = await self._get_aweme_detail(video_id, suppress_error=True)
        if detail_data:
            json_data = {"aweme_details": [detail_data]}
        elif should_try_slides:
            # 图集 / 实况内容优先尝试 slidesinfo 接口
            json_data = await self._get_slides_info(video_id)

        if not json_data:
            # 如果专用API失败或者不是图集，使用标准解析方式
            pattern = re.compile(
                pattern=r"window\._ROUTER_DATA\s*=\s*(.*?)</script>",
                flags=re.DOTALL,
            )
            find_res = pattern.search(response.text)

            if not find_res or not find_res.group(1):
                raise ValueError("parse video json info from html fail")

            json_data = json.loads(find_res.group(1).strip())

        # 处理不同的数据结构
        data = None
        if isinstance(json_data, dict) and "aweme_details" in json_data:
            # 专用API返回的数据结构
            if len(json_data["aweme_details"]) > 0:
                data = json_data["aweme_details"][0]
        elif isinstance(json_data, dict) and "loaderData" in json_data:
            # 标准HTML解析返回的数据结构
            original_video_info = self._extract_video_info_res(json_data["loaderData"])
            if not original_video_info:
                raise Exception(
                    "failed to parse Videos or Photo Gallery info from json"
                )

            # 如果没有视频信息，获取并抛出异常
            if len(original_video_info["item_list"]) == 0:
                err_detail_msg = "failed to parse video info from HTML"
                if len(filter_list := original_video_info["filter_list"]) > 0:
                    err_detail_msg = filter_list[0]["detail_msg"]
                raise Exception(err_detail_msg)

            data = original_video_info["item_list"][0]
        else:
            raise Exception("Unknown data structure")

        if not data:
            raise Exception("Failed to extract data from response")

        # 获取图集图片地址
        images = []
        # 如果data含有 images，并且 images 是一个列表
        if "images" in data and isinstance(data["images"], list):
            # 获取每个图片的url_list中的第一个元素，优先获取非 .webp 格式的图片 url
            for img in data["images"]:
                image_candidates = []
                if (
                    "url_list" in img
                    and isinstance(img["url_list"], list)
                    and len(img["url_list"]) > 0
                ):
                    image_candidates.extend(img["url_list"])
                if "download_url_list" in img and isinstance(
                    img["download_url_list"], list
                ):
                    image_candidates.extend(img["download_url_list"])
                if image_candidates:
                    image_url = self._get_best_image_url(image_candidates)
                    if image_url:
                        live_photo_url = ""
                        if self._should_debug_live_photo():
                            self._debug_live_photo_candidate(img)
                        if (
                            "video" in img
                            and "play_addr" in img["video"]
                            and "url_list" in img["video"]["play_addr"]
                        ):
                            live_photo_url = (
                                img["video"]["play_addr"]["url_list"][0]
                                if img["video"]["play_addr"]["url_list"]
                                else ""
                            )
                        images.append(
                            ImgInfo(url=image_url, live_photo_url=live_photo_url)
                        )

        # 获取视频和音频播放地址
        video_url = ""
        music_url = ""
        if "video" in data and "play_addr" in data["video"]:
            if "url_list" in data["video"]["play_addr"]:
                video_url = data["video"]["play_addr"]["url_list"][0].replace(
                    "playwm", "play"
                )
            music_url = data["video"]["play_addr"].get("uri", "")

        # 如果图集地址不为空时，因为没有视频，上面抖音返回的视频地址无法访问，置空处理
        if len(images) > 0:
            video_url = ""
        else:
            # 图集时, video.play_addr.uri 是音频地址; 视频时不是
            music_url = ""

        # 获取重定向后的mp4视频地址
        # 图集时，视频地址为空，不处理
        video_mp4_url = ""
        if len(video_url) > 0:
            video_mp4_url = await self.get_video_redirect_url(video_url)

        # 获取封面图片，优先获取非 .webp 格式的图片 url
        cover_url = ""
        if (
            "video" in data
            and "cover" in data["video"]
            and "url_list" in data["video"]["cover"]
        ):
            cover_url = self._get_no_webp_url(data["video"]["cover"]["url_list"])

        video_info = VideoInfo(
            video_url=video_mp4_url,
            cover_url=cover_url,
            music_url=music_url,
            title=data.get("desc", ""),
            images=images,
            author=VideoAuthor(
                uid=data.get("author", {}).get("sec_uid", ""),
                name=data.get("author", {}).get("nickname", ""),
                avatar=(
                    data.get("author", {})
                    .get("avatar_thumb", {})
                    .get("url_list", [""])[0]
                    if data.get("author", {}).get("avatar_thumb", {}).get("url_list")
                    else ""
                ),
            ),
        )
        return video_info

    async def _get_aweme_detail(
        self, video_id: str, suppress_error: bool = False
    ) -> dict:
        if not video_id:
            return None

        aid_candidates = ("6383", "1128")
        async with httpx.AsyncClient() as client:
            for aid in aid_candidates:
                url = self._build_signed_aweme_detail_url(video_id, aid)
                if not url:
                    continue

                try:
                    data = await self._get_json_safe(client, url)
                except Exception:
                    if suppress_error:
                        continue
                    raise

                if not isinstance(data, dict):
                    continue

                detail = data.get("aweme_detail")
                if isinstance(detail, dict) and detail:
                    return detail

                filter_info = data.get("filter_detail")
                if isinstance(filter_info, dict) and filter_info.get("filter_reason"):
                    continue

        return None

    async def get_video_redirect_url(self, video_url: str) -> str:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.get(video_url, headers=self._get_douyin_headers())
        # 返回重定向后的地址，如果没有重定向则返回原地址(抖音中的西瓜视频,重定向地址为空)
        return response.headers.get("location") or video_url

    async def parse_video_id(self, video_id: str) -> VideoInfo:
        req_url = self._get_request_url_by_video_id(video_id)
        return await self.parse_share_url(req_url)

    def _get_request_url_by_video_id(self, video_id) -> str:
        return f"https://www.iesdouyin.com/share/video/{video_id}/"

    def _build_signed_aweme_detail_url(self, video_id: str, aid: str) -> str:
        params = self._build_aweme_detail_query(video_id, aid)
        base_url = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
        query = urlencode(params)
        signer = XBogus(self._get_pc_user_agent())
        signed_url, _ua = signer.build(f"{base_url}?{query}")
        return signed_url

    def _build_aweme_detail_query(self, video_id: str, aid: str) -> dict:
        return {
            "device_platform": "webapp",
            "aid": aid,
            "channel": "channel_pc_web",
            "update_version_code": "170400",
            "pc_client_type": "1",
            "version_code": "290100",
            "version_name": "29.1.0",
            "cookie_enabled": "true",
            "screen_width": "1920",
            "screen_height": "1080",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "130.0.0.0",
            "browser_online": "true",
            "engine_name": "Blink",
            "engine_version": "130.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "cpu_core_num": "12",
            "device_memory": "8",
            "platform": "PC",
            "downlink": "10",
            "effective_type": "4g",
            "round_trip_time": "100",
            "msToken": self._get_ms_token(),
            "aweme_id": video_id,
        }

    async def _parse_app_share_url(self, share_url: str) -> tuple[str, str]:
        """解析app分享链接 https://v.douyin.com/xxxxxx"""
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.get(share_url, headers=self._get_douyin_headers())

        location = response.headers.get("location")
        if not location:
            return "", ""

        # 检查是否是西瓜视频链接
        if "ixigua.com" in location:
            # 如果是西瓜视频，这里应该返回特殊处理，暂时返回空
            # 在实际应用中可能需要调用西瓜视频解析器
            return "", location

        return self._parse_video_id_from_path(location), location

    def _parse_video_id_from_path(self, url_path: str) -> str:
        """从URL路径中解析视频ID"""
        if not url_path:
            return ""

        try:
            parsed_url = urlparse(url_path)

            # 判断网页精选页面的视频
            # https://www.douyin.com/jingxuan?modal_id=7555093909760789812
            query_params = parse_qs(parsed_url.query)
            if "modal_id" in query_params:
                return query_params["modal_id"][0]

            # 判断其他页面的视频
            # https://www.iesdouyin.com/share/video/7424432820954598707/?region=CN&mid=7424432976273869622&u_code=0
            # https://www.douyin.com/video/xxxxxx
            path = parsed_url.path.strip("/")
            if path:
                path_parts = path.split("/")
                if len(path_parts) > 0:
                    return path_parts[-1]
        except Exception:
            pass

        return ""

    def _get_no_webp_url(self, url_list: list) -> str:
        """优先获取非 .webp 格式的图片 url"""
        if not url_list:
            return ""

        preferred_exts = (".jpeg", ".jpg", ".png")
        for url in url_list:
            if not url:
                continue
            path = urlparse(url).path.lower()
            if path.endswith(preferred_exts):
                return url

        # 其次获取非 .webp 格式的图片 url
        for url in url_list:
            if not url:
                continue
            path = urlparse(url).path.lower()
            if not path.endswith(".webp"):
                return url

        # 如果没找到，使用第一项
        return url_list[0] if url_list and url_list[0] else ""

    def _get_best_image_url(self, url_list: list) -> str:
        """
        优先返回无水印图片URL，再进行格式优选
        """
        if not url_list:
            return ""

        no_watermark_list = [
            item
            for item in url_list
            if item and not self._is_watermark_image_url(item)
        ]
        if no_watermark_list:
            return self._get_no_webp_url(no_watermark_list)

        return self._get_no_webp_url(url_list)

    def _is_watermark_image_url(self, url: str) -> bool:
        if not url:
            return False
        lower_url = url.lower()
        watermark_markers = (
            "tplv-dy-water",
            "watermark",
        )
        return any(marker in lower_url for marker in watermark_markers)

    def _is_note_content(self, html_content: str, share_url: str) -> bool:
        """检查是否是图集内容"""
        try:
            # 方法1: 检查canonical URL是否包含/note/
            pattern = re.compile(
                r'<link[^>]*rel=["\']canonical["\'][^>]*href=["\']([^' r'"\']+)["\']',
                re.IGNORECASE,
            )
            match = pattern.search(html_content)
            if match:
                canonical_url = match.group(1)
                if (
                    "/note/" in canonical_url
                    or "/share/slides/" in canonical_url
                    or self._has_modal_id(canonical_url)
                ):
                    return True

            # 方法2: 检查URL路径是否包含note相关路径
            parsed_url = urlparse(share_url)
            if "/note/" in parsed_url.path:
                return True

            # 方法2.1: 检查是否是图集链接 share/slides
            if "/share/slides/" in parsed_url.path:
                return True

            # 方法2.2: 带 modal_id 的页面也可能是图集 / 实况内容
            if self._has_modal_id(share_url):
                return True

            # 方法3: 检查HTML中是否有图集相关的标识
            if (
                "note_" in html_content
                or "图文" in html_content
                or "live_photo" in html_content
                or "livePhoto" in html_content
                or "slides" in html_content
            ):
                return True

        except Exception:
            pass

        return False

    def _should_try_slides_info(
        self, share_url: str, redirect_url: str, html_content: str
    ) -> bool:
        candidates = [share_url, redirect_url]
        for url in candidates:
            if self._is_slides_or_note_url(url) or self._has_modal_id(url):
                return True

        html_markers = ["live_photo", "livePhoto", "slides", "note_"]
        return any(marker in html_content for marker in html_markers)

    async def _get_slides_info(self, video_id: str) -> dict:
        """获取图集的详细信息，包括Live Photo"""
        try:
            async with httpx.AsyncClient() as client:
                # 优先尝试不带 a_bogus 的请求（更稳定）
                basic_url = (
                    f"https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
                    f"?reflow_source=reflow_page"
                    f"&web_id={video_id}"
                    f"&device_id={video_id}"
                    f"&from_did="
                    f"&user_cip="
                    f"&aweme_ids=%5B{video_id}%5D"
                    f"&request_source=200"
                )
                data = await self._get_json_safe(client, basic_url)
                if data and data.get("aweme_details"):
                    return data

                # 生成web_id和a_bogus参数作为兜底
                web_id = "75" + self._generate_fixed_length_numeric_id(15)
                a_bogus = self._rand_seq(64)

                api_url = (
                    f"https://www.iesdouyin.com/web/api/v2/aweme/slidesinfo/"
                    f"?reflow_source=reflow_page"
                    f"&web_id={web_id}"
                    f"&device_id={web_id}"
                    f"&aweme_ids=%5B{video_id}%5D"
                    f"&request_source=200"
                    f"&a_bogus={a_bogus}"
                )
                data = await self._get_json_safe(client, api_url)
                return data if data and data.get("aweme_details") else None

        except Exception:
            return None

    def _generate_fixed_length_numeric_id(self, length: int) -> str:
        """生成固定位数的随机数字ID"""
        return "".join(secrets.choice(string.digits) for _ in range(length))

    def _rand_seq(self, n: int) -> str:
        """生成随机字符串"""
        chars = string.ascii_letters + string.digits
        return "".join(secrets.choice(chars) for _ in range(n))

    def _is_slides_or_note_url(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed_url = urlparse(url)
            if "/share/slides/" in parsed_url.path or "/note/" in parsed_url.path:
                return True
            query_params = parse_qs(parsed_url.query)
            if query_params.get("is_slides", [""])[0] == "1":
                return True
        except Exception:
            return False
        return False

    def _has_modal_id(self, url: str) -> bool:
        if not url:
            return False
        try:
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            return bool(query_params.get("modal_id", [""])[0])
        except Exception:
            return False

    def _extract_video_info_res(self, loader_data: dict) -> dict:
        if not isinstance(loader_data, dict):
            return None

        preferred_keys = ["video_(id)/page", "note_(id)/page", "slides_(id)/page"]
        for key in preferred_keys:
            page_data = loader_data.get(key)
            if (
                isinstance(page_data, dict)
                and isinstance(page_data.get("videoInfoRes"), dict)
            ):
                return page_data["videoInfoRes"]

        for _, page_data in loader_data.items():
            if not isinstance(page_data, dict):
                continue
            video_info_res = page_data.get("videoInfoRes")
            if isinstance(video_info_res, dict) and (
                "item_list" in video_info_res or "filter_list" in video_info_res
            ):
                return video_info_res

        return None

    def _get_pc_user_agent(self) -> str:
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/130.0.0.0 Safari/537.36"
        )

    def _get_ms_token(self) -> str:
        cookie_str = os.getenv("PARSE_VIDEO_DOUYIN_COOKIE", "")
        if cookie_str:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_str)
                morsel = cookie.get("msToken")
                if morsel and morsel.value:
                    return morsel.value.strip()
            except Exception:
                pass

        return self._generate_false_ms_token()

    def _generate_false_ms_token(self) -> str:
        return "".join(
            random.choice(string.ascii_letters + string.digits) for _ in range(182)
        ) + "=="

    def _should_debug_live_photo(self) -> bool:
        flag = os.getenv("PARSE_VIDEO_DEBUG_DOUYIN_LIVE_PHOTO", "")
        return flag.lower() in {"1", "true", "yes", "on"}

    def _debug_live_photo_candidate(self, img: dict) -> None:
        try:
            print("[douyin-live-photo-debug] image keys:", sorted(img.keys()))
            candidate_keys = [
                key
                for key in img.keys()
                if any(
                    marker in key.lower()
                    for marker in ["video", "live", "motion", "dynamic", "photo"]
                )
            ]
            print("[douyin-live-photo-debug] candidate keys:", candidate_keys)
            candidate_payload = {key: img.get(key) for key in candidate_keys}
            if candidate_payload:
                print(
                    "[douyin-live-photo-debug] candidate payload:",
                    pprint.pformat(candidate_payload, width=120),
                )
        except Exception:
            pass

    def _get_douyin_headers(self) -> dict:
        headers = self.get_default_headers()
        headers.setdefault("Referer", "https://www.iesdouyin.com/")
        headers.setdefault("Accept", "application/json, text/plain, */*")
        cookie = os.getenv("PARSE_VIDEO_DOUYIN_COOKIE")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _get_json_safe(self, client: httpx.AsyncClient, url: str) -> dict:
        headers = self._get_douyin_headers()
        if "aweme/v1/web/aweme/detail/" in url:
            headers["User-Agent"] = self._get_pc_user_agent()
            headers["Referer"] = "https://www.douyin.com/"
            headers["Accept"] = "application/json"
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return None
