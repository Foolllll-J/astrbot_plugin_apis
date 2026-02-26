import os
import json
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import (
    BaseMessageComponent,
    Image,
    Plain,
    Record,
    Video,
)
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.api_manager import APIManager
from .core.local import LocalDataManager
from .core.request import RequestManager
from .core.utils import get_nickname


class APIPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 启用的 API 类型
        self.enable_api_type = [
            k[7:] for k, v in config.get("type_switch", {}).items() if v
        ]
        # 本地数据存储路径
        self.local_data_dir = StarTools.get_data_dir("astrbot_plugin_apis")
        # API 数据文件
        self.system_api_file = Path(__file__).parent / "system_api.json"
        self.user_api_file = self.local_data_dir / "user_api.json"

    async def initialize(self):
        self.local = LocalDataManager(self.local_data_dir)
        self.api = APIManager(
            self.system_api_file,
            self.user_api_file,
            enable_fuzzy_match=self.conf.get("enable_fuzzy_match", False),
        )
        self.apis_names = self.api.get_apis_names()
        self.web = RequestManager(self.conf, self.api)

    @staticmethod
    async def data_to_chain(
        api_type: str, text: str | None = "", path: str | Path | None = ""
    ) -> list[BaseMessageComponent]:
        """根据数据类型构造消息链。"""
        chain = []
        if api_type == "text" and text:
            chain = [Plain(text)]

        elif api_type == "image" and path:
            chain = [Image.fromFileSystem(str(path))]

        elif api_type == "video" and path:
            chain = [Video.fromFileSystem(str(path))]

        elif api_type == "audio" and path:
            chain = [Record.fromFileSystem(str(path))]

        return chain  # type: ignore

    async def _supplement_args(self, event: AstrMessageEvent, args: list, params: dict):
        """
        补充参数逻辑。
        :param event: 事件对象
        :param args: 当前参数列表（可能为空）
        :param params: 参数字典
        :return: 更新后的 args 和 params
        """
        # 尝试从回复消息中提取参数
        if not args:
            reply_seg = next(
                (seg for seg in event.get_messages() if isinstance(seg, Comp.Reply)),
                None,
            )
            if reply_seg and reply_seg.chain:
                for seg in reply_seg.chain:
                    if isinstance(seg, Comp.Plain):
                        args = seg.text.strip().split(" ")
                        break

        # 如果仍未获取到参数，尝试从 @ 消息中提取昵称
        if not args:
            for seg in event.get_messages():
                if isinstance(seg, Comp.At):
                    seg_qq = str(seg.qq)
                    if seg_qq != event.get_self_id():
                        nickname = await get_nickname(event, seg_qq)
                        if nickname:
                            args.append(nickname)
                            break
        # 如果仍未获取到参数，尝试使用发送者名称补参数
        if not args:
            extra_arg = event.get_sender_name()
            params = {
                key: extra_arg if not value else value for key, value in params.items()
            }

        return args, params

    @filter.command("api列表", alias={"API列表"})
    async def api_list(self, event: AstrMessageEvent, api_name: str | None = None):
        """指令：api列表 / API列表。返回当前已收录 API 的分类列表。"""
        api_info = self.api.list_api()
        yield event.plain_result(api_info)

    @filter.command("api详情", alias={"API详情"})
    async def api_detail(self, event: AstrMessageEvent, api_name: str | None = None):
        """指令：api详情 / API详情 <api名称>。查看指定 API 的详细配置。"""
        if not api_name:
            yield event.plain_result("未指定api名称")
            return
        api_detail = self.api.get_detail(api_name)
        yield event.plain_result(api_detail)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("添加api", alias={"添加API"})
    async def api_add(self, event: AstrMessageEvent):
        """指令：添加api / 添加API。管理员添加 API，支持 CLI 与旧格式文本。"""
        parts = event.message_str.split(maxsplit=1)
        api_detail = parts[1].strip() if len(parts) > 1 else ""
        try:
            data = self.api.from_add_input(api_detail)
            self.api.add_api(data)
            yield event.plain_result(f"添加api成功:\n{data}")
        except Exception as e:
            logger.error(e)
            yield event.plain_result(
                "添加api失败。支持两种格式：\n"
                "1) 旧格式：直接粘贴 `api详情` 输出。\n"
                "2) CLI格式示例：添加api 天气 -t -u https://api.example.com -p city=北京 -g data.msg\n"
                "类型快捷参数：-v(video) -i(image) -t(text) -a(audio)"
            )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除api", alias={"删除API"})
    async def remove_api(self, event: AstrMessageEvent, api_name: str):
        """指令：删除api / 删除API <api名称>。管理员删除指定 API。"""
        self.api.remove_api(api_name)
        yield event.plain_result(f"已删除api：{api_name}")

    @filter.command("api测试", alias={"API测试"})
    async def api_status(self, event: AstrMessageEvent):
        """指令：api测试 / API测试。轮询所有 API 并输出可用/失效结果。"""
        yield event.plain_result(f"正在轮询{len(self.api.apis.keys())}个api，请稍等...")
        abled, disabled = await self.web.batch_test_apis()
        msg = (
            f"【可用的API】\n{', '.join(abled)}\n\n【失效的API】\n{', '.join(disabled)}"
        )
        yield event.plain_result(f"{msg}")

    @filter.event_message_type(EventMessageType.ALL)
    async def match_api(self, event: AstrMessageEvent):
        """监听入口：匹配关键词并执行 API 调用链路。"""

        # 流程：前缀判断 -> 匹配 API -> 禁用检查 -> 参数补全 -> 调用接口 -> 发送结果
        # 前缀模式
        if self.conf["prefix_mode"] and not event.is_at_or_wake_command:
            return

        # 匹配 API
        msgs = event.message_str.split(" ")
        api_data = self.api.match_api_by_name(msgs[0])
        if not api_data:
            return

        # 检查 API 是否被禁用
        disabled_apis = {
            str(item).strip() for item in self.conf.get("disable_apis", []) if str(item).strip()
        }
        api_keywords = [
            str(item).strip() for item in api_data.get("keywords", []) if str(item).strip()
        ]
        if api_data["name"] in disabled_apis or any(k in disabled_apis for k in api_keywords):
            logger.debug("该 API 已被禁用")
            return

        # 检查站点是否被禁用
        disable_sites = [
            str(site).strip() for site in self.conf.get("disable_sites", []) if str(site).strip()
        ]
        disable_site_bases = {self.api.extract_base_url(site) for site in disable_sites}
        for url in api_data["urls"]:
            url_base = self.api.extract_base_url(url)
            if url_base in disable_site_bases:
                logger.debug(f"该站点已被禁用: {url}")
                return
            for site in disable_sites:
                if url.startswith(site):
                    logger.debug(f"该站点已被禁用: {url}")
                    return

        # 检查 API 类型是否被禁用
        if api_data["type"] not in self.enable_api_type:
            logger.debug("该 API 类型已被禁用")
            return

        # 获取参数
        args = msgs[1:]

        # 参数补充
        args, params = await self._supplement_args(event, args, api_data["params"])

        # 生成 update_params，保留 params 中默认值
        update_params = {
            key: args[i] if i < len(args) else params[key]
            for i, key in enumerate(params.keys())
        }
        # 获取数据
        try:
            text, path, source = await self.call_api(api_data, update_params)
        except Exception as e:
            logger.error(f"获取数据失败: {e}")
            return

        final_type = api_data["type"]
        if text and not path:
            final_type = "text"

        chain = await self.data_to_chain(
            api_type=final_type, text=text, path=path
        )
        await event.send(event.chain_result(chain))
        event.stop_event()

        # 清理临时文件
        if source == "api" and path and not self.conf["auto_save_data"]:
            os.remove(path)

    async def call_api_by_name(
        self, name: str, params: dict | None = None
    ) -> tuple[str | None, Path | None, str]:
        """Call API by name for external usage."""
        api_data = self.api.match_api_by_name(name)
        logger.debug(api_data)
        if not api_data:
            return None, None, "error"

        return await self.call_api(api_data, params)

    async def call_api(
        self, api_data: dict, params: dict | None = None
    ) -> tuple[str | None, Path | None, str]:
        """Call API and return (text, path, source)."""
        try:
            # === 外部接口调用 ===
            api_text, api_byte = await self.web.get_data(
                urls=api_data["urls"],
                params=params or api_data["params"],
                api_type=api_data["type"],
                target=api_data["target"],
            )

            is_abnormal = False
            if api_byte is None and api_text is None:
                is_abnormal = True
            elif isinstance(api_text, dict):
                code = api_text.get("code")
                if isinstance(code, int) and code not in (0, 200):
                    is_abnormal = True
            elif isinstance(api_text, str):
                stripped = api_text.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, dict):
                        code = parsed.get("code")
                        if isinstance(code, int) and code not in (0, 200):
                            is_abnormal = True

            error_reply = (self.conf.get("error_reply") or "").strip()
            if is_abnormal:
                if error_reply:
                    return error_reply, None, "error"
                raise RuntimeError(f"API 返回异常 [{api_data['name']}]")

            if api_text or api_byte:
                saved_text, saved_path = await self.local.save_data(
                    api_type=api_data["type"],
                    path_name=api_data["name"],
                    text=api_text,
                    byte=api_byte,
                )
                return saved_text, saved_path, "api"

        except Exception as e:
            error_reply = (self.conf.get("error_reply") or "").strip()
            if error_reply:
                logger.warning(
                    f"API 调用失败 [{api_data['name']}]，使用 error_reply: {e}"
                )
                return error_reply, None, "error"
            logger.warning(f"API 调用失败 [{api_data['name']}]，尝试本地兜底: {e}")

        # === 本地兜底 ===
        try:
            local_text, local_path = await self.local.get_data(
                api_type=api_data["type"], path_name=api_data["name"]
            )
            return local_text, local_path, "local"
        except Exception as e:
            logger.error(f"本地兜底失败 [{api_data['name']}] : {e}")
            return None, None, "error"

    async def terminate(self):
        """Close plugin network session."""
        await self.web.terminate()
        logger.info("已关闭 astrbot_plugin_apis 的网络连接")
