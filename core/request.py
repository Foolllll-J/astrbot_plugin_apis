import asyncio
import json
import re
import traceback
from collections import defaultdict

import aiohttp
from bs4 import BeautifulSoup

from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig

from .api_manager import APIManager
from .utils import dict_to_string, extract_urls, get_nested_value, parse_api_keys


class RequestManager:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
    }

    def __init__(self, config: AstrBotConfig, api_manager: APIManager) -> None:
        self.session = aiohttp.ClientSession()
        self.api_key_dict = parse_api_keys(config.get("api_keys", []).copy())
        self.api_sites = list(self.api_key_dict.keys())
        self.api = api_manager

    @staticmethod
    def _normalize_text_payload(text: str) -> str:
        """归一化文本：转义换行转真实换行，并清理每行末尾重复中文句号。"""
        normalized = (
            text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
        )
        lines = normalized.split("\n")
        cleaned_lines = []
        for line in lines:
            stripped = line.rstrip()
            tail_spaces = line[len(stripped) :]
            stripped = re.sub(r"。{2,}$", "", stripped)
            cleaned_lines.append(stripped + tail_spaces)
        return "\n".join(cleaned_lines)

    async def request(
        self, urls: list[str], params: dict | None = None, test_mode: bool = False
    ) -> bytes | str | dict | None:
        last_exc = None
        for url in urls:
            try:
                request_headers = self.headers.copy()
                request_params = dict(params) if params is not None else {}

                base_url = self.api.extract_base_url(url)
                if base_url in self.api_key_dict:
                    api_key = self.api_key_dict[base_url]
                    if "ckey" not in request_headers:
                        request_headers["ckey"] = api_key
                    if "ckey" not in request_params:
                        request_params["ckey"] = api_key

                logger.debug(f"[API request] url={url} test_mode={test_mode} params={request_params}")

                async with self.session.get(
                    url=url, headers=request_headers, params=request_params or None, timeout=30
                ) as resp:
                    resp.raise_for_status()
                    ct = resp.headers.get("Content-Type", "").lower()
                    logger.debug(
                        "[API response] url=%s status=%s content_type=%s",
                        url,
                        resp.status,
                        ct or "unknown",
                    )
                    if test_mode:
                        return
                    if "application/json" in ct:
                        text = await resp.text()
                        try:
                            data = json.loads(text)
                        except Exception:
                            data = text
                        logger.debug(f"[API response body] url={url} data={data}")
                        return data
                    if "text/" in ct:
                        data = (await resp.text()).strip()
                        logger.debug(f"[API response body] url={url} text={data}")
                        return data
                    content = await resp.read()
                    logger.debug(f"[API response body] url={url} bytes_len={len(content)}")
                    return content
            except Exception as e:
                last_exc = e
                logger.error(f"请求失败 {url}: {e}\n{traceback.format_exc()}")
        if last_exc:
            raise last_exc

    async def get_data(
        self,
        urls: list[str],
        params: dict | None = None,
        api_type: str = "",
        target: str = "",
    ) -> tuple[str | None, bytes | None]:
        """对外接口，获取数据"""

        data = await self.request(urls, params)

        if isinstance(data, dict) and target:
            nested_value = get_nested_value(data, target)
            if isinstance(nested_value, dict):
                data = dict_to_string(nested_value)
            else:
                data = nested_value

        if isinstance(data, str) and api_type != "text":
            if new_urls := extract_urls(data):
                downloaded = await self.request(new_urls)
                if isinstance(downloaded, bytes):
                    data = downloaded
                else:
                    raise RuntimeError(f"下载数据失败: {new_urls}")  # 抛异常给外部

        # data为HTML字符串时，解析HTML
        if isinstance(data, str) and data.strip().startswith("<!DOCTYPE html>"):
            soup = BeautifulSoup(data, "html.parser")
            # 提取HTML中的文本内容
            data = soup.get_text(strip=True)

        if isinstance(data, str):
            data = self._normalize_text_payload(data)

        text = data if isinstance(data, str) else None
        byte = data if isinstance(data, bytes) else None

        return text, byte

    async def batch_test_apis(self) -> tuple[list[str], list[str]]:
        """
        将每个 URL 都作为独立测试项按站点分组；每轮从每个站点 pop 一个 URL 并发测试，直到所有 URL 测完。
        返回 (abled_api_list, disabled_api_list)
        """
        # 1) 展平每个 API 的所有 URL -> 按 site 分组
        site_to_entries = defaultdict(list)  # site -> list of entries
        for api_name, api_data in self.api.apis.items():
            url = api_data["url"]
            urls = [url] if isinstance(url, str) else url
            for u in urls:
                site = self.api.extract_base_url(u)
                site_to_entries[site].append(
                    {
                        "api_name": api_name,
                        "url": u,
                        "params": api_data.get("params", {}),
                    }
                )

        # 2) 记录每个 API 是否已成功（任一 URL 成功即为成功）
        api_succeeded = dict.fromkeys(self.api.apis.keys(), False)

        # 3) 按轮次从每个站点各取一个 URL 并发测试，直到所有站点的 entry 列表空
        while any(site_to_entries.values()):
            batch = []
            for site, entries in list(site_to_entries.items()):
                while entries:
                    batch.append(entries.pop(0))
                    break

            if not batch:
                break  # 没有需要测试的 entry 了

            # 并发测试这一轮的所有 URL
            tasks = [self.request([e["url"]], e["params"], True) for e in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 处理结果：任何非 Exception 的返回都视为成功
            for entry, res in zip(batch, results):
                if isinstance(res, Exception):
                    pass
                else:
                    api_succeeded[entry["api_name"]] = True

        # 4) 汇总
        abled = [k for k, v in api_succeeded.items() if v]
        disabled = [k for k, v in api_succeeded.items() if not v]
        return abled, disabled

    async def terminate(self):
        """关闭会话，断开连接"""
        await self.session.close()
