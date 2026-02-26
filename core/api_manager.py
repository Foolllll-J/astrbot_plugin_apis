
import ast
import copy
import json
import os
import re
import shlex
from urllib.parse import urlparse

from astrbot.api import logger


class APIManager:
    """API管理器"""

    ALLOWED_TYPES = ["text", "image", "video", "audio"]  # 支持的 API 类型常量

    def __init__(self, system_api_file, user_api_file, enable_fuzzy_match=False):
        self.system_api_file = system_api_file
        self.user_api_file = user_api_file
        self.enable_fuzzy_match = enable_fuzzy_match
        self.system_apis = {}
        self.user_apis = {}
        self.load_data()
        self.default_api_type = "image"

    def load_data(self):
        """从 JSON 文件加载数据 (系统 + 用户)"""
        self.apis = {}
        self.system_apis = {}

        # 系统 API
        if os.path.exists(self.system_api_file):
            with open(self.system_api_file, encoding="utf-8") as file:
                try:
                    self.system_apis.update(json.load(file))
                    self.apis.update(self.system_apis)
                    logger.info(f"已加载{len(self.system_apis.keys())}个系统 API")
                except json.JSONDecodeError:
                    logger.warning(f"{self.system_api_file} 格式错误，已跳过。")
        else:
            self._save_data(target="system")

        # 用户 API
        if os.path.exists(self.user_api_file):
            with open(self.user_api_file, encoding="utf-8") as file:
                try:
                    user_data = json.load(file)              # 只 load 一次
                    self.user_apis.update(user_data)
                    self.apis.update(user_data)
                    logger.info(f"已加载{len(self.user_apis)}个用户 API")
                except json.JSONDecodeError:
                    logger.warning(f"{self.user_api_file} 格式错误，已跳过。")
        else:
            self._save_data(target = "user")

    def _save_data(self, target: str = "user"):
        """保存 API 数据"""
        if target == "system":
            with open(self.system_api_file, "w", encoding="utf-8") as file:
                json.dump(self.system_apis, file, ensure_ascii=False, indent=4)
        elif target == "user":
            with open(self.user_api_file, "w", encoding="utf-8") as file:
                json.dump(self.user_apis, file, ensure_ascii=False, indent=4)

    def add_api(self, api_info: dict):
        """添加一个新的API（只写入 user_file）"""
        name = api_info["keyword"][0]
        self.apis[name] = api_info
        self.user_apis[name] = api_info
        self._save_data()

    def remove_api(self, name: str):
        """移除一个API"""
        if name in self.user_apis:
            del self.user_apis[name]
            if name in self.apis:
                del self.apis[name]
            self._save_data("user")
            logger.info(f"已删除用户 API '{name}'。")

        elif name in self.system_apis:
            del self.system_apis[name]
            if name in self.apis:
                del self.apis[name]
            self._save_data("system")
            logger.info(f"已删除系统 API '{name}'。")

        else:
            logger.warning(f"API '{name}' 不存在。")

    @staticmethod
    def extract_base_url(full_url: str) -> str:
        """
        剥离 URL 中的站点部分，例如：
        输入: "https://api.pearktrue.cn/api/stablediffusion/"
        输出: "https://api.pearktrue.cn"
        """
        parsed = urlparse(full_url)
        return (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed.scheme and parsed.netloc
            else full_url
        )

    def get_apis_names(self):
        """获取所有API的名称"""
        names = []
        for api in self.apis.values():
            name_field = api.get("name", [])
            if isinstance(name_field, str):
                names.append(name_field)
            elif isinstance(name_field, list):
                names.extend(name_field)
        return names

    def normalize_api_data(self, name: str) -> dict:
        """标准化 API 配置，返回深拷贝，避免被外部修改"""
        raw_api = self.apis.get(name, {})
        url = raw_api.get("url", "")
        urls = [url] if isinstance(url, str) else url
        keywords = raw_api.get("keyword", [])
        if isinstance(keywords, str):
            keywords = [keywords]

        api_type = raw_api.get("type", "")
        if api_type not in self.ALLOWED_TYPES:
            api_type = self.default_api_type

        normalized = {
            "name": name,
            "keywords": keywords,
            "urls": urls,
            "type": api_type,
            "params": raw_api.get("params", {}) or {},
            "target": raw_api.get("target", ""),
            "fuzzy": raw_api.get("fuzzy", self.enable_fuzzy_match),
        }
        return copy.deepcopy(normalized)

    def match_api_by_name(self, msg: str) -> dict | None:
        """
        通过触发词匹配API，返回 (key, 处理过的api_data)。
        """
        for key, raw_api in self.apis.items():
            keywords = raw_api.get("keyword", [])
            if isinstance(keywords, str):
                keywords = [keywords]
            # API 内部 fuzzy 优先；未配置时回退到全局开关
            fuzzy_enabled = raw_api.get("fuzzy", self.enable_fuzzy_match)

            matched = False
            # 精准匹配
            if msg in keywords:
                matched = True
            # 模糊匹配
            elif fuzzy_enabled and any(k in msg for k in keywords):
                matched = True

            if matched:
                return self.normalize_api_data(key)

        return None

    def list_api(self):
        """
        根据API字典生成分类字符串,即api列表。
        """
        # 用 ALLOWED_TYPES 初始化分类字典
        api_types = {t: [] for t in self.ALLOWED_TYPES}

        # 遍历apis字典，按type分类
        for key, value in self.apis.items():
            api_type = value.get("type", "unknown")
            if api_type in api_types:
                api_types[api_type].append(key)

        # 生成最终字符串
        result = f"----共收录了{len(self.apis)}个API----\n\n"
        for api_type in api_types:
            if api_types[api_type]:
                result += f"【{api_type}】{len(api_types[api_type])}个：\n"
                for key in api_types[api_type]:
                    result += f"{key}、"
            result += "\n\n"

        return result.strip()

    def get_detail(self, api_name: str):
        """查看api的详细信息"""
        api_info = self.apis.get(api_name)
        if not api_info:
            return "API不存在"
        # 构造参数字符串
        params = api_info.get("params", {})
        params_list = [
            f"{key}={value}" if value is not None and value != "" else key
            for key, value in params.items()
        ]
        params_str = ",".join(params_list) if params_list else "无"

        return (
            f"api匹配词：{api_info.get('keyword') or '无'}\n"
            f"api地址：{api_info.get('url') or '无'}\n"
            f"api类型：{api_info.get('type') or '无'}\n"
            f"所需参数：{params_str}\n"
            f"解析路径：{api_info.get('target') or '无'}"
        )


    @classmethod
    def from_add_input(cls, raw_input: str) -> dict:
        """
        Parse API definition from:
        1) legacy multi-line detail text (same as `api详情` output)
        2) CLI-like one line command, e.g.
           `天气 -t -u https://api.example.com -p city=beijing -g data.msg`
        """
        text = (raw_input or "").strip()
        if not text:
            raise ValueError("empty api input")

        # Legacy format compatibility.
        if "\n" in text:
            parsed = cls.from_detail_str(text)
            if parsed.get("keyword") and parsed.get("url"):
                return parsed

        tokens = shlex.split(text)
        if not tokens:
            raise ValueError("empty api input")

        name = tokens[0].strip()
        if not name:
            raise ValueError("missing api name")

        keywords: list[str] = [name]
        urls: list[str] = []
        api_type = "image"
        params: dict[str, str] = {}
        target = ""
        fuzzy: bool | None = None

        def require_value(index: int, option: str) -> str:
            if index >= len(tokens):
                raise ValueError(f"missing value for {option}")
            return tokens[index]

        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token in ("-v", "--video"):
                api_type = "video"
                i += 1
                continue
            if token in ("-i", "--image"):
                api_type = "image"
                i += 1
                continue
            if token in ("-t", "--text"):
                api_type = "text"
                i += 1
                continue
            if token in ("-a", "--audio"):
                api_type = "audio"
                i += 1
                continue
            if token in ("-u", "--url"):
                value = require_value(i + 1, token)
                urls.append(value)
                i += 2
                continue
            if token in ("-k", "--keyword"):
                value = require_value(i + 1, token)
                parsed_keywords = [k.strip() for k in value.split(",") if k.strip()]
                if not parsed_keywords:
                    raise ValueError("keyword cannot be empty")
                keywords = parsed_keywords
                i += 2
                continue
            if token in ("-p", "--param"):
                value = require_value(i + 1, token)
                if "=" in value:
                    key, val = value.split("=", 1)
                    key = key.strip()
                    if not key:
                        raise ValueError("param key cannot be empty")
                    params[key] = val.strip()
                else:
                    key = value.strip()
                    if not key:
                        raise ValueError("param key cannot be empty")
                    params[key] = ""
                i += 2
                continue
            if token in ("-g", "--target"):
                target = require_value(i + 1, token).strip()
                i += 2
                continue
            if token in ("-f", "--fuzzy"):
                value = require_value(i + 1, token).strip().lower()
                if value in ("true", "1", "yes", "on"):
                    fuzzy = True
                elif value in ("false", "0", "no", "off"):
                    fuzzy = False
                else:
                    raise ValueError(f"invalid fuzzy value: {value}")
                i += 2
                continue

            # Allow URL as positional argument.
            if token.startswith("http://") or token.startswith("https://"):
                urls.append(token)
                i += 1
                continue

            raise ValueError(f"unknown option: {token}")

        if not urls:
            raise ValueError("missing api url, use -u/--url")

        data = {
            "keyword": keywords,
            "url": urls[0] if len(urls) == 1 else urls,
            "type": api_type,
            "params": params,
            "target": target,
        }
        if fuzzy is not None:
            data["fuzzy"] = fuzzy
        return data

    @staticmethod
    def from_detail_str(detail: str) -> dict:
        """
        将 get_detail 的字符串逆向解析为 API 配置字典
        """
        api_info: dict = {}
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        ordered_values: list[str] = []

        def parse_keywords(raw: str) -> list[str]:
            if not raw or raw == "无":
                return []
            if raw.startswith("[") and raw.endswith("]"):
                try:
                    parsed = ast.literal_eval(raw)
                    if isinstance(parsed, list):
                        return [str(k).strip() for k in parsed if str(k).strip()]
                except Exception:
                    pass
            return [k.strip() for k in raw.split(",") if k.strip()]

        def parse_params(raw: str) -> dict[str, str]:
            if not raw or raw == "无":
                return {}
            params: dict[str, str] = {}
            for kv in raw.split(","):
                kv = kv.strip()
                if not kv:
                    continue
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    params[k.strip()] = v.strip()
                else:
                    params[kv] = ""
            return params

        alias_map = {
            "api匹配词": "keyword",
            "api地址": "url",
            "api类型": "type",
            "所需参数": "params",
            "解析路径": "target",
            "keyword": "keyword",
            "url": "url",
            "type": "type",
            "params": "params",
            "target": "target",
        }

        for line in lines:
            parts = re.split(r"[：:]", line, maxsplit=1)
            if len(parts) != 2:
                continue

            raw_label, value = parts[0].strip(), parts[1].strip()
            normalized_label = re.sub(r"\s+", "", raw_label).lower()
            canonical = alias_map.get(normalized_label)

            if canonical is None:
                ordered_values.append(value)
                continue
            if canonical == "keyword":
                api_info["keyword"] = parse_keywords(value)
            elif canonical == "url":
                api_info["url"] = "" if value == "无" else value
            elif canonical == "type":
                api_info["type"] = "" if value == "无" else value
            elif canonical == "params":
                api_info["params"] = parse_params(value)
            elif canonical == "target":
                api_info["target"] = "" if value == "无" else value

        # 兜底：旧异常标签按固定顺序解析（匹配词、地址、类型、参数、解析路径）
        if not api_info and len(ordered_values) >= 5:
            api_info["keyword"] = parse_keywords(ordered_values[0])
            api_info["url"] = "" if ordered_values[1] == "无" else ordered_values[1]
            api_info["type"] = "" if ordered_values[2] == "无" else ordered_values[2]
            api_info["params"] = parse_params(ordered_values[3])
            api_info["target"] = "" if ordered_values[4] == "无" else ordered_values[4]

        return api_info

